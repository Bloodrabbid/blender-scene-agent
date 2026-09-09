"""The assistant bubble as a viewport overlay.

Fourth surface, bottom-right corner. Same contract as the other three: read
add-on state into a state object, solve the tree in logical pixels, raster it
into a cached GPU layer, publish hit rects in region coordinates for the modal.

Two things here are its own:

- **Two layers, two shapes.** The bubble and the panel are different enough
  that morphing one into the other would mean re-wrapping every message in the
  thread on each frame of the move. They are rastered separately and the
  entrance is a cross-fade plus a translate of two cached textures — the
  explorer's lesson, at a fifth of its size.
- **The conversation is not here.** ``features/conversation.py`` holds the
  thread and the turn in flight, beside the client that fills them; this
  module hands the prompt over and is told when a page landed. That is what
  lets a turn keep running with the panel shut, and lets teardown drop the
  panel without dropping the answer.

The text field is the composer's, reused rather than rewritten: the pure
measurement helpers (``_line_spans``, ``_caret_xy``, ``_selection_rects``,
``caret_index_at``) and the platform modifier predicates all come from there,
so the two fields cannot drift apart on what Option+Left means.
"""

from __future__ import annotations

import contextlib
import time

from ... import hfui, perf, skui
from ...hfui import chat as chat_module
from .. import conversation
from ...hfui import field as field_module
from . import CLIPBOARD, composer_surface as cs
from . import pump as pump_module
from . import redraw_viewports
from . import skia_runtime as sk

# Room around the panel for its shadow.
MARGIN = 19.0
# Inset from the viewport's bottom and right edges. The bottom matches the
# composer's so the two sit on one line.
BOTTOM = 10.0
RIGHT = 10.0

# The tallest share of the viewport the panel may take. A 320-point panel on a
# short region would otherwise run off the top.
MAX_SHARE = 0.8

# One wheel notch, per region. The prompt shows about two lines and rarely
# holds more than four, so it gets a line rather than the thread's page.
THREAD_WHEEL_STEP = 48.0
PROMPT_WHEEL_STEP = 18.0

_MORPH_FPS = 60.0

# Layers are resources, not state: each owns a Skia surface and a GPU texture
# that have to be *released* in order, so they are not fields of the object
# below and teardown does not rebuild them. Same for the pump's timer.
_layer = sk.Layer("chat")
# Deep enough to hold a whole period of the breath plus the pressed variant and
# the resting frame, so the loop settles into texture replays instead of
# re-rastering forever. A disc texture is 140x140 RGBA16F, ~153 KB, so the
# whole cache is under 6 MB — less than the composer's single layer, and it
# buys back 8% of the main thread for as long as Blender is open.
_bubble_layer = sk.Layer("chat-bubble", keep=chat_module.PULSE_STEPS + 4)
_tooltip_layer = sk.Layer("chat-tooltip")


def _field_metrics():
    """What the prompt wraps at: font, width, book. `hfui.TextField` asks."""
    return hfui.tokens.BODY, _field_limit(), measure().fonts


class Chat:
    """Everything the panel remembers between frames.

    One object rather than twenty-seven globals: teardown is a reconstruction,
    the defaults live where the fields are declared, and `__slots__` makes a
    misspelled field an error rather than a new attribute nothing reads.

    The thread is not in here — see `_conversation`. What is in here is the
    field the user types into and the geometry the last draw measured.

    Grouped by **kind**, in the order every surface uses — see
    `docs/state-model.md`. The kind is the rule: who may write the field, and
    what happens to it at teardown.
    """

    __slots__ = (
        # -- Resources: lifetime, released in order, never values.
        "measure",
        "host",
        # -- Model: what input writes, and only input.
        "pointer",
        "thread",
        "prompt",
        "text",
        "field",
        "hover_since",
        "pan_accum",
        "thread_select",
        "thread_dragging",
        # -- Anim: what the clock writes.
        "motion",
        # -- Derived: pure, and always safe to drop.
        "state",
        "frame_cache",
        "bubble_cache",
        "tooltip_cache",
        "metrics_cache",
        # -- Published: what the draw hands the modal.
        "panel_rect",
        "bubble_rect",
        "prompt_rect",
        "thread_rect",
        "msg_rects",
    )

    def __init__(self):
        # -- Resources ------------------------------------------------------
        self.measure = None
        # The add-on module, latched by each draw. Timers and event handlers
        # are handed no `hb` of their own and reach it through here.
        self.host = None

        # -- Model ----------------------------------------------------------
        self.pointer = hfui.Pointer()
        # Two viewports into content that does not fit them, host-owned
        # exactly like the composer's three. The thread sticks to the bottom
        # until the user scrolls up: a reply landing below the fold is a reply
        # nobody reads. The prompt chases the caret instead, on an edge — see
        # `_clamp_prompt`.
        self.thread = hfui.Scroller(follow=True)
        self.prompt = hfui.Scroller()
        # The field. The thread it writes into is not here — see
        # `_conversation` — and it starts empty: a canned greeting is a message
        # the assistant did not send, and it reads as one, same ink and same
        # place a real reply will land. The prompt's placeholder already says
        # what to do, where the answer is typed.
        self.text = ""
        # Caret, selection, undo and the editing keymap — the composer's
        # prompt's, exactly. Only the storage differs: that one lives in a
        # scene property and this one is the field above, so the operations
        # take a string and return one.
        self.field = hfui.TextField(
            cap=4000, measure=_field_metrics, clipboard=CLIPBOARD
        )
        # When the pointer settled on the resting disc, or None when it is
        # elsewhere. The tooltip is owed after `TOOLTIP_DELAY` of that; the
        # pump wakes for it.
        self.hover_since = None
        self.pan_accum = 0.0
        # Read-only selection in the thread: ``(message index, anchor, caret)``.
        self.thread_select = None
        self.thread_dragging = False

        # -- Anim -----------------------------------------------------------
        self.motion = hfui.Motion()
        # ``Motion.to`` reads the current value with the *target* as its
        # default, so a track aimed at 1 that has never held a value lands
        # there — the panel would appear rather than rise. Seed it shut here,
        # so a rebuild cannot forget to.
        self.motion.set("show", 0.0)

        # -- Derived --------------------------------------------------------
        self.state = None
        # The last solved panel, under the same key the layer rasterizes
        # against.
        self.frame_cache = None
        # The disc's solved trees, one per key the layer can be asked for. A
        # dict rather than the panel's single slot because the breath cycles
        # through a fixed set and a one-deep cache would miss every frame of
        # it. Bounded by construction: `PULSE_STEPS` phases times the pointer
        # states.
        self.bubble_cache = {}
        self.tooltip_cache = None
        self.metrics_cache = None

        # -- Published ------------------------------------------------------
        # Region-space rects, settled (never the animating ones): hover would
        # hand the pointer in and out of the panel mid-flight otherwise.
        self.panel_rect = None
        self.bubble_rect = None
        self.prompt_rect = None
        self.thread_rect = None
        # Unclipped region-space rects for ``msg:N`` nodes, so a scrolled clip
        # still maps a click onto the right character.
        self.msg_rects = {}

    def renewed(self):
        """A fresh object for teardown, carrying the user's own text across.

        Everything here is this surface's business and is meant to go, with one
        exception: the draft is a sentence the user typed, and `release` runs
        when the viewport UI is switched *off* as well as at unregister.
        Coming back to an emptied field would read as the add-on having lost
        the message rather than having put the panel away.
        """
        fresh = Chat()
        fresh.text = self.text
        fresh.field.caret = self.field.caret
        return fresh


_chat = Chat()


# ---------------------------------------------------------------------------
# The conversation
#
# The thread itself lives in `features/conversation.py`, beside the client that
# fills it. This end is a listener: it hands the prompt over on a send and is
# told when a poll page landed. The conversation outlives the panel — a turn
# started before the panel was shut keeps running and its answer is there when
# it opens again — so nothing here owns it.
# ---------------------------------------------------------------------------


def _conversation():
    """The live thread, with this surface subscribed to it."""
    chat = conversation.live()
    if chat.on_change is not _changed:
        chat.on_change = _changed
    return chat


def _changed():
    """A page landed, or a file was staged. Both change what the tree measures."""
    _invalidate()
    redraw_viewports()


def send(hb):
    """Commit the prompt. The field empties only if the turn was accepted."""

    if not _conversation().send(hb, _chat.text):
        return False
    _chat.text = ""
    _chat.field.caret = 0
    _chat.field.anchor = None
    _chat.field.forget()
    _chat.prompt.reset()
    # A reply arriving off the bottom of the thread is a reply nobody sees.
    _chat.thread.follow = True
    _invalidate()
    return True


def clear(hb):
    """Empty the thread, and start a new chat on the next send."""

    _conversation().clear(hb)
    _chat.thread.reset()
    _chat.thread.follow = True
    _clear_thread_select()
    return True


def attach(paths):
    """Add picked files to the pending attachments. Called by the operator."""
    added = _conversation().attach(paths)
    if added:
        state().pinned = True
    return added


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


def state():
    if _chat.state is None:
        _chat.state = chat_module.ChatState()
    return _chat.state


def measure():
    if _chat.measure is None:
        _chat.measure = skui.Measure()
    return _chat.measure


def pointer():
    return _chat.pointer


def ui_scale():
    """The composer's, deliberately — see ``launcher_surface.ui_scale``."""
    return cs.ui_scale()


def _invalidate():
    """Anything that changes what the tree measures drops the metrics cache."""
    _chat.metrics_cache = None


def _editing():
    return bool(_chat.host is not None and _chat.host.chat_state.get("prompt_active"))


def _tooltip_due_in():
    """Seconds until the tooltip is owed, 0 if it is owed now, None if not.

    The delay is the only thing in this surface that has to happen without an
    event to hang it on, so the pump has to be told to come back for it — a
    pointer resting on the disc generates no further mouse moves.
    """
    if _chat.hover_since is None:
        return None
    if _chat.state is not None and _chat.state.open:
        return None
    return max(0.0, chat_module.TOOLTIP_DELAY - (time.monotonic() - _chat.hover_since))


def _ellipsis_step():
    """The dot phase for waiting rows, or None when nothing is waiting.

    Only while the panel is open: the disc has its own animation for a turn
    running behind a closed panel, and a phase changing under a thread nobody
    is looking at would tick the pump and bust the panel's caches for pixels
    that are not on screen.
    """
    if _chat.state is None or not _chat.state.open:
        return None
    if not _conversation().waiting():
        return None
    return chat_module.DOTS.step()


def _awake():
    """This is the one pump in the product that never retires on its own.

    The disc breathes for as long as Blender is open, so the guard has to be
    exhaustive: the handler can be pulled by the ``viewport_ui`` preference or
    by a breaker trip, and a timer left calling ``redraw_viewports`` against a
    surface nobody draws would tag the viewport forever with nothing to show.

    **Ask `mount` for the handler, not the host.** This read used to be
    ``getattr(host, "_chat_handler", None)``, from when that global lived in
    the add-on's root module; it moved here with the rest of the mounting and
    the `getattr` default then answered `None` for every call, so the one pump
    that is supposed to never retire retired on its first tick and the disc
    only breathed on whatever redraw happened along. A default is a fine way
    to tolerate a missing attribute and a terrible way to find out you are
    reading the wrong object.
    """
    from . import mount

    return (
        _chat.host is not None
        and mount._chat_handler is not None
        and not _chat.host.chat_breaker.tripped
    )


def _nap():
    """The entrance, the tooltip, the caret blink and the waiting dots.

    **`due` is a duration, so test it against `None`, not for truth.** Zero
    means *owed right now*, and treating that as "nothing to do" retired the
    pump on the exact tick the label came due — before the redraw that aims
    the track — so the tooltip only ever appeared if some unrelated redraw
    happened along. That is the whole reason it looked like hover did nothing.
    """
    due = _tooltip_due_in()
    waiting = due is not None and due > 0.0
    # The frame that aims the fade has to be asked for: `draw` is the only
    # place the track is pointed at its target.
    unaimed = abs(_chat.motion.value("tip", 0.0) - (1.0 if due == 0.0 else 0.0)) > 1e-3
    if _chat.motion.running or unaimed:
        return 1.0 / _MORPH_FPS
    # One tick per quantised step. Waking faster only redraws a frame that
    # rounds to the phase already on screen.
    if _mark_step() is not None:
        return chat_module.BREATH.due()
    # Whatever is owed soonest. The dots and the caret can both be running,
    # and a turn can land while the tooltip is still pending.
    naps = []
    if _ellipsis_step() is not None:
        naps.append(chat_module.DOTS.due())
    if (
        _chat.state is not None
        and _chat.state.open
        and _conversation().busy()
    ):
        # The working line under the thread ticks once a second.
        naps.append(1.0)
    if waiting:
        # Sleep exactly to the moment it is owed. Waking early only redraws
        # a frame that would draw the same thing.
        naps.append(max(1.0 / 120.0, due))
    if _editing():
        naps.append(max(0.05, field_module.CARET.due()))
    return min(naps) if naps else None


_pump = pump_module.Pump("chat pump", _nap, alive=_awake)
park = _pump.park
_ensure_pulse = _pump.wake


# ---------------------------------------------------------------------------
# Reading the host
# ---------------------------------------------------------------------------


def visible(hb):
    return bool(hb.is_authenticated() and sk.available())


def collect(hb, cap):
    """The host's half of the state.

    Indices in; the measured `caret_pos` and `selection_rects` are left to
    `_metrics` and `draw`. Same split as `composer_surface.collect`.
    """
    from .operators import os_drop_surface

    current = state()
    chat = _conversation()
    current.messages = tuple(chat.messages)
    current.prompt = _chat.text
    current.attachments = tuple(chat.attachments)
    current.editing = _editing()
    current.caret = _caret_index() if current.editing else None
    current.selection = selection() if current.editing else None
    current.caret_on = field_module.caret_on() if current.editing else False
    current.busy = chat.busy()
    current.working = (
        chat_module.working_status(chat.working_seconds()) if chat.busy() else ""
    )
    current.activity = (
        chat_module.turn_activity(chat.turn.message) if chat.busy() else ""
    )
    current.ellipsis = _ellipsis_step() or 0
    current.away = bool(hb.bar_state.get("visible"))
    current.height = cap
    current.drop_target = os_drop_surface() == "chat"
    current.ask = chat.ask_state()
    if current.ask is not None:
        current.pinned = True
    if chat_module.ask_hides_prompt(current.ask):
        current.editing = False
        current.caret = None
        current.selection = None
    current.thread_select = _chat.thread_select
    return current


def selection():
    """``(start, end)`` of the selected run, or None when nothing is selected."""
    return _chat.field.selection()


def _caret_index():
    return _chat.field.index(_chat.text)


def _field_limit():
    """Width the prompt wraps at, in logical pixels."""
    return max(
        40.0,
        chat_module.inner_width()
        - 2 * chat_module.INPUT_PAD
        - 2 * hfui.controls.PROMPT_PAD_X,
    )


def _thread_select_rects(current, fonts):
    """Highlight rects for the selected run in one thread message."""
    selected = current.thread_select
    if selected is None:
        return ()
    index, start, end = selected
    start, end = min(start, end), max(start, end)
    if start >= end or index < 0 or index >= len(current.messages):
        return ()
    message = current.messages[index]
    text = message.text or ""
    if not text:
        return ()
    return field_module.selection_rects(
        text,
        start,
        end,
        hfui.tokens.BODY,
        chat_module.wrap_width(message.role, chat_module.inner_width()),
        fonts,
    )


def _metrics(current):
    """``(prompt viewport, prompt content, thread height, thread content,
    caret pos, selection rects, thread selection rects)``, memoised on
    everything that moves them."""

    # Keyed on the indices, never on the `caret_pos` / `selection_rects` this
    # is on its way to producing — those hold the previous frame's answer
    # until `draw` writes the new one, so a key over them lags by a frame.
    span = current.selection
    key = (
        current.prompt,
        current.editing,
        current.caret,
        span,
        tuple(message.key for message in current.messages),
        current.attachments,
        current.ask.key if current.ask is not None else None,
        current.thread_select,
        round(current.height, 1),
    )
    if _chat.metrics_cache is not None and _chat.metrics_cache[0] == key:
        return _chat.metrics_cache[1]

    book = measure()
    fonts = book.fonts
    font = hfui.tokens.BODY
    limit = _field_limit()
    showing = hfui.controls.prompt_text(current.prompt, chat_module.PLACEHOLDER)
    _lines, _width, text_height = book.text(showing, font, limit, 0, False)
    content = text_height + 2 * hfui.controls.PROMPT_PAD_Y
    viewport = min(content, chat_module.MAX_PROMPT_HEIGHT)

    caret_pos = None
    rects = ()
    if current.editing:
        caret_x, caret_y = field_module.caret_xy(
            current.prompt, _caret_index(), font, limit, fonts
        )
        _ascent, _descent, line_height = fonts.metrics(font)
        caret_pos = (caret_x, caret_y, line_height)
        if span:
            rects = field_module.selection_rects(
                current.prompt, span[0], span[1], font, limit, fonts
            )

    thread_rects = _thread_select_rects(current, fonts)

    # Solved rather than summed: the attachment tray wraps, so a fifth file
    # adds a row, and the thread has to give up exactly that much — the panel
    # is capped and cannot grow to absorb it.
    if chat_module.ask_hides_prompt(current.ask):
        input_height = (
            skui.solve(
                chat_module.ask_card(current.ask),
                book,
                width=chat_module.inner_width(),
            ).height
        )
    else:
        input_height = skui.solve(
            chat_module.input_block(current, None, viewport),
            book,
            width=chat_module.inner_width(),
        ).height
        if current.ask is not None:
            input_height += (
                skui.solve(
                    chat_module.ask_card(current.ask),
                    book,
                    width=chat_module.inner_width(),
                ).height
                + hfui.tokens.LG
            )
    thread_height = chat_module.thread_height(current, input_height)
    thread_content = skui.solve(
        chat_module.thread_body(current, chat_module.inner_width()), book
    ).height
    result = (
        viewport,
        content,
        thread_height,
        thread_content,
        caret_pos,
        rects,
        thread_rects,
    )
    _chat.metrics_cache = (key, result)
    return result


def _clamp_thread():
    if _chat.thread.follow:
        _chat.thread.to_end()
    return _chat.thread.clamp()


def _clamp_prompt(caret_y=None, line_height=0.0):
    """Keep the caret in the prompt's viewport, but only when it moved.

    The chase has to be an edge, not a level. Run on every frame it pinned the
    scroll to wherever the caret is — which is the end of the text — so a wheel
    over an overflowing prompt moved it and the very next draw put it back, and
    the field read as unscrollable. The key is the caret the viewport was last
    aimed at; the text length rides along because a forward delete moves the
    caret's *position* without moving its index.
    """

    if _chat.prompt.overflow <= 0.0:
        return _chat.prompt.reset()
    if caret_y is None:
        return _chat.prompt.clamp()
    view = chat_module.MAX_PROMPT_HEIGHT - 2 * hfui.controls.PROMPT_PAD_Y
    chased = (_caret_index(), len(_chat.text)) if _editing() else None
    return _chat.prompt.reveal(caret_y, caret_y + line_height, view, key=chased)


# ---------------------------------------------------------------------------
# Drawing
# ---------------------------------------------------------------------------


def _geometry(hb, region, area, scale):
    """Bottom-right corner of the usable middle, clear of the N-panel."""
    _, _left_off, right_off = hb._bar_region_margins(area)
    right = region.width - right_off - RIGHT * scale
    return right, BOTTOM * scale


def draw(hb):
    """Draw handler body. Publishes ``rect`` and ``controls`` for the modal."""

    import bpy

    _chat.host = hb
    _pump.revive()
    chat = hb.chat_state
    if not visible(hb):
        chat["rect"] = (0, 0, 0, 0)
        chat["controls"] = []
        _chat.panel_rect = _chat.bubble_rect = None
        _chat.msg_rects = {}
        return
    region = bpy.context.region
    if region is None:
        return

    scale = ui_scale()
    cap = max(
        chat_module.MIN_HEIGHT,
        min(
            chat_module.MAX_HEIGHT,
            region.height / max(scale, 0.001) - 2 * BOTTOM,
            region.height * MAX_SHARE / max(scale, 0.001),
        ),
    )
    started = perf.now()
    current = collect(hb, cap)
    perf.mark("collect", started)

    _chat.motion.tick()
    _chat.motion.to(
        "show",
        1.0 if current.open else 0.0,
        chat_module.SHOW_DURATION if current.open else chat_module.HIDE_DURATION,
        hfui.motion.ease_out,
    )
    shown = _chat.motion.value("show", 1.0 if current.open else 0.0)
    owed = _tooltip_due_in() == 0.0
    _chat.motion.to("tip", 1.0 if owed else 0.0, chat_module.TOOLTIP_FADE)
    tip = _chat.motion.value("tip", 1.0 if owed else 0.0)

    right, bottom = _geometry(hb, region, bpy.context.area, scale)
    bubble_size = chat_module.BUBBLE * scale
    _chat.bubble_rect = (right - bubble_size, bottom, bubble_size, bubble_size)

    if current.away and shown <= 0.001:
        # The explorer has the viewport. Stand down entirely, the way the
        # island does — two surfaces reporting at once is one too many.
        chat["rect"] = (0, 0, 0, 0)
        chat["controls"] = []
        _chat.panel_rect = None
        _chat.bubble_rect = None
        _chat.msg_rects = {}
        return

    started = perf.now()
    (
        prompt_view,
        prompt_content,
        thread_height,
        thread_content,
        caret_pos,
        rects,
        thread_rects,
    ) = _metrics(current)
    perf.mark("metrics", started)
    _chat.thread.fit(thread_content, thread_height)
    _chat.prompt.fit(prompt_content, prompt_view)
    current.scroll = _clamp_thread()
    current.prompt_scroll = _clamp_prompt(
        caret_pos[1] if caret_pos else None, caret_pos[2] if caret_pos else 0.0
    )
    current.caret_pos = caret_pos
    current.selection_rects = rects
    current.thread_select_rects = thread_rects

    panel_width = chat_module.WIDTH * scale
    panel_height = cap * scale
    panel_x = right - panel_width
    panel_y = bottom
    _chat.panel_rect = (panel_x, panel_y, panel_width, panel_height)

    if shown > 0.001:
        _draw_panel(hb, current, scale, prompt_view, thread_height, shown)
        # An empty panel sizes to its own header and prompt rather than to the
        # cap, so what the modal tests the pointer against is the *solved*
        # height, not the room the host allowed. Corrected after the solve;
        # `_draw_panel` reads only the corner out of this.
        frame = _chat.frame_cache[1] if _chat.frame_cache else None
        if frame is not None:
            _chat.panel_rect = (panel_x, panel_y, panel_width, frame.height * scale)
    if shown < 0.999:
        _draw_bubble(scale, shown)
        if tip > 0.001:
            _draw_tooltip(scale, tip * (1.0 - shown))

    _publish(hb, current, scale, shown, thread_height)
    if (
        _chat.motion.running
        or current.editing
        or _tooltip_due_in() is not None
        or _mark_step() is not None
        or _ellipsis_step() is not None
        or current.busy
    ):
        _ensure_pulse()


def _draw_panel(hb, current, scale, prompt_view, thread_height, shown):

    tree_key = (
        current.key,
        _chat.pointer.key,
        round(prompt_view, 1),
        round(thread_height, 1),
        round(scale, 3),
    )
    if _chat.frame_cache is not None and _chat.frame_cache[0] == tree_key:
        frame = _chat.frame_cache[1]
        perf.count("tree_hits")
    else:
        started = perf.now()
        frame = skui.solve(
            chat_module.build(
                current,
                _chat.pointer,
                prompt_height=prompt_view,
                thread=thread_height,
            ),
            measure(),
        )
        perf.mark("solve", started)
        _chat.frame_cache = (tree_key, frame)

    # Allocated once at the panel's largest shape: growing the surface with
    # the panel would reallocate the Skia surface, the float32 scratch and the
    # texture on every frame the region is resized.
    box_width = (chat_module.WIDTH + 2 * MARGIN) * scale
    box_height = (chat_module.MAX_HEIGHT + 2 * MARGIN) * scale
    # Hung from the bottom of the box: the panel grows upward out of the
    # bubble, so its bottom edge is the one that must not move.
    top = MARGIN + (chat_module.MAX_HEIGHT - frame.height)

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(MARGIN, top)
        skui.paint(canvas, frame, measure(), _chat.pointer.state)

    # `tree_key`, not a second tuple: see the note in `launcher_surface`. The
    # panel's height is `frame.height`, which is a function of the tree, so
    # the key it was keyed on before was this one reached through two
    # derivations.
    _layer.ensure(box_width, box_height, (tree_key,), render)
    panel_x, panel_y, _w, _h = _chat.panel_rect
    # The entrance is a translate and an alpha over the cached texture: the
    # panel comes up out of the bubble rather than cutting in on one frame.
    rise = (1.0 - shown) * chat_module.RISE * scale
    _layer.blit(panel_x - MARGIN * scale, panel_y - MARGIN * scale - rise, shown)


def _mark_step():
    """Which frame of the breath is owed, or `None` while the disc is hidden.

    The quantising and why it pays for itself is `motion.Loop`'s to explain;
    the only thing decided here is whether the disc is on screen at all.
    """
    if _chat.state is not None and _chat.state.open:
        return None
    return chat_module.BREATH.step()


def _draw_bubble(scale, shown):
    step = _mark_step()
    key = (round(scale, 3), _chat.pointer.key, step)
    frame = _chat.bubble_cache.get(key)
    if frame is None:
        # Marked rather than left in `other`: this is the one phase the loop's
        # first period pays for, and it should be visible that it stops.
        started = perf.now()
        scales = (
            None if step is None else chat_module.pulse(chat_module.BREATH.at(step))
        )
        frame = skui.solve(chat_module.bubble(scales), measure())
        _chat.bubble_cache[key] = frame
        perf.mark("solve", started)
    else:
        perf.count("tree_hits")
    box = (chat_module.BUBBLE + 2 * MARGIN) * scale

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(MARGIN, MARGIN)
        skui.paint(canvas, frame, measure(), _chat.pointer.state)

    _bubble_layer.ensure(box, box, key, render)
    x, y, _w, _h = _chat.bubble_rect
    _bubble_layer.blit(x - MARGIN * scale, y - MARGIN * scale, 1.0 - shown)


def _draw_tooltip(scale, alpha):
    """The label, above the disc and flush with its right edge.

    Its own layer rather than a sibling in the bubble's tree: the bubble is
    rastered at a fixed box around a circle, and a label four times its width
    would mean allocating that whole box for a surface that is empty most of
    the time.
    """

    key = round(scale, 3)
    if _chat.tooltip_cache is None or _chat.tooltip_cache[0] != key:
        _chat.tooltip_cache = (key, skui.solve(chat_module.tooltip(), measure()))
    frame = _chat.tooltip_cache[1]

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(MARGIN, MARGIN)
        skui.paint(canvas, frame, measure(), _chat.pointer.state)

    # The tail hangs below the box and out of the layout, the way a shadow
    # does, so the layer has to be told about it — `frame.height` is the label
    # alone.
    drawn = frame.height + chat_module.TOOLTIP_TAIL_H
    width = (frame.width + 2 * MARGIN) * scale
    height = (drawn + 2 * MARGIN) * scale
    _tooltip_layer.ensure(width, height, key, render)
    x, y, bubble_width, bubble_height = _chat.bubble_rect
    # Right edges flush, not centres: centred would hang the label off the
    # viewport, since the disc sits a margin from the edge and the label is
    # much wider than it. `MARGIN` is the layer's own shadow gutter, so both
    # coordinates back out of it to land the *content* where it belongs.
    _tooltip_layer.blit(
        x + bubble_width - width + MARGIN * scale,
        y + bubble_height + chat_module.TOOLTIP_GAP * scale - MARGIN * scale,
        alpha,
    )


def _publish(hb, current, scale, shown, thread_height):
    """Hit rects, in region space. One surface answers at a time.

    Published at the *settled* position rather than following the entrance:
    the move is 0.18 s and a target that arrives before the pixels do is less
    surprising than one that slides out from under the pointer.
    """

    chat = hb.chat_state
    started = perf.now()
    if shown <= 0.5:
        x, y, width, height = _chat.bubble_rect
        chat["rect"] = _chat.bubble_rect
        chat["controls"] = [(chat_module.BUBBLE_ID, None, x, y, width, height)]
        _chat.prompt_rect = None
        _chat.thread_rect = None
        _chat.msg_rects = {}
        perf.mark("publish", started)
        return

    panel_x, panel_y, _pw, panel_height = _chat.panel_rect
    frame = _chat.frame_cache[1] if _chat.frame_cache else None
    if frame is None:
        perf.mark("publish", started)
        return

    def to_region(rect):
        x, y, width, height = rect
        return (
            panel_x + x * scale,
            panel_y + (frame.height - y - height) * scale,
            width * scale,
            height * scale,
        )

    published = []
    msg_rects = {}
    prefix = chat_module.MSG + ":"
    for placed in frame.items:
        if placed.node.id is None:
            continue
        visible_rect = placed.visible
        if visible_rect[2] > 0.5 and visible_rect[3] > 0.5:
            published.append((placed.node.id, None, *to_region(visible_rect)))
        if placed.node.id.startswith(prefix):
            msg_rects[placed.node.id] = to_region(placed.rect)
    chat["rect"] = _chat.panel_rect
    chat["controls"] = published
    _chat.msg_rects = msg_rects
    prompt = frame.index.get(chat_module.PROMPT)
    _chat.prompt_rect = to_region(prompt.visible) if prompt else None
    thread = frame.index.get(chat_module.THREAD)
    _chat.thread_rect = to_region(thread.visible) if thread else None
    perf.mark("publish", started)


# ---------------------------------------------------------------------------
# Input
# ---------------------------------------------------------------------------


def _within(rect, mx, my):
    if rect is None:
        return False
    x, y, width, height = rect
    return x <= mx <= x + width and y <= my <= y + height


def pointer_over(mx, my):
    """True when the pointer is on the bubble or on the open panel."""
    if _within(_chat.bubble_rect, mx, my):
        return True
    return bool(state().open and _within(_chat.panel_rect, mx, my))


def blocks_viewport(mx, my):
    return pointer_over(mx, my)


def hit_control(mx, my):
    if _chat.host is None:
        return None
    for node_id, _value, x, y, width, height in reversed(
        _chat.host.chat_state.get("controls") or ()
    ):
        if x <= mx <= x + width and y <= my <= y + height:
            return node_id
    return None


def hover(hb, mx, my):
    """Mouse move: track the hovered control and arm the tooltip.

    It does not open anything — the panel is click-only. What it does own is
    the tooltip clock, which restarts whenever the pointer arrives on the
    resting disc and stops the moment it leaves or the panel opens.
    """

    _chat.host = hb
    current = state()
    inside = pointer_over(mx, my)
    changed = False
    if inside != current.hovered:
        current.hovered = inside
        changed = True
    on_disc = inside and not current.open
    if on_disc and _chat.hover_since is None:
        _chat.hover_since = time.monotonic()
        changed = True
    elif not on_disc and _chat.hover_since is not None:
        _chat.hover_since = None
        changed = True
    node = hit_control(mx, my) if inside else None
    if node != _chat.pointer.hovered:
        _chat.pointer.hovered = node
        changed = True
    if changed:
        _ensure_pulse()
    return changed


def pressed():
    return _chat.pointer.pressed is not None


def press(hb, mx, my, extend=False):
    """A press landed on the surface. True when it was ours."""

    _chat.host = hb
    node = hit_control(mx, my)
    _chat.pointer.pressed = node
    if node is None:
        return pointer_over(mx, my)
    if node == chat_module.PROMPT:
        current = state()
        current.pinned = True
        _clear_thread_select()
        hb.chat_state["prompt_active"] = True
        place_caret(hb, mx, my, extend=extend, drag=True)
        _ensure_pulse()
        return True
    index = chat_module.message_index(node)
    if index is not None:
        current = state()
        current.pinned = True
        _blur(hb)
        _press_thread(index, mx, my, extend=extend)
        _ensure_pulse()
        return True
    _clear_thread_select()
    return True


def release(hb, mx, my):
    """The matching release. Runs the control the press started on."""

    node = _chat.pointer.pressed
    _chat.pointer.pressed = None
    swept = end_drag()
    if node is None:
        return False
    if node == hit_control(mx, my):
        if node == chat_module.PROMPT:
            if not swept:
                place_caret(hb, mx, my)
            return True
        return activate(hb, node)
    return True


def _toggle(hb, current):
    """The disc's click. Disarms the tooltip it was inviting.

    In practice this only ever opens: the panel is anchored to the same corner
    and covers the disc's whole footprint, so the second click lands on the
    panel. Close, Escape and a click outside are the exits. It is written as a
    toggle anyway because the disc is the one control whose meaning should not
    depend on what happens to be drawn over it.
    """

    current.pinned = not current.pinned
    _chat.hover_since = None
    if not current.pinned:
        _blur(hb)
        _clear_thread_select()


def activate(hb, node_id):
    """Route a click by node id. Every id the panel publishes lands here."""
    if node_id is None:
        return False
    current = state()
    if node_id == chat_module.BUBBLE_ID:
        _toggle(hb, current)
        return True
    if node_id == chat_module.CLOSE:
        current.pinned = False
        _blur(hb)
        return True
    if node_id == chat_module.CLEAR:
        clear(hb)
        return True
    if node_id == chat_module.SEND_ID:
        send(hb)
        return True
    if _conversation().handle_ask(node_id):
        if node_id == chat_module.ASK_CUSTOM:
            hb.chat_state["prompt_active"] = True
            current.pinned = True
        return True
    if node_id == chat_module.STOP:
        _conversation().stop(hb)
        return True
    if node_id == chat_module.ATTACH:
        import bpy

        with contextlib.suppress(Exception):
            bpy.ops.scene_agent.chat_attach("INVOKE_DEFAULT")
        return True
    if node_id.startswith("chat:attachment:"):
        parts = node_id.split(":")
        if len(parts) > 3 and parts[3] == "remove":
            _conversation().detach(parts[2])
        return True
    if node_id == chat_module.THREAD or node_id == chat_module.PANEL:
        # Empty panel: the click pins it open, which is all it should mean.
        current.pinned = True
        return True
    return False


def skip_ask():
    """Escape during a questionnaire. True when there was one to skip."""
    return _conversation().skip_ask()


def dismiss(hb):
    """Click outside, or Escape: unpin and blur, but never clear the thread."""

    current = state()
    changed = current.pinned or bool(hb.chat_state.get("prompt_active"))
    current.pinned = False
    _chat.hover_since = None
    _blur(hb)
    _clear_thread_select()
    if changed:
        _ensure_pulse()
    return changed


def _blur(hb):

    hb.chat_state["prompt_active"] = False
    _chat.field.anchor = None
    _chat.field.undo_kind = None


# -- the text field --------------------------------------------------------
#
# The field itself is `hfui.TextField`, shared with the composer's prompt.
# What is here is the storage — `_chat.text` — and the redraw, since a
# keystroke that changes nothing on screen is not worth a frame.


def _write(text):
    _chat.text = text
    _invalidate()
    return text


def _edit(mutate, coalesce=None):
    return _write(_chat.field.apply(_chat.text, mutate, coalesce))


def insert_text(chunk):
    _write(_chat.field.insert(_chat.text, chunk))


def backspace(word=False, line=False):
    _write(_chat.field.backspace(_chat.text, word=word, line=line))


def delete_forward(word=False):
    _write(_chat.field.delete_forward(_chat.text, word=word))


def move_caret(delta=0, to=None, extend=False):
    _chat.field.move(_chat.text, delta=delta, to=to, extend=extend)


def move_to_line_edge(end=False, extend=False):
    _chat.field.move_to_line_edge(_chat.text, end=end, extend=extend)


def move_by_word(forward=False, extend=False):
    _chat.field.move_by_word(_chat.text, forward=forward, extend=extend)


def move_by_line(down=False, extend=False):
    _chat.field.move_by_line(_chat.text, down=down, extend=extend)


def select_all():
    _chat.field.select_all(_chat.text)


def copy_selection():
    return _chat.field.copy(_chat.text)


def cut_selection():
    text, cut_out = _chat.field.cut(_chat.text)
    if cut_out:
        _write(text)
    return cut_out


def paste_clipboard():
    text, pasted = _chat.field.paste(_chat.text)
    if pasted:
        _write(text)
    return pasted


def undo():
    text, moved = _chat.field.step_back(_chat.text)
    return bool(moved and _write(text) is not None)


def redo():
    text, moved = _chat.field.step_forward(_chat.text)
    return bool(moved and _write(text) is not None)


def key_event(hb, event):
    """One key while the chat prompt is focused. True if the field used it.

    The keymap is `hfui.TextField.keys`, the composer's. What is local is
    what Enter means: here it **sends**, because the field is the bottom of a
    conversation rather than a value being edited.

    `keys` hands back the string it was *given* — every edit it made is in
    there, but a submit is not an edit. `send` empties the field from inside
    the call, so writing that string back unconditionally put the sent prompt
    straight back in the box. Only a rejected send leaves the text to keep.
    """
    if chat_module.ask_hides_prompt(state().ask):
        return False
    sent = False

    def submit():
        nonlocal sent
        sent = send(hb)

    text, handled = _chat.field.keys(
        _chat.text, event, submit=submit, cancel=lambda: _blur(hb)
    )
    if not handled:
        return False
    if not sent and event.value in {"PRESS", "REPEAT"} and text != _chat.text:
        _chat.text = text
    _invalidate()
    return True


def _index_at(mx, my):
    """Caret index under a region-space point in the prompt field."""
    if _chat.prompt_rect is None:
        return len(_chat.text)
    x, y, width, height = _chat.prompt_rect
    scale = ui_scale()
    local_x = (mx - x) / max(scale, 0.001) - hfui.controls.PROMPT_PAD_X
    local_y = (y + height - my) / max(scale, 0.001) - hfui.controls.PROMPT_PAD_Y
    local_y += _chat.prompt.offset
    return _chat.field.index_at(_chat.text, local_x, local_y)


def place_caret(hb, mx, my, extend=False, drag=False):
    _chat.field.press(_index_at(mx, my), extend, drag)
    _invalidate()


def drag_caret(hb, mx, my):
    if _chat.thread_dragging:
        return _drag_thread(mx, my)
    if not _chat.field.drag_to(_index_at(mx, my)):
        return False
    _invalidate()
    return True


def end_drag():
    was_thread, _chat.thread_dragging = _chat.thread_dragging, False
    return _chat.field.end_drag() or was_thread


def select_word_at(hb, mx, my):
    node = hit_control(mx, my)
    index = chat_module.message_index(node)
    if index is not None:
        return _select_thread_word(index, mx, my)
    _clear_thread_select()
    _chat.field.select_word(_chat.text, _index_at(mx, my))
    _invalidate()
    return True


def _clear_thread_select():
    if _chat.thread_select is None and not _chat.thread_dragging:
        return False
    _chat.thread_select = None
    _chat.thread_dragging = False
    _invalidate()
    return True


def _thread_span():
    selected = _chat.thread_select
    if selected is None:
        return None
    index, start, end = selected
    start, end = min(start, end), max(start, end)
    if start >= end:
        return None
    return index, start, end


def _thread_message(index):
    messages = _conversation().messages
    if index < 0 or index >= len(messages):
        return None
    return messages[index]


def _index_in_message(index, mx, my):
    message = _thread_message(index)
    if message is None:
        return 0
    text = message.text or ""
    rect = _chat.msg_rects.get(chat_module.message_id(index))
    if rect is None:
        return len(text)
    x, y, _width, height = rect
    scale = ui_scale()
    local_x = (mx - x) / max(scale, 0.001)
    local_y = (y + height - my) / max(scale, 0.001)
    return field_module.caret_index_at(
        text,
        hfui.tokens.BODY,
        chat_module.wrap_width(message.role, chat_module.inner_width()),
        local_x,
        local_y,
        measure().fonts,
    )


def _press_thread(index, mx, my, extend=False):
    caret = _index_in_message(index, mx, my)
    prev = _chat.thread_select
    if extend and prev is not None and prev[0] == index:
        _chat.thread_select = (index, prev[1], caret)
    else:
        _chat.thread_select = (index, caret, caret)
    _chat.thread_dragging = True
    _invalidate()


def _drag_thread(mx, my):
    selected = _chat.thread_select
    if selected is None:
        return False
    index, anchor, _caret = selected
    caret = _index_in_message(index, mx, my)
    if caret == _caret:
        return False
    _chat.thread_select = (index, anchor, caret)
    _invalidate()
    return True


def _select_thread_word(index, mx, my):
    message = _thread_message(index)
    if message is None:
        return False
    start, end = field_module.word_at(
        message.text or "", _index_in_message(index, mx, my)
    )
    _chat.thread_select = (index, start, end)
    _chat.thread_dragging = False
    _invalidate()
    return True


def _select_thread_all(index):
    message = _thread_message(index)
    if message is None:
        return False
    text = message.text or ""
    if not text:
        return False
    _chat.thread_select = (index, 0, len(text))
    _chat.thread_dragging = False
    _invalidate()
    return True


def _thread_selected_text():
    span = _thread_span()
    if span is None:
        return ""
    index, start, end = span
    message = _thread_message(index)
    if message is None:
        return ""
    return (message.text or "")[start:end]


def _copy_thread():
    text = _thread_selected_text()
    if not text:
        return False
    CLIPBOARD.write(text)
    return True


def _paste_into_prompt(hb):
    hb.chat_state["prompt_active"] = True
    state().pinned = True
    _clear_thread_select()
    return paste_clipboard()


def clipboard_event(hb, event, mx, my):
    """Cmd/Ctrl C/V/A/X while the panel is open, focused or not.

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
    if kind == "C":
        if _editing() and selection():
            return copy_selection()
        return _copy_thread()
    if kind == "V":
        if pointer_over(mx, my) or _editing():
            return _paste_into_prompt(hb)
        return False
    if kind == "A":
        node = hit_control(mx, my) if pointer_over(mx, my) else None
        index = chat_module.message_index(node)
        if index is not None:
            _blur(hb)
            return _select_thread_all(index)
        if node == chat_module.PROMPT or _editing():
            hb.chat_state["prompt_active"] = True
            _clear_thread_select()
            select_all()
            _invalidate()
            return True
        if _thread_span() is not None:
            return _select_thread_all(_chat.thread_select[0])
        return False
    if kind == "X":
        if not _editing():
            return False
        return cut_selection()
    return False


# -- scrolling -------------------------------------------------------------


def scroll_thread_by(delta):
    moved = _chat.thread.by(delta)
    # Back at the bottom: resume following the stream. `by` clears `follow`
    # for everyone, and the thread is the one viewport where arriving at the
    # end is itself an instruction.
    _chat.thread.follow = _chat.thread.at_end()
    return moved


def scroll_prompt_by(delta):
    return _chat.prompt.by(delta)


def scroll_event(hb, event, mx, my):
    """A wheel or pan over the panel. Always ours while the panel is open.

    Swallowed even when nothing scrolls: a gesture over a panel that zoomed
    the scene behind it is the one thing every other surface here refuses.
    """

    if event.type != "TRACKPADPAN" and event.type not in cs._WHEEL:
        return False
    if not state().open or not _within(_chat.panel_rect, mx, my):
        return False
    scale = ui_scale()
    # A notch has to be sized to the thing it moves. The thread is most of the
    # panel and the prompt is two visible lines with a couple more behind them,
    # so the thread's step here scrolled the prompt end to end in one click.
    on_prompt = _within(_chat.prompt_rect, mx, my)
    target = scroll_prompt_by if on_prompt else scroll_thread_by
    step = PROMPT_WHEEL_STEP if on_prompt else THREAD_WHEEL_STEP
    if event.type == "TRACKPADPAN":
        dy = float(getattr(event, "mouse_y", 0) - getattr(event, "mouse_prev_y", 0))
        _chat.pan_accum += dy / max(scale, 0.001)
        if abs(_chat.pan_accum) < 2.0:
            return True
        delta, _chat.pan_accum = _chat.pan_accum, 0.0
        target(delta)
        return True
    target(step if event.type in cs._WHEEL_DOWN else -step)
    return True


# ---------------------------------------------------------------------------


def release_resources():
    """Drop GPU and Skia resources; called from unregister().

    The three layers are released and everything else is rebuilt. The frame,
    bubble and tooltip caches all hold `Placed` nodes measured by the font
    book, and the `show` track has to land back at zero or the panel appears
    rather than rising — all four are the object's own business now, so none of
    them can be forgotten here.

    The conversation is not rebuilt: it outlives this surface. It only loses
    its listener, which is a closure over a module the next hot reload purges,
    and whatever turn was in flight loses its destination.
    """
    global _chat

    # A park is a fact about a draw that raised, and this rebuilds the thing
    # that raised. The pump outlives the surface object — it holds a timer
    # registration — so it has to be told, where before the flag simply went
    # with the object.
    _pump.revive()
    _layer.release()
    _bubble_layer.release()
    _tooltip_layer.release()
    chat = conversation.live()
    chat.abandon()
    chat.on_change = None
    _chat = _chat.renewed()
