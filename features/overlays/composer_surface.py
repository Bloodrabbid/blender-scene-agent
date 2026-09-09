"""The composer as a viewport overlay.

This is the only file in the new UI stack that knows about Blender. It reads
add-on state into a ``ComposerState``, lays the tree out in logical pixels,
rasterizes it once into a cached GPU layer, and publishes hit rectangles in the
region coordinates the modal operator already expects.

Three coordinate spaces meet here:

* **logical** — what the tree is written in, Figma pixels.
* **surface** — logical times the UI scale, what Skia rasterizes, y down.
* **region** — Blender's framebuffer pixels, y up, origin bottom-left.

``_to_region`` is the only place the flip happens. Everything above it can stay
in the coordinate space it was authored in.
"""

from __future__ import annotations

import contextlib
import math
import os
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from ... import diagnostics, hfui, media, perf, skui
from ...work import add_report, run_async, run_on_main_thread
from .. import agent, conversation, scene_history
from ...hfui import composer as composer_module
from ...hfui import field as field_module
from . import CLIPBOARD
from . import redraw_viewports
from . import pump as pump_module
from . import skia_runtime as sk
from . import binding as binding_module
from . import thread_viewport as thread_viewport_module

# Text editing follows the host platform, not Blender: Cmd on macOS for
# select-all and clipboard, Option for word jumps; Ctrl for both elsewhere.
MAC = sys.platform == "darwin"

# Room around the composer for shadows, and for a select popup that opens
# upward past the card's top edge.
MARGIN = 22.0
BOTTOM = 10.0

# Characters the prompt will hold. This used to be a flat 1000 whatever the
# model was, which silently dropped the tail of anything longer — the schema
# says what each model really takes and `_prompt_cap` reads it. `PROMPT_CAP` is
# the fallback for a model that declares no limit (most image models). Scene
# Builder has a larger explicit cap because its requests commonly carry full
# scene instructions. The ceiling is a frame budget rather than a backend limit:
# `_card_metrics` re-wraps the whole prompt on every keystroke, measured at
# 0.18 ms per 1000 characters, so 8000 keeps a keypress inside ~1.5 ms.
PROMPT_CAP = 4000
PROMPT_CAP_CEILING = 8000
SCENE_BUILDER_PROMPT_CAP = 20_000
# Retexture's prompt is a scene property with its own RNA `maxlen`.
RETEXTURE_PROMPT_CAP = 600
# How often reaching the cap may say so. Long enough that typing on against
# the limit is one message rather than one a keystroke, short enough that
# coming back to the limit after reading it reports again.
PROMPT_CAP_NOTICE = 2.0

_SCENE_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif", ".tif", ".tiff"}
)
_RETEXTURE_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".exr", ".hdr"}
)
_RETEXTURE_JOB_TYPE = "meshy_v5_retexture"
# The phone camera's flight modes, in the order the Camera card's Mode chip
# cycles them. Spelled out rather than read off the RNA enum because this module
# is bpy-free at module scope; the ids must match `props.cam_mode`.
PHONECAM_MODES = (("WALK", "Walking"), ("FPV", "FPV"), ("MAP", "Map path"))
# Not `_SCENE_IMAGE_SUFFIXES | media.VIDEO_SUFFIXES | media.FILE_SUFFIXES`
# any more. That set was the hosted agent's: it uploaded media to a service
# that transcoded it. This one hands a *path* to a CLI, so the question is
# what that CLI can open — see `binding.AGENT_READABLE_SUFFIXES`.
_SCENE_MEDIA_SUFFIXES = binding_module.AGENT_READABLE_SUFFIXES
# Dynamic rows are measured synchronously only near the viewport. Tool rows are
# exact without measurement; this budget applies to speculative overscan rows.
_MEASURE_BUDGET = 0.003
# A browser compositor can translate already-painted rows without asking the UI
# framework to render. The composer is one Skia texture, so seek mode uses a
# coarse visual offset instead; the logical scroller still consumes every exact
# delta and the settled frame is pixel-exact. Two compact tool rows per step is
# below reading speed while materially reducing full-surface uploads.
_SEEK_STEP = 48.0

# Layers are resources, not state: each owns a Skia surface and a GPU texture
# that have to be *released* in order, so they are not fields of the object
# below and teardown does not rebuild them. Same for the pump's timer.
_layer = sk.Layer("composer")
_menu_layer = sk.Layer("composer-menu")


def _field_metrics():
    """What the prompt wraps at: font, width, book. `hfui.TextField` asks."""
    return hfui.tokens.PROMPT, _wrap_limit(_composer.host), measure().fonts


def _field_cap():
    """How long the prompt may get. `hfui.TextField` asks, once per edit.

    A callable for `_field_metrics`' reason: the answer belongs to the model
    the picker is currently on, and the field is built once.
    """
    hb = _composer.host
    if hb is None:
        return (
            SCENE_BUILDER_PROMPT_CAP
            if _composer.standalone_mode == composer_module.SCENE_BUILDER
            else PROMPT_CAP
        )
    return _prompt_cap(hb, _binding(hb))


def _field_overflowed(dropped, limit):
    """The cap refused ``dropped`` characters: say so instead of losing them.

    Rate limited rather than reported per edit. `add_report` already folds a
    repeated text into the live toast, so the message deliberately carries no
    count — but it also logs every call, and typing on against the limit is
    thirty calls a second.
    """
    del dropped

    now = time.monotonic()
    if now - _composer.prompt_cap_notice < PROMPT_CAP_NOTICE:
        return
    _composer.prompt_cap_notice = now
    hb = _composer.host
    declared = False
    model = ""
    if hb is not None:
        binding = _binding(hb)
        declared = _prompt_limit(hb, binding)[1]
        # `model_label` answers "Choose model" when there is none, which would
        # read as the name of the thing refusing the text.
        if binding is not None and getattr(binding, "model", None):
            model = str(binding.model_label or "")
    if declared and model:
        text = f"{model} accepts at most {limit} characters in the prompt."
    elif declared:
        text = f"This model accepts at most {limit} characters in the prompt."
    else:
        # Our own ceiling, not the backend's: name no model, or the message
        # blames a limit the model does not have.
        text = f"The prompt is limited to {limit} characters."
    add_report(text, type="ERROR")


class Composer:
    """Everything the card remembers between frames.

    One object rather than thirty-two globals: teardown is a reconstruction,
    the defaults live where the fields are declared, and `__slots__` makes a
    misspelled field an error rather than a new attribute nothing reads.

    Grouped by **kind**, in the order every surface uses — see
    `docs/state-model.md`. The kind is the rule: who may write the field, and
    what happens to it at teardown. This surface has no Work, because
    everything it draws is already in hand.
    """

    __slots__ = (
        # -- Resources: lifetime, released in order, never values.
        "measure",
        "host",
        "scene_chats",
        "scene_active",
        "scene_next",
        "scene_scrolls",
        "scene_drafts",
        "scene_mentions",
        "scene_bindings",
        "scene_workspace",
        "scene_identity",
        "scene_pointer",
        # Resources rather than Model for the drafts' reason: the restore —
        # which runs on the draw path — writes it back, and the draw clamps
        # it to the region the way it clamps every scroll.
        "scene_thread_height",
        "prompt_height",
        # -- Model: what input writes, and only input.
        "pointer",
        "prompt",
        "chips",
        "responses",
        "menu",
        "menu_values",
        "field",
        "prompt_cap_notice",
        "slider_drag",
        "context_drag",
        "resize_drag",
        "context_indices",
        "context_hidden",
        "pan_accum",
        "response_seek",
        "prompts",
        "standalone_mode",
        "scene_attachments",
        "scene_model",
        "element_query_span",
        "thread_select",
        "thread_dragging",
        # -- Anim: what the clock writes.
        "motion",
        # -- Work: fetched off the main thread; history pages merge by chat id.
        "scene_recent",
        "scene_history_cursor",
        "scene_history_has_more",
        "scene_remote_loading",
        "element_results",
        "element_request",
        # -- Derived: pure, and always safe to drop.
        "thread_viewport",
        "thread_units",
        "thread_units_key",
        "state",
        "frame",
        "frame_cache",
        "metrics_cache",
        "scale",
        # -- Published: what the draw hands the modal.
        "card_rect",
        "pill_rect",
        "menu_rect",
        "chips_rect",
        "responses_rect",
        "msg_rects",
    )

    def __init__(self):
        # -- Resources ------------------------------------------------------
        self.measure = None
        # The add-on module, latched by each draw. Timers and event handlers
        # are handed no `hb` of their own and reach it through here.
        self.host = None
        # Scene Builder owns a small in-memory history of independent backend
        # threads. Switching never interrupts a turn running in another thread.
        self.scene_active = "1"
        self.scene_next = 2
        self.scene_scrolls = {}
        self.scene_drafts = {"1": ""}
        self.scene_mentions = {"1": hfui.MentionDocument()}
        # Persistent Scene UUID → local thread id. The UUID lives on the
        # Blender scene; this workspace-scoped half lives with chat history.
        self.scene_bindings = {}
        # Workspace owning the thread set above. Set by the first history
        # restore and changed only by the workspace event, never by a draw.
        self.scene_workspace = None
        # The Blender scene last observed by the message-bus callback. Keeping
        # both values distinguishes a copied scene (same saved UUID, new ID
        # pointer) from a redundant notification for the current scene.
        self.scene_identity = None
        self.scene_pointer = None
        self.scene_chats = {
            "1": conversation.Conversation(
                on_change=lambda: _scene_changed("1")
            )
        }
        # How tall the thread's viewport is, in authored pixels. The grip
        # drag writes it, the draw clamps it to the region it actually has,
        # and scene history carries it across restarts — Resources for the
        # drafts' reason: the restore runs on the draw path.
        self.scene_thread_height = float(composer_module.RESPONSES_HEIGHT)
        # How tall the prompt's viewport is on the resizable tabs, in authored
        # pixels, where 0 means "however tall the text needs". Same kind and
        # the same reasons as the thread's height above it, plus one of its
        # own: `None` is "the preference has not been read yet", because at
        # import time there are no preferences to read. `_restore_prompt_height`
        # resolves it on the first draw.
        self.prompt_height = None

        # -- Model ----------------------------------------------------------
        self.pointer = hfui.Pointer()
        # Three viewports into content that does not fit them. Host-owned
        # rather than part of ComposerState, so a wheel tick does not have to
        # go through collect() to take effect. `follow` on the prompt is the
        # caret chase: typing keeps the caret in view until a wheel says
        # otherwise.
        self.prompt = hfui.Scroller(follow=True)
        # The chip strip, which scrolls sideways. ``chips_rect`` is its
        # viewport in region space, so a wheel can be aimed at it without
        # asking which chip is under the pointer.
        self.chips = hfui.Scroller()
        self.responses = hfui.Scroller(follow=True)
        # An open select, which caps its height and scrolls its rows. It is
        # `place`d on first sight rather than starting at the top: a long
        # catalog opening with the current value off-screen is a select that
        # looks like it lost the setting.
        self.menu = hfui.Scroller()
        # Raw values behind the rows of the open menu, in row order. The rows
        # address themselves by index because a catalog value can contain a
        # colon.
        self.menu_values = ()
        # The prompt: caret, selection, undo and the whole editing keymap.
        # It does not hold the text — that lives in a scene property the
        # N-panel writes too — so every edit here reads a string in and
        # writes one back. `on_caret` re-aims the scroller, which is what
        # keeps the caret in view while typing.
        self.field = hfui.TextField(
            cap=_field_cap,
            measure=_field_metrics,
            clipboard=CLIPBOARD,
            on_caret=self._chase,
            on_overflow=_field_overflowed,
        )
        # When the cap last said no, so it does not say it per keystroke.
        self.prompt_cap_notice = 0.0
        # The slider under the held button, as ``(key, left edge, width)`` in
        # region space. Kept from the press so a move is arithmetic rather than
        # another hit test — the pointer leaves the chip constantly while
        # dragging past its end.
        self.slider_drag = None
        # The grip drag resizing the Scene Builder thread, as ``(press my,
        # height at press)`` in region / authored pixels. Same shape as the
        # slider's: a move is arithmetic on the press, not another hit test.
        self.resize_drag = None
        # The selected object is an atomic token in the prompt. Its insertion
        # index is stored per mode/object; dragging changes only this index,
        # never the prompt string sent to the backend.
        self.context_drag = None
        self.context_indices = {}
        self.context_hidden = set()
        self.pan_accum = 0.0
        self.response_seek = thread_viewport_module.ScrollSeek()
        # The composer keeps its own copy of the prompt, per mode. Not every
        # job type has a `prompt` parameter — an image-to-3D one has none — and
        # without a buffer the field would silently swallow everything the user
        # typed. Per mode, because the tabs switch between unrelated catalogs
        # (plus Scene Builder) and a shared buffer would carry one request into
        # another mode.
        self.prompts = {}
        # Scene Builder has no N-panel section or catalog binding. All other
        # modes remain derived from `props.ui_section`; this one override is
        # written only by clicking its composer tab.
        self.standalone_mode = composer_module.SCENE_BUILDER
        self.scene_attachments = []
        # Empty until the user picks, or a catalog landing snaps to Opus 5.
        # Starting on Free mode would keep it after the catalog arrived,
        # because Free mode is a valid row.
        self.scene_model = ""
        # ``(start, end, query)`` for the unfinished @word under the caret.
        self.element_query_span = None
        # Read-only selection in the Scene Builder thread:
        # ``(message index, anchor, caret)``.
        self.thread_select = None
        self.thread_dragging = False

        # -- Anim -----------------------------------------------------------
        self.motion = hfui.Motion()

        # -- Work -------------------------------------------------------------
        # Folder-backed history pages, as `(chat_id, label)` rows for the menu,
        # or None before the first page lands. The cursor follows the same
        # six-at-a-time contract as the web sidebar.
        self.scene_recent = None
        self.scene_history_cursor = None
        self.scene_history_has_more = True
        # Local thread id → backend chat id, for threads whose history is
        # still downloading. The collect shows "Loading…" for them, and the
        # landing merges in front of anything the user typed meanwhile.
        self.scene_remote_loading = {}
        self.element_results = ()
        self.element_request = 0

        # -- Derived --------------------------------------------------------
        self.thread_viewport = thread_viewport_module.ThreadViewport(gap=hfui.tokens.SM)
        self.thread_units = ()
        self.thread_units_key = None
        self.state = None
        self.frame = None
        # The last solved tree, under the same key the layer rasterizes
        # against. The texture was already cached; the tree that produced it
        # was not, so a frame that changed nothing still paid to describe and
        # lay out the whole composer — measured at 4.2 ms of a 5.1 ms draw, or
        # 28% of the main thread at 60fps.
        self.frame_cache = None
        self.metrics_cache = None
        self.scale = None

        # -- Published ------------------------------------------------------
        self.card_rect = None
        self.pill_rect = None
        self.menu_rect = None
        self.chips_rect = None
        self.responses_rect = None
        self.msg_rects = {}

    def _chase(self):
        self.prompt.follow = True

    def renewed(self):
        """A fresh object for teardown, carrying the user's own text across.

        Everything here is this surface's business and is meant to go, with one
        exception: the prompt buffers hold sentences the user typed, and
        `release` runs when the viewport UI is switched *off* as well as at
        unregister. Coming back to an emptied field would read as the add-on
        having lost the prompt rather than having put the panel away.
        """
        fresh = Composer()
        fresh.prompts = self.prompts
        fresh.standalone_mode = self.standalone_mode
        fresh.scene_attachments = self.scene_attachments
        fresh.scene_model = self.scene_model
        fresh.scene_chats = self.scene_chats
        fresh.scene_active = self.scene_active
        fresh.scene_next = self.scene_next
        fresh.scene_scrolls = self.scene_scrolls
        fresh.scene_drafts = self.scene_drafts
        fresh.scene_mentions = self.scene_mentions
        fresh.scene_bindings = self.scene_bindings
        fresh.scene_workspace = self.scene_workspace
        fresh.scene_identity = self.scene_identity
        fresh.scene_pointer = self.scene_pointer
        # A history import in flight lands into the carried `scene_chats`, so
        # its bookkeeping has to come across too or the thread never leaves
        # "Loading…". The recents cache rides along because it is the menu's
        # instant answer and costs nothing to keep.
        fresh.scene_recent = self.scene_recent
        fresh.scene_history_cursor = self.scene_history_cursor
        fresh.scene_history_has_more = self.scene_history_has_more
        fresh.scene_remote_loading = self.scene_remote_loading
        fresh.element_results = self.element_results
        fresh.element_request = self.element_request
        # The thread height is the user's own adjustment, like the prompt.
        fresh.scene_thread_height = self.scene_thread_height
        fresh.context_indices = self.context_indices
        fresh.context_hidden = self.context_hidden
        return fresh


_composer = Composer()

_WHEEL = frozenset(
    {"WHEELUPMOUSE", "WHEELDOWNMOUSE", "WHEELLEFTMOUSE", "WHEELRIGHTMOUSE"}
)
_WHEEL_DOWN = frozenset({"WHEELDOWNMOUSE", "WHEELRIGHTMOUSE"})


def _editing():
    return bool(
        _composer.host is not None
        and _composer.host.generate_3d_overlay_state.get("prompt_active")
    )


def _nap():
    """The open/close morph, then the caret."""
    if _composer.motion.running:
        return 1.0 / 60.0
    seek_due = _composer.response_seek.remaining(time.monotonic())
    if seek_due is not None:
        return seek_due
    # The Scene Builder thread carries a live "Working for 9m 18s" under
    # the last message while its turn runs. Running tool rows count their
    # dots on the same quantised clock as the compact chat.
    if state().scene_chat:
        chat = _scene_conversation()
        if chat.busy():
            return min(1.0, hfui.chat.DOTS.due()) if chat.waiting() else 1.0
    if not _editing():
        return None
    # Only the caret is animating: wake at the next blink flip instead of
    # every frame. A redraw re-solves the tree, which re-wraps the prompt.
    return max(0.05, field_module.CARET.due())


_pump = pump_module.Pump(
    "composer pump",
    _nap,
    alive=lambda: _composer.host is not None,
    on_settle=redraw_viewports,
)
park = _pump.park
_ensure_pulse = _pump.wake


def ui_scale():
    """Framebuffer pixels per layout pixel: the display's own factor, nothing else.

    Blender's ``system.dpi`` already folds ``ui_scale`` and ``pixel_size``
    together — 72 means 1:1 — so it is the whole answer, and multiplying
    ``pixel_size`` back in would double-count it on Retina.

    **Nothing of the design's own may be folded in here.** This used to carry a
    0.8, because the `hfui` metrics were authored large and the card took a
    third of a 1080p viewport. One number to bring the whole design down is a
    tempting place to put it and it is the wrong place: it made the raster
    scale fractional on every display. A layer is rastered at this factor and
    blitted one texel to one pixel, so at 0.8 a 1pt hairline became 0.8 of a
    pixel and a 12pt caption 9.6 — nothing in the UI could land on a pixel
    boundary, and non-Retina users got a panel that was soft everywhere. The
    design is authored at its real size now: `tokens` and the four widget
    modules hold the numbers this used to shrink, so 1× and 2× displays both
    rasterize on the grid.

    Restricted contexts (script bridges, background jobs) report the bare
    defaults rather than the window's real values, so the cache is what a real
    window last told us and only a windowless call falls back to it. **The
    presence of a window is the test, never the value.** 72 dpi is both the
    bare default and the correct reading for every 1× display, so treating a
    plain 1.0 as "nobody told me" locked the surfaces to whatever larger figure
    they had seen before: drag the window from a Retina screen to an external
    1080p one and every surface stayed at 2×, which halves the logical width
    the explorer lays its grid out in — ten columns became three.

    A Blender UI Scale the user has set by hand can still be fractional, and
    that is theirs to choose: it is passed through rather than rounded, exactly
    as Blender treats its own chrome. Callers put the result in their tree and
    layer keys, so the scale changing under a live session repaints rather than
    going stale.
    """
    import bpy

    value = 0.0
    windowed = False
    try:
        value = float(bpy.context.preferences.system.dpi) / 72.0
        windowed = not bpy.app.background and bpy.context.window is not None
    except Exception:
        value = 0.0
    if value <= 0.0 or not windowed:
        return _composer.scale or value or 1.0
    _composer.scale = value
    return value


def state():
    """The live ComposerState, created on first use."""
    if _composer.state is None:
        _composer.state = hfui.ComposerState()
    return _composer.state


def pointer():
    return _composer.pointer


def measure():
    if _composer.measure is None:
        _composer.measure = skui.Measure()
    return _composer.measure


def release():
    """Drop GPU and Skia resources; called from unregister().

    Both layers are released and everything else is rebuilt. The frame and the
    frame cache hold `Placed` nodes measured by the font book, so they have to
    go with it — which they do, because they are fields of one object rather
    than assignments whose order somebody has to keep right.
    """
    global _composer

    # The last chance to keep a draft typed since the last thread event:
    # release also runs at Blender quit, where no further save will come.
    if _history_restored:
        _save_scene_thread()
        _persist_scene_history()
    # A park is a fact about a draw that raised, and this rebuilds the thing
    # that raised. The pump outlives the surface object — it holds a timer
    # registration — so it has to be told, where before the flag simply went
    # with the object.
    _pump.revive()
    _layer.release()
    _menu_layer.release()
    _composer = _composer.renewed()


# History is read once per Blender session, on the first real draw. Not at
# import: the snapshot scripts load this module headlessly and must never see
# (or, through an empty save, erase) a user's actual threads.
_history_restored = False


def _restore_scene_history():
    """Bring last session's threads back, before the first frame shows them."""
    global _history_restored
    if _history_restored:
        return
    _history_restored = True
    workspace_id = str(_composer.host._active_workspace_id() or "") or None
    _adopt_scene_workspace(workspace_id, scene_history.load(workspace_id))


def _restore_prompt_height():
    """Read the dragged prompt height back, once, from the preferences.

    A size the user chose is theirs across restarts, and it belongs to the
    person rather than to the .blend — which is what makes it a preference and
    not a scene property.
    """
    from ...props import addon_preferences

    if _composer.prompt_height is not None:
        return
    prefs = addon_preferences()
    _composer.prompt_height = float(
        getattr(prefs, "composer_prompt_height", 0.0) if prefs is not None else 0.0
    )


def _persist_prompt_height():
    """Write it back. On release only — a drag is dozens of moves a second."""
    from ...props import addon_preferences

    prefs = addon_preferences()
    if prefs is None:
        return
    with contextlib.suppress(AttributeError, RuntimeError):
        prefs.composer_prompt_height = float(_composer.prompt_height or 0.0)


def _active_scene_identity(*, create):
    """``(saved UUID, runtime pointer)`` for Blender's active scene.

    A copied Blender scene inherits every RNA property. When the active ID
    pointer changes while the saved UUID does not, the new copy receives its
    own UUID before it can inherit the source scene's chat.
    """
    import bpy

    scene = getattr(bpy.context, "scene", None)
    props = getattr(scene, "scene_agent", None) if scene is not None else None
    if props is None:
        return None, None
    pointer = scene.as_pointer()
    identity = str(props.scene_builder_id or "") or None
    copied = (
        identity is not None
        and _composer.scene_pointer is not None
        and pointer != _composer.scene_pointer
        and identity == _composer.scene_identity
    )
    if create and (identity is None or copied):
        identity = uuid.uuid4().hex
        props.scene_builder_id = identity
    return identity, pointer


def _bind_active_scene(thread_id=None, *, replace=False):
    """Bind the active Blender scene to a local thread, without selecting it."""
    identity, pointer = _active_scene_identity(create=True)
    _composer.scene_identity = identity
    _composer.scene_pointer = pointer
    if identity is None:
        return False
    thread_id = str(thread_id or _composer.scene_active)
    if thread_id not in _composer.scene_chats:
        return False
    if not replace and identity in _composer.scene_bindings:
        return False
    if _composer.scene_bindings.get(identity) == thread_id:
        return False
    _composer.scene_bindings[identity] = thread_id
    return True


def scene_context_changed():
    """Select the chat remembered by the newly active Blender scene.

    This is deliberately one-way. A Blender scene switch selects its chat;
    choosing another chat in the history menu never selects a Blender scene
    and never rewrites this binding.
    """
    if not _history_restored:
        return False
    previous_pointer = _composer.scene_pointer
    identity, pointer = _active_scene_identity(create=True)
    if (
        identity == _composer.scene_identity
        and pointer == _composer.scene_pointer
        and identity in _composer.scene_bindings
    ):
        return False
    _composer.scene_identity = identity
    _composer.scene_pointer = pointer
    thread_id = _composer.scene_bindings.get(identity)
    if thread_id in _composer.scene_chats:
        changed = thread_id != _composer.scene_active
        if changed:
            _switch_scene_thread(thread_id)
    elif pointer == previous_pointer:
        # First observation after an upgrade, file load or workspace change:
        # the scene is already on screen, so the thread restored as active is
        # its best-known chat. Bind it instead of inventing a second empty one.
        changed = _bind_active_scene(_composer.scene_active, replace=True)
        if changed:
            _persist_scene_history()
    else:
        changed = _new_scene_thread(bind_scene=True)
    if changed:
        redraw_viewports()
    return bool(changed)


def _adopt_scene_workspace(workspace_id, restored=None):
    """Replace the visible thread set with one workspace's saved state."""
    _composer.scene_workspace = str(workspace_id or "") or None
    _composer.scene_scrolls = {}
    if restored is None:
        chats = {"1": conversation.Conversation()}
        drafts = {"1": ""}
        mention_documents = {"1": hfui.MentionDocument()}
        active = "1"
        next_id = 2
        thread_height = None
        scene_bindings = {}
    else:
        (
            chats,
            drafts,
            active,
            next_id,
            thread_height,
            scene_bindings,
            mention_documents,
        ) = restored
    identity, pointer = _active_scene_identity(create=False)
    bound = scene_bindings.get(identity)
    if bound in chats:
        active = bound
    if thread_height is None:
        _composer.scene_thread_height = float(composer_module.RESPONSES_HEIGHT)
    else:
        # The draw clamps against the region, so a height saved on a taller
        # monitor only needs the floor held here.
        _composer.scene_thread_height = max(
            composer_module.RESPONSES_MIN, float(thread_height)
        )
    # `on_change` is left unbound here; `_scene_conversation` re-aims it on
    # first touch, exactly as it does for a composer that `renewed()`.
    _composer.scene_chats = chats
    _composer.scene_drafts = drafts
    _composer.scene_mentions = mention_documents
    _composer.scene_bindings = scene_bindings
    _composer.scene_active = active
    _composer.scene_next = next_id
    _composer.scene_identity = identity
    _composer.scene_pointer = pointer
    _composer.scene_scrolls = {}
    _composer.prompts[composer_module.SCENE_BUILDER] = drafts.get(active, "")
    _composer.responses.follow = True
    _composer.frame_cache = None
    _composer.metrics_cache = None


def workspace_changed(previous, current):
    """Move Scene Builder to the newly active workspace.

    Running turns are stopped before their thread set is put away. Each turn's
    client is pinned to its original workspace, so the interrupt cannot cross
    the boundary even though the account state has already changed.
    """
    global _history_requested
    if not _history_restored:
        return
    current = str(current or "") or None
    if current == _composer.scene_workspace:
        return
    _save_scene_thread()
    host = _composer.host
    if host is not None:
        for chat in tuple(_composer.scene_chats.values()):
            if chat.busy():
                chat.stop(host)
    _persist_scene_history()
    _history_requested = None
    _composer.scene_recent = None
    _composer.scene_history_cursor = None
    _composer.scene_history_has_more = True
    _composer.scene_remote_loading = {}
    _composer.scene_attachments = []
    _composer.element_query_span = None
    _composer.element_results = ()
    _adopt_scene_workspace(current, scene_history.load(current))
    if host is not None and _composer.standalone_mode == composer_module.SCENE_BUILDER:
        _request_scene_history(host)
    redraw_viewports()


def _persist_scene_history():
    """Write the threads to disk, whole. Guarded until `load` has run once:
    saving the defaults before the read would erase the real history."""
    if not _history_restored:
        return
    scene_history.save(
        _composer.scene_chats,
        _composer.scene_drafts,
        _composer.scene_active,
        _composer.scene_next,
        thread_height=_composer.scene_thread_height,
        scene_bindings=_composer.scene_bindings,
        mention_documents=_composer.scene_mentions,
        workspace_id=_composer.scene_workspace,
    )


# One-shot / in-flight guards for the Supercomputer fetches. Module-level like
# `_history_restored`: they describe the session, not the surface object, and
# a teardown must not make a landed catalog look unfetched.
_prefetched = False
_models_requested = False
_history_requested = None
_SCENE_HISTORY_PAGE = 3
_ELEMENT_PICKER_SIZE = 50


def _prefetch_scene_data(hb):
    """Warm the model catalog and first history page, once per session.

    Called from clicks (the Scene Builder tab, the two menus) and never from a
    draw: the landings write Work fields and re-open menus, which is exactly
    what the state model keeps off the draw path. Both requests self-guard, so
    repeats are two flag reads.
    """
    global _prefetched
    if _prefetched:
        return
    _prefetched = True
    # Disk catalog is enough to pin Opus 5 before the fetch returns.
    _snap_scene_model()
    _request_scene_models(hb)
    _request_scene_history(hb)


def _request_scene_models(hb):
    """No catalog to fetch: the CLI resolves its own model aliases.

    The picker's rows are `agent.model_choices()`, which is a constant.
    """
    return None


def _element_from_api(row):
    if not isinstance(row, dict):
        return None
    element_id = str(row.get("id") or "").strip()
    if not element_id:
        return None
    medias = row.get("medias") or ()
    first = medias[0] if medias and isinstance(medias[0], dict) else {}
    return hfui.ElementMention(
        element_id,
        str(row.get("name") or "").strip(),
        str(row.get("category") or "").strip(),
        str(first.get("url") or "").strip(),
        str(row.get("display_name") or row.get("name") or "").strip(),
    )


def _element_icon(element):
    return {
        "character": "settings/people",
        "environment": "image-sparkle",
        "prop": "settings/mesh",
    }.get(str(element.category or "").lower(), "image-sparkle")


def _open_element_menu(*, preserve_scroll=False):
    """Show current reference-element picker results above the prompt."""
    rows = [
        (element.title, _element_icon(element))
        for element in _composer.element_results
    ]
    values = list(_composer.element_results)
    if not rows:
        rows = [("No matching elements", None)]
        values = [None]
    _open_menu(
        "prompt",
        rows,
        values,
        object(),  # Typeahead results have no persisted selected value.
        title="Elements",
        preserve_scroll=preserve_scroll,
    )


def _request_element_picker(hb, query):
    """The searchable library was the workspace's, and went with it.

    ``@`` still mentions objects in the scene — that half is local and is
    collected in `_scene_mention_document`. This was the other half.
    """
    _composer.element_request += 1
    return None


def _landed_element_picker(
    payload,
    request,
    workspace_id,
    query,
    *,
    append=False,
):
    if request != _composer.element_request:
        return
    if workspace_id != str(_composer.scene_workspace or ""):
        return
    span = _composer.element_query_span
    if span is None or span[2] != query:
        return
    incoming = tuple(
        element
        for element in (
            _element_from_api(row) for row in (payload or {}).get("items") or ()
        )
        if element is not None
    )
    if append:
        known = {element.id for element in _composer.element_results}
        _composer.element_results += tuple(
            element for element in incoming if element.id not in known
        )
    else:
        _composer.element_results = incoming
    _open_element_menu(preserve_scroll=append)
    redraw_viewports()


def _invalidate_element_picker():
    """Make rows from the previous @ query impossible to choose."""
    current = state()
    _composer.element_results = ()
    if current.menu in {None, "prompt"}:
        _composer.menu_values = ()
    if current.menu == "prompt":
        current.hide_menu()


def _sync_element_query(hb):
    """Open/update the @ picker for the token immediately before the caret."""
    if _composer.standalone_mode != composer_module.SCENE_BUILDER:
        return False
    text = _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    query = hfui.mentions.query_at(text, _composer.field.index(text))
    if query is None:
        changed = _composer.element_query_span is not None or state().menu == "prompt"
        _composer.element_query_span = None
        _invalidate_element_picker()
        return changed
    if query == _composer.element_query_span and state().menu == "prompt":
        return False
    _invalidate_element_picker()
    _composer.element_query_span = query
    _request_element_picker(hb, query[2])
    return True


def _insert_element_mention(hb, element):
    span = _composer.element_query_span
    if (
        span is None
        or element is None
        or element not in _composer.element_results
    ):
        return False
    text = _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    start, end, _query = span
    marker = _scene_mention_document().add(element)

    def replace(value, _caret):
        return value[:start] + marker + value[end:], start + 1

    text = _composer.field.apply(text, replace)
    _store(None, text)
    _composer.element_query_span = None
    if state().menu == "prompt":
        state().hide_menu()
    _composer.metrics_cache = None
    redraw_viewports()
    return True


def _element_picker_key(hb, event):
    """Keyboard navigation while the inline picker is open."""
    current = state()
    if current.menu != "prompt" or event.value not in {"PRESS", "REPEAT"}:
        return False
    if event.type == "ESC":
        current.hide_menu()
        _composer.element_query_span = None
    elif event.type in {"UP_ARROW", "DOWN_ARROW"}:
        count = len(_composer.menu_values)
        if not count:
            return True
        index = current.menu_value if isinstance(current.menu_value, int) else 0
        index += -1 if event.type == "UP_ARROW" else 1
        current.menu_value = max(0, min(count - 1, index))
    elif event.type in {"RET", "NUMPAD_ENTER"}:
        index = current.menu_value if isinstance(current.menu_value, int) else 0
        try:
            element = _composer.menu_values[index]
        except (IndexError, TypeError):
            return True
        _insert_element_mention(hb, element)
    else:
        return False
    _composer.field.claimed.add(event.type)
    return True


def _scene_model():
    """The picked model, if the CLI still offers it.

    The list is a constant now — the CLI's own aliases — so a pick either
    matches a row or is a leftover from another provider, and a leftover
    falls back to letting the CLI choose.
    """
    choices = agent.model_choices()
    if _composer.scene_model and any(
        model == _composer.scene_model for model, _label, _provider in choices
    ):
        return _composer.scene_model
    return agent.default_model()


def _snap_scene_model():
    _composer.scene_model = _scene_model()


def _scene_model_to_send():
    """The alias the next turn carries, or ``None`` to let the CLI decide."""
    model = _scene_model()
    return None if model == agent.AUTO_MODEL else model


def _open_scene_model_menu():
    choices = agent.model_choices()
    _open_menu(
        "scene-model",
        [
            (label, hfui.providers.agent(model, label, provider))
            for model, label, provider in choices
        ],
        [model for model, _label, _provider in choices],
        _scene_model(),
    )


def _scene_workspace_label(hb):
    """No workspace to name. The chip that showed it does not draw."""
    return None


def _open_scene_workspace_menu(hb):
    return None


def _open_scene_history_menu(*, preserve_scroll=False):
    """Local threads first, then backend chats not already open locally."""
    threads = list(reversed(tuple(_composer.scene_chats)))
    rows = [
        (
            _scene_title(thread_id)
            + (" · working" if _scene_conversation(thread_id).busy() else ""),
            "settings/clock",
        )
        for thread_id in threads
    ]
    values = list(threads)
    local_ids = {
        chat.chat_id for chat in _composer.scene_chats.values() if chat.chat_id
    }
    for chat_id, label in _composer.scene_recent or ():
        if chat_id in local_ids:
            continue
        rows.append((_clip(label, 22), "scene-builder"))
        values.append(f"remote:{chat_id}")
    _open_menu(
        "scene-history",
        rows,
        values,
        _composer.scene_active,
        preserve_scroll=preserve_scroll,
    )


def _import_remote_chat(hb, chat_id):
    """There is no remote chat to import."""
    return None


def _landed_remote_messages(workspace_id, thread_id, history):
    if workspace_id != _composer.scene_workspace:
        return
    _composer.scene_remote_loading.pop(thread_id, None)
    chat = _composer.scene_chats.get(thread_id)
    if chat is None:
        return
    chat.messages[:0] = history
    _scene_changed(thread_id)


def _failed_remote_messages(workspace_id, thread_id, message):
    if workspace_id != _composer.scene_workspace:
        return
    _composer.scene_remote_loading.pop(thread_id, None)
    chat = _composer.scene_chats.get(thread_id)
    if chat is None:
        return
    chat.messages.append(hfui.chat.Message(hfui.chat.ASSISTANT, message, failed=True))
    _scene_changed(thread_id)


def _working_status(chat):
    """The thread's live "Working for 9m 18s", as the web chat shows it."""
    return hfui.chat.working_status(chat.working_seconds())


def _scene_activity(chat):
    """One short line of what the turn is doing right now, or "".

    The tool rows already carry the reader-facing gerund ("Generating image"),
    so a running one names the moment better than any timer; between tools the
    phase is all there is to say — "Thinking" before the first token,
    "Answering" while the text streams.
    """
    if not chat.busy():
        return ""
    return hfui.chat.turn_activity(chat.turn.message)


def _scene_changed(thread_id):
    """A page landed; redraw only when that thread is currently visible."""
    chat = _composer.scene_chats.get(thread_id)
    if chat is not None and not chat.busy():
        # The turn settled, failed, or a followed generation landed. Streaming
        # deltas arrive many times a second and are skipped: the settle that
        # ends them writes the same messages once.
        _persist_scene_history()
    if thread_id != _composer.scene_active:
        return
    _composer.responses.follow = True
    _composer.frame_cache = None
    _composer.metrics_cache = None
    # A turn just started or advanced; the pump may be asleep, and the
    # "Working for" timer only ticks while it runs.
    _ensure_pulse()
    redraw_viewports()


def _scene_conversation(thread_id=None):
    thread_id = str(thread_id or _composer.scene_active)
    chat = _composer.scene_chats[thread_id]
    if getattr(chat.on_change, "_scene_changed_owner", None) is not _scene_changed:
        def changed(thread_id=thread_id):
            _scene_changed(thread_id)

        changed._scene_changed_owner = _scene_changed
        chat.on_change = changed
    return chat


def _scene_mention_document(thread_id=None):
    """Atomic element registry for one Scene Builder draft."""
    thread_id = str(thread_id or _composer.scene_active)
    document = _composer.scene_mentions.get(thread_id)
    if document is None:
        document = hfui.MentionDocument()
        _composer.scene_mentions[thread_id] = document
    return document


def _save_scene_thread():
    thread_id = _composer.scene_active
    _composer.scene_scrolls[thread_id] = (
        _composer.responses.offset,
        _composer.responses.follow,
    )
    _composer.scene_drafts[thread_id] = _composer.prompts.get(
        composer_module.SCENE_BUILDER, ""
    )


def _switch_scene_thread(thread_id):
    """Show one existing thread without cancelling whichever one was visible."""
    thread_id = str(thread_id)
    if thread_id not in _composer.scene_chats:
        return False
    if thread_id == _composer.scene_active:
        return True
    _save_scene_thread()
    _composer.scene_active = thread_id
    _composer.prompts[composer_module.SCENE_BUILDER] = _composer.scene_drafts.get(
        thread_id, ""
    )
    _scene_mention_document(thread_id)
    _composer.element_query_span = None
    offset, follow = _composer.scene_scrolls.get(thread_id, (0.0, True))
    _composer.responses.reset()
    _composer.responses.offset = offset
    _composer.responses.follow = follow
    _composer.response_seek.reset()
    _composer.thread_viewport.clear()
    _composer.thread_units = ()
    _composer.thread_units_key = None
    _composer.field.caret = None
    _composer.field.anchor = None
    _composer.field.forget()
    _composer.prompt.reset()
    _composer.frame_cache = None
    _composer.metrics_cache = None
    _clear_thread_select()
    # The switch is when the outgoing thread's draft was just written down,
    # and the active id is part of what load() restores.
    _persist_scene_history()
    return True


def _new_scene_thread(*, bind_scene=False):
    _save_scene_thread()
    thread_id = str(_composer.scene_next)
    _composer.scene_next += 1
    _composer.scene_chats[thread_id] = conversation.Conversation(
        on_change=lambda thread_id=thread_id: _scene_changed(thread_id)
    )
    _composer.scene_drafts[thread_id] = ""
    _composer.scene_mentions[thread_id] = hfui.MentionDocument()
    _composer.scene_active = thread_id
    _composer.prompts[composer_module.SCENE_BUILDER] = ""
    _composer.scene_drafts[_composer.scene_active] = ""
    _composer.element_query_span = None
    _composer.responses.reset()
    _composer.responses.follow = True
    _composer.response_seek.reset()
    _composer.thread_viewport.clear()
    _composer.thread_units = ()
    _composer.thread_units_key = None
    _composer.prompt.reset()
    _composer.field.caret = 0
    _composer.field.anchor = None
    _composer.field.forget()
    _composer.frame_cache = None
    _composer.metrics_cache = None
    _clear_thread_select()
    if bind_scene:
        _bind_active_scene(thread_id, replace=True)
    _persist_scene_history()
    return True


def _scene_title(thread_id=None):
    chat = _scene_conversation(thread_id)
    for message in chat.messages:
        if message.role != hfui.chat.USER:
            continue
        text = " ".join(message.text.split())
        if text:
            return _clip(text, 22)
        if message.attachments:
            return "Image reference"
    return "New chat"


def _binding(hb, props=None):
    """The active mode's collections, catalog and hooks. Cheap; never cached."""
    if _composer.standalone_mode in (
        composer_module.SCENE_BUILDER,
        composer_module.CAMERA,
    ):
        return None
    if _composer.standalone_mode == composer_module.RETEXTURE:
        return SimpleNamespace(
            mode=composer_module.RETEXTURE,
            props=props if props is not None else _props(),
        )
    return hb._composer_binding(props)


def prompt_value(hb, props):
    """What the field should show. Read-only: this runs inside the draw."""
    binding = _binding(hb, props)
    if binding is None:
        return _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    if binding.mode == composer_module.RETEXTURE:
        return str(props.retexture_prompt or "")
    buffered = _composer.prompts.get(binding.mode, "")
    item = binding.prompt_item
    if item is None:
        return buffered
    return str(item.string_value or "") or buffered


def _prompt_limit(hb, binding):
    """``(characters allowed, whether the model itself said so)``.

    The second half is only for the wording of the refusal: telling the user a
    model accepts at most 8000 characters when its schema says 60000 would be
    a lie about the backend to describe a limit of our own.

    Scene Builder and Motion have no schema-backed prompt to ask, and neither
    does a model whose only input is an image. Scene Builder has its own larger
    cap; the other two keep the default.
    """
    if _composer.standalone_mode == composer_module.SCENE_BUILDER:
        return SCENE_BUILDER_PROMPT_CAP, False
    # One prompt, one cap. Each generation model used to publish its own
    # maximum in its JSON schema and the field enforced that model's number;
    # a chat has no such limit to read.
    return PROMPT_CAP, False


def _prompt_cap(hb, binding):
    """Characters this mode's prompt accepts, from the model's own schema."""
    return _prompt_limit(hb, binding)[0]




def _phonecam_mode_label(mode):
    return dict(PHONECAM_MODES).get(mode, PHONECAM_MODES[0][1])




def _blur_prompt(binding):
    """Leave the field when the string underneath it changes identity.

    The anchor would delete a range of the *new* string and the undo stack
    holds whole strings, so a Cmd+Z after a shot switch would write one shot's
    text into another.
    """
    _composer.field.anchor = None
    _composer.field.caret = None
    _composer.field.undo_kind = None
    with contextlib.suppress(AttributeError):
        _composer.field.forget()


def _span(value, low, high):
    """Where ``value`` sits between its bounds, as 0..1."""
    if high <= low:
        return 0.0
    return max(0.0, min(1.0, (float(value) - low) / (high - low)))








def selection():
    """``(start, end)`` of the selected run, or None when nothing is selected."""
    return _composer.field.selection()


def _caret_index(text):
    """Clamped caret into ``text``; ``None`` parks at the end."""
    return _composer.field.index(text)


def _current_text(binding):
    """What the field is editing: the schema parameter, or the mode's buffer.

    A parameter rebuilt by a model switch starts empty and inherits from the
    buffer, which is why this is not simply the property.
    """
    if binding is None:
        return _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    if binding.mode == composer_module.RETEXTURE:
        return str(binding.props.retexture_prompt or "")
    item = binding.prompt_item
    buffered = _composer.prompts.get(binding.mode, "")
    if item is None:
        return buffered
    return str(item.string_value or "") or buffered


def _track_context_edit(binding, before, after):
    """Keep an atomic object's insertion index attached across text edits."""
    mode = composer_module.SCENE_BUILDER if binding is None else binding.mode
    chips = _object_context_chips(mode)
    if not chips or before == after:
        return
    key = _context_group_key(mode, chips)
    index = min(max(0, int(_composer.context_indices.get(key, 0))), len(before))

    prefix = 0
    limit = min(len(before), len(after))
    while prefix < limit and before[prefix] == after[prefix]:
        prefix += 1
    old_end = len(before)
    new_end = len(after)
    while (
        old_end > prefix
        and new_end > prefix
        and before[old_end - 1] == after[new_end - 1]
    ):
        old_end -= 1
        new_end -= 1

    if old_end < index or (old_end == index and prefix < index):
        index += (new_end - prefix) - (old_end - prefix)
    elif prefix < index < old_end:
        index = prefix
    _composer.context_indices[key] = min(max(0, index), len(after))


def _store(binding, value):
    """Write an edited prompt back to both the buffer and the property."""
    _track_context_edit(binding, _current_text(binding), value)
    if binding is None:
        _composer.prompts[composer_module.SCENE_BUILDER] = value
        _composer.scene_drafts[_composer.scene_active] = value
        _composer.prompt.follow = True
        return value
    if binding.mode == composer_module.RETEXTURE:
        binding.props.retexture_prompt = value[:600]
        _composer.prompts[binding.mode] = binding.props.retexture_prompt
        _composer.prompt.follow = True
        return binding.props.retexture_prompt
    if binding.mode == "motion":
        pass
        binding.schedule_cost()
        _composer.prompt.follow = True
        return value
    _composer.prompts[binding.mode] = value
    if binding.prompt_item is not None:
        # Writing the RNA property is also what schedules the cost estimate.
        binding.prompt_item.string_value = value
    _composer.prompt.follow = True
    return value


def edit_prompt(hb, mutate, coalesce=None):
    """Apply ``mutate`` to the prompt. Only call this from an operator.

    Writing a property from a draw handler is not allowed, so the schema
    parameter is mirrored here, on the input path, rather than in ``collect``.
    ``mutate`` receives ``(text, caret)`` and returns ``(text, caret)``.
    """
    binding = _binding(hb)
    return _store(
        binding, _composer.field.apply(_current_text(binding), mutate, coalesce)
    )


def _edit(hb, method, *args, **kwargs):
    """Run one of the field's editing methods against the live prompt."""
    binding = _binding(hb)
    return _store(
        binding, method(_composer.field, _current_text(binding), *args, **kwargs)
    )


def insert_text(hb, chunk):
    """Type ``chunk`` in, replacing the selection."""
    return _edit(hb, hfui.TextField.insert, chunk)


def type_character(hb, character):
    return insert_text(hb, character)


def backspace(hb, word=False, line=False):
    """Delete backwards: the selection, a word, to the line start, or one."""
    binding = _binding(hb)
    removed, selected = _delete_object_context(binding)
    if removed and not selected:
        return _current_text(binding)
    return _edit(hb, hfui.TextField.backspace, word=word, line=line)


def delete_forward(hb, word=False):
    binding = _binding(hb)
    removed, selected = _delete_object_context(binding)
    if removed and not selected:
        return _current_text(binding)
    return _edit(hb, hfui.TextField.delete_forward, word=word)


def move_caret(hb, delta=0, to=None, extend=False):
    """Move the caret by ``delta`` or jump to ``to`` (``0`` / ``len``)."""
    return _composer.field.move(
        prompt_value(hb, _props()), delta=delta, to=to, extend=extend
    )


def _props():
    import bpy

    return _composer.host._props(bpy.context)


_word_left = field_module.word_left
_word_right = field_module.word_right
_word_at = field_module.word_at


def _wrap_limit(hb):
    """Width the prompt wraps at, from the rect the last draw published."""
    overlay = hb.generate_3d_overlay_state
    for action, _value, _x, _y, width, _height in overlay.get("controls") or ():
        if action == "prompt":
            scale = ui_scale()
            return max(40.0, width / max(scale, 0.001) - 2 * hfui.controls.PROMPT_PAD_X)
    return 400.0


def move_to_line_edge(hb, end=False, extend=False):
    """Home / End on the *visual* line — the prompt soft-wraps."""
    return _composer.field.move_to_line_edge(
        prompt_value(hb, _props()), end=end, extend=extend
    )


def move_by_word(hb, forward=False, extend=False):
    return _composer.field.move_by_word(
        prompt_value(hb, _props()), forward=forward, extend=extend
    )


def move_by_line(hb, down=False, extend=False):
    """Up / down a visual line, keeping the caret's x where it can."""
    return _composer.field.move_by_line(
        prompt_value(hb, _props()), down=down, extend=extend
    )


def select_all(hb):
    return _composer.field.select_all(prompt_value(hb, _props()))


def selected_text(hb):
    return _composer.field.selected(prompt_value(hb, _props()))


def copy_selection(hb):
    binding = _binding(hb)
    text = _current_text(binding)
    selected = _composer.field.selected(text)
    if not selected:
        return False
    if binding is None:
        selected = _scene_mention_document().visible(selected)
    CLIPBOARD.write(selected)
    return True


def cut_selection(hb):
    binding = _binding(hb)
    text = _current_text(binding)
    if binding is None:
        if not copy_selection(hb):
            return False
        text = _composer.field.apply(text, _composer.field.replacing(""))
        cut_out = True
    else:
        text, cut_out = _composer.field.cut(text)
    if cut_out:
        _store(binding, text)
    return cut_out


def paste_clipboard(hb):
    binding = _binding(hb)
    text, pasted = _composer.field.paste(_current_text(binding))
    if pasted:
        _store(binding, text)
        if binding is None:
            _sync_element_query(hb)
    return pasted


MAC = field_module.MAC
_command = field_module.command
_word_modifier = field_module.word_modifier
_line_modifier = field_module.line_modifier


def attach_drop(paths, dest="composer"):
    """Stage files dropped on the prompt. ``dest`` is ``scene`` or ``composer``.

    Pins the card open so the tray that just grew is the thing the user sees,
    not a collapsed pill that hid the attachment they just made.
    """
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    if dest == "scene":
        added = attach_scene_builder(paths)
    elif _composer.standalone_mode == composer_module.RETEXTURE:
        added = attach_retexture(paths)
    else:
        import bpy

        from . import addon
        from .binding import _composer_add_image_paths

        hb = _composer.host or addon()
        binding = _binding(hb, hb._props(bpy.context))
        if binding is None:
            raise RuntimeError("The composer is not accepting references.")
        added = _composer_add_image_paths(binding, list(paths))
    current = state()
    current.pinned = True
    current.hovering = True
    _composer.frame_cache = None
    _composer.metrics_cache = None
    return added


def attach_retexture(paths):
    """Use one dropped or chosen image as the Meshy style reference."""
    if isinstance(paths, (str, os.PathLike)):
        paths = [paths]
    valid = [
        Path(raw).expanduser()
        for raw in paths
        if Path(raw).expanduser().is_file()
        and Path(raw).expanduser().suffix.lower() in _RETEXTURE_IMAGE_SUFFIXES
    ]
    if not valid:
        raise RuntimeError("Choose a PNG, JPG, WEBP, BMP, TIFF, EXR, or HDR image.")
    _props().retexture_image_path = str(valid[0].resolve())
    _composer.frame_cache = None
    _composer.metrics_cache = None
    return 1


def attach_scene_builder(paths):
    """Stage files for the next Scene Builder request."""
    added = 0
    for raw in paths:
        path = Path(raw).expanduser()
        if (
            path.exists()
            and path.is_file()
            and path.suffix.lower() in _SCENE_MEDIA_SUFFIXES
        ):
            resolved = str(path.resolve())
            if (
                resolved not in _composer.scene_attachments
                and len(_composer.scene_attachments) < agent.MAX_MEDIAS
            ):
                _composer.scene_attachments.append(resolved)
                added += 1
    if not added:
        raise RuntimeError(binding_module.AGENT_READABLE_HINT)
    return added


def scene_builder_image_suffixes():
    return _SCENE_MEDIA_SUFFIXES


def _choose_scene_images():
    """Open Blender's file browser on the prompt's own attachments.

    Through a timer rather than straight from here. This runs inside the
    overlay's modal handler, and a file browser started from inside a running
    modal operator does not get the window it needs — the browser never
    appears and the click reads as a dead button, which is what *Upload* was.
    """
    import bpy

    def open_browser():
        try:
            bpy.ops.scene_agent.add_composer_images("INVOKE_DEFAULT")
        except Exception as error:
            diagnostics.event("attach", "upload_failed", error=str(error))
        return None

    diagnostics.event("attach", "upload_clicked")

    bpy.app.timers.register(open_browser, first_interval=0.0)
    return True


def _capture_scene_builder(hb, source):
    """Attach a viewport or scene-camera capture to Scene Builder."""
    import bpy

    from ...blender.viewport import render_viewport_to_file

    try:
        camera = None
        if source == "RENDER_RESULT":
            camera = bpy.context.scene.camera
            if camera is None:
                raise RuntimeError("Choose a scene camera before rendering.")
        path = render_viewport_to_file(camera=camera)
        # `attach_drop`, not `attach_scene_builder`: the tray has to be
        # measured again or the card keeps the height it had and the capture
        # is staged where nobody can see it.
        attach_drop([path], "scene")
    except Exception as error:
        hb.add_report(str(error), type="ERROR")
        return False
    return True


def _detach_scene_builder(index):
    try:
        _composer.scene_attachments.pop(int(index))
    except (ValueError, IndexError):
        return False
    return True


def _active_context_objects(mode):
    """The selected objects this mode can act on.

    Scene Builder receives the whole Blender selection. Retexture receives one
    active mesh because one returned material set has one application target.
    """
    import bpy

    if mode not in {
        composer_module.SCENE_BUILDER,
        composer_module.RETEXTURE,
    }:
        return ()
    obj = bpy.context.active_object
    if obj is None or not obj.select_get():
        return ()
    if mode == composer_module.RETEXTURE and obj.type != "MESH":
        return ()
    if mode == composer_module.RETEXTURE:
        return (obj,)
    others = sorted(
        (selected for selected in bpy.context.selected_objects if selected != obj),
        key=lambda selected: selected.name.casefold(),
    )
    return (obj, *others)


def _active_context_object(mode):
    objects = _active_context_objects(mode)
    return objects[0] if objects else None


def _context_key(mode, obj):
    return mode, obj.name


def _context_object(mode):
    """The active object unless its prompt token was explicitly deleted."""
    obj = _active_context_object(mode)
    if obj is None or _context_key(mode, obj) in _composer.context_hidden:
        return None
    return obj


def _context_objects(mode):
    """Selected objects whose prompt tokens have not been deleted."""
    return tuple(
        obj
        for obj in _active_context_objects(mode)
        if _context_key(mode, obj) not in _composer.context_hidden
    )


def sync_context_selection():
    """Forget a deleted token after selection leaves that exact object.

    Called from modal input, not a draw: the next click that reselects the
    object first observes the intervening empty/different selection and makes
    the context available again.
    """
    if not _composer.context_hidden:
        return False
    current = state()
    mode = (
        composer_module.RETEXTURE
        if _composer.standalone_mode == composer_module.RETEXTURE
        else current.mode
    )
    selected = {
        _context_key(mode, obj)
        for obj in _active_context_objects(mode)
    }
    stale = {
        key
        for key in _composer.context_hidden
        if key[0] == mode and key not in selected
    }
    if not stale:
        return False
    _composer.context_hidden.difference_update(stale)
    _composer.frame_cache = None
    _composer.metrics_cache = None
    return True


def _object_context_chips(mode):
    return tuple(
        hfui.Chip(
            f"object-context:{obj.name}",
            _clip(f"@{obj.name}", 20),
            "settings/mesh",
            kind=hfui.composer.STATUS,
        )
        for obj in _context_objects(mode)
    )


def _context_group_key(mode, chips):
    return mode, tuple(chip.label for chip in chips)


def _clear_object_context(current):
    current.context = ()
    current.context_index = None
    current.context_reserve = ""
    current.context_pos = None
    current.context_display_span = None


def _set_object_context(current, mode):
    """Project element mentions and the selected-object group into the field."""
    document = (
        _scene_mention_document()
        if mode == composer_module.SCENE_BUILDER
        else hfui.MentionDocument()
    )
    book = measure()
    blank = "\u2800"
    blank_width = max(0.001, book.fonts.width(blank, hfui.tokens.PROMPT))

    def element_reserve(element):
        width = skui.solve(composer_module.element_mention(element), book).width
        return blank * max(1, math.ceil((width + 4.0) / blank_width))

    projection = document.project(current.prompt, element_reserve)
    current.plain_prompt = document.visible(current.prompt)
    current.element_positions = ()
    chips = _object_context_chips(mode)
    if not chips:
        _clear_object_context(current)
        current.mention_projection = projection
        current.display_prompt = projection.text
        return

    key = _context_group_key(mode, chips)
    index = min(max(0, int(_composer.context_indices.get(key, 0))), len(current.prompt))
    _composer.context_indices[key] = index
    # Braille blanks have width but are not whitespace. Reserve the tag's
    # measured width plus the designed 8 px gap before prompt text.
    mention_width = sum(
        skui.solve(
            composer_module.context_mention(
                chip,
                dismiss=mode != composer_module.RETEXTURE,
            ),
            book,
        ).width
        for chip in chips
    ) + 8.0 * (len(chips) - 1)
    reserve = blank * max(1, math.ceil((mention_width + 8.0) / blank_width))
    current.context = chips
    current.context_index = index
    current.context_reserve = reserve
    if not current.prompt:
        projection = hfui.mentions.Projection(current.placeholder, (0,), ())
    projection, start, end = projection.insert(index, reserve)
    current.context_display_span = (start, end)
    current.mention_projection = projection
    current.display_prompt = projection.text


def _delete_object_context(binding):
    """Remove the atomic context at the caret or inside the selection.

    Returns ``(removed, had_selection)``. A lone tag consumes the key itself;
    when a text selection also covers it, the field must still delete that text.
    """
    current = state()
    if binding is not None and binding.mode == composer_module.RETEXTURE:
        return False, _composer.field.selection() is not None
    context_index = current.context_index
    if context_index is None or not current.context:
        return False, False

    text = _current_text(binding)
    selection = _composer.field.selection()
    if selection is None:
        if _composer.field.index(text) != context_index:
            return False, False
    elif not (selection[0] <= context_index <= selection[1]):
        return False, True

    mode = composer_module.SCENE_BUILDER if binding is None else binding.mode
    objects = _active_context_objects(mode)
    if not objects:
        return False, selection is not None
    _composer.context_hidden.update(_context_key(mode, obj) for obj in objects)
    _forget_object_context(current)
    return True, selection is not None


def _dismiss_object_context(name):
    """Hide one selected-object token after its close control was pressed."""
    current = state()
    mode = (
        composer_module.RETEXTURE
        if _composer.standalone_mode == composer_module.RETEXTURE
        else current.mode
    )
    target = next(
        (obj for obj in _active_context_objects(mode) if obj.name == name),
        None,
    )
    if target is None:
        return False
    index = current.context_index
    _composer.context_hidden.add(_context_key(mode, target))
    chips = _object_context_chips(mode)
    if index is not None and chips:
        _composer.context_indices[_context_group_key(mode, chips)] = index
    _composer.field.undo_kind = None
    _composer.prompt.follow = True
    _composer.frame_cache = None
    _composer.metrics_cache = None
    _set_object_context(current, mode)
    return True


def _forget_object_context(current):
    _composer.field.undo_kind = None
    _composer.prompt.follow = True
    _composer.frame_cache = None
    _composer.metrics_cache = None
    _clear_object_context(current)


def _blend_project_name():
    """The open .blend filename, or Untitled when the file has never been saved."""
    import bpy

    filepath = bpy.data.filepath
    if filepath:
        return Path(filepath).name
    return "Untitled"


def _submit_scene_builder(hb):
    """Send the Scene Builder prompt through its composer-local thread."""
    if not agent.ready():
        agent.act()
        return False
    logical_text = _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    document = _scene_mention_document()
    text = document.visible(logical_text)
    targets = _context_objects(composer_module.SCENE_BUILDER)
    request_text = document.wire(logical_text)
    elements = document.active(logical_text)
    tags = ()
    if targets:
        tags = tuple(f"@{target.name}" for target in targets)
        descriptions = ", ".join(
            f"{target.type.lower()} object named {target.name!r}"
            for target in targets
        )
        request_text = (
            f"{request_text}\n\n"
            f"Blender object context: work on the existing selected objects: {descriptions}. "
            "They were selected when this request was sent. For animation requests, "
            "animate these objects in place instead of creating replacements."
        ).strip()
    chat = _scene_conversation()
    # The first accepted turn makes this thread the scene's primary chat.
    # An existing binding is never replaced here: manually opening another
    # chat is intentionally temporary and must not steal the scene.
    _bind_active_scene(_composer.scene_active)
    if not chat.messages:
        project = _blend_project_name()
        request_text = (
            f"{request_text}\n\n"
            "Use the blender connector among custom MCP. "
            f"The Blender project name is {project!r}."
        ).strip()
    attachments = tuple(_composer.scene_attachments)
    if not chat.send(
        hb,
        text,
        attachments=attachments,
        model=_scene_model_to_send(),
        request_text=request_text,
        tags=tags,
        elements=elements,
    ):
        if chat.asking():
            return False
        if chat.busy():
            hb.add_report("Scene Builder is already working.", type="WARNING")
        return False

    _composer.prompts[composer_module.SCENE_BUILDER] = ""
    _composer.scene_drafts[_composer.scene_active] = ""
    _composer.scene_mentions[_composer.scene_active] = hfui.MentionDocument()
    _composer.element_query_span = None
    if state().menu == "prompt":
        state().hide_menu()
    _composer.scene_attachments.clear()
    _composer.field.caret = 0
    _composer.field.anchor = None
    _composer.field.forget()
    _composer.prompt.reset()
    current = state()
    current.prompt = ""
    current.enabled = False
    _blur(hb)
    # The question goes to disk now, not at the settle: a Blender that dies
    # mid-turn still remembers what was asked.
    _persist_scene_history()

    return True


def _projected_scene_navigation(hb, event):
    """Move by visual lines in Scene Builder's projected mention document.

    The shared ``TextField`` edits the logical string, where each mention is
    one private character and selected Blender objects are absent. Up/down and
    line-edge movement must instead measure the projected string the user can
    see, then map the destination back to a logical caret.
    """
    if event.value not in {"PRESS", "REPEAT"}:
        return False
    kind = event.type
    command = field_module.command(event)
    line_edge = kind in {"LEFT_ARROW", "RIGHT_ARROW"} and field_module.line_modifier(
        event
    )
    home_end = kind in {"HOME", "END"} and not command
    vertical = kind in {"UP_ARROW", "DOWN_ARROW"} and not command
    if not (line_edge or home_end or vertical):
        return False

    current = state()
    projection = current.mention_projection
    if projection is None:
        return False
    logical = _composer.prompts.get(composer_module.SCENE_BUILDER, "")
    display = current.display_prompt
    font, limit, fonts = _field_metrics()
    display_index = projection.display_index(_composer.field.index(logical))

    if vertical:
        _ascent, _descent, line_height = fonts.metrics(font)
        x, y = _caret_xy(display, display_index, font, limit, fonts)
        target_y = y + (line_height if kind == "DOWN_ARROW" else -line_height)
        line_count = len(fonts.wrap(display, font, limit, 0) or [""])
        if target_y < 0:
            target = 0
        elif target_y >= line_height * line_count:
            target = len(display)
        else:
            target = caret_index_at(display, font, limit, x, target_y, fonts)
    else:
        spans = _line_spans(display, font, limit, fonts)
        target = len(display) if kind in {"RIGHT_ARROW", "END"} else 0
        for _line, bounds in reversed(spans):
            if bounds and bounds[0] <= display_index <= bounds[-1]:
                target = bounds[-1] if kind in {"RIGHT_ARROW", "END"} else bounds[0]
                break

    _composer.field.set_caret(
        projection.logical_index(target),
        extend=bool(event.shift),
    )
    _composer.field.claimed.add(kind)
    _sync_element_query(hb)
    return True


def key_event(hb, event):
    """Handle one key while the prompt is focused. True if the field used it.

    The keymap itself is `hfui.TextField.keys`, shared with the chat's prompt.
    What is local is where the text lives and what Enter means: catalog modes
    commit their job parameter, while Scene Builder starts a Supercomputer turn.
    """
    if hfui.chat.ask_hides_prompt(state().ask):
        return False
    binding = _binding(hb)
    sent = False

    if binding is None and _element_picker_key(hb, event):
        return True

    if binding is None and _projected_scene_navigation(hb, event):
        return True

    if (
        binding is None
        and event.value in {"PRESS", "REPEAT"}
        and field_module.command(event)
    ):
        letter = field_module.command_letter(event)
        if letter == "C":
            copy_selection(hb)
            _composer.field.claimed.add(event.type)
            return True
        if letter == "X":
            cut_selection(hb)
            _sync_element_query(hb)
            _composer.field.claimed.add(event.type)
            return True
        # PRESS only: holding Cmd+V repeats, and clipboard image data is
        # written to a fresh file each time it is read — a held key would
        # stack copies of one picture.
        if letter == "V" and event.value == "PRESS" and _paste_media(hb):
            # Only when the clipboard actually held one. Otherwise fall
            # through and let the field paste text, as it always has.
            _composer.field.claimed.add(event.type)
            return True

    if event.value in {"PRESS", "REPEAT"} and event.type in {"BACK_SPACE", "DEL"}:
        removed, selected = _delete_object_context(binding)
        if removed and not selected:
            _composer.field.claimed.add(event.type)
            return True

    def submit():
        nonlocal sent
        if binding is None:
            sent = _submit_scene_builder(hb)
        else:
            _blur(hb)

    text, handled = _composer.field.keys(
        _current_text(binding), event, submit=submit, cancel=lambda: _blur(hb)
    )
    if handled and not sent and event.value in {"PRESS", "REPEAT"}:
        _store(binding, text)
        if binding is None:
            _sync_element_query(hb)
    return handled


def _blur(hb):
    """Leave the field: no focus, no selection, no half-finished undo run."""

    hb.generate_3d_overlay_state["prompt_active"] = False
    _composer.field.anchor = None
    _composer.field.undo_kind = None


def undo(hb):
    binding = _binding(hb)
    text, moved = _composer.field.step_back(_current_text(binding))
    if moved:
        _store(binding, text)
    return moved


def redo(hb):
    binding = _binding(hb)
    text, moved = _composer.field.step_forward(_current_text(binding))
    if moved:
        _store(binding, text)
    return moved


_caret_xy = field_module.caret_xy
_line_spans = field_module.line_spans
_selection_rects = field_module.selection_rects
caret_index_at = field_module.caret_index_at


def _index_at_pointer(hb, mx, my):
    """Caret index under a region-space point, or None if the field is not up."""
    import bpy

    overlay = hb.generate_3d_overlay_state
    prompt_rect = None
    for action, _value, x, y, width, height in overlay.get("controls") or ():
        if action == "prompt":
            prompt_rect = (x, y, width, height)
            break
    current = state()
    text = current.display_prompt
    if prompt_rect is None:
        return len(current.prompt)

    x, y, width, height = prompt_rect
    scale = ui_scale()
    # Region y is up; Skia y is down from the top of the field.
    local_x = (mx - x) / max(scale, 0.001) - hfui.controls.PROMPT_PAD_X
    local_y = (
        (y + height - my) / max(scale, 0.001)
        - hfui.controls.PROMPT_PAD_Y
        + _composer.prompt.offset
    )
    inner = max(40.0, width / max(scale, 0.001) - 2 * hfui.controls.PROMPT_PAD_X)
    virtual = caret_index_at(
        text, hfui.tokens.PROMPT, inner, local_x, local_y, measure().fonts
    )
    if current.context_display_span is not None:
        start, end = current.context_display_span
        if start <= virtual <= end:
            return int(current.context_index or 0)
    projection = current.mention_projection
    if projection is None:
        return min(virtual, len(current.prompt))
    return min(projection.logical_index(virtual), len(current.prompt))


def place_caret(hb, mx, my, extend=False, drag=False):
    """Set the caret from a region-space click on the prompt field."""
    _clear_thread_select()
    result = _composer.field.press(_index_at_pointer(hb, mx, my), extend, drag)
    _sync_element_query(hb)
    return result


def drag_caret(hb, mx, my):
    """Sweep the selection while the button is held. True if it changed."""
    if _composer.thread_dragging:
        return _drag_thread(hb, mx, my)
    return _composer.field.drag_to(_index_at_pointer(hb, mx, my))


def press_context(node_id):
    """Arm the selected-object token for an insertion-point drag."""
    current = state()
    if (
        node_id != "object-context"
        or not current.context
        or current.context_index is None
    ):
        return False
    mode = (
        composer_module.RETEXTURE
        if _composer.standalone_mode == composer_module.RETEXTURE
        else current.mode
    )
    _composer.context_drag = _context_group_key(mode, current.context)
    current.pinned = True
    return True


def drag_context(hb, mx, my):
    """Move the atomic object token to the text position under the pointer."""
    if _composer.context_drag is None:
        return False
    current = state()
    mode, labels = _composer.context_drag
    index = _index_at_pointer(hb, mx, my)
    if index == current.context_index:
        return False
    _composer.context_indices[(mode, labels)] = index
    _set_object_context(current, mode)
    _composer.metrics_cache = None
    return True


def end_drag():
    """Release: stop every sweep. Returns whether a selection was swept."""
    _composer.slider_drag = None
    _composer.context_drag = None
    if _composer.resize_drag is not None:
        node_id, _start_my, _start_height = _composer.resize_drag
        _composer.resize_drag = None
        # The height is worth a restart, like the thing it sizes. Saved on
        # release rather than per move — a drag is dozens of moves a second.
        if node_id == composer_module.PROMPT_RESIZE_ID:
            _persist_prompt_height()
        else:
            _persist_scene_history()
    was_thread, _composer.thread_dragging = _composer.thread_dragging, False
    return _composer.field.end_drag() or was_thread


def _slider_key(hb, node_id):
    """The parameter key when ``node_id`` is a slider chip, else None."""
    import bpy

    node = str(node_id or "")
    if node in ("motion:duration", "motion:shot_duration"):
        # Not a schema parameter, but the drag arithmetic is identical: a
        # bounded number and a chip to travel along.
        return node
    if node in ("phonecam:sens", "phonecam:speed", "phonecam:smooth", "phonecam:walk"):
        # Same shape again: the Camera card's sliders write scene properties.
        return node
    if not node.startswith("param:"):
        return None
    key = str(node_id).split(":")[1]
    binding = _binding(hb, hb._props(bpy.context))
    if not hb._composer_is_slider(binding.param(key)):
        return None
    return key


def press_slider(hb, node_id, mx):
    """Press on a slider chip: set the value from x and arm the drag.

    Returns False for anything that is not a slider, so the caller can go on
    treating it as a normal chip.
    """

    key = _slider_key(hb, node_id)
    if key is None:
        return False
    published = next(
        (
            control
            for control in (hb.generate_3d_overlay_state.get("controls") or ())
            if control[0] == node_id
        ),
        None,
    )
    if published is None:
        return False
    _composer.slider_drag = (key, float(published[2]), float(published[4]))
    # Pin on press, not on release like every other chip: a drag routinely
    # travels past the card's edge, and an unpinned composer would fall shut
    # under the pointer halfway through setting a value.
    state().pinned = True
    _drag_slider_to(hb, mx)
    return True


def drag_slider(hb, mx):
    """Continue a slider drag. True when the value actually moved."""
    if _composer.slider_drag is None:
        return False
    return _drag_slider_to(hb, mx)


def _drag_slider_to(hb, mx):
    import bpy

    key, left, width = _composer.slider_drag
    if width <= 0.0:
        return False
    return hb._composer_set_slider(
        _binding(hb, hb._props(bpy.context)), key, (float(mx) - left) / width
    )


def _drawn_prompt_height():
    """The prompt viewport the last frame drew, in authored pixels.

    Read off the published rect for the same reason `_wrap_limit` is: the draw
    is the only side that worked it out, and an automatic prompt has no stored
    height to start a drag from.
    """
    hb = _composer.host
    if hb is not None:
        overlay = hb.generate_3d_overlay_state
        for action, _value, _x, _y, _width, height in overlay.get("controls") or ():
            if action == "prompt":
                return float(height) / max(ui_scale(), 0.001)
    return composer_module.MAX_PROMPT_HEIGHT


def press_resize(node_id, my):
    """Press on a grip — the Scene Builder thread's, or the prompt's.

    The press remembers where it started and how tall the thing was; a move is
    then pure arithmetic, exactly like the slider's, because the pointer leaves
    the 10pt strip on the first frame of any real drag.
    """
    if node_id == composer_module.RESIZE_ID:
        start = float(_composer.scene_thread_height)
    elif node_id == composer_module.PROMPT_RESIZE_ID:
        # An untouched prompt is sized by its own text, so the drag has to
        # start from what was actually on screen. Starting from zero would
        # snap the field to the floor on the first pixel of movement.
        start = float(_composer.prompt_height or 0.0) or _drawn_prompt_height()
    else:
        return False
    _composer.resize_drag = (node_id, float(my), start)
    # Pin for the slider's reason: the drag travels far outside the card.
    state().pinned = True
    return True


def resizing():
    """Whether a grip drag is armed — the modal keeps MOVE_Y through it."""
    return _composer.resize_drag is not None


def drag_resize(my):
    """Continue a grip drag. True when the height changed.

    Up grows what the grip sits on — both grips are top edges, so the hand
    motion and the edge move together. The draw clamps against the region;
    this only holds the floor so a wild drag cannot fold either away entirely.
    """
    if _composer.resize_drag is None:
        return False
    node_id, start_my, start_height = _composer.resize_drag
    height = start_height + (float(my) - start_my) / max(ui_scale(), 0.001)
    if node_id == composer_module.PROMPT_RESIZE_ID:
        height = max(composer_module.MIN_PROMPT_HEIGHT, height)
        if abs(height - float(_composer.prompt_height or 0.0)) < 0.5:
            return False
        _composer.prompt_height = height
        return True
    height = max(composer_module.RESPONSES_MIN, height)
    if abs(height - _composer.scene_thread_height) < 0.5:
        return False
    _composer.scene_thread_height = height
    return True


def reset_prompt_height():
    """Double click on the prompt grip: back to a field sized by its text.

    The only other way back is dragging onto the exact content height, which
    is not a thing a pointer can be asked to hit.
    """
    if not _composer.prompt_height:
        return False
    _composer.prompt_height = 0.0
    _persist_prompt_height()
    return True


def dragging():
    return _composer.field.dragging or _composer.thread_dragging


def select_word_at(hb, mx, my):
    """Double click: take the word under the pointer."""
    node = None
    overlay = hb.generate_3d_overlay_state
    for action, _value, x, y, width, height in reversed(
        overlay.get("controls") or ()
    ):
        if x <= mx <= x + width and y <= my <= y + height:
            node = action
            break
    index = hfui.chat.message_index(node)
    if index is not None:
        return _select_thread_word(hb, index, mx, my)
    _clear_thread_select()
    text = prompt_value(hb, _props())
    index = _index_at_pointer(hb, mx, my)
    if state().mode == composer_module.SCENE_BUILDER:
        document = _scene_mention_document()
        if index < len(text) and document.element(text[index]) is not None:
            _composer.field.anchor = index
            _composer.field.caret = index + 1
            _composer.field.dragging = False
            _composer.field.blink_reset()
            return True
    return _composer.field.select_word(text, index)


def _clear_thread_select():
    if _composer.thread_select is None and not _composer.thread_dragging:
        return False
    _composer.thread_select = None
    _composer.thread_dragging = False
    _composer.metrics_cache = None
    return True


def _thread_span():
    selected = _composer.thread_select
    if selected is None:
        return None
    index, start, end = selected
    start, end = min(start, end), max(start, end)
    if start >= end:
        return None
    return index, start, end


def _thread_response(index):
    responses = state().responses
    if index < 0 or index >= len(responses):
        return None
    return responses[index]


def _scene_wrap(role):
    scale = ui_scale()
    card = _composer.card_rect
    width = (
        card[2] / max(scale, 0.001) if card is not None else composer_module.WIDTH
    )
    return composer_module.message_wrap(role, width)


def _index_in_message(hb, index, mx, my):
    response = _thread_response(index)
    if response is None:
        return 0
    text = composer_module.response_text(response)
    rect = _composer.msg_rects.get(hfui.chat.message_id(index))
    if rect is None:
        return len(text)
    x, y, _width, height = rect
    scale = ui_scale()
    local_x = (mx - x) / max(scale, 0.001)
    local_y = (y + height - my) / max(scale, 0.001)
    return caret_index_at(
        text,
        hfui.tokens.BODY_MEDIUM,
        _scene_wrap(response.role),
        local_x,
        local_y,
        measure().fonts,
    )


def press_thread(hb, node_id, mx, my, extend=False):
    """A press on a Scene Builder message. Starts a drag-select."""
    index = hfui.chat.message_index(node_id)
    if index is None:
        return False
    state().pinned = True
    hb.generate_3d_overlay_state["prompt_active"] = False
    _composer.field.anchor = None
    caret = _index_in_message(hb, index, mx, my)
    prev = _composer.thread_select
    if extend and prev is not None and prev[0] == index:
        _composer.thread_select = (index, prev[1], caret)
    else:
        _composer.thread_select = (index, caret, caret)
    _composer.thread_dragging = True
    _composer.metrics_cache = None
    return True


def _drag_thread(hb, mx, my):
    selected = _composer.thread_select
    if selected is None:
        return False
    index, anchor, caret = selected
    nxt = _index_in_message(hb, index, mx, my)
    if nxt == caret:
        return False
    _composer.thread_select = (index, anchor, nxt)
    _composer.metrics_cache = None
    return True


def _select_thread_word(hb, index, mx, my):
    response = _thread_response(index)
    if response is None:
        return False
    start, end = field_module.word_at(
        response.text or "", _index_in_message(hb, index, mx, my)
    )
    _composer.thread_select = (index, start, end)
    _composer.thread_dragging = False
    hb.generate_3d_overlay_state["prompt_active"] = False
    _composer.metrics_cache = None
    return True


def _select_thread_all(index):
    response = _thread_response(index)
    if response is None:
        return False
    text = response.text or ""
    if not text:
        return False
    _composer.thread_select = (index, 0, len(text))
    _composer.thread_dragging = False
    _composer.metrics_cache = None
    return True


def _thread_selected_text():
    span = _thread_span()
    if span is None:
        return ""
    index, start, end = span
    response = _thread_response(index)
    if response is None:
        return ""
    return (response.text or "")[start:end]


def _copy_thread():
    text = _thread_selected_text()
    if not text:
        return False
    CLIPBOARD.write(text)
    return True


def _paste_media(hb):
    """Cmd+V holding a picture rather than words. True if it was handled.

    Tried before the text paste, never instead of it. The clipboard holds text
    almost every time, and when it does this costs one question to the OS and
    answers no — see `features.clipboard_media`, which is where the platforms
    differ and where the reasons are.
    """
    from ..clipboard_media import clipboard_paths

    found = clipboard_paths()
    if not found:
        return False
    diagnostics.event("attach", "paste_media", files=len(found), first=found[0])
    try:
        attach_drop(found, "scene")
    except Exception as error:
        # The clipboard held a file and the prompt will not take it. Saying so
        # is the answer; pasting its path as text would not be.
        hb.add_report(str(error), type="ERROR")
    return True


def _paste_into_prompt(hb):
    hb.generate_3d_overlay_state["prompt_active"] = True
    state().pinned = True
    _clear_thread_select()
    if _paste_media(hb):
        return True
    return paste_clipboard(hb)


def clipboard_event(hb, event, mx, my):
    """Cmd/Ctrl C/V/A/X while the card is open, focused or not.

    The focused prompt already handles these through ``key_event``. This is
    the unfocused path: copy a thread selection, paste into the prompt, or
    select-all the message under the pointer. Other Cmd combos still reach
    Blender.
    """
    if event.value not in {"PRESS", "REPEAT"}:
        return False
    kind = field_module.command_letter(event)
    if kind not in {"C", "V", "A", "X"}:
        return False
    if not field_module.command(event):
        return False
    if not state().open:
        return False
    over = pointer_over(mx, my)
    editing = bool(hb.generate_3d_overlay_state.get("prompt_active"))
    if kind == "C":
        if editing and selection():
            return copy_selection(hb)
        return _copy_thread()
    if kind == "V":
        if over or editing:
            return _paste_into_prompt(hb)
        return False
    if kind == "A":
        node = None
        if over:
            overlay = hb.generate_3d_overlay_state
            for action, _value, x, y, width, height in reversed(
                overlay.get("controls") or ()
            ):
                if x <= mx <= x + width and y <= my <= y + height:
                    node = action
                    break
        index = hfui.chat.message_index(node)
        if index is not None:
            hb.generate_3d_overlay_state["prompt_active"] = False
            return _select_thread_all(index)
        if node == "prompt" or editing:
            hb.generate_3d_overlay_state["prompt_active"] = True
            _clear_thread_select()
            select_all(hb)
            return True
        if _thread_span() is not None:
            return _select_thread_all(_composer.thread_select[0])
        return False
    if kind == "X":
        if not editing:
            return False
        return cut_selection(hb)
    return False


def _clamp_scroll(content, viewport, follow=False, caret_y=None):
    """Fit the prompt's viewport and chase the caret if it is being followed.

    Chasing moves the least that keeps the caret's line visible — not always to
    the bottom. Without a caret there is nowhere to aim, so following means the
    end of the text.
    """

    scroller = _composer.prompt
    scroller.fit(content, viewport)
    if follow or scroller.follow:
        if caret_y is None:
            scroller.to_end()
        else:
            line = 20.0
            view = float(viewport) - 2 * hfui.controls.PROMPT_PAD_Y
            scroller.reveal(caret_y, float(caret_y) + line, view)
    return scroller.offset, scroller.overflow


def scroll_by(delta):
    """Shift the prompt viewport by ``delta`` logical pixels. True if it moved."""

    return _composer.prompt.by(delta)


def _clamp_chip_scroll(overflow):
    """Keep the chip strip inside the range its content leaves it."""

    return _composer.chips.extent(overflow)


def scroll_chips_by(delta):
    """Shift the chip strip sideways. Returns True if it moved."""

    return _composer.chips.by(delta)


def scroll_responses_by(delta):
    """Shift the Scene Builder answer viewport vertically."""
    before = _composer.responses.offset
    moved = _composer.responses.by(delta)
    if moved and _heavy_response_thread(state().responses):
        _composer.response_seek.push(
            _composer.responses.offset - before,
            time.monotonic(),
        )
        _ensure_pulse()
    return moved


def _clamp_menu_scroll(content, viewport, selected=None):
    """Keep the open select inside its range, placing it on first sight.

    First sight is the first frame after the popup opens, which is when the row
    height and the cap are both known: the value the chip currently holds is
    centred rather than left wherever the top of the list happens to be.
    Twenty-six video models with the selected one off-screen is a select that
    looks like it lost the setting.
    """

    _composer.menu.fit(content, viewport)
    row = hfui.controls.MENU_ITEM + hfui.controls.MENU_GAP
    centre = (row * float(selected or 0)) - (viewport - row) * 0.5
    return _composer.menu.place(centre if selected else 0.0)


def scroll_menu_by(delta):
    """Shift the open select's rows. Returns True if it moved."""

    return _composer.menu.by(delta)


def _menu_scroll_event(hb, event, scale):
    """Wheel or pan over an open select. Always ours — it is a modal surface.

    Even when the list fits, the gesture is swallowed: a wheel over a popup
    that zoomed the scene behind it would be the one place the composer lets
    the viewport through while something is open on top of it.
    """

    if event.type == "TRACKPADPAN":
        dy = float(getattr(event, "mouse_y", 0) - getattr(event, "mouse_prev_y", 0))
        _composer.pan_accum += dy / max(scale, 0.001)
        if abs(_composer.pan_accum) < 2.0:
            return True
        delta, _composer.pan_accum = _composer.pan_accum, 0.0
        scroll_menu_by(delta)
        if delta > 0.0:
            _request_history_near_end(hb)
        return True
    # One notch ≈ two rows.
    step = 2 * (hfui.controls.MENU_ITEM + hfui.controls.MENU_GAP)
    down = event.type in _WHEEL_DOWN
    scroll_menu_by(step if down else -step)
    if down:
        _request_history_near_end(hb)
    return True


def _request_history_near_end(hb):
    """Continue history when a downward gesture reaches its loaded tail."""
    if state().menu != "scene-history" or not _composer.scene_history_has_more:
        return
    row = hfui.controls.MENU_ITEM + hfui.controls.MENU_GAP
    if _composer.menu.at_end(slack=2 * row):
        _request_scene_history(hb)


def _chip_scroll_event(event, scale):
    """Wheel or pan over the chip strip. Returns True when it was ours."""

    if _composer.chips.overflow <= 0.0:
        return False
    if event.type == "TRACKPADPAN":
        dx = float(getattr(event, "mouse_x", 0) - getattr(event, "mouse_prev_x", 0))
        dy = float(getattr(event, "mouse_y", 0) - getattr(event, "mouse_prev_y", 0))
        # A sideways gesture reads as sideways; a vertical one still scrolls the
        # strip, because a horizontal list is the only thing under the pointer.
        # Signs follow view2d, which pans by ``prev - current``.
        _composer.pan_accum += (-dx if abs(dx) >= abs(dy) else dy) / max(scale, 0.001)
        if abs(_composer.pan_accum) < 2.0:
            return True
        delta, _composer.pan_accum = _composer.pan_accum, 0.0
        scroll_chips_by(delta)
        return True
    # One notch ≈ two chips.
    scroll_chips_by(96.0 if event.type in _WHEEL_DOWN else -96.0)
    return True


def scroll_event(hb, event, mx, my):
    """Consume a wheel / trackpad pan when it lands on something that scrolls.

    Returns True when the event was handled (caller should ``RUNNING_MODAL``).
    """

    if event.type != "TRACKPADPAN" and event.type not in _WHEEL:
        return False
    scale = ui_scale()
    # The select first: it is drawn over the card, so a gesture inside it is
    # never meant for the strip or the prompt underneath.
    if _within(_composer.menu_rect, mx, my) and _menu_scroll_event(
        hb, event, scale
    ):
        return True
    if _within(_composer.responses_rect, mx, my):
        if event.type == "TRACKPADPAN":
            dy = float(
                getattr(event, "mouse_y", 0) - getattr(event, "mouse_prev_y", 0)
            )
            _composer.pan_accum += dy / max(scale, 0.001)
            if abs(_composer.pan_accum) >= 2.0:
                delta, _composer.pan_accum = _composer.pan_accum, 0.0
                scroll_responses_by(delta)
        else:
            scroll_responses_by(
                20.0 if event.type in _WHEEL_DOWN else -20.0
            )
        # The answer is a viewport even when its current contents fit. Never
        # zoom the Blender scene through it.
        return True
    if _within(_composer.chips_rect, mx, my) and _chip_scroll_event(event, scale):
        return True
    if _composer.prompt.overflow <= 0.0:
        return False
    # Only steal the gesture when the pointer is on the prompt field itself —
    # elsewhere over the card, orbit / zoom still belong to the viewport.
    hover = hb.generate_3d_overlay_state.get("hover_control")
    if not hover or hover[0] != "prompt":
        return False

    if event.type == "TRACKPADPAN":
        dy = float(getattr(event, "mouse_y", 0) - getattr(event, "mouse_prev_y", 0))
        # Match Blender's own regions rather than reason about "natural
        # scrolling": view2d pans by ``prev_y - y`` and adds that to the view
        # rect, which moves content up the screen when the reported y rises.
        # A rising prompt offset does the same here, so the sign is ``+dy``
        # — whatever GHOST has already done with the OS preference applies to
        # us exactly as it does to every other scrollable area.
        _composer.pan_accum += dy / max(scale, 0.001)
        if abs(_composer.pan_accum) < 2.0:
            return True
        delta, _composer.pan_accum = _composer.pan_accum, 0.0
        scroll_by(delta)
        return True

    # One notch ≈ one body line.
    step = 20.0
    delta = step if event.type in _WHEEL_DOWN else -step
    scroll_by(delta)
    return True


PLACEHOLDERS = {
    composer_module.SCENE_BUILDER: "Describe the scene you imagine...",
    composer_module.CAMERA: "Phone Camera",
    "3d": "Describe the model you imagine...",
    "motion": "Describe how you want your character to move and act...",
    "image": "Describe the image you imagine...",
    "video": "Describe the shot you imagine...",
}
VIDEO_SOURCES = (
    ("UPLOAD", "Upload Video"),
    ("VIEWPORT", "Viewport"),
    ("CAMERA", "Camera"),
)
IMAGE_SOURCES = (
    ("UPLOAD", "Upload", "plus"),
    ("VIEWPORT", "Capture", "image-sparkle"),
    ("RENDER_RESULT", "Render scene", "settings/camera-auto"),
)


def _clip(text, limit=24):
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _param_chip(hb, item):
    """One catalog parameter, in whichever chip shape fits it best.

    ``hfui.params`` decides everything the schema cannot: which glyph the key
    wears, whether a numeric enum is a ramp, and whether the value reads for
    itself. What is left over — a free string, a raw JSON object — still gets
    a chip, because the alternative is a setting nobody knows exists; the chip
    just sends the click to the N-panel.
    """
    title = hfui.params.label(item.key, item.title)
    value = hb._composer_param_display(item)
    icon = hfui.params.icon(item.key, item.kind, value)
    if item.kind == "boolean":
        return hfui.Chip(
            f"param:{item.key}",
            _clip(title),
            icon,
            kind=hfui.TOGGLE,
            active=bool(item.bool_value),
        )
    if hb._composer_is_slider(item):
        return hfui.Chip(
            f"param:{item.key}",
            _clip(title),
            icon,
            kind=hfui.SLIDER,
            value=value,
            fraction=hb._composer_slider_fraction(item),
        )
    # An enum the catalog gave one option is a statement, not a choice — the
    # chevron would promise a menu with nothing in it. A choosable enum wears
    # the value on the chip and the field name on the dropdown.
    choosable = item.kind == "enum" and len(hb._composer_param_options(item)) > 1
    if (choosable or item.key in hfui.params.BARE_VALUE) and value:
        label = value
    elif value and item.kind != "json":
        label = f"{title}: {value}"
    else:
        label = title
    return hfui.Chip(
        f"param:{item.key}",
        _clip(label),
        icon,
        trailing="chevron-down" if choosable else None,
    )


MEDIA_ICONS = {
    "image": "image-sparkle",
    "video": "video",
    "audio": "settings/audio",
    "file": "settings/file",
}
# Stands in wherever the model's lab has no brand mark bundled, which today is
# most of the 3D catalog — Meshy, Tripo, Tencent and Meta have no glyph yet.
MODEL_ICON = "circle-dotted"
# What Skia will actually decode for a tray thumbnail. ``.exr`` and ``.hdr``
# count as reference images the backend accepts but Skia cannot open, so they
# get the icon tile like a video does.
_THUMBNAIL_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})


def _attached(binding):
    """``[(index, reference, media item)]`` for slots holding a real file."""
    return [
        (index, reference, item)
        for index, reference, item in binding.reference_slots()
        if reference.source == "FILE" and reference.file_path
    ]


def _reference_tiles(hb, binding):
    """References as tray tiles, the image itself wherever possible."""
    import bpy

    tiles = []
    for index, reference, item in binding.reference_slots():
        if reference.source == "FILE" and not reference.file_path:
            continue
        source = None
        if reference.source == "FILE":
            path = bpy.path.abspath(reference.file_path)
            if (
                Path(path).suffix.lower() in _THUMBNAIL_SUFFIXES
                and os.path.isfile(path)
            ):
                source = path
        icon = {
            "VIEWPORT": "image-sparkle",
            "RENDER_RESULT": "settings/camera-auto",
        }.get(
            reference.source,
            MEDIA_ICONS.get(hb._composer_media_kind(item), "image-sparkle"),
        )
        tiles.append(
            hfui.Reference(
                f"reference:{index}",
                source,
                icon,
            )
        )
    return tuple(tiles)


def _reference_chips(hb, binding):
    """The add button. Existing references live in the tray."""
    media = binding.media_items()
    if not media:
        return []
    kinds = {hb._composer_media_kind(item) for item in media}
    kind = next(iter(kinds)) if len(kinds) == 1 else "reference"
    required = any(item.required for item in media)
    empty = not any(
        reference.source != "FILE" or bool(reference.file_path)
        for _index, reference, _item in binding.reference_slots()
    )
    return [
        hfui.Chip("reference", f"Add {kind}" if required and empty else "", "plus")
    ]


def _scene_responses():
    """Both sides of the active Scene Builder thread.

    Tool calls stay as the same one-line rows the compact Supercomputer panel
    draws. Attachments and generations travel as paths and are drawn as tiles;
    a turn that is only a tool or image is still a visible turn.
    """
    responses = []
    for message in _scene_conversation().messages:
        text = message.text.strip()
        user = message.role == hfui.chat.USER
        images = () if user else tuple(message.images)
        # Same rule as the reference tray: a path Skia cannot decode becomes
        # the icon tile rather than a blank square. Keep the original path so
        # a video or PDF still occupies a tile after send.
        attachments = tuple(message.attachments) if user else ()
        tools = () if user else tuple(message.tools)
        if not text and not images and not attachments and not tools:
            continue
        responses.append(
            composer_module.Response(
                text,
                role="user" if user else "assistant",
                failed=message.failed,
                images=images,
                attachments=attachments,
                tools=tools,
                identity=id(message),
            )
        )
    return tuple(responses)


def _wrapped_tiles_height(count, tile, wrap):
    """Exact height of the uniform wrapping row used for thread media."""
    count = max(0, int(count))
    if not count:
        return 0.0
    tile = max(1.0, float(tile))
    wrap = max(1.0, float(wrap))
    gap = float(hfui.tokens.XS)
    # skui.solve adds a gap only before the second and later children and
    # allows half a pixel for layout rounding. Algebraically, N tiles fit when
    # N * tile + (N - 1) * gap <= wrap + 0.5.
    per_row = max(1, int((wrap + gap + 0.5) // (tile + gap)))
    rows = math.ceil(count / per_row)
    return rows * tile + max(0, rows - 1) * gap


def _response_height_estimate(response, width):
    """A conservative row height until that row enters the measured window."""
    wrap = max(24.0, composer_module.message_wrap(response.role, width))
    # BODY_MEDIUM is an 11pt face.  Six pixels per character deliberately
    # overestimates common prose so a fresh viewport does not pull hundreds of
    # unmeasured rows into its first frame.
    columns = max(12, int(wrap / 6.0))
    shown_text = composer_module.response_text(response)
    raw_lines = shown_text.splitlines()
    visual_lines = sum(
        max(1, math.ceil(len(line.expandtabs(4)) / columns))
        for line in raw_lines
        if line.strip()
    )
    text_height = 0.0
    if visual_lines:
        blocks = sum(1 for line in raw_lines if line.strip())
        text_height = visual_lines * 15.0 + max(0, blocks - 1) * hfui.tokens.SM

    tools = min(len(response.tools), composer_module.RESPONSE_TOOLS)
    tool_height = tools * hfui.chat.TOOL_ROW
    if tools > 1:
        tool_height += (tools - 1) * hfui.tokens.SM

    sources = response.attachments if response.role == "user" else response.images
    tile = (
        hfui.controls.THUMB_SIZE
        if response.role == "user"
        else composer_module.RESULT_TILE
    )
    media_height = 0.0
    if sources:
        media_height = _wrapped_tiles_height(len(sources), tile, wrap)

    parts = sum(value > 0.0 for value in (tool_height, text_height, media_height))
    estimated = tool_height + text_height + media_height
    estimated += max(0, parts - 1) * hfui.tokens.SM
    if shown_text != (response.text or "") or tools < len(response.tools):
        estimated += 15.0 + hfui.tokens.SM
    if response.role == "user":
        estimated += 2 * hfui.tokens.MD
    return max(hfui.chat.TOOL_ROW, estimated)


def _response_unit_estimate(unit, width):
    """Initial height and whether it is already exact for one visual unit."""
    response = unit.response
    if unit.kind == "tool":
        return float(hfui.chat.TOOL_ROW), True
    if unit.kind == "media":
        wrap = max(24.0, composer_module.message_wrap("assistant", width))
        tile = composer_module.RESULT_TILE
        height = _wrapped_tiles_height(len(response.images), tile, wrap)
        return max(1.0, height), True
    if unit.kind == "omission":
        return 15.0, False
    if unit.kind == "user":
        return _response_height_estimate(response, width), False

    # Assistant text has no bubble or tool/media contribution. Six pixels per
    # character deliberately leans high so correction tends to remove space
    # rather than pull unmeasured content through the viewport.
    wrap = max(24.0, composer_module.message_wrap("assistant", width))
    columns = max(12, int(wrap / 6.0))
    raw_lines = composer_module.response_text(response).splitlines()
    visual_lines = sum(
        max(1, math.ceil(len(line.expandtabs(4)) / columns))
        for line in raw_lines
        if line.strip()
    )
    blocks = sum(1 for line in raw_lines if line.strip())
    height = visual_lines * 15.0 + max(0, blocks - 1) * hfui.tokens.SM
    return max(hfui.chat.TOOL_ROW, height), False


def _response_clock_extent(current):
    if not current.scene_status.startswith("Working"):
        return 0.0
    return hfui.tokens.SM + hfui.chat.TOOL_ROW


def _heavy_response_thread(responses):
    """Whether repeated offsets would repaint enough content to need pacing."""
    if len(responses) > 40:
        return True
    return any(
        len(response.tools) > 40 or len(response.text or "") > 8_000
        for response in responses
    )


def _response_paint_offset(offset, overflow, seeking):
    """Logical offset normally; a compositor-like bucket during scroll seek."""
    offset = max(0.0, min(float(overflow), float(offset)))
    if not seeking:
        return offset
    return max(0.0, min(float(overflow), round(offset / _SEEK_STEP) * _SEEK_STEP))


def _prepare_response_window(current, limit):
    """Project the complete Scene Builder transcript into its visible slice."""
    if not current.scene_chat or not current.responses:
        current.response_window = None
        return

    width = max(1.0, limit - 2 * composer_module.SHELL_PAD)
    viewport = max(1.0, float(current.responses_height))
    virtual = _composer.thread_viewport
    source_key = tuple(
        (response.virtual_id, response.key) for response in current.responses
    )
    units_changed = source_key != _composer.thread_units_key
    if units_changed:
        _composer.thread_units = composer_module.response_units(current.responses)
        _composer.thread_units_key = source_key
    units = _composer.thread_units

    # Capture identity and its local screen position before a prepend or a
    # streamed unit rebuild changes the index. Measurements are keyed, not
    # positional, so the same visible content remains under the pointer.
    old_anchor_key = None
    old_anchor_screen = 0.0
    if virtual.keys and not _composer.responses.follow:
        old_anchor = virtual.row_at(_composer.responses.offset)
        if old_anchor is not None:
            old_anchor_key = virtual.keys[old_anchor]
            old_anchor_screen = (
                virtual.position(old_anchor) - _composer.responses.offset
            )

    width_changed = virtual.width != round(float(width), 1)
    if units_changed or width_changed:
        sizing = tuple(_response_unit_estimate(unit, width) for unit in units)
        virtual.sync(
            (unit.key for unit in units),
            (item[0] for item in sizing),
            width,
            exact=(item[1] for item in sizing),
        )
        if old_anchor_key is not None:
            new_anchor = virtual.index(old_anchor_key)
            if new_anchor is not None:
                _composer.responses.offset = (
                    virtual.position(new_anchor) - old_anchor_screen
                )

    def content_height():
        return virtual.total + _response_clock_extent(current) + 2 * hfui.tokens.SM

    _composer.responses.fit(content_height(), viewport)
    if _composer.responses.follow:
        _composer.responses.to_end()

    # Estimates are corrected only for rows in or near the viewport.  Preserve
    # the first visible row's screen position when a correction lands above it;
    # following the live edge instead re-aims at the new end.
    if not current.response_scrolling:
        for _pass in range(2):
            offset = _composer.responses.offset
            anchor = virtual.row_at(offset)
            anchor_top = virtual.position(anchor or 0)
            core = virtual.window(offset, viewport)
            direction = _composer.response_seek.direction
            look = viewport * 0.45
            expanded = virtual.window(
                offset,
                viewport,
                before=look if direction <= 0 else viewport * 0.15,
                after=look if direction >= 0 else viewport * 0.15,
            )
            candidates = list(range(core.first, core.last + 1))
            candidates.extend(
                index
                for index in range(expanded.first, expanded.last + 1)
                if index < core.first or index > core.last
            )
            measured = []
            started = time.perf_counter()
            for index in candidates:
                if virtual.exact[index]:
                    continue
                # Visible rows must be exact before their full representation
                # is painted. Speculative overscan yields when its small frame
                # budget is gone and will be picked up on a later draw.
                if (
                    index < core.first or index > core.last
                ) and time.perf_counter() - started >= _MEASURE_BUDGET:
                    break
                measured.append(
                    (
                        index,
                        composer_module.measure_response_unit(
                            current,
                            units[index],
                            width,
                            measure(),
                        ),
                    )
                )
            if not measured or not virtual.set_heights(measured):
                break
            if _composer.responses.follow:
                _composer.responses.fit(content_height(), viewport)
                _composer.responses.to_end()
            else:
                correction = virtual.position(anchor or 0) - anchor_top
                _composer.responses.offset += correction
                _composer.responses.fit(content_height(), viewport)

    offset = _response_paint_offset(
        _composer.responses.offset,
        _composer.responses.overflow,
        current.response_scrolling,
    )
    direction = _composer.response_seek.direction
    if current.response_scrolling:
        ahead = viewport * 0.2
        behind = viewport * 0.05
    else:
        ahead = viewport * 0.45
        behind = viewport * 0.15
    visible = virtual.window(
        offset,
        viewport,
        before=ahead if direction <= 0 else behind,
        after=ahead if direction >= 0 else behind,
    )
    current.response_window = composer_module.ResponseWindow(
        tuple(
            (index, units[index], virtual.heights[index])
            for index in range(visible.first, visible.last + 1)
        ),
        visible.before,
        visible.after,
        visible.total,
    )
    current.response_scroll = offset






def _prompt_drop_armed(*surfaces):
    """True when an OS file drag would attach to this prompt.

    There used to be a second source: a tile dragged out of the assets browser,
    which armed a plan the browser owned. The browser is gone, so a drop can
    only come from outside Blender.
    """
    from .operators import os_drop_surface

    return os_drop_surface() in surfaces


def collect(hb, props):
    """Read the active mode's add-on state into the composer's state object."""
    current = state()
    current.login_required = False
    current.camera = None
    current.bridge = None
    current.response_window = None
    current.response_scrolling = False
    current.thread_select = None
    current.thread_select_rects = ()
    current.submit_label = "Generate"
    current.submit_icon = "sparkle"
    # Off unless the mode below claims it: the state object outlives a tab
    # switch, so a stale True would leave a grip on a card that has no prompt.
    current.prompt_resize = False
    binding = _binding(hb, props)
    if binding is None:
        current.mode = composer_module.SCENE_BUILDER
        current.placeholder = PLACEHOLDERS[composer_module.SCENE_BUILDER]
        current.prompt = prompt_value(hb, props)
        current.editing = bool(hb.generate_3d_overlay_state.get("prompt_active"))
        current.scroll = _composer.prompt.offset
        current.chip_scroll = 0.0
        current.caret = _caret_index(current.prompt) if current.editing else None
        current.caret_on = field_module.caret_on() if current.editing else True
        current.selection = selection() if current.editing else None
        current.references = tuple(
            hfui.Reference(
                f"scene-reference:{index}",
                path if Path(path).suffix.lower() in _THUMBNAIL_SUFFIXES else None,
                MEDIA_ICONS.get(media.kind(path), "image-sparkle"),
            )
            for index, path in enumerate(_composer.scene_attachments)
        )
        current.responses = _scene_responses()
        current.response_scrolling = (
            _heavy_response_thread(current.responses)
            and _composer.response_seek.is_active(time.monotonic())
        )
        current.response_scroll = _composer.responses.offset
        current.responses_height = _composer.scene_thread_height
        current.scene_chat = True
        # Every other mode ran a paid generation and said so on its button.
        # This one sends a message to an agent.
        current.submit_label = "Send"
        current.submit_icon = "sparkle"
        current.scene_title = _scene_title()
        current.scene_workspace = _scene_workspace_label(hb)
        current.scene_busy = _scene_conversation().busy()
        if current.scene_busy:
            current.scene_status = _working_status(_scene_conversation())
            current.scene_activity = _scene_activity(_scene_conversation())
            current.scene_ellipsis = (
                hfui.chat.DOTS.step() if _scene_conversation().waiting() else 0
            )
        elif _composer.scene_active in _composer.scene_remote_loading:
            current.scene_status = "Loading…"
            current.scene_activity = ""
            current.scene_ellipsis = 0
        else:
            current.scene_status = ""
            current.scene_activity = ""
            current.scene_ellipsis = 0
        # Resolve before the chip is labelled, not only when the fetch lands:
        # the offline catalog is read lazily on the first `model_choices`.
        # Keep this pure because collect runs on the draw path.
        scene_model = _scene_model()
        # Shipped Free mode sits under the catalog so the chip can label it
        # before `GET /models` lands, instead of reading "Model".
        model_rows = {
            model: (label, provider)
            for model, label, provider in (
                *agent.MODELS,
                *agent.model_choices(),
            )
        }
        model_label, model_provider = model_rows.get(scene_model, ("Model", ""))
        _set_object_context(current, composer_module.SCENE_BUILDER)
        current.chips = (
            hfui.Chip("scene-reference-add", "", "plus"),
            hfui.Chip(
                "scene-model",
                model_label,
                hfui.providers.agent(scene_model, model_label, model_provider),
                trailing="chevron-down",
            ),
        )
        current.credits = None
        current.enabled = bool(
            current.prompt.strip() or _composer.scene_attachments
        ) and not _scene_conversation().busy()
        current.drop_target = _prompt_drop_armed("scene")
        current.ask = _scene_conversation().ask_state()
        if current.ask is not None:
            current.pinned = True
            current.hovering = True
        if hfui.chat.ask_hides_prompt(current.ask):
            current.editing = False
            current.caret = None
            current.selection = None
        current.thread_select = _composer.thread_select
        if current.login_required:
            current.enabled = False
            current.chips = ()
        elif not agent.ready():
            current.bridge = composer_module.BridgeGate(
                status=agent.status(),
                error=agent.error() or "",
            )
            current.enabled = False
            current.editing = False
            current.chips = ()
            current.caret = None
            current.selection = None
            current.drop_target = False
            current.pinned = True
            current.hovering = True
        return current

    current.mode = binding.mode
    current.placeholder = PLACEHOLDERS.get(binding.mode, PLACEHOLDERS["3d"])
    current.prompt_resize = binding.mode in composer_module.RESIZABLE_PROMPT_MODES

    current.prompt = prompt_value(hb, props)
    current.editing = bool(hb.generate_3d_overlay_state.get("prompt_active"))
    current.scroll = _composer.prompt.offset
    current.chip_scroll = _composer.chips.offset
    current.caret = _caret_index(current.prompt) if current.editing else None
    current.caret_on = field_module.caret_on() if current.editing else True
    current.selection = selection() if current.editing else None

    current.references = _reference_tiles(hb, binding)
    current.responses = ()
    current.response_scroll = 0.0
    current.scene_chat = False
    current.scene_title = "New chat"
    current.scene_status = ""
    current.scene_activity = ""
    current.scene_busy = False
    current.scene_ellipsis = 0
    chips = _reference_chips(hb, binding)
    _set_object_context(current, "")
    if binding.mode != "motion":
        # One job type; a picker with a single entry is furniture.
        model_chip = hfui.Chip(
            "model",
            _clip(binding.model_label),
            hfui.providers.for_model(binding.model, MODEL_ICON),
            trailing="chevron-down",
        )
        if binding.mode == "3d":
            chips.insert(0, model_chip)
        else:
            chips.append(model_chip)
    if binding.video_route() is not None:
        chips.append(
            hfui.Chip(
                "source",
                dict(VIDEO_SOURCES).get(props.fnf_video_input_source, "Input"),
                "video",
                trailing="chevron-down",
            )
        )
        if props.fnf_video_input_source == "CAMERA":
            camera = props.fnf_video_camera
            chips.append(
                hfui.Chip(
                    "camera",
                    _clip(camera.name if camera else "Choose camera", 16),
                    "video",
                    trailing="chevron-down",
                )
            )
        summary = hb._composer_video_summary(binding)
        if summary and summary[2]:
            chips.append(
                hfui.Chip(
                    "capture-warning",
                    summary[2],
                    "video",
                    kind=hfui.STATUS,
                )
            )
    for item in hb._composer_chip_params(binding):
        chips.append(_param_chip(hb, item))
    current.chips = tuple(chips)

    status, credits = binding.cost()
    current.credits = (
        f"{credits:g}" if status == "ready" and credits is not None else None
    )
    current.enabled = bool(binding.can_submit())
    current.drop_target = _prompt_drop_armed("composer")
    current.ask = None
    if current.login_required:
        current.credits = None
        current.enabled = False
        current.chips = ()
    return current


def draw(hb):
    """Draw handler body. Publishes ``rect`` and ``controls`` for the modal."""
    import bpy

    _composer.host = hb
    _pump.revive()
    _restore_scene_history()
    _restore_prompt_height()
    overlay = hb.generate_3d_overlay_state
    if not overlay.get("visible") or not sk.available():
        overlay["rect"] = (0, 0, 0, 0)
        overlay["controls"] = []
        _composer.menu_rect = None
        _composer.chips_rect = None
        _composer.responses_rect = None
        _composer.msg_rects = {}
        return
    region = bpy.context.region
    if region is None:
        return

    scale = ui_scale()
    started = perf.now()
    current = collect(hb, hb._props(bpy.context))
    perf.mark("collect", started)

    hovered = overlay.get("hover_control")
    pressed = overlay.get("control_pressed")
    _composer.pointer.hovered = hovered[0] if hovered else None
    _composer.pointer.pressed = pressed[0] if pressed else None

    started = perf.now()
    _, left_off, right_off = hb._bar_region_margins(bpy.context.area)
    perf.mark("geometry", started)
    span = region.width - left_off - right_off
    # The composer keeps its authored size and only gives ground on viewports
    # too narrow to hold it; it never grows to fill a wide one.
    limit = composer_module.clamp_width(span / scale - 2 * MARGIN)
    if current.scene_chat and current.responses:
        # The grip only stretches the thread into room the region actually
        # has: the card proper, the mode bar and the margins keep their own.
        cap = (
            region.height / scale
            - composer_module.MAX_HEIGHT
            - composer_module.MODES_HEIGHT
            - composer_module.MODES_GAP
            - composer_module.SCENE_HEADER_HEIGHT
            - 2 * composer_module.SCENE_HEADER_GAP
            - composer_module.RESIZE_GRIP
            - 2 * MARGIN
        )
        if current.references:
            cap -= (
                composer_module.measure_tray(
                    current,
                    _composer.pointer,
                    measure(),
                    limit - 2 * composer_module.SHELL_PAD,
                )
                + composer_module.SCENE_HEADER_GAP
            )
        cap = max(composer_module.RESPONSES_MIN, cap)
        _composer.scene_thread_height = max(
            composer_module.RESPONSES_MIN,
            min(float(_composer.scene_thread_height), cap),
        )
        current.responses_height = _composer.scene_thread_height
    if current.prompt_resize and _composer.prompt_height:
        # Same bargain as the thread's: the grip stretches the prompt into room
        # the region actually has, and every other part of the card — the mode
        # bar, the paddings, the grip itself, the chip strip and an attachment
        # tray — keeps its own out of the deal.
        cap = (
            region.height / scale
            - 2 * MARGIN
            - composer_module.MODES_HEIGHT
            - composer_module.MODES_GAP
            - 2 * composer_module.SHELL_PAD
            - 2 * composer_module.PANEL_PAD
            - composer_module.RESIZE_GRIP
        )
        if current.chips:
            cap -= composer_module.TOOLBAR_HEIGHT + composer_module.PROMPT_TOOLBAR_GAP
        if current.references:
            cap -= composer_module.measure_tray(
                current,
                _composer.pointer,
                measure(),
                limit - 2 * composer_module.SHELL_PAD,
            )
        _composer.prompt_height = max(
            composer_module.MIN_PROMPT_HEIGHT,
            min(
                float(_composer.prompt_height),
                max(composer_module.MIN_PROMPT_HEIGHT, cap),
            ),
        )
    current.prompt_height = (
        float(_composer.prompt_height or 0.0) if current.prompt_resize else 0.0
    )

    _prepare_response_window(current, limit)

    animating = _composer.motion.tick()
    started = perf.now()
    (
        field,
        target,
        prompt_height,
        prompt_content,
        chip_overflow,
        tray,
        response_content,
        caret_pos,
        selection_rects,
        context_pos,
        element_positions,
        thread_rects,
    ) = _card_metrics(current, limit)
    perf.mark("metrics", started)
    current.caret_pos = caret_pos
    current.selection_rects = selection_rects
    current.context_pos = context_pos
    current.element_positions = element_positions
    current.thread_select_rects = thread_rects
    current.chip_overflow = chip_overflow
    current.chip_scroll = _clamp_chip_scroll(chip_overflow)
    response_offset = _composer.responses.fit(
        response_content, current.responses_height
    )
    if _composer.responses.follow:
        response_offset = _composer.responses.to_end()
    current.response_scroll = _response_paint_offset(
        response_offset,
        _composer.responses.overflow,
        current.response_scrolling,
    )
    current.scroll, _ = _clamp_scroll(
        prompt_content,
        prompt_height,
        caret_y=caret_pos[1] if caret_pos else None,
    )
    # Everything ``build`` reads is in this key, so a hit is the same tree the
    # last frame produced. It has to miss whenever the motion tracks move, or
    # ``build`` would never re-aim them — but a track only starts moving when
    # something already in the key (``open``, the mode) has changed, so the
    # aim is never skipped on the frame that needs it.
    tree_key = (
        current.key,
        _composer.pointer.key,
        _composer.motion.key,
        round(limit, 2),
        round(scale, 3),
    )
    if _composer.frame_cache is not None and _composer.frame_cache[0] == tree_key:
        frame = _composer.frame_cache[1]
        perf.count("tree_hits")
    else:
        started = perf.now()
        frame = skui.solve(
            composer_module.build(
                current,
                _composer.pointer,
                _composer.motion,
                available=limit,
                height=target,
                field=field,
                prompt_height=prompt_height,
                tray=tray,
            ),
            measure(),
        )
        perf.mark("solve", started)
        _composer.frame_cache = (tree_key, frame)
    _composer.frame = frame

    # The layer is sized for the composer's largest shape and never changes
    # mid-animation: growing the surface with the card reallocates the numpy
    # scratch, the GPU buffer and the texture every step, which measured 3.6x
    # the cost of re-rasterizing into a surface that already exists.
    settled = composer_module.open_height(target)
    box_width = limit + 2 * MARGIN
    box_height = settled + 2 * MARGIN
    inset_x = MARGIN + (limit - frame.width) * 0.5
    inset_y = box_height - MARGIN - frame.height
    origin_x = left_off + (span - limit * scale) * 0.5 - MARGIN * scale
    origin_y = BOTTOM * scale - MARGIN * scale

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(inset_x, inset_y)
        skui.paint(canvas, frame, measure(), _composer.pointer.state)

    # `tree_key`, not a second tuple: see the note in `launcher_surface`.
    # `box_width` and `inset_x` are both functions of `limit` and of the
    # solved frame, and the frame is what the tree key already stands for.
    _layer.ensure(box_width * scale, box_height * scale, (tree_key,), render)
    _layer.blit(origin_x, origin_y)

    def to_region(rect, dx=inset_x, dy=inset_y):
        x, y, node_width, node_height = rect
        return (
            origin_x + (dx + x) * scale,
            origin_y + (box_height - dy - y - node_height) * scale,
            node_width * scale,
            node_height * scale,
        )

    # ``visible``, not ``rect``: a chip scrolled out of the strip is still laid
    # out, and publishing where it would have been leaves a hit target sitting
    # under the prompt.
    started = perf.now()
    published = []
    msg_rects = {}
    prefix = hfui.chat.MSG + ":"
    for placed in frame.items:
        if placed.node.id is None:
            continue
        shown = placed.visible
        if shown[2] > 0.5 and shown[3] > 0.5:
            published.append((placed.node.id, None, *to_region(shown)))
        if placed.node.id.startswith(prefix):
            msg_rects[placed.node.id] = to_region(placed.rect)
    overlay["rect"] = to_region((0.0, 0.0, frame.width, frame.height))
    published.extend(_draw_menu(current, frame, scale, to_region, region.height))
    overlay["controls"] = published
    _composer.msg_rects = msg_rects
    perf.mark("publish", started)

    chips = frame.index.get("chips")
    _composer.chips_rect = to_region(chips.visible) if chips else None
    responses = frame.index.get("responses")
    _composer.responses_rect = to_region(responses.visible) if responses else None

    # Hover uses these instead of the live rect: a rectangle that is itself
    # animating would hand the pointer in and out of the composer mid-flight.
    _composer.card_rect = (
        origin_x + MARGIN * scale,
        origin_y + MARGIN * scale,
        limit * scale,
        settled * scale,
    )
    _composer.pill_rect = (
        origin_x + (MARGIN + (limit - composer_module.PILL_WIDTH) * 0.5) * scale,
        origin_y + MARGIN * scale,
        composer_module.PILL_WIDTH * scale,
        composer_module.PILL_HEIGHT * scale,
    )

    if animating or _composer.motion.running or current.editing:
        _ensure_pulse()


def _thread_select_rects(current, limit, fonts):
    """Highlight rects for the selected run in one Scene Builder message."""
    selected = current.thread_select
    if selected is None:
        return ()
    index, start, end = selected
    start, end = min(start, end), max(start, end)
    if start >= end or index < 0 or index >= len(current.responses):
        return ()
    response = current.responses[index]
    text = composer_module.response_text(response)
    if not text:
        return ()
    start = min(start, len(text))
    end = min(end, len(text))
    if start >= end:
        return ()
    return _selection_rects(
        text,
        start,
        end,
        hfui.tokens.BODY_MEDIUM,
        composer_module.message_wrap(response.role, limit),
        fonts,
    )


def _card_metrics(current, limit):
    """``(field, card height, prompt viewport, prompt content, chip overflow,
    tray height, response content, caret pos, selection rects, context pos,
    element positions, thread selection rects)``.

    ``caret pos`` is ``(x, y, height)`` in the text's own space, or None when
    the prompt is not being edited.

    Everything here re-wraps the prompt, which costs tens of milliseconds on a
    long one — far too much to repeat at the blink's 60fps. Scroll and the
    blink phase stay out of the key: neither changes any of these numbers.
    """

    key = (
        current.mode,
        current.prompt,
        current.editing,
        current.caret,
        current.selection,
        current.chips,
        current.context,
        current.context_index,
        current.context_reserve,
        current.context_display_span,
        current.mention_projection,
        current.references,
        (
            current.response_window.key
            if current.response_window is not None
            else current.responses
        ),
        round(float(current.responses_height or 0.0), 1),
        round(float(current.prompt_height or 0.0), 1),
        current.prompt_resize,
        # The credits ride on the Generate button and change its width, which
        # moves every number worked out here.
        current.credits,
        current.thread_select,
        round(limit, 1),
    )
    if _composer.metrics_cache is None or _composer.metrics_cache[0] != key:
        (
            field,
            height,
            viewport,
            content,
            chip_overflow,
            tray,
            response_content,
        ) = composer_module.measure_card(current, _composer.pointer, measure(), limit)
        caret_pos = None
        rects = ()
        context_pos = None
        element_positions = ()
        fonts = measure().fonts
        inner = max(40.0, field - 2 * hfui.controls.PROMPT_PAD_X)
        projection = current.mention_projection
        _ascent, _descent, line_height = fonts.metrics(hfui.tokens.PROMPT)
        mention_inset = max(
            0.0,
            (line_height - hfui.controls.MENTION_HEIGHT) * 0.5,
        )

        def mention_pos(index):
            x, y = _caret_xy(
                current.display_prompt,
                index,
                hfui.tokens.PROMPT,
                inner,
                fonts,
            )
            return x, y + mention_inset

        if current.context_display_span is not None and current.context:
            context_pos = mention_pos(current.context_display_span[0])
        if projection is not None:
            element_positions = tuple(
                (
                    mention.element,
                    *mention_pos(mention.start),
                )
                for mention in projection.mentions
            )
        if current.editing:
            caret_index = (
                current.caret if current.caret is not None else len(current.prompt)
            )
            if projection is not None:
                caret_index = projection.display_index(caret_index)
            caret_x, caret_y = _caret_xy(
                current.display_prompt,
                caret_index,
                hfui.tokens.PROMPT,
                inner,
                fonts,
            )
            caret_height = float(hfui.tokens.BODY.line_height or line_height)
            caret_inset = max(0.0, (line_height - caret_height) * 0.5)
            caret_pos = (caret_x, caret_y + caret_inset, caret_height)
            if current.selection:
                start, end = current.selection
                if projection is not None:
                    start = projection.display_index(start)
                    end = projection.display_index(end)
                rects = _selection_rects(
                    current.display_prompt,
                    start,
                    end,
                    hfui.tokens.PROMPT,
                    inner,
                    fonts,
                )
        thread_rects = _thread_select_rects(current, limit, fonts)
        _composer.metrics_cache = (
            key,
            (
                field,
                height,
                viewport,
                content,
                chip_overflow,
                tray,
                response_content,
                caret_pos,
                rects,
                context_pos,
                element_positions,
                thread_rects,
            ),
        )
    return _composer.metrics_cache[1]


def _draw_menu(current, frame, scale, to_region, region_height):
    """The open select, on its own layer above the composer.

    It is anchored to the chip in *this* frame rather than the last one, so it
    cannot drift while the card is still animating.

    Its height is capped at whatever the room above that chip allows, and never
    more than ``MENU_MAX_HEIGHT``: the catalog decides how many rows a select
    has, and the video model list alone comes to nine hundred pixels of them.
    """

    _composer.menu_rect = None
    anchor = frame.index.get(current.menu) if current.menu else None
    if anchor is None:
        return ()

    anchor_x = anchor.x
    anchor_y = anchor.y
    anchor_height = anchor.height
    if current.menu == "prompt" and _composer.element_query_span is not None:
        # A normal select belongs to its control. The element typeahead belongs
        # to the unfinished @token, which can be anywhere inside a wrapped,
        # scrolled prompt. Project the logical trigger through the same mention
        # map used by the caret so preceding chips and object context count at
        # their rendered widths rather than as their one-character markers.
        query_start = _composer.element_query_span[0]
        projection = current.mention_projection
        if projection is not None:
            query_start = projection.display_index(query_start)
        inner = max(
            40.0,
            anchor.width - 2 * hfui.controls.PROMPT_PAD_X,
        )
        query_x, query_y = _caret_xy(
            current.display_prompt,
            query_start,
            hfui.tokens.PROMPT,
            inner,
            measure().fonts,
        )
        _ascent, _descent, line_height = measure().fonts.metrics(hfui.tokens.PROMPT)
        anchor_x += hfui.controls.PROMPT_PAD_X + query_x
        anchor_y += (
            hfui.controls.PROMPT_PAD_Y + query_y - max(0.0, current.scroll)
        )
        anchor_height = line_height

    # In region space the chip's top edge is its published bottom plus its
    # height. Everything above that is the popup's, less the gap it opens with
    # and a margin that keeps it off the region's top edge.
    _ax, ay, _aw, ah = to_region((anchor_x, anchor_y, 0.0, anchor_height))
    room = (region_height - (ay + ah)) / max(scale, 0.001) - (
        hfui.tokens.SM + hfui.tokens.MD
    )
    if current.menu == "scene-history":
        current.menu_limit = max(
            hfui.controls.menu_height(1, title=current.menu_title),
            min(
                hfui.controls.menu_height(3, title=current.menu_title),
                room,
            ),
        )
    else:
        current.menu_limit = max(
            3 * hfui.controls.MENU_ITEM,
            min(hfui.controls.MENU_MAX_HEIGHT, room),
        )
    current.menu_scroll = _clamp_menu_scroll(
        hfui.controls.menu_rows_height(len(current.menu_items)),
        hfui.controls.menu_viewport(current.menu_limit, current.menu_title),
        current.menu_value,
    )

    tree = composer_module.popup(current, _composer.pointer)
    if tree is None:
        return ()
    menu = skui.solve(tree, measure())
    # Selects follow their chip's left edge. The workspace menu follows the
    # card's right edge so it cannot run past it; history now belongs to the
    # left edge with its trigger.
    if current.menu == "scene-workspace":
        menu_x = frame.width - menu.width
    elif current.menu == "prompt":
        menu_x = max(0.0, min(anchor_x, frame.width - menu.width))
    else:
        menu_x = anchor_x
    menu_y = anchor_y - menu.height - hfui.tokens.SM
    origin_x, origin_y, _, _ = to_region((menu_x - MARGIN, menu_y - MARGIN, 0.0, 0.0))
    origin_y -= (menu.height + 2 * MARGIN) * scale

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(MARGIN, MARGIN)
        skui.paint(canvas, menu, measure(), _composer.pointer.state)

    _menu_layer.ensure(
        (menu.width + 2 * MARGIN) * scale,
        (menu.height + 2 * MARGIN) * scale,
        (
            round(scale, 3),
            current.menu,
            current.menu_items,
            current.menu_value,
            current.menu_title,
            round(current.menu_limit, 1),
            round(current.menu_scroll, 1),
            _composer.pointer.key,
        ),
        render,
    )
    _menu_layer.blit(origin_x, origin_y)

    def to_menu_region(rect):
        x, y, width, height = rect
        return (
            origin_x + (MARGIN + x) * scale,
            origin_y + (menu.height + MARGIN - y - height) * scale,
            width * scale,
            height * scale,
        )

    _composer.menu_rect = to_menu_region((0.0, 0.0, menu.width, menu.height))
    # ``visible`` again, for the same reason the composer uses it: a row
    # scrolled out of the capped viewport is still laid out, and publishing
    # where it would have been leaves a target hanging over the card.
    published = []
    for placed in menu.items:
        if placed.node.id is None:
            continue
        shown = placed.visible
        if shown[2] > 0.5 and shown[3] > 0.5:
            published.append((placed.node.id, None, *to_menu_region(shown)))
    return published


def _within(rect, mx, my):
    if rect is None:
        return False
    x, y, width, height = rect
    return x <= mx <= x + width and y <= my <= y + height


def pointer_over(mx, my):
    """True when the pointer is on the pill, the open card, or its menu."""
    current = state()
    target = _composer.card_rect if current.open else _composer.pill_rect
    return _within(target, mx, my) or _within(_composer.menu_rect, mx, my)


def blocks_viewport(mx, my):
    """True when viewport navigation must not run under the pointer.

    Covers the closed pill, the expanded card, and an open select menu. Outside
    those bounds the scene stays fully interactive even if the composer is
    pinned open.
    """
    return pointer_over(mx, my)


def hover(hb, mx, my):
    """Mouse move: returns whether anything visually changed.

    Opening takes the pill, staying open takes the whole card — without that
    hysteresis the pointer would fall out of the composer as it grows, and it
    would flicker at the edges.

    Reaching the pill opens the card on that very move. Leaving starts the
    close animation on that same move, unless a click has pinned it open.
    """

    _composer.host = hb
    current = state()
    return current.hover(pointer_over(mx, my))


def activate(hb, node_id, mx=None, my=None):
    """Route a click by node id. Returns True when the composer handled it.

    Every id published by ``draw`` lands here, and every branch writes through
    to the same properties the N-panel edits — so a chip and its panel widget
    cannot disagree, and a value changed here re-estimates cost by the same
    RNA update the panel relies on.

    ``mx`` / ``my`` (region space) let a prompt click place the caret; without
    them the caret stays where it was (or parks at the end on first focus).
    """
    import bpy

    current = state()
    overlay = hb.generate_3d_overlay_state
    if node_id is None:
        if _scene_conversation().skip_ask():
            return True
        current.click(node_id)
        current.hide_menu()
        overlay["prompt_active"] = False
        _composer.field.anchor = None
        _clear_thread_select()
        return True
    current.click(node_id)
    if _scene_conversation().handle_ask(node_id):
        if node_id == hfui.chat.ASK_CUSTOM:
            overlay["prompt_active"] = True
        return True

    props = hb._props(bpy.context)
    if node_id == "prompt":
        overlay["prompt_active"] = True
        if mx is not None and my is not None:
            place_caret(hb, mx, my)
        elif _composer.field.caret is None:
            _composer.field.caret = len(prompt_value(hb, props))
        _ensure_pulse()
        return True
    overlay["prompt_active"] = False
    _composer.field.anchor = None

    if node_id.startswith("menu:"):
        _choose(hb, props, node_id)
        return True
    if current.menu:
        # Any other click closes an open select, the same way clicking out
        # does — and clicking the trigger that opened it only closes it.
        anchor = current.menu
        current.hide_menu()
        if node_id == anchor:
            return True

    if node_id.startswith("mode:"):
        return _switch_mode(hb, props, node_id.split(":", 1)[1])
    if node_id == "chips":
        # The gap between chips. Nothing to do — the click has already pinned
        # the card open, which is all landing on empty strip should mean.
        return True
    if node_id == "object-context":
        # Dragging owns this token; a release without movement is inert.
        return True
    if node_id.startswith("object-context:") and node_id.endswith(":close"):
        name = node_id[len("object-context:") : -len(":close")]
        if name:
            _dismiss_object_context(name)
        return True
    if _composer.standalone_mode == composer_module.CAMERA:
        if node_id.startswith("phonecam:"):
            pass
            return True
        if node_id == "submit":
            # The camera card draws no Generate button; the collapsed pill
            # still has its round one, and there is nothing to submit.
            return True
    if node_id == "bridge-connect":
        agent.act()
        return True
    if node_id == "scene-stop":
        _scene_conversation().stop(hb)
        return True
    if (
        node_id == "submit"
        and _composer.standalone_mode == composer_module.SCENE_BUILDER
    ):
        _submit_scene_builder(hb)
        return True
    if _composer.standalone_mode == composer_module.SCENE_BUILDER:
        if node_id == "scene-new-chat":
            # New Chat is the explicit way to replace this scene's primary
            # thread. Picking an existing history row never does so.
            _new_scene_thread(bind_scene=True)
            return True
        if node_id == "scene-history":
            # Open on what is in hand, refresh behind it: the landing rebuilds
            # the menu in place if it is still up.
            _request_scene_history(hb)
            _open_scene_history_menu()
            return True
        if node_id == "scene-workspace":
            _open_scene_workspace_menu(hb)
            return True
        if node_id == "scene-reference-add":
            _open_menu(
                "scene-reference-add",
                [(label, icon) for _value, label, icon in IMAGE_SOURCES],
                [value for value, _label, _icon in IMAGE_SOURCES],
            )
            return True
        if node_id == "scene-model":
            _request_scene_models(hb)
            _open_scene_model_menu()
            return True
        if node_id.startswith("scene-reference:"):
            parts = node_id.split(":")
            if len(parts) > 2 and parts[2] == "remove":
                with contextlib.suppress(ValueError):
                    _detach_scene_builder(int(parts[1]))
            return True

    if _composer.standalone_mode == composer_module.RETEXTURE:
        if node_id == "model":
            _open_model_menu(
                hb._composer_binding(props, "3d"),
                selected=_RETEXTURE_JOB_TYPE,
            )
            return True
        if node_id == "retexture:image":
            _choose_scene_images()
            return True
        if node_id == "retexture:uv":
            props.retexture_original_uv = not props.retexture_original_uv
            return True
        if node_id == "retexture:pbr":
            props.retexture_pbr = not props.retexture_pbr
            return True
        if node_id.startswith("retexture-reference:"):
            if node_id.endswith(":remove"):
                props.retexture_image_path = ""
            return True
        if node_id == "submit":
            if not current.enabled:
                hb.add_report(
                    "Select a mesh and enter a style prompt or image.",
                    type="ERROR",
                )
                return True
            bpy.ops.scene_agent.retexture("EXEC_DEFAULT")
            return True
        return True

    binding = _binding(hb, props)
    if node_id.startswith("motion:"):
        return False
    if node_id == "model":
        _open_model_menu(binding)
        return True
    if node_id == "source":
        _open_menu(
            "source",
            [(label, "video") for _value, label in VIDEO_SOURCES],
            [value for value, _label in VIDEO_SOURCES],
            props.fnf_video_input_source,
        )
        return True
    if node_id == "camera":
        cameras = [obj for obj in bpy.context.scene.objects if obj.type == "CAMERA"]
        if not cameras:
            hb.add_report("The scene has no camera to render from.", type="ERROR")
            return True
        selected = props.fnf_video_camera
        _open_menu(
            "camera",
            [(camera.name, "video") for camera in cameras],
            [camera.name for camera in cameras],
            selected.name if selected else None,
        )
        return True
    if node_id == "reference":
        if binding.mode == "3d":
            _choose_scene_images()
            return True
        _open_menu(
            "reference",
            [(label, icon) for _value, label, icon in IMAGE_SOURCES],
            [value for value, _label, _icon in IMAGE_SOURCES],
        )
        return True
    if node_id.startswith("reference:"):
        # ``reference:<index>`` is the tray tile, which is not itself a button;
        # only its remove badge does anything.
        parts = node_id.split(":")
        if len(parts) > 2 and parts[2] == "remove":
            with contextlib.suppress(ValueError):
                hb._composer_remove_reference(binding, int(parts[1]))
        return True
    if node_id.startswith("param:"):
        return _activate_param(hb, binding, node_id)
    if node_id == "submit":
        if not binding.submit():
            hb.add_report("Complete the required fields.", type="ERROR")
        return True
    return False


def _switch_mode(hb, props, mode):
    """The mode tabs: move the N-panel tab, then wake the mode up.

    A mode the user has not visited has no catalog, no parameters and no cost
    estimate, so without this the composer would open on an empty model chip
    and a Generate button that never enables.
    """
    if mode == composer_module.ASSETS:
        hb._show_bar()
        return True

    _composer.field.caret = None
    _composer.field.anchor = None
    _composer.field.forget()
    _composer.chips.reset()

    if mode == composer_module.SCENE_BUILDER:
        _composer.standalone_mode = mode
        # A click, so scheduling network work is allowed here: warm the model
        # catalog and the recents page for the two menus this mode owns.
        _prefetch_scene_data(hb)
        agent.ensure()
        return True

    if mode == composer_module.CAMERA:
        # Opening the tab is the pairing gesture, so the server starts here
        # rather than behind one more button. Runs from a click, never a draw.
        import bpy

        from .. import phonecam

        _composer.standalone_mode = mode
        if not phonecam.cam_is_running():
            with contextlib.suppress(Exception):
                bpy.ops.scene_agent.cam_start()
        return True

    _composer.standalone_mode = None
    section = hb.COMPOSER_SECTIONS.get(mode)
    if not section:
        return True
    if props.ui_section != section:
        props.ui_section = section
    # The new mode brings its own chips; leaving the strip mid-scroll would
    # open it somewhere in the middle of a row the user has not seen.
    hb._composer_prepare(hb._composer_binding(props, mode))
    return True


def switch_mode(hb, mode):
    """Switch from another surface through the composer's shared mode tabs."""
    import bpy

    return _switch_mode(hb, hb._props(bpy.context), mode)


def _activate_param(hb, binding, node_id):
    """``param:<key>``: toggle it, reroll it, open its menu, or hand it over.

    The last case is the point of exposing every setting: a free string or a
    raw JSON object has no control on the overlay, but its chip still says the
    setting is there and takes the user to where it can be edited.
    """
    key = node_id.split(":")[1] if ":" in node_id else ""
    item = binding.param(key)
    if item is None:
        return True
    if item.kind == "boolean":
        hb._composer_toggle_param(binding, key)
        return True
    # A slider was already set on press, by position; the release has nothing
    # left to do with it.
    if hb._composer_is_slider(item):
        return True
    if item.kind == "enum":
        options = hb._composer_param_options(item)
        if len(options) > 1:
            _open_menu(
                node_id,
                [(label, None) for _identifier, label in options],
                [identifier for identifier, _label in options],
                item.enum_value,
                title=hfui.params.label(item.key, item.title),
            )
        return True
    if key in hfui.params.REROLL_KEYS:
        hb._composer_reroll_param(binding, key)
        return True
    hb._composer_open_settings(binding.mode)
    return True


def _open_menu(
    anchor,
    rows,
    values,
    selected=None,
    title=None,
    *,
    preserve_scroll=False,
):
    """Show a select over ``anchor`` and remember what its rows stand for."""

    _composer.menu_values = tuple(values)
    # Unplaced: the first draw scrolls the current value into view, once it
    # knows how much room the popup was given.
    if not preserve_scroll:
        _composer.menu.reset()
    index = next((row for row, value in enumerate(values) if value == selected), None)
    state().show_menu(anchor, rows, index, title=title)


def _open_model_menu(binding, *, selected=None):
    """Open a catalog picker, including grouped 3D transform models."""
    models = binding.catalog.get("models") or {}
    if binding.mode == "3d":
        rows = []
        values = []
        for _workflow, group_label, items in binding.model_groups():
            rows.append((group_label, None, "heading"))
            values.append(None)
            for job, label in items:
                rows.append(
                    (
                        label,
                        hfui.providers.for_model(models.get(job), MODEL_ICON),
                    )
                )
                values.append(job)
    else:
        items = binding.model_items()
        rows = [
            (label, hfui.providers.for_model(models.get(job), MODEL_ICON))
            for job, label in items
        ]
        values = [job for job, _label in items]
    if not values or not any(value is not None for value in values):
        binding.refresh_catalog()
        return
    _open_menu(
        "model",
        rows,
        values,
        binding.job_type if selected is None else selected,
    )




def _choose(hb, props, node_id):
    """A select row was clicked: ``menu:<anchor>:<row index>``."""
    current = state()
    head, _, row = node_id.rpartition(":")
    anchor = head[len("menu:") :]
    current.hide_menu()
    try:
        value = _composer.menu_values[int(row)]
    except (ValueError, IndexError):
        return
    if value is None:
        return

    if anchor == "prompt":
        _insert_element_mention(hb, value)
        return
    if anchor == "scene-workspace":
        props.fnf_workspace = value
        return
    if anchor == "scene-model":
        _composer.scene_model = value
        return
    if anchor == "scene-reference-add":
        if value == "UPLOAD":
            _choose_scene_images()
        else:
            _capture_scene_builder(hb, value)
        return
    if anchor == "scene-history":
        if value.startswith("remote:"):
            _import_remote_chat(hb, value[len("remote:") :])
        else:
            _switch_scene_thread(value)
        return

    binding = _binding(hb, props)
    if anchor == "reference":
        if value == "UPLOAD":
            _choose_scene_images()
        else:
            hb._composer_add_reference_source(binding, value)
    elif anchor == "model":
        if value == _RETEXTURE_JOB_TYPE:
            pass
            return
        if _composer.standalone_mode == composer_module.RETEXTURE:
            _composer.standalone_mode = None
            binding = hb._composer_binding(props, "3d")
            _composer.prompt.reset()
            _composer.chips.reset()
        text = _current_text(binding)
        binding = binding.select_model(value)
        if not binding:
            return
        # Rebuild starts the prompt param empty; put the field's text back so
        # cost decode sees it without waiting for another keystroke.
        if binding.prompt_item is not None and text:
            binding.prompt_item.string_value = text
            _composer.prompts[binding.mode] = text
    elif anchor == "source":
        props.fnf_video_input_source = value
        binding.schedule_cost()
    elif anchor == "camera":
        import bpy

        props.fnf_video_camera = bpy.context.scene.objects.get(value)
    elif anchor.startswith("param:"):
        hb._composer_set_enum(binding, anchor.split(":", 1)[1], value)
