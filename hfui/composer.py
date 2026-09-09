"""The prompt composer: one surface, two shapes, animated between them.

Collapsed it is a pill — placeholder text and a round submit. Expanded it is the
full card: mode tabs above, prompt, option chips and the Generate button.
Hovering opens it; touching anything inside pins it open; clicking outside lets
it fall shut. All of that is three booleans on ``ComposerState`` and one
animated value.

The tree is rebuilt every frame from state, so nothing here is stateful except
the state object itself, and both shapes can be rendered to a PNG for review.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .. import media
from ..skui import (
    BETWEEN,
    CENTER,
    END,
    START,
    STRETCH,
    Border,
    Column,
    Gradient,
    Image,
    Override,
    Row,
    Stack,
    Style,
    Text,
    View,
    edges,
    replace,
)
from . import chat, controls, markdown, motion as motion_module, tokens

# Expanded metrics, in device pixels at 1×. Larger than the web design: a
# viewport overlay is read at arm's length — but only by as much as it can
# afford, since a surface is rastered at the display's own scale and blitted
# texel for pixel.
WIDTH = 720.0
# Below this the chips wrap into an unusable stack; narrower viewports get a
# clipped composer rather than a broken one.
MIN_WIDTH = 336.0
# Hard ceiling on the composer proper. The prompt field stops growing earlier
# (``MAX_PROMPT_HEIGHT``) and scrolls the rest. The reference tray is added on
# top of this, so the drawn card can be taller.
MAX_HEIGHT = 224.0
# Three prompt rows, including the field's vertical padding. Prompt rows are
# taller than ordinary body rows because they can hold reference chips; past
# this height the field becomes a viewport so the card stays compact.
MAX_PROMPT_HEIGHT = 68.0
# Where a *dragged* prompt may stop: one body line plus the field's padding.
# The automatic size is a compromise between a card that stays out of the way
# and a prompt long enough to read back, and the grip is how the user says
# which of the two they wanted.
MIN_PROMPT_HEIGHT = 24.0
# The tabs whose prompts get that grip. Image and video are where a prompt runs
# to paragraphs; 3D and Motion take a sentence, and Scene Builder's card
# already carries a grip for its thread.
RESIZABLE_PROMPT_MODES = ("image", "video")
TOOLBAR_HEIGHT = 28.0
# The Generate button is a fixed height in the design (Figma 13810:67346),
# bottom-aligned against the chip strip rather than spanning the panel. Letting
# it stretch turned a long prompt into a slab of brand green twice the height of
# everything it sits next to.
GENERATE_HEIGHT = 80.0
# How far the chip strip dissolves into the panel at an edge it can scroll
# past. Wide enough to read as "there is more this way", short enough that it
# never swallows a whole chip.
CHIP_FADE = 26.0
# Horizontal mode bar above the prompt (Figma 4146:7944). A mode tab is 26px
# high; the shell adds 2px on both sides. The old 28px slot clipped away the
# shell's bottom padding and border.
MODES_HEIGHT = 30.0
MODES_GAP = 4.0
# Scene Builder answers occupy the attachment slot after a request is sent.
# The slot never grows with the thread; its contents scroll instead — but the
# user can drag the grip above the card to give the conversation more room.
# ``RESPONSES_HEIGHT`` is where a fresh thread starts, and the floor keeps a
# drag from collapsing the thread into a slit that reads as broken.
RESPONSES_HEIGHT = 120.0
RESPONSES_MIN = 60.0
# The drag strip across the card's top edge. Tall enough to hit without
# aiming, short enough that it reads as an edge rather than a toolbar.
RESIZE_GRIP = 10.0
RESIZE_ID = "scene-resize"
# The same strip, above the prompt instead of above the thread. Two grips never
# appear together: Scene Builder has the thread and no prompt grip, the
# generation tabs the reverse.
PROMPT_RESIZE_ID = "prompt-resize"
RESIZE_IDS = (RESIZE_ID, PROMPT_RESIZE_ID)
# A generation the thread produced, shown under its answer — the same tile the
# Supercomputer panel uses, at the same size.
RESULT_TILE = 86.0
# A backend reply is normally tens of lines and folded tool rows.  These are
# deliberately far beyond normal use and exist only so one malformed response
# cannot defeat message-level virtualization by being a whole transcript on
# its own.  The host still owns and persists the complete Response.
RESPONSE_TEXT_LINES = 300
RESPONSE_TEXT_CHARS = 30_000
RESPONSE_TOOLS = 400
# What Skia will actually decode as a thumbnail. Anything else keeps its path
# for the kind glyph — video, PDF — so a sent attachment is never a blank tile.
_THUMB_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"})
_KIND_ICON = {
    "video": "video",
    "audio": "settings/audio",
    "file": "settings/file",
}

SCENE_HEADER_HEIGHT = 36.0
SCENE_HEADER_GAP = 4.0
SHELL_PAD = 4.0
PANEL_PAD = 12.0
PANEL_GAP = 12.0
PROMPT_TOOLBAR_GAP = 4.0

# Collapsed metrics.
PILL_WIDTH = 288.0
PILL_HEIGHT = 45.0
LOGIN_PILL_WIDTH = 180.0

SCENE_BUILDER = "scene_builder"
CAMERA = "phone_camera"
ASSETS = "assets"
# Nested under the 3D tab rather than listed in ``MODES``: this transforms the
# selected mesh, while the 3D catalog modes create a new object.
RETEXTURE = "retexture"

# Scene Builder is the whole composer now. The other six tabs — 3D, character
# animation, image, video, phone camera and the asset browser — were the
# upstream account's generation catalogs, and every one of them needed a
# token to do anything at all. Their cards are still drawn by code below;
# nothing selects them.
MODES = ((SCENE_BUILDER, "scene-builder", "Scene builder"),)

# The QR tile on the Camera card: the Generate button's height, so the camera
# card comes out exactly as tall as every other mode's. Small for a QR, but the
# URL is short (a version-2 code) and the tile rasters at the display scale —
# it scans at arm's length, which is where the phone already is.
QR_SIZE = GENERATE_HEIGHT
QR_PAD = 6.0
THUMB_QR_RADIUS = 12.0

OPEN_DURATION = 0.22

# Chip shapes the toolbar knows how to draw.
CHIP = "chip"  # tap to act, or to open the chip's menu
TOGGLE = "toggle"  # on/off, a brighter neutral fill when on
SLIDER = "slider"  # a bounded number, dragged
STATUS = "status"  # read-only: a cost, a duration, a running job


@dataclass(frozen=True)
class Reference:
    """One attached file, as the tray needs to draw it.

    ``source`` is a decodable image path or None; the host decides which,
    because it is the side that knows a `.mp4` from a `.png`.
    """

    id: str
    source: str | None = None
    icon: str = "image-sparkle"


@dataclass(frozen=True)
class Response:
    """One Scene Builder message shown above the composer.

    ``images`` are the generations an assistant turn produced, as decodable
    paths — they land minutes after the text, so they hang under the answer
    rather than being woven into it. ``attachments`` are the files a user turn
    sent, shown inside the bubble the way the Supercomputer panel shows them.
    ``tools`` are the same one-line calls the compact chat renders.
    """

    text: str
    role: str = "assistant"
    failed: bool = False
    images: tuple = ()
    attachments: tuple = ()
    tools: tuple = ()
    identity: object | None = None

    @property
    def key(self):
        """Immutable cache key, including mutable live tool rows."""
        return (
            self.text,
            self.role,
            self.failed,
            self.images,
            self.attachments,
            tuple(tool.key for tool in self.tools),
        )

    @property
    def virtual_id(self):
        """Stable identity for geometry caches, separate from content revision."""
        return self.identity if self.identity is not None else self.key


@dataclass(frozen=True)
class ResponseUnit:
    """One independently virtualized piece of a Scene Builder message.

    Assistant tool rows are units of their own so a single response containing
    thousands of actions cannot defeat message-level virtualization. Text,
    media and user bubbles remain semantic units and keep their transcript-wide
    message index for selection and copying.
    """

    message: int
    response: Response
    kind: str
    ordinal: int = 0

    @property
    def key(self):
        response = self.response
        if self.kind == "tool":
            tool = response.tools[self.ordinal]
            identity = tool.call_id if tool.call_id is not None else self.ordinal
            content = tool.key
        elif self.kind == "text":
            content = (response_text(response), response.failed)
            identity = "text"
        elif self.kind == "media":
            content = response.images
            identity = "media"
        elif self.kind == "omission":
            content = (len(response.text or ""), len(response_text(response)))
            identity = "omission"
        else:
            content = response.key
            identity = self.kind
        return (response.virtual_id, self.kind, identity, content)


def response_units(responses):
    """Flatten responses into visual units without changing conversation data."""
    units = []
    for index, response in enumerate(responses):
        if response.role == "user":
            units.append(ResponseUnit(index, response, "user"))
            continue
        units.extend(
            ResponseUnit(index, response, "tool", ordinal)
            for ordinal in range(len(response.tools))
        )
        shown = response_text(response)
        if shown:
            units.append(ResponseUnit(index, response, "text"))
        if response.images:
            units.append(ResponseUnit(index, response, "media"))
        if len(shown) < len(response.text or ""):
            units.append(ResponseUnit(index, response, "omission"))
    return tuple(units)


@dataclass(frozen=True)
class ResponseWindow:
    """The Scene Builder rows laid out this frame, with virtual spacers.

    ``items`` retain their indices in the complete visual-unit list, and each
    ``ResponseUnit`` retains its complete-transcript message index. That keeps
    hit ids, selection and copy stable while rows outside the viewport do not
    exist in the Skia tree.
    """

    items: tuple
    before: float = 0.0
    after: float = 0.0
    total: float = 0.0

    @property
    def key(self):
        return (
            tuple(
                (index, unit.key, round(float(height), 1))
                for index, unit, height in self.items
            ),
            round(float(self.before), 1),
            round(float(self.after), 1),
            round(float(self.total), 1),
        )


@dataclass(frozen=True)
class CameraPanel:
    """The Phone Camera card, as the host reads it off the feature.

    Frozen for the same reason ``Chip`` is: the tree key hashes it, and a
    telemetry field that changes identity every draw would re-raster the card
    at the phone's send rate. The host quantises before it builds one.
    """

    running: bool = False
    url: str = ""
    qr: str | None = None  # path to the QR PNG, drawn as an Image
    https: bool = True
    connected: bool = False
    recording: bool = False
    # One already-quantised line each, so the key only misses when the text
    # the user can actually read changes.
    pose: str = ""
    lens: str = ""
    hint: str = ""


@dataclass(frozen=True)
class BridgeGate:
    """Scene Builder is locked until there is a CLI on this machine to run.

    This used to be the hosted connector's gate — install it in the workspace,
    sign in on a web page, wait for it to go active. What it asks now is local
    and answerable in one look: whether the agent's executable is on disk.
    """

    status: str = "missing"
    error: str = ""

    @property
    def title(self):
        if self.status == "error":
            return "The agent CLI could not run"
        return "Scene Builder needs a coding CLI"

    @property
    def detail(self):
        if self.error:
            return self.error
        return "Install Claude Code, or point the add-on at it in Preferences."

    @property
    def action(self):
        return "Check again"

    @property
    def enabled(self):
        return True


@dataclass(frozen=True)
class Chip:
    """One toolbar entry, described by the host rather than built by it.

    The host knows what a catalog parameter is; the toolbar knows what a chip
    looks like. This is the whole vocabulary between them, and it is frozen so
    a state key can hash it.
    """

    id: str
    label: str = ""
    icon: str | None = None
    kind: str = CHIP
    active: bool = False
    trailing: str | None = None
    # SLIDER only: the value to print at the right end, and where the fill
    # stops as a 0..1 position within the parameter's schema bounds.
    value: str = ""
    fraction: float = 0.0


class ComposerState:
    """What the composer shows, and whether it is open.

    ``pinned`` is the "an interaction happened" latch: hover alone opens the
    composer, but once something inside is clicked it stays open until a click
    lands outside.
    """

    def __init__(
        self,
        mode="3d",
        prompt="",
        chips=(),
        context=(),
        references=(),
        responses=(),
        credits=None,
        enabled=True,
    ):
        self.mode = mode
        self.prompt = prompt
        self.chips = tuple(chips)
        # Host context attached to the prompt itself, such as the selected
        # Blender object. These tags belong inside the text box, not among the
        # model controls in the toolbar below it.
        self.context = tuple(context)
        # The prompt as laid out around the atomic context token. The real
        # prompt never contains the token or its reserved spacing.
        self.display_prompt = prompt
        self.plain_prompt = prompt
        self.mention_projection = None
        self.element_positions = ()
        self.context_index = None
        self.context_reserve = ""
        self.context_pos = None
        self.context_display_span = None
        # Attached files, drawn as thumbnails above the panel rather than named
        # in the chip strip.
        self.references = tuple(references)
        # Scene Builder's own replies. Attachments take this same slot while
        # they are staged; after send they clear and the answers appear.
        self.responses = tuple(responses)
        # None keeps standalone snapshots and older callers on the complete
        # body.  The Blender host supplies a window for a live Scene Builder
        # thread after it knows the viewport width and scroll offset.
        self.response_window = None
        # A heavy transcript is moving under wheel/trackpad input. The host
        # keeps the full scroll geometry but defers expensive message paint
        # until the gesture settles.
        self.response_scrolling = False
        self.response_scroll = 0.0
        # How tall the thread's viewport is, host-owned like every scroll:
        # the grip drag writes it and the host clamps it to the region.
        self.responses_height = RESPONSES_HEIGHT
        self.scene_chat = False
        self.scene_title = "New chat"
        # Live-turn clock, drawn under the thread — "Working for 9m 18s".
        # Loading a remote chat still uses this slot in the header, because
        # there is no thread yet to pin a clock under.
        self.scene_status = ""
        # What the turn is doing right now — "Thinking", "Generating image" —
        # for the working line's trailing verb and the collapsed pill.
        self.scene_activity = ""
        # The Supercomputer is answering this thread. Drives the Stop button
        # in Generate's slot; ``scene_status`` is the clock under the thread.
        self.scene_busy = False
        # Quantised waiting dots for running tool rows.
        self.scene_ellipsis = 0
        # Active account workspace, shown as a compact select in the header.
        self.scene_workspace = None
        # The Phone Camera card, or None for every other mode. Set per draw by
        # the host, like everything else on this object.
        self.camera = None
        self.bridge = None
        # Never true any more: there is no account to be signed out of. The
        # field stays because it is part of the state tuple the surface
        # compares to decide whether a redraw is needed.
        self.login_required = False
        # The settled cost estimate, shown on the Generate button. Nothing else
        # is reported on the card: a strip that narrated what it was waiting for
        # spent its width on lines the disabled button already implied.
        self.credits = credits
        self.enabled = enabled
        self.submit_label = "Generate"
        self.submit_icon = "sparkle"
        # An explorer tile (or an OS file) is held over the prompt. Lights the
        # card so letting go is visibly an attach, not a scene drop.
        self.drop_target = False
        # A parked Supercomputer questionnaire, same card the chat draws.
        self.ask = None
        # Read-only selection in the Scene Builder thread:
        # ``(message index, start, end)``.
        self.thread_select = None
        self.thread_select_rects = ()
        self.hovering = False
        self.pinned = False
        self.editing = False
        # Prompt-field scroll, in logical pixels. Host-owned; clamped each draw.
        self.scroll = 0.0
        # How tall the prompt's own viewport is, in authored pixels, once the
        # user has dragged its grip. ``0`` means "however tall the text needs",
        # which is what every mode did before the grip existed. Host-owned like
        # the thread's height, and clamped to the region the same way.
        self.prompt_height = 0.0
        # Whether this mode offers that grip at all — see
        # ``RESIZABLE_PROMPT_MODES``.
        self.prompt_resize = False
        # The same for the chip strip, sideways. ``chip_overflow`` is how far
        # it can travel — the strip needs it to know which edges to fade.
        self.chip_scroll = 0.0
        self.chip_overflow = 0.0
        # Caret index into ``prompt`` and whether the blink is in the on phase.
        self.caret = None
        self.caret_on = True
        # Where that index actually sits: ``(x, y, height)`` in the text's own
        # space. Only the host can work it out — measuring is its job.
        self.caret_pos = None
        # Selected run as ``(start, end)`` indices, and the rectangles that
        # cover it — one per visual line, measured host-side like the caret.
        self.selection = None
        self.selection_rects = ()
        self.placeholder = "Describe the scene you imagine..."
        # Open select popup: the chip id it belongs to, plus its options.
        self.menu = None
        self.menu_items = ()
        self.menu_value = None
        self.menu_title = None
        # How tall that popup may get, and how far its rows are scrolled inside
        # it. Both host-owned, like the prompt's and the chip strip's — the cap
        # depends on the room above the chip, which only the host measures.
        self.menu_limit = None
        self.menu_scroll = 0.0
        # Motion's timeline, as ``(id, label, text, seconds)`` per shot, newest
        # last. Empty for every other mode, and an empty tuple is what keeps
        # `_expanded` on its original single-prompt path.
        self.shots = ()
        self.shot_active = 0

    @property
    def open(self):
        if self.login_required:
            return False
        return bool(self.pinned or self.hovering)

    def show_menu(self, anchor, items, value=None, title=None):
        """``items`` is ``(label, icon)``; ``value`` is the selected index."""
        self.menu = anchor
        self.menu_items = tuple(items)
        self.menu_value = value
        self.menu_title = title
        self.pinned = True

    def hide_menu(self):
        changed = self.menu is not None
        self.menu = None
        self.menu_title = None
        return changed

    def hover(self, inside):
        inside = bool(inside)
        changed = inside != self.hovering
        self.hovering = inside
        return changed

    def click(self, node_id):
        """Route a click. ``None`` means the click landed outside."""
        if node_id is None:
            changed = self.pinned
            self.pinned = False
            return changed
        self.pinned = True
        if node_id.startswith("mode:"):
            self.mode = node_id.split(":", 1)[1]
        return True

    @property
    def key(self):
        return (
            self.mode,
            self.prompt,
            self.chips,
            self.context,
            self.display_prompt,
            self.plain_prompt,
            self.mention_projection,
            self.element_positions,
            self.context_index,
            self.context_pos,
            self.context_display_span,
            self.references,
            (
                self.response_window.key
                if self.response_window is not None
                else tuple(response.key for response in self.responses)
            ),
            self.scene_chat,
            self.scene_title,
            self.scene_status,
            self.scene_activity,
            self.scene_busy,
            self.scene_ellipsis,
            self.scene_workspace,
            self.camera,
            self.bridge,
            self.login_required,
            self.credits,
            self.enabled,
            self.submit_label,
            self.submit_icon,
            self.drop_target,
            self.ask.key if self.ask is not None else None,
            self.thread_select,
            self.thread_select_rects,
            self.open,
            self.editing,
            round(float(self.scroll or 0.0), 1),
            round(float(self.prompt_height or 0.0), 1),
            self.prompt_resize,
            round(float(self.chip_scroll or 0.0), 1),
            self.response_scrolling,
            round(float(self.response_scroll or 0.0), 1),
            round(float(self.responses_height or 0.0), 1),
            round(float(self.chip_overflow or 0.0), 1),
            self.caret_pos if self.editing else None,
            bool(self.caret_on) if self.editing else None,
            self.selection_rects if self.editing else (),
        )


def mode_tabs(active):
    """Horizontal mode pills above the prompt card (Figma 4146:7944)."""
    shown_active = "3d" if active == RETEXTURE else active
    return controls.card(
        Row(
            *[
                controls.mode_tab(
                    f"mode:{key}",
                    icon,
                    label,
                    active=key == shown_active,
                )
                for key, icon, label in MODES
            ],
            gap=2.0,
            align=CENTER,
        ),
        radius=tokens.RADIUS_SURFACE,
        pad=2.0,
    )


def _chip(entry, pointer):
    """One toolbar entry, in whichever shape it asked for."""
    if entry.kind == SLIDER:
        return controls.slider(
            entry.id,
            entry.label,
            entry.value,
            entry.fraction,
            height=TOOLBAR_HEIGHT,
            icon=entry.icon,
        )
    if entry.kind == STATUS:
        return controls.chip(
            entry.id,
            entry.label,
            entry.icon,
            height=TOOLBAR_HEIGHT,
            muted=True,
        )
    if entry.kind == TOGGLE:
        return controls.toggle_chip(
            entry.id,
            entry.label,
            entry.icon,
            active=entry.active,
            height=TOOLBAR_HEIGHT,
        )
    if not entry.label:
        return controls.icon_chip(
            entry.id, entry.icon, active=entry.active, size=TOOLBAR_HEIGHT
        )
    return controls.chip(
        entry.id,
        entry.label,
        entry.icon,
        trailing=entry.trailing,
        active=entry.active,
        height=TOOLBAR_HEIGHT,
    )


def chip_strip(state, pointer):
    """The chips in one unbroken row, at whatever width they come to.

    Every chip here is a control the model actually takes. Nothing narrates —
    the strip's width is scarce, and a chip that only reports is a chip the
    user tries to click.
    """
    return Row(
        *[_chip(entry, pointer) for entry in state.chips],
        gap=tokens.SM,
        align=CENTER,
        height=TOOLBAR_HEIGHT,
    )


def _fade(side):
    """One edge of the strip dissolving into the panel underneath it.

    A full-box layer rather than a positioned one: it grows to the viewport and
    justifies the gradient to its own edge, so nothing here needs to know how
    wide the viewport turned out to be.
    """
    stops = (
        ((0.0, tokens.PANEL_CLEAR), (1.0, tokens.PANEL))
        if side == END
        else ((0.0, tokens.PANEL), (1.0, tokens.PANEL_CLEAR))
    )
    return Stack(
        View(width=CHIP_FADE, style=Style(fill=Gradient(stops=stops, angle=0.0))),
        align=STRETCH,
        justify=side,
        grow=1.0,
    )


def _toolbar(state, pointer):
    """The chip strip as a horizontal viewport.

    Chips used to wrap, which grew the card a row at a time — a catalog model
    with a dozen settings walked it up the viewport. One row that scrolls keeps
    the card a fixed size no matter what the schema hands over.
    """
    scroll = max(0.0, float(state.chip_scroll or 0.0))
    overflow = max(0.0, float(state.chip_overflow or 0.0))
    layers = [replace(chip_strip(state, pointer), dx=-scroll)]
    if scroll > 0.5:
        layers.append(_fade(START))
    if scroll < overflow - 0.5:
        layers.append(_fade(END))
    return Stack(
        *layers,
        align=CENTER,
        height=TOOLBAR_HEIGHT,
        grow=1.0,
        style=Style(clip=True),
        id="chips",
    )


def _tray(state, pointer):
    """Attached references, above the panel — the files themselves, not names.

    A thumbnail says what a reference is in a way "portrait_final_v3" never
    will, and it takes the attachments out of the chip strip, which is for
    settings.
    """
    return Row(
        *[
            controls.thumbnail(
                entry.id, entry.source, entry.icon, pointer, controls.THUMB_SIZE
            )
            for entry in state.references
        ],
        gap=tokens.MD,
        align=CENTER,
        # Catalog models take up to sixteen references, which is more than one
        # row holds. Wrapping grows the card; scrolling would hide attachments
        # behind an interaction, and an attachment you cannot see is one you
        # forget you are paying for.
        wrap=True,
        pad=edges(x=tokens.MD, y=tokens.MD),
    )


def message_wrap(role, width):
    """How wide a Scene Builder message wraps, matching `_response_body`."""
    inner = max(1.0, width - 2 * tokens.MD)
    if role != "user":
        return inner
    return max(1.0, inner * 0.86 - 2 * tokens.LG)


def _thread_thumb(source, size):
    """A picture if Skia can decode it, else the media-kind glyph."""
    path = str(source or "")
    suffix = Path(path).suffix.lower() if path else ""
    shown = path if suffix in _THUMB_SUFFIXES else None
    icon = _KIND_ICON.get(media.kind(path) if path else "image", "image-sparkle")
    return controls.thumbnail(None, shown, icon=icon, size=size)


def _thread_tiles(sources, size):
    """Generations or attachments, with no control id to swallow a click."""
    if not sources:
        return None
    return Row(
        *[_thread_thumb(source, size) for source in sources],
        gap=tokens.XS,
        wrap=True,
    )


def response_text(response):
    """The bounded text rendered for one response; always an exact prefix."""
    text = response.text or ""
    if len(text) <= RESPONSE_TEXT_CHARS and text.count("\n") < RESPONSE_TEXT_LINES:
        return text
    return "".join(text.splitlines(keepends=True)[:RESPONSE_TEXT_LINES])[
        :RESPONSE_TEXT_CHARS
    ]


def _response_omission(response, shown_text, shown_tools):
    hidden_chars = max(0, len(response.text or "") - len(shown_text))
    hidden_tools = max(0, len(response.tools) - shown_tools)
    if not hidden_chars and not hidden_tools:
        return None
    parts = []
    if hidden_tools:
        parts.append(f"{hidden_tools:,} more actions")
    if hidden_chars:
        parts.append(f"{hidden_chars:,} more characters")
    return Text(
        "Large response folded · " + " · ".join(parts) + " · full text is kept",
        font=tokens.CAPTION,
        color=tokens.TEXT_SECONDARY,
        lines=1,
        ellipsis=True,
    )


def _response_message(state, response, index, inner):
    """One complete message row, addressed by its transcript-wide index."""
    bubble = inner * 0.86
    ink = tokens.DANGER if response.failed else tokens.TEXT_PRIMARY
    selected = state.thread_select
    highlights = (
        state.thread_select_rects
        if selected is not None and selected[0] == index
        else ()
    )
    node = chat.message_id(index)
    shown_text = response_text(response)
    shown_tools = min(len(response.tools), RESPONSE_TOOLS)
    omitted = _response_omission(response, shown_text, shown_tools)
    if response.role != "user":
        lines = markdown.blocks(shown_text, ink, inner, font=tokens.BODY_MEDIUM)
        text = (
            chat.selectable_text(lines, node, highlights)
            if lines and shown_text
            else None
        )
        return Column(
            *[
                chat._tool_line(tool, state.scene_ellipsis)
                for tool in response.tools[:RESPONSE_TOOLS]
            ],
            text,
            _thread_tiles(response.images, RESULT_TILE),
            omitted,
            gap=tokens.SM,
            align=STRETCH,
        )
    lines = markdown.blocks(
        shown_text,
        ink,
        bubble - 2 * tokens.LG,
        font=tokens.BODY_MEDIUM,
    )
    text = (
        chat.selectable_text(lines, node, highlights)
        if lines and shown_text
        else None
    )
    return Row(
        Column(
            _thread_tiles(response.attachments, controls.THUMB_SIZE),
            text,
            omitted,
            gap=tokens.SM,
            pad=edges(x=tokens.LG, y=tokens.MD),
            align=STRETCH,
            max_width=bubble,
            style=Style(
                fill=tokens.SURFACE,
                radius=tokens.RADIUS_BUTTON,
            ),
        ),
        justify=END,
    )


def _response_unit(state, unit, inner):
    """One visual unit from a complete response, retaining message semantics."""
    response = unit.response
    index = unit.message
    ink = tokens.DANGER if response.failed else tokens.TEXT_PRIMARY
    selected = state.thread_select
    highlights = (
        state.thread_select_rects
        if selected is not None and selected[0] == index
        else ()
    )
    if unit.kind == "tool":
        return chat._tool_line(response.tools[unit.ordinal], state.scene_ellipsis)
    if unit.kind == "text":
        shown = response_text(response)
        lines = markdown.blocks(shown, ink, inner, font=tokens.BODY_MEDIUM)
        return chat.selectable_text(lines, chat.message_id(index), highlights)
    if unit.kind == "media":
        return _thread_tiles(response.images, RESULT_TILE)
    if unit.kind == "omission":
        return _response_omission(response, response_text(response), len(response.tools))
    return _response_message(state, response, index, inner)


def measure_response_unit(state, unit, width, measure):
    """Exact unit height; tool rows are known and never reach this path."""
    from ..skui import solve

    inner = max(1.0, width - 2 * tokens.MD)
    return solve(_response_unit(state, unit, inner), measure, width=inner).height


def _response_seek_unit(unit, height, inner):
    """Same-height, cheap representation used only while scrolling quickly."""
    response = unit.response
    height = max(1.0, float(height))
    if unit.kind == "tool":
        tool = response.tools[unit.ordinal]
        detail = f" · {tool.detail}" if tool.detail else ""
        return Row(
            Text(
                f"{tool.label}{detail}",
                font=tokens.CAPTION,
                color=tokens.TEXT_SECONDARY,
                lines=1,
                ellipsis=True,
                max_width=inner,
            ),
            height=height,
            align=CENTER,
        )

    role = "You" if response.role == "user" else "Agent"
    if unit.kind == "media":
        summary = f"{len(response.images):,} generated media"
    elif unit.kind == "omission":
        summary = "Large response · full text is kept"
    else:
        summary = " ".join((response.text or "").split()) or "Message has no text"
        if len(summary) > 180:
            summary = summary[:179].rstrip() + "…"
    surface = tokens.SURFACE if response.role == "user" else tokens.PANEL
    return Stack(
        View(
            height=height,
            style=Style(fill=surface, radius=tokens.RADIUS_BUTTON),
        ),
        Column(
            Text(
                role,
                font=tokens.BODY_STRONG,
                color=tokens.DANGER if response.failed else tokens.TEXT_PRIMARY,
                lines=1,
            ),
            Text(
                summary,
                font=tokens.CAPTION,
                color=tokens.TEXT_SECONDARY,
                lines=2,
                ellipsis=True,
                max_width=max(1.0, inner - 2 * tokens.MD),
            ),
            gap=tokens.XS,
            pad=edges(x=tokens.MD, y=tokens.SM),
            align=STRETCH,
        ),
        height=height,
        align=STRETCH,
        style=Style(clip=True),
    )


def _response_body(state, width):
    """Scene Builder messages, projected through an optional host window.

    The two spacers preserve the complete thread's scroll geometry while only
    the intersecting rows are described and solved.  Standalone snapshots can
    omit the window and retain the original complete-body behavior.
    """
    inner = max(1.0, width - 2 * tokens.MD)
    clock = (
        chat.working_line(state.scene_status, state.scene_activity)
        if state.scene_status.startswith("Working")
        else None
    )
    window = state.response_window
    if window is None:
        rows = [
            _response_message(state, response, index, inner)
            for index, response in enumerate(state.responses)
        ]
        if clock is not None:
            rows.append(clock)
        return Column(*rows, gap=tokens.SM, align=STRETCH, width=inner)

    rows = []
    if window.before > 0.0:
        rows.append(View(height=window.before))
    for position, (_index, unit, height) in enumerate(window.items):
        rows.append(
            _response_seek_unit(unit, height, inner)
            if state.response_scrolling
            else _response_unit(state, unit, inner)
        )
        if position + 1 < len(window.items):
            rows.append(View(height=tokens.SM))
    if window.after > 0.0:
        rows.append(View(height=window.after))
    if clock is not None:
        rows.extend((View(height=tokens.SM), clock))
    return Column(*rows, gap=0.0, align=STRETCH, width=inner)


def _scene_header(state, pointer):
    # Loading a remote chat still lives here: there is no thread yet. The
    # live "Working for" clock sits under the messages instead, on the
    # edge that is still moving.
    loading = (
        state.scene_status
        if state.scene_status and not state.scene_status.startswith("Working")
        else None
    )
    title = Row(
        Text(
            loading or state.scene_title,
            font=tokens.CAPTION,
            color=tokens.TEXT_SECONDARY if loading else tokens.TEXT_PRIMARY,
            lines=1,
            max_width=240.0,
        ),
        align=CENTER,
        justify=CENTER,
        grow=1.0,
    )
    # The workspace chip named the account's workspace. With no account there
    # is no label, and `None` is how the host says "do not draw it".
    actions = Row(
        controls.icon_chip("scene-history", "settings/clock", size=24.0),
        controls.icon_chip("scene-new-chat", "plus", size=24.0),
        View(grow=1.0),
        *(
            (
                controls.chip(
                    "scene-workspace",
                    state.scene_workspace,
                    "settings/people",
                    trailing="chevron-down",
                    height=24.0,
                ),
            )
            if state.scene_workspace
            else ()
        ),
        gap=4.0,
        align=CENTER,
        grow=1.0,
    )
    return Stack(
        actions,
        title,
        align=CENTER,
        justify=CENTER,
        height=SCENE_HEADER_HEIGHT,
        pad=edges(x=12.0, y=6.0),
    )


def _resize_grip(pointer, node_id=RESIZE_ID):
    """A drag strip along an edge: the thread's top, or the prompt's.

    The whole strip is the control — a 4pt pill alone is a target nobody hits
    with a viewport pointer — and the pill inside it is the affordance, lit
    when the pointer arrives the way the chat header's controls light.
    """
    lit = pointer is not None and pointer.state(node_id) is not None
    return Stack(
        View(
            width=36.0,
            height=4.0,
            style=Style(
                fill=tokens.SELECTED if lit else tokens.SURFACE,
                radius=tokens.RADIUS_ROUND,
            ),
        ),
        align=CENTER,
        justify=CENTER,
        height=RESIZE_GRIP,
        id=node_id,
    )


def _responses(state, width, pointer=None):
    body = _response_body(state, width)
    thread = Stack(
        replace(body, dy=-max(0.0, float(state.response_scroll or 0.0))),
        pad=edges(x=tokens.MD, y=tokens.SM),
        width=width,
        height=state.responses_height if state.responses else 0.0,
        align=STRETCH,
        style=Style(clip=True),
        id="responses",
    )
    return Column(
        _resize_grip(pointer) if state.responses else None,
        _scene_header(state, pointer),
        thread if state.responses else None,
        gap=SCENE_HEADER_GAP,
        width=width,
        align=STRETCH,
    )


def _above_prompt(state, pointer, response_width=None):
    """Thread and/or attachment tray stacked above the prompt.

    Attachments used to replace the Scene Builder history: the tray and the
    thread shared one slot, so staging a file hid the tabs and the replies.
    The tray is what the next send will carry, so it sits under the thread.
    """
    width = response_width or WIDTH
    thread = _responses(state, width, pointer) if state.scene_chat else None
    tray = _tray(state, pointer) if state.references else None
    if thread is None:
        return tray
    if tray is None:
        return thread
    return Column(
        thread, tray, gap=SCENE_HEADER_GAP, width=width, align=STRETCH
    )


def _bridge_panel(state, pointer, response_width=None):
    """Replace the prompt until the Blender MCP connector is active."""
    gate = state.bridge
    body = Column(
        Text(gate.title, font=tokens.BODY_STRONG, color=tokens.TEXT_PRIMARY),
        Text(gate.detail, font=tokens.BODY, color=tokens.TEXT_SECONDARY),
        gap=tokens.XS,
        align=START,
        grow=1.0,
    )
    panel = controls.surface(
        Row(
            body,
            Column(
                _submit_button(state, pointer),
                align=STRETCH,
                justify=END,
            ),
            gap=PANEL_GAP,
            align=STRETCH,
            grow=1.0,
        ),
        direction="row",
        pad=PANEL_PAD,
        align=STRETCH,
        grow=1.0,
    )
    if not state.scene_chat:
        return panel
    extra = _above_prompt(state, pointer, response_width)
    if state.ask is not None:
        extra = Column(
            extra,
            chat.ask_card(state.ask, pointer),
            gap=tokens.SM,
            align=STRETCH,
        )
    return Column(extra, panel, align=STRETCH, grow=1.0)


def _camera_panel(state, pointer):
    """The Phone Camera card: QR beside the connection story, controls below.

    It replaces the prompt/Generate layout wholesale — there is nothing to
    type and nothing to spend, so the card is the pairing surface Magnific
    puts in a popup: scan, see that the phone took, adjust how it drives.
    """
    cam = state.camera
    if not cam.running:
        body = Column(
            Text("Phone Camera", font=tokens.BODY_STRONG, color=tokens.TEXT_PRIMARY),
            Text(
                "Drive the scene camera from your phone: gyro to aim, "
                "walk to move.",
                font=tokens.BODY,
                color=tokens.TEXT_SECONDARY,
            ),
            Row(
                controls.chip(
                    "phonecam:start",
                    "Start camera server",
                    "settings/capture",
                    height=TOOLBAR_HEIGHT,
                ),
                align=CENTER,
            ),
            gap=tokens.XS,
            align=START,
            justify=CENTER,
            grow=1.0,
        )
        return controls.surface(body, pad=PANEL_PAD, align=STRETCH, grow=1.0)

    inner = QR_SIZE - 2 * QR_PAD
    tile = (
        Stack(
            Image(cam.qr, size=inner),
            pad=QR_PAD,
            align=CENTER,
            justify=CENTER,
            size=QR_SIZE,
            # White behind the code, not the panel colour: phone cameras lock
            # on high contrast, and the dark theme would halve it.
            style=Style(fill=tokens.TEXT_PRIMARY, radius=THUMB_QR_RADIUS, clip=True),
        )
        if cam.qr
        else View(size=QR_SIZE, style=Style(fill=tokens.SURFACE, radius=THUMB_QR_RADIUS))
    )
    if cam.recording:
        detail = " · ".join(part for part in (cam.pose, cam.lens) if part)
        status = "REC — baking keyframes" + (f" · {detail}" if detail else "")
        status_color = tokens.DANGER
    elif cam.connected:
        status = "Phone connected"
        detail = " · ".join(part for part in (cam.pose, cam.lens) if part)
        if detail:
            status = f"{status} · {detail}"
        status_color = tokens.TEXT_PRIMARY
    else:
        status = "Waiting for a phone to connect…"
        status_color = tokens.TEXT_SECONDARY
    # One caption line carries the address and the pairing hint; the QR is the
    # primary path and the text only backs it up.
    address = cam.url
    if cam.hint:
        address = f"{address}  ·  {cam.hint}"
    info = Column(
        Text(
            "Scan with your phone to drive this camera",
            font=tokens.BODY_STRONG,
            color=tokens.TEXT_PRIMARY,
        ),
        Text(status, font=tokens.BODY, color=status_color),
        Text(address, font=tokens.CAPTION, color=tokens.TEXT_SECONDARY),
        gap=2.0,
        align=START,
    )
    right_children = [info]
    if state.chips:
        right_children.append(replace(_toolbar(state, pointer), grow=0))
    right = Column(
        *right_children,
        justify=BETWEEN,
        align=STRETCH,
        grow=1.0,
    )
    # The QR sits where the Generate button does in every other mode: a fixed
    # square spanning the panel, with the mode's content beside it. That is
    # what keeps this card the same height as its neighbours.
    content = Row(tile, right, gap=PANEL_GAP, align=STRETCH, grow=1.0)
    return controls.surface(content, pad=PANEL_PAD, align=STRETCH, grow=1.0)


def context_mention(entry, *, dismiss=True):
    """Selected-object tag, optionally with a dismiss control of its own."""
    return controls.mention(
        entry.label,
        entry.icon,
        dismiss=f"{entry.id}:close" if dismiss else None,
    )


def element_mention(entry):
    """A reference element as an atomic inline prompt token."""
    category = str(entry.category or "").lower()
    icon = {
        "character": "settings/people",
        "environment": "image-sparkle",
        "prop": "settings/mesh",
    }.get(category, "image-sparkle")
    return controls.mention(entry.label, icon)


def _submit_button(state, pointer=None):
    """Generate, or Stop while Scene Builder is answering.

    Same slot either way so the prompt wrap does not jump. Stop is white, not
    brand: green is Generate's alone.
    """
    if state.bridge is not None:
        return controls.generate(
            "bridge-connect",
            state.bridge.action,
            None,
            state.bridge.enabled,
            pointer,
            height=GENERATE_HEIGHT,
        )
    if state.scene_chat and state.scene_busy:
        return controls.generate(
            "scene-stop",
            "Stop",
            None,
            True,
            pointer,
            height=GENERATE_HEIGHT,
            tone="stop",
        )
    return controls.generate(
        "submit",
        state.submit_label,
        state.credits,
        state.enabled,
        pointer,
        height=GENERATE_HEIGHT,
        icon=state.submit_icon,
    )


def _submit_round(state, pointer=None):
    if state.bridge is not None:
        return controls.generate_round(
            "bridge-connect", state.bridge.enabled, pointer
        )
    if state.scene_chat and state.scene_busy:
        return controls.generate_round("scene-stop", True, pointer, tone="stop")
    return controls.generate_round(
        "submit", state.enabled, pointer, icon=state.submit_icon
    )


def _expanded(
    state, pointer, field=None, prompt_height=None, response_width=None
):
    if state.camera is not None:
        return _camera_panel(state, pointer)
    if state.bridge is not None:
        return _bridge_panel(state, pointer, response_width)
    mentions = [
        replace(element_mention(entry), dx=x, dy=y)
        for entry, x, y in state.element_positions
    ]
    if state.context and state.context_pos is not None:
        mentions.append(
            replace(
                Row(
                    *[
                        context_mention(entry, dismiss=state.mode != RETEXTURE)
                        for entry in state.context
                    ],
                    gap=8.0,
                    align=CENTER,
                    id="object-context",
                ),
                dx=state.context_pos[0],
                dy=state.context_pos[1],
            )
        )
    content_children = [
        # Neither child grows: the leftover height has to stay leftover for
        # BETWEEN to hand out, which is what puts the prompt against the top
        # and the chips against the bottom.
        replace(
            controls.prompt_field(
                "prompt",
                state.display_prompt,
                state.placeholder,
                font=tokens.PROMPT,
                editing=state.editing,
                scroll=state.scroll,
                height=prompt_height,
                caret=state.caret_pos,
                caret_on=state.caret_on,
                selection=state.selection_rects,
                mentions=tuple(mentions),
                placeholder_ink=not bool(state.prompt),
            ),
            grow=0,
            width=field,
        )
    ]
    if state.chips:
        content_children.append(replace(_toolbar(state, pointer), grow=0))
    content = Column(
        *content_children,
        gap=PROMPT_TOOLBAR_GAP if state.chips else 0.0,
        justify=BETWEEN,
        align=STRETCH,
        grow=1.0,
    )
    panel = controls.surface(
        Row(
            content,
            # The button keeps its own height, so it needs a column to sit in
            # that does stretch — a fixed-height child of a STRETCH row is left
            # at the top, and the design hangs it off the bottom edge.
            Column(
                _submit_button(state, pointer),
                align=STRETCH,
                justify=END,
            ),
            gap=PANEL_GAP,
            # STRETCH, not CENTER: the column has to span the panel's full
            # height, otherwise it collapses to its content and there is no
            # slack at all.
            align=STRETCH,
            grow=1.0,
        ),
        direction="row",
        pad=PANEL_PAD,
        align=STRETCH,
        grow=1.0,
        armed=state.drop_target,
    )
    if chat.ask_hides_prompt(state.ask):
        extra = Column(
            _responses(state, response_width or WIDTH, pointer),
            chat.ask_card(state.ask, pointer),
            gap=tokens.SM,
            align=STRETCH,
        )
        # The questionnaire takes the prompt's slot: no field, no Stop.
        return extra
    if state.prompt_resize:
        # On the panel's own top edge rather than the card's, because that is
        # the edge the drag moves: with a tray attached the card's top belongs
        # to the thumbnails, and a grip there would claim to size those.
        panel = Column(
            _resize_grip(pointer, PROMPT_RESIZE_ID),
            panel,
            align=STRETCH,
            grow=1.0,
        )
    extra = _above_prompt(state, pointer, response_width)
    if extra is None:
        return panel
    # The extras keep their natural height and the panel absorbs the rest,
    # which is what stops attachments from eating the prompt.
    return Column(extra, panel, align=STRETCH, grow=1.0)


def _collapsed(state, pointer):
    if state.login_required:
        return Row(
            _submit_round(state, pointer),
            justify=CENTER,
            align=CENTER,
            grow=1.0,
        )
    # While the agent runs a turn, the pill is the only surface in
    # sight, so it says what is happening — "Thinking…", "Generating image…" —
    # instead of inviting a prompt the disabled button would refuse anyway.
    # The thread's timer is the fallback for states with no activity of their
    # own ("Loading…"). A typed draft still wins the slot: the status is only
    # ever shown where the placeholder would have been.
    placeholder = state.placeholder
    if state.bridge is not None:
        placeholder = state.bridge.title
    elif state.scene_chat and not state.prompt:
        live = state.scene_activity or state.scene_status
        if live and not live.endswith("…"):
            live = f"{live}…"
        placeholder = live or placeholder
    return Row(
        controls.prompt_field(
            "prompt",
            state.plain_prompt,
            placeholder,
            lines=1,
            font=tokens.BODY_MEDIUM,
            editing=state.editing,
            # No caret in the pill: its text is ellipsized, so a measured
            # position from the full string would point at the wrong glyph.
            dimmed=True,
        ),
        _submit_round(state, pointer),
        gap=tokens.MD,
        pad=edges(left=8.0, right=4.0),
        align=CENTER,
        grow=1.0,
    )


def clamp_width(available):
    """The card width for a viewport that has ``available`` authored pixels."""
    if available is None:
        return WIDTH
    return max(MIN_WIDTH, min(WIDTH, available))


def measure_tray(state, pointer, measure, width):
    """Natural height of the attachment row at ``width``, or 0.

    Solved rather than assumed: the tray wraps, so this is however many rows
    the attachments came to.
    """
    if not state.references:
        return 0.0
    from ..skui import solve

    return float(solve(_tray(state, pointer), measure, width=width).height)


def measure_card(state, pointer, measure, available=None):
    """``(field, card height, prompt viewport, prompt content, chip overflow,
    tray height, response content height)``.

    The solver measures a child against its parent's whole inner width, so a
    prompt sharing a row with the Generate button would wrap as if the button
    were not there — reporting too few lines and then overflowing the box it
    actually gets. Giving the field an explicit width removes the guess: it
    wraps at the width it will be drawn at, and the card can be sized from the
    line count with nothing left to disagree about.

    ``prompt content`` is the natural height of the padded field; ``prompt
    viewport`` is that height capped at ``MAX_PROMPT_HEIGHT``, or exactly
    ``state.prompt_height`` once the grip has been dragged. When content
    exceeds the viewport the host scrolls instead of growing the card.

    ``chip overflow`` is how far the chip strip can scroll: the width its chips
    come to, less the row they are drawn in.
    """
    from ..skui import solve

    if state.camera is not None:
        # The camera card is the standard card: the QR tile takes the Generate
        # button's slot, so the height formula is every other mode's floor.
        body = clamp_width(available)
        # The chip strip runs beside the QR, not under the full panel.
        inner = body - 2 * SHELL_PAD - 2 * PANEL_PAD - QR_SIZE - PANEL_GAP
        strip = solve(chip_strip(state, pointer), measure).width if state.chips else 0.0
        card = 2 * SHELL_PAD + 2 * PANEL_PAD + max(QR_SIZE, GENERATE_HEIGHT)
        return (inner, card, 0.0, 0.0, max(0.0, strip - inner), 0.0, 0.0)

    if state.bridge is not None:
        body = clamp_width(available)
        field = max(40.0, body - 2 * SHELL_PAD - 2 * PANEL_PAD)
        height = 2 * SHELL_PAD + 2 * PANEL_PAD + GENERATE_HEIGHT
        card = min(MAX_HEIGHT, height)
        extra_width = body - 2 * SHELL_PAD
        tray = 0.0
        response_content = 0.0
        if state.scene_chat:
            if state.responses:
                response_content = solve(
                    _response_body(state, extra_width),
                    measure,
                    width=extra_width,
                ).height + 2 * tokens.SM
            tray = SCENE_HEADER_HEIGHT + SCENE_HEADER_GAP
            if state.responses:
                tray += state.responses_height + RESIZE_GRIP + SCENE_HEADER_GAP
            if state.ask is not None:
                tray += (
                    solve(
                        chat.ask_card(state.ask),
                        measure,
                        width=extra_width,
                    ).height
                    + tokens.SM
                )
        attached = measure_tray(state, pointer, measure, extra_width)
        if attached:
            tray += (SCENE_HEADER_GAP if tray else 0.0) + attached
        return (field, card + tray, 0.0, 0.0, 0.0, tray, response_content)

    button = solve(_submit_button(state, pointer), measure).width
    body = clamp_width(available)
    field = max(
        40.0,
        body - 2 * SHELL_PAD - 2 * PANEL_PAD - PANEL_GAP - button,
    )

    showing = controls.prompt_text(state.display_prompt, state.placeholder)
    _lines, _width, text_height = measure.text(
        showing,
        tokens.PROMPT,
        max(40.0, field - 2 * controls.PROMPT_PAD_X),
        0,
        False,
    )
    content = text_height + 2 * controls.PROMPT_PAD_Y
    dragged = float(state.prompt_height or 0.0) if state.prompt_resize else 0.0
    if dragged > 0.0:
        # A dragged prompt is the height it was dragged to and nothing else: it
        # no longer grows with its text and no longer stops at the automatic
        # cap. Overflow scrolls, which is what it already did past the cap.
        viewport = max(MIN_PROMPT_HEIGHT, dragged)
    else:
        viewport = min(content, MAX_PROMPT_HEIGHT)

    strip = 0.0
    if state.chips:
        strip = solve(chip_strip(state, pointer), measure).width
    toolbar = TOOLBAR_HEIGHT if state.chips else 0.0
    toolbar_gap = PROMPT_TOOLBAR_GAP if state.chips else 0.0
    content_height = viewport + toolbar_gap + toolbar
    height = (
        2 * SHELL_PAD  # the card's own padding around the panel
        + 2 * PANEL_PAD  # the panel's padding around its contents
        + (RESIZE_GRIP if state.prompt_resize else 0.0)
        + max(content_height, GENERATE_HEIGHT)
    )
    # The tray is added after the clamp, not before it: attachments sit on top
    # of the composer, and taking their room out of it would shrink the prompt
    # the moment you attached anything.
    # Every mode uses the same content-sized card as Scene Builder. A fixed
    # floor left the generation modes visibly taller with the same prompt.
    #
    # A dragged prompt is exempt from the ceiling — it *is* the request to go
    # past it — and the host is what keeps it inside the region instead.
    card = height if dragged > 0.0 else min(MAX_HEIGHT, height)
    extra_width = body - 2 * SHELL_PAD
    tray = 0.0
    response_content = 0.0
    if state.scene_chat:
        if state.responses:
            response_content = solve(
                _response_body(state, extra_width),
                measure,
                width=extra_width,
            ).height + 2 * tokens.SM
        tray = SCENE_HEADER_HEIGHT + SCENE_HEADER_GAP
        if state.responses:
            # The grip and its own gap only exist alongside a thread to size.
            tray += state.responses_height + RESIZE_GRIP + SCENE_HEADER_GAP
        if chat.ask_hides_prompt(state.ask):
            tray += (
                solve(
                    chat.ask_card(state.ask),
                    measure,
                    width=extra_width,
                ).height
                + tokens.SM
            )
            # Questionnaire owns the card: no prompt panel underneath.
            return (
                field,
                2 * SHELL_PAD + tray,
                0.0,
                0.0,
                0.0,
                tray,
                response_content,
            )
    attached = measure_tray(state, pointer, measure, extra_width)
    if attached:
        # Attachments sit under the thread, same stack `_above_prompt` draws.
        tray += (SCENE_HEADER_GAP if tray else 0.0) + attached
    return (
        field,
        card + tray,
        viewport,
        content,
        max(0.0, strip - field),
        tray,
        response_content,
    )


def popup(state, pointer=None):
    """The open select, as a standalone tree.

    It is not part of the composer: it has to reach past the card's edges, and
    the host draws it as its own layer anchored to the chip it belongs to. Rows
    are ``(label, icon[, kind])`` and answer to
    ``menu:<anchor>:<row index>``; heading rows publish no control id.

    The height cap comes from the host, which is the only side that knows how
    much room is left above the chip.
    """
    if not state.menu:
        return None
    return controls.menu(
        f"menu:{state.menu}",
        state.menu_items,
        state.menu_value,
        scroll=state.menu_scroll,
        limit=state.menu_limit,
        title=state.menu_title,
    )


def open_height(card):
    """Settled open size: mode bar + gap + prompt card (``card`` from measure)."""
    return float(card) + MODES_HEIGHT + MODES_GAP


def build(
    state,
    pointer=None,
    motion=None,
    available=None,
    height=None,
    field=None,
    prompt_height=None,
    tray=0.0,
):
    """The whole composer as one node, at any point of the open animation.

    One shell morphs from pill to card — same fill, same shadow, interpolated
    size and corner radius — and only its contents cross-fade. Fading the shell
    itself would dip to near-nothing halfway and read as a flicker.

    ``available`` is the room the host has, in authored pixels. The composer
    only ever shrinks into it: a wider viewport does not make a wider card.
    ``height`` is the open card's height; ``prompt_height`` is the clipped
    prompt viewport the host sizes from ``measure_card``. ``tray`` is kept for
    callers that still pass it — attachments grow the card upward and are
    already folded into ``height``.
    """
    del tray
    motion = motion or motion_module.Motion()
    motion.to(
        "open", 1.0 if state.open else 0.0, OPEN_DURATION, motion_module.ease_in_out
    )
    t = max(0.0, min(1.0, motion.value("open", 1.0 if state.open else 0.0)))

    limit = clamp_width(available)
    # ``measure_card`` has already clamped the card and added the tray on top.
    # Re-clamping here would crop the tray.
    grown = max(PILL_HEIGHT, height or PILL_HEIGHT)
    width = motion_module.mix(min(PILL_WIDTH, limit), limit, t)
    if state.login_required:
        width = min(LOGIN_PILL_WIDTH, limit)
    shell_height = motion_module.mix(PILL_HEIGHT, grown, t)
    modes_slot = motion_module.mix(0.0, MODES_HEIGHT, t)
    modes_gap = motion_module.mix(0.0, MODES_GAP, t)
    radius = motion_module.mix(tokens.RADIUS_PILL, tokens.RADIUS_CARD, t)

    layers = []
    if t < 0.999:
        layers.append(
            Stack(_collapsed(state, pointer), grow=1.0, style=Style(opacity=1.0 - t))
        )
    if t > 0.001:
        layers.append(
            Stack(
                _expanded(
                    state,
                    pointer,
                    field,
                    prompt_height,
                    response_width=limit - 2 * SHELL_PAD,
                ),
                grow=1.0,
                style=Style(opacity=t),
            )
        )

    shell_style = (
        Style()
        if state.login_required
        else Style(
            fill=tokens.BACKGROUND,
            radius=radius,
            border=(
                Border(1.5, tokens.TEXT_PRIMARY) if state.drop_target else None
            ),
            shadows=tokens.CARD_SHADOW,
            clip=True,
        )
    )
    shell = Stack(
        *layers,
        pad=SHELL_PAD,
        align=STRETCH,
        width=width,
        height=shell_height,
        style=shell_style,
        id="composer:body",
    )

    modes = None
    if t > 0.001:
        # Clip the bar into the growing slot so it does not flash full-size
        # before the open animation has room for it. Centred over the card —
        # the bar is content-sized, the slot is card-wide.
        modes = Column(
            mode_tabs(state.mode),
            align=CENTER,
            width=width,
            height=modes_slot,
            style=Style(opacity=t, clip=True),
        )

    return Column(
        modes,
        shell,
        gap=modes_gap,
        align=CENTER,
        width=width,
        height=shell_height + modes_slot + modes_gap,
        id="composer",
    )
