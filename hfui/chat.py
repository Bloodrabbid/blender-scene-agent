"""The assistant, as a bubble in the corner that opens into a conversation.

The fourth viewport surface, and the only one that is a *dialogue* rather than
a control panel. It borrows the shape every assistant on the web has settled
on — a small circle at the bottom-right that is almost nothing at rest, and a
tall panel above it once you reach for it — because that is the shape users
already know how to dismiss.

Two things make it different from the composer, and both are deliberate:

- **It is anchored to its bottom-right corner.** The panel grows *up and left*
  out of the bubble, so the thing you reached for does not move out from under
  the pointer as the panel arrives.
- **The panel is one fixed size, once there is a conversation.** The composer
  sizes itself to its prompt because its content is a sentence; a conversation
  has no natural height, so a panel that tracked its content would walk up the
  viewport with every reply. ``MAX_HEIGHT`` is the whole answer, and the thread
  scrolls inside it. The one exception is an *empty* thread, which takes no
  height at all: the panel is then its header and its prompt, because there is
  no greeting to fill it and 246pt of nothing reads as a panel that failed to
  load.

Nothing here imports bpy: the host owns the messages, the scroll offsets and
the caret, and hands them in.
"""

from __future__ import annotations

from ..skui import (
    CENTER,
    COVER,
    END,
    START,
    STRETCH,
    Border,
    Column,
    Image,
    Override,
    Row,
    Shadow,
    Stack,
    Style,
    Tail,
    Text,
    View,
    edges,
    replace,
)
from . import controls, markdown, tokens
from . import motion as motion_module

# One name for this thing, everywhere: the tooltip that offers it, the header
# that titles it, and the sentence it introduces itself with. It was "Higgsie"
# in the header while the disc's tooltip said "Supercomputer", which reads as
# two features.
NAME = "Supercomputer"

# -- the resting bubble ----------------------------------------------------

# 26 is this product's number for a control that stands on its own — the
# launcher island's resting height, the composer's toolbar chips, the
# explorer's action chips — and the island in the opposite corner is the disc's
# direct peer. One step above it says "this is the button, not the readout"
# without leaving the family. 45 and 58 were both tried and both read as a
# floating web widget somebody dropped on the viewport.
BUBBLE = 32.0
# The island's mark is 16 and so is this one — the two corner widgets carry
# their glyphs at the same size, which is most of what makes them read as one
# system. Anything past 18 and the four dots stop being a mark on a button and
# become the button.
BUBBLE_ICON = 16.0

# -- the tooltip -----------------------------------------------------------

# The disc opens on a click and nothing about a circle says so, and a Skia
# overlay has no host tooltip to lean on — so it draws its own. It sits
# **above** the disc, which is where a tooltip goes and where the eye already
# is. There is no room below, and beside it read as a label attached to the
# button rather than a hint about it. It cannot be *centred* above, though:
# the disc is 10pt off the right edge and the label is four times its width,
# so its right edge lines up with the disc's instead — which is exactly why it
# has a tail. Nothing collides: the panel only exists after a click, by which
# time the tooltip is gone.
TOOLTIP = "Open %s" % NAME
TOOLTIP_PAD_X = tokens.SM
TOOLTIP_PAD_Y = tokens.XS
# A label floating above a button is a label; a label with a tail is a label
# *about that button*, which matters here because the tooltip is off to one
# side of the thing it describes rather than centred over it. The tail is what
# says which of the two corner surfaces it belongs to.
TOOLTIP_TAIL_W = 10.0
TOOLTIP_TAIL_H = 5.0
# Nearly closed. The gap used to be `SM` because the label was a separate
# object hanging in space; a tail that stops short of what it points at is
# just an arrow.
TOOLTIP_GAP = 2.0
# Long enough that crossing the corner on the way somewhere else does not
# flash it, short enough that resting on the disc feels answered.
TOOLTIP_DELAY = 0.4
TOOLTIP_FADE = 0.12

# -- the panel it opens into -----------------------------------------------

WIDTH = 304.0
# Two and a half times the composer's resting card (128), which is the
# reference the panel was sized against: tall enough that a reply of a few
# paragraphs is read rather than scrolled, and short enough to leave the
# viewport behind it usable. The host caps this again against the region.
MAX_HEIGHT = 320.0
MIN_HEIGHT = 192.0

PAD = tokens.LG
HEADER = 26.0
CONTROL = 21.0

# The input block at the foot of the panel. The prompt grows with what is
# typed and then scrolls, exactly like the composer's.
MAX_PROMPT_HEIGHT = 51.0
ACTIONS = 22.0
SEND = 22.0
INPUT_PAD = tokens.MD

# A tile in the attachment tray. Smaller than the composer's 45: the panel is
# a third of its width and four references would fill the row.
ATTACH_TILE = 32.0
# A generation the agent produced, shown under its answer. Three to a row in
# the 284pt column — big enough to tell two renders apart, small enough that a
# batch of four does not take the whole panel.
RESULT_TILE = 86.0

# How wide a message is allowed to get before it wraps. Never the full column:
# a user bubble that reaches both edges stops reading as one side of a
# conversation.
BUBBLE_SHARE = 0.82

SHOW_DURATION = 0.18
HIDE_DURATION = 0.14
# How far the panel travels on its way in, in logical pixels. It comes up out
# of the bubble, so the move is down-and-back rather than a fade in place.
RISE = 16.0

USER = "user"
ASSISTANT = "assistant"

# Published node ids.
BUBBLE_ID = "chat:bubble"
CLEAR = "chat:clear"
CLOSE = "chat:close"
THREAD = "chat:thread"
PROMPT = "chat:prompt"
ATTACH = "chat:attach"
SEND_ID = "chat:send"
STOP = "chat:stop"
PANEL = "chat:panel"
MSG = "msg"
ASK_OPTION = "ask:option"
ASK_PREV = "ask:prev"
ASK_NEXT = "ask:next"
ASK_SKIP = "ask:skip"
ASK_CONTINUE = "ask:continue"
ASK_CUSTOM = "ask:custom"

TITLE = NAME
PLACEHOLDER = "Ask anything, or describe a shot…"
# What the assistant says between the send and its first token. It is a
# message with no text yet, not a separate spinner: the row it will fill is
# already there, so the first token does not shove the thread. The word is the
# stem — `ellipsis` counts out the dots after it.
THINKING = "Thinking"

# A pending row says so by counting: `Thinking.`, `..`, `...`, over and over.
# A spinner would be a second animation and a nicer one, but the panel is a
# single layer, so every frame of it re-describes, re-solves, re-paints and
# re-uploads the whole conversation — 14 ms empty and 22 with a long reply in
# view. Three dots on a slow cycle is the most motion this can afford, and it
# is enough: the question a waiting reader has is whether anything is still
# happening, and text that moves at all answers it.
ELLIPSIS_STEPS = 3
ELLIPSIS_PERIOD = 1.2
DOTS = motion_module.Loop(ELLIPSIS_PERIOD, ELLIPSIS_STEPS)


def ellipsis(step):
    """The trailing dots at a quantised phase, one to `ELLIPSIS_STEPS`.

    Quantised because the phase is in the state key: a continuous one would
    miss the tree and texture caches on every single frame.
    """
    return "." * (1 + int(step) % ELLIPSIS_STEPS)


# The product's own mark, and not one from the icon pack: on fnf-web the
# Supercomputer's nav link draws four dots in a 2x2 grid (`SupercomputerDots`
# in `entities/header/v2/model/nav-config.tsx`) — four 4px circles at 0 and 5
# in a 9px box.
#
# **It is four nodes here, not the SVG it started as**, because the web
# animates each dot on its own delay and an `Image` is one thing that can only
# be scaled as one thing. The proportions are the glyph's, carried over: in its
# 24px box the circles are 8.8 across with 11.2 between centres, so a cell is
# `MARK_GAP` and the dot sits centred in it at whatever scale the pulse asks
# for. That keeps a growing dot from moving its neighbours — the thing that
# makes this a pulse rather than a jitter.
MARK = BUBBLE_ICON
MARK_DOT = MARK * (8.8 / 24.0)
MARK_GAP = MARK * (11.2 / 24.0)

# `--animate-supercomputer-dot` from `shared/theme/animation.css`: ease-in-out,
# up by 34% of the period, back by 68%, a rest for the last third, each dot a
# step behind the one before it. The *shape* is the web's and the timing is
# not — the web runs 1.45s to a 1.28 peak, which is right for a nav bar you
# glance at and too much for something sitting in the corner of a viewport
# while you model. Slowed to 2.2s and shallowed to 1.18: the dot travels 1.3pt
# instead of 2.1, and the wave still reads. It is also cheaper, since the same
# `PULSE_STEPS` now cover a longer period.
#
# The brightness and the halo the web pairs with this ride the same curve; they
# live with the palette that makes them possible, in the glow block below.
PULSE_PERIOD = 2.2
PULSE_STAGGER = 0.24
PULSE_PEAK = 1.18
PULSE_RISE = 0.34
PULSE_FALL = 0.68
# The breath is quantised to this many frames, and the number is a memory
# budget rather than a smoothness one — see `motion.Loop`. 36 over 2.2s is
# ~16fps, and the dot moves 0.17 of a device pixel between steps, so the
# quantisation is well under what antialiasing already smooths.
PULSE_STEPS = 36
# One object so the step, the seconds it stands for and the pump's sleep all
# come from the same two numbers. They used to be recombined by hand in three
# places, one of them in another file.
BREATH = motion_module.Loop(PULSE_PERIOD, PULSE_STEPS)


def _mix(a, b, t):
    return tuple(x + (y - x) * t for x, y in zip(a, b))


# -- the glow --------------------------------------------------------------
#
# The rest of `--animate-supercomputer-dot`: `brightness(1.5)` on the dot and a
# cyan `drop-shadow` under it, both at the peak, both staggered with the scale.
#
# **A glow needs the dots to be lighter than what is behind them**, so it is
# really a question about the disc's fill, and it is the reason the disc is
# dark. It was brand green first, on the argument that a dark circle is the
# island's costume and two of them in opposite corners read as two of the same
# thing. But near-black dots on green cannot glow: `brightness` greys them out
# and reads as fading, and a halo reads as the dot being out of focus. Four
# treatments were rendered side by side and only the web's own palette reads as
# *light*. The disc gives up some of its pull as a button for it, which is the
# trade that was made knowingly.
#
# `PANEL` rather than the island's `BACKGROUND`, so the two are at least not
# the same dark.
DISC_FILL = tokens.PANEL
DISC_PRESSED = _mix(tokens.PANEL, (1.0, 1.0, 1.0, 1.0), 0.09)
DOT_INK = tokens.BRAND
GLOW_INK = tokens.hexa("#4fcee4")  # `rgb(79 206 228)`, the web's halo
GLOW_ALPHA = 0.75
GLOW_BLUR = MARK_DOT  # a 4px halo on a 4px dot, at the size ours is drawn
GLOW_LIFT = 0.5  # `brightness(1.5)`, expressed as a step toward white


# -- what the agent did on its way to answering --------------------------
#
# A turn is mostly tool calls, and the panel is 356 points wide, so each one
# gets **one line**: a glyph, what it is doing, and at most a handful of words
# about how it went. The full input and output are on the web; what a modeller
# needs from the corner of a viewport is whether the thing is moving and what
# it produced. Anything longer than a line here and three tool calls push the
# answer off the bottom of the thread.
TOOL_RUNNING = "running"
TOOL_DONE = "done"
TOOL_FAILED = "failed"

TOOL_ICON = 10.0
TOOL_ROW = 13.0

# The MCP server attribution beside a tool label: the secondary ink (#828282)
# at just over half strength, so "adobe_connector" reads as provenance, not a
# verb.
_SERVER_INK = tokens.rgba(0x82 / 0xFF, 0x82 / 0xFF, 0x82 / 0xFF, 0.55)


def worked_duration(total):
    """``9m 18s`` — the web's ``formatWorkedDuration``, same breakpoints."""
    total = max(0, int(total))
    hours, minutes, seconds = total // 3600, (total % 3600) // 60, total % 60
    if hours >= 2:
        return f"{hours}h"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    if minutes:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    return f"{seconds}s"


def working_status(seconds):
    """The live ``Working for 9m 18s``, or ``""`` when idle."""
    if seconds is None:
        return ""
    if seconds < 1.0:
        return "Working…"
    return f"Working for {worked_duration(seconds)}"


def turn_activity(message):
    """The current verb of a live turn, or ``""``.

    A running tool names the moment better than a timer; between tools the
    phase is all there is — "Thinking" before the first token, "Answering"
    while the text streams. The working line prints this after a bullet;
    trailing dots belong to the collapsed pill, not here.
    """
    if message is None:
        return ""
    for tool in reversed(message.tools):
        if tool.status == TOOL_RUNNING:
            return tool.label
    if message.text.strip():
        return "Answering"
    return "Thinking"


def working_line(status, activity=""):
    """The live-turn readout, pinned under the thread.

    Clock and duration in brand ink, current verb a step quieter after a
    bullet — the shape the web chat uses for "Working for 7s • Viewing skill".
    Not a tool row: those are the machinery, this is the clock. Sits at the
    bottom of the thread so it stays on the live edge as the answer grows.
    """
    if not status:
        return None
    verb = " ".join(str(activity or "").replace("…", " ").split())
    parts = [
        Image(tokens.icon("settings/clock"), tint=tokens.BRAND, size=TOOL_ICON),
        Text(status, font=tokens.CAPTION, color=tokens.BRAND, lines=1),
    ]
    if verb:
        parts.append(
            Text("•", font=tokens.CAPTION, color=tokens.TEXT_SECONDARY, lines=1)
        )
        parts.append(
            Text(
                verb,
                font=tokens.CAPTION,
                color=tokens.TEXT_SECONDARY,
                lines=1,
                grow=1.0,
            )
        )
    return Row(*parts, gap=tokens.SM, align=CENTER, height=TOOL_ROW)


def _ask_pager_btn(node_id, icon, enabled):
    ink = tokens.TEXT_PRIMARY if enabled else tokens.TEXT_SECONDARY
    return Stack(
        Image(tokens.icon(icon), tint=ink, size=10),
        align=CENTER,
        justify=CENTER,
        size=18.0,
        style=Style(
            radius=9.0,
            hover=Override(fill=tokens.SURFACE_HOVER) if enabled else None,
            pressed=Override(fill=tokens.SURFACE_PRESSED) if enabled else None,
        ),
        id=node_id if enabled else None,
    )


def _ask_option(index, label, selected):
    number = f"{index + 1}."
    return Row(
        Text(number, font=tokens.BODY_MEDIUM, color=tokens.TEXT_SECONDARY, lines=1),
        Text(label, font=tokens.BODY_MEDIUM, color=tokens.TEXT_PRIMARY, lines=0, grow=1.0),
        gap=tokens.SM,
        pad=edges(x=tokens.MD, y=tokens.SM),
        align=CENTER,
        style=Style(
            fill=tokens.SURFACE_HOVER if selected else None,
            radius=tokens.RADIUS_BUTTON,
            hover=Override(fill=tokens.SURFACE_HOVER),
            pressed=Override(fill=tokens.SURFACE_PRESSED),
        ),
        id=f"{ASK_OPTION}:{index}",
    )


def ask_hides_prompt(ask):
    """True while the questionnaire owns the input slot.

    The prompt comes back only after "tell what to do differently", so a
    parked question cannot sit on top of a field the user is not meant to
    use.
    """
    return ask is not None and not ask.custom


def ask_card(ask, pointer=None):
    """The parked questionnaire: numbered options, pager, Skip / Continue.

    Lives above the prompt so letting go of a pick is the same place the
    user already looks to send. ``pointer`` is accepted for the same reason
    every other card takes it and is unused: hover is on the Style.
    """
    del pointer
    if ask is None:
        return None
    can_back = ask.page > 1
    can_next = ask.page < ask.pages
    pager = None
    if ask.pages > 1:
        pager = Row(
            _ask_pager_btn(ASK_PREV, "chevron-left", can_back),
            Text(
                f"{ask.page}/{ask.pages}",
                font=tokens.CAPTION,
                color=tokens.TEXT_SECONDARY,
                lines=1,
            ),
            _ask_pager_btn(ASK_NEXT, "chevron-right", can_next),
            gap=tokens.XS,
            align=CENTER,
        )
    options = [
        _ask_option(index, label, index == ask.selected)
        for index, label in enumerate(ask.options)
    ]
    ready = ask.selected is not None
    continue_btn = Row(
        Text(
            "Continue",
            font=tokens.BRAND_LABEL,
            color=tokens.ON_BRAND if ready else tokens.TEXT_SECONDARY,
            lines=1,
        ),
        pad=edges(x=tokens.LG, y=tokens.SM),
        align=CENTER,
        justify=CENTER,
        style=Style(
            fill=tokens.BRAND if ready else tokens.SURFACE,
            radius=tokens.RADIUS_ROUND,
            hover=Override(fill=tokens.BRAND_BEVEL) if ready else None,
            pressed=Override(fill=tokens.BRAND_BEVEL) if ready else None,
        ),
        id=ASK_CONTINUE if ready else None,
    )
    skip = Row(
        Text("Skip", font=tokens.BODY_MEDIUM, color=tokens.TEXT_PRIMARY, lines=1),
        Text("ESC", font=tokens.CAPTION, color=tokens.TEXT_SECONDARY, lines=1),
        gap=tokens.XS,
        pad=edges(x=tokens.SM, y=tokens.SM),
        align=CENTER,
        style=Style(
            radius=tokens.RADIUS_BUTTON,
            hover=Override(fill=tokens.SURFACE_HOVER),
            pressed=Override(fill=tokens.SURFACE_PRESSED),
        ),
        id=ASK_SKIP,
    )
    return Column(
        Row(
            Text(
                ask.title,
                font=tokens.BODY_MEDIUM,
                color=tokens.TEXT_PRIMARY,
                lines=0,
                grow=1.0,
            ),
            pager,
            gap=tokens.SM,
            align=CENTER,
        ),
        Column(*options, gap=tokens.XS, align=STRETCH) if options else None,
        View(
            height=1.0,
            style=Style(fill=tokens.BORDER_FAINT),
        ),
        Row(
            Text(
                "No, and tell what to do differently",
                font=tokens.CAPTION,
                color=tokens.TEXT_SECONDARY,
                lines=0,
                ellipsis=False,
                grow=1.0,
            ),
            pad=edges(y=tokens.XS),
            align=START,
            style=Style(
                radius=tokens.RADIUS_BUTTON,
                hover=Override(fill=tokens.SURFACE_HOVER),
                pressed=Override(fill=tokens.SURFACE_PRESSED),
            ),
            id=ASK_CUSTOM,
        ),
        Row(
            View(grow=1.0),
            skip,
            continue_btn,
            gap=tokens.SM,
            align=CENTER,
        ),
        gap=tokens.SM,
        pad=tokens.MD,
        align=STRETCH,
        style=Style(
            fill=tokens.SURFACE,
            radius=tokens.RADIUS_SURFACE,
        ),
    )


class Tool:
    """One tool call, as the single line the thread shows for it.

    ``count`` is how many identical calls the row stands for. An agent reading
    four of its own skill docs in a row is one fact about the turn, not four,
    and four lines of "Reading" pushes the answer off the panel.
    """

    __slots__ = ("call_id", "label", "icon", "detail", "status", "count", "server")

    def __init__(
        self,
        call_id,
        label,
        icon="settings/sparkles-three",
        detail="",
        status=TOOL_RUNNING,
        count=1,
        server="",
    ):
        self.call_id = call_id
        self.label = label
        self.icon = icon
        self.detail = detail
        self.status = status
        self.count = int(count)
        # The MCP server an `mcp__<server>__<tool>` call came through
        # ("adobe_connector"), printed after the label the way the web does.
        self.server = server

    @property
    def key(self):
        return (
            self.call_id,
            self.label,
            self.icon,
            self.detail,
            self.status,
            self.count,
            self.server,
        )


class AskState:
    """The question currently on screen. One page of a parked questionnaire.

    The host owns the full list; the tree only needs the page it is drawing
    so a 3-of-N set does not rebuild when the user is looking at page 1.
    """

    __slots__ = ("title", "options", "selected", "page", "pages", "custom")

    def __init__(
        self, title, options=(), selected=None, page=1, pages=1, custom=False
    ):
        self.title = title
        self.options = tuple(options)
        self.selected = selected
        self.page = int(page)
        self.pages = int(pages)
        self.custom = bool(custom)

    @property
    def key(self):
        return (
            self.title,
            self.options,
            self.selected,
            self.page,
            self.pages,
            self.custom,
        )


class Message:
    """One turn. ``streaming`` is the assistant still typing into it.

    ``tools`` and ``images`` are the assistant's half of a turn that did more
    than talk: the calls it made, and whatever generations came back. Both are
    lists because the host appends to them while the turn streams; ``key`` is
    what makes that visible to the caches.
    """

    __slots__ = (
        "role",
        "text",
        "streaming",
        "attachments",
        "elements",
        "tags",
        "tools",
        "images",
        "failed",
    )

    def __init__(
        self,
        role,
        text="",
        streaming=False,
        attachments=(),
        elements=(),
        tags=(),
        tools=(),
        images=(),
        failed=False,
    ):
        self.role = role
        self.text = text
        self.streaming = bool(streaming)
        self.attachments = tuple(attachments)
        self.elements = tuple(elements)
        self.tags = tuple(tags)
        self.tools = list(tools)
        self.images = list(images)
        self.failed = bool(failed)

    @property
    def key(self):
        return (
            self.role,
            self.text,
            self.streaming,
            self.attachments,
            self.elements,
            self.tags,
            tuple(tool.key for tool in self.tools),
            tuple(self.images),
            self.failed,
        )


class ChatState:
    """What the surface is showing, and how far open it is being asked to be."""

    def __init__(self):
        self.messages = ()
        self.prompt = ""
        self.attachments = ()
        # The pointer is on the bubble or the panel. Only the tooltip and the
        # hover affordance read this — opening is a click.
        self.hovered = False
        # The panel is open. Set by a click on the disc and cleared by Close,
        # Escape or a click outside; nothing about where the pointer is will
        # change it.
        self.pinned = False
        self.editing = False
        # Character indices, straight from the host: where the caret is and
        # what is selected.
        self.caret = None
        self.selection = None
        # The same two, **measured** — `(x, y, height)` and one rect per
        # visual line. `ComposerState` splits them the same way and for the
        # same reason: the tree paints the pixels and only the host can work
        # them out, so one field cannot be both. Collapsing them here handed
        # `prompt_field` a `(start, end)` pair where it iterates rects, and
        # the surface raised on the first selection anyone made.
        self.caret_pos = None
        self.selection_rects = ()
        self.caret_on = True
        self.scroll = 0.0
        self.prompt_scroll = 0.0
        # The explorer has the viewport; stand down, same as the island.
        self.away = False
        # A reply is landing token by token: the send button is spent.
        self.busy = False
        # An explorer tile is held over the prompt: the input block lights.
        self.drop_target = False
        # Live-turn clock under the thread: "Working for 7s", and the verb
        # that rides after the bullet ("Generating image"). Empty when idle.
        self.working = ""
        self.activity = ""
        # Which of `ELLIPSIS_STEPS` the waiting rows are counting on.
        self.ellipsis = 0
        self.height = MAX_HEIGHT
        # A parked ``ask_user_question``: the card above the prompt.
        self.ask = None
        # Read-only selection in the thread: ``(message index, start, end)``.
        self.thread_select = None
        self.thread_select_rects = ()

    @property
    def open(self):
        """Click to open, and only a click.

        Hover-to-open is right for the composer, whose pill is a lip on the
        edge of the viewport that a pointer only reaches deliberately. This
        disc sits in the corner every other overlay's controls run toward, and
        a 380x400 panel that unfurls because somebody passed through on the
        way to the timeline is a panel that has taken the viewport without
        being asked.
        """
        return bool(self.pinned and not self.away)

    @property
    def key(self):
        return (
            tuple(message.key for message in self.messages),
            self.prompt,
            self.attachments,
            self.editing,
            # The measured pair, not the indices: these are what the tree
            # draws, and they are what has to invalidate the raster.
            self.caret_pos,
            self.selection_rects,
            self.caret_on,
            round(self.scroll, 1),
            round(self.prompt_scroll, 1),
            self.busy,
            self.drop_target,
            self.working,
            self.activity,
            self.ellipsis,
            round(self.height, 1),
            self.ask.key if self.ask is not None else None,
            self.thread_select,
            self.thread_select_rects,
        )


# -- the bubble ------------------------------------------------------------


def pulse(elapsed):
    """The four dot scales at ``elapsed`` seconds into the animation.

    A `Loop` rather than a `Motion` track, because the tracks are
    target-seeking and this has no target. The stagger is the whole reason the
    mark is four nodes and not the SVG it started as: each dot reads the same
    curve a fixed distance behind the one before it, which is a shift of the
    clock and nothing more, so there is no per-dot state to keep.
    """
    out = []
    for index in range(4):
        phase = BREATH.phase(elapsed - index * PULSE_STAGGER)
        if phase < PULSE_RISE:
            grown = motion_module.ease_in_out(phase / PULSE_RISE)
        elif phase < PULSE_FALL:
            grown = 1.0 - motion_module.ease_in_out(
                (phase - PULSE_RISE) / (PULSE_FALL - PULSE_RISE)
            )
        else:
            grown = 0.0
        out.append(1.0 + (PULSE_PEAK - 1.0) * grown)
    return tuple(out)


def mark(scales=None, ink=None):
    """The four dots, each centred in a fixed cell so a pulse cannot shift them.

    A dot's place in the breath is the one number that drives everything about
    it: ``lit`` is that scale expressed as 0..1, and the size, the brightness
    and the halo all ride it, so the three land together without a second
    clock.
    """
    ink = DOT_INK if ink is None else ink
    scales = scales or (1.0, 1.0, 1.0, 1.0)
    cells = []
    for size in scales:
        lit = (size - 1.0) / (PULSE_PEAK - 1.0)
        paint = _mix(ink, (1.0, 1.0, 1.0, ink[3]), lit * GLOW_LIFT) if lit else ink
        glow = (
            (Shadow(0.0, 0.0, GLOW_BLUR, GLOW_INK[:3] + (GLOW_ALPHA * lit,)),)
            if lit > 0.01
            else ()
        )
        dot = View(
            size=MARK_DOT * size,
            style=Style(fill=paint, radius=tokens.RADIUS_ROUND, shadows=glow),
        )
        cells.append(Stack(dot, align=CENTER, justify=CENTER, size=MARK_GAP))
    return Column(Row(cells[0], cells[1]), Row(cells[2], cells[3]))


def bubble(scales=None):
    """The resting circle: one dark fill, four brand dots, no words.

    The dots are the light in here, which is what the disc's fill is for — see
    the glow block above.
    """
    return Stack(
        mark(scales),
        align=CENTER,
        justify=CENTER,
        size=BUBBLE,
        style=Style(
            fill=DISC_FILL,
            radius=BUBBLE / 2.0,
            shadows=tokens.CARD_SHADOW,
            pressed=Override(fill=DISC_PRESSED),
        ),
        id=BUBBLE_ID,
    )


def tooltip():
    """The label above the resting disc. Says the verb, since the shape can't.

    Not a control and carries no id: it is drawn on its own layer above the
    bubble and never answers the pointer, so it cannot swallow the click it is
    there to invite.

    The tail is `skui`'s own, so the fill and the border are one outline: it
    aims at the disc's centre, which is half a disc in from the label's right
    edge, since the two are flush there. It hangs **below** the box and takes
    no part in layout, exactly like the shadow — `TOOLTIP_TAIL_H` is the
    caller's to account for.
    """
    return Row(
        Text(TOOLTIP, font=tokens.BODY, color=tokens.TEXT_PRIMARY),
        pad=edges(x=TOOLTIP_PAD_X, y=TOOLTIP_PAD_Y),
        align=CENTER,
        style=Style(
            fill=tokens.BACKGROUND,
            radius=tokens.RADIUS_CHIP,
            border=Border(1.0, tokens.BORDER_FAINT),
            tail=Tail(TOOLTIP_TAIL_W, TOOLTIP_TAIL_H, offset=BUBBLE / 2.0, align=END),
            shadows=tokens.CARD_SHADOW,
        ),
    )


# -- the panel -------------------------------------------------------------


def _control(node_id, icon, pointer=None, enabled=True):
    """A header button: a glyph, and a surface that only appears under the
    pointer. The header is three controls beside a title in 356 points, so
    resting chrome on each of them would read as a toolbar.

    The one control in the product whose glyph brightens along with its
    surface, and ``tint`` is a field on the ``Image`` rather than anything a
    ``Style`` can carry — so the pointer is still read here, for the ink alone.
    """
    ink = tokens.ICON_SECONDARY if enabled else tokens.BORDER_STRONG
    if enabled and pointer is not None and pointer.state(node_id) is not None:
        ink = tokens.ICON_PRIMARY
    return Stack(
        Image(tokens.icon(icon), tint=ink, size=13),
        align=CENTER,
        justify=CENTER,
        size=CONTROL,
        style=Style(
            radius=tokens.RADIUS_CHIP,
            hover=Override(fill=tokens.SURFACE_HOVER) if enabled else None,
            pressed=Override(fill=tokens.SURFACE_PRESSED) if enabled else None,
        ),
        id=node_id if enabled else None,
    )


def _header(state, pointer=None):
    """The title and the two controls. No avatar.

    A brand disc here was the third mark on screen saying the same thing — the
    one on the button that opened the panel, this one, and the app's own — and
    the panel is already unmistakably ours. The name carries the identity and
    the row is quieter for losing it.
    """
    return Row(
        Text(TITLE, font=tokens.BODY_MEDIUM, color=tokens.TEXT_PRIMARY, grow=1.0),
        _control(CLEAR, "settings/eraser", pointer, bool(state.messages)),
        _control(CLOSE, "close", pointer),
        gap=tokens.MD,
        align=CENTER,
        height=HEADER,
    )


# The agent writes markdown; `hfui.markdown` reads the half of it that is
# *structure* — blocks, lists, tables — and flattens inline emphasis. It lives
# in its own module because the Scene Builder thread reads the same replies,
# and two renderers is two answers to "what is a list" in one product.
_blocks = markdown.blocks


def wrap_width(role, width):
    """How wide a message's text wraps, matching `_message`."""
    if role == USER:
        return max(1.0, width * BUBBLE_SHARE - 2 * tokens.LG)
    return max(1.0, width)


def message_id(index):
    return f"{MSG}:{int(index)}"


def message_index(node_id):
    """The thread index encoded in a ``msg:N`` node, or None."""
    prefix = MSG + ":"
    raw = str(node_id or "")
    if not raw.startswith(prefix):
        return None
    try:
        return int(raw[len(prefix) :])
    except ValueError:
        return None


def selection_paint(rects):
    """Highlight views in text space. Empty when there is no selection."""
    if not rects:
        return ()
    return tuple(
        View(
            width=float(width),
            height=float(height),
            dx=float(x),
            dy=float(y),
            style=Style(fill=tokens.SELECTION, radius=2.0),
        )
        for x, y, width, height in rects
    )


def selectable_text(blocks, node_id, rects=()):
    """The reply's text, hit-testable, with the host's highlight behind it."""
    body = Column(*blocks, gap=tokens.SM, align=START, id=node_id)
    paints = selection_paint(rects)
    if not paints:
        return body
    return Stack(*paints, body, align=START)


def _tool_line(tool, step=0):
    """One tool call as one line: a glyph, a verb, and how it went.

    Never a card and never two lines. The row has to be quiet enough that a
    turn which called five tools still reads as one answer with some machinery
    behind it, rather than as a log the answer is buried in — so it is caption
    type in the secondary ink, and only a failure raises its voice.

    The counting dots are the row's whole liveness signal, so they replace the
    static ellipsis rather than sitting beside it: a tool that has been running
    for two minutes and one that hung look identical without them.
    """
    ink = tokens.DANGER if tool.status == TOOL_FAILED else tokens.TEXT_SECONDARY
    text = tool.label
    if tool.count > 1:
        text = f"{text} ×{tool.count}"
    if tool.detail:
        text = f"{text} · {tool.detail}"
    elif tool.status == TOOL_RUNNING:
        text = f"{text}{ellipsis(step)}"
    # The MCP server rides after the label a step quieter, the way the web
    # chat prints "Bl screenshot  adobe_connector": attribution, not a verb.
    server = (
        Text(tool.server, font=tokens.CAPTION, color=_SERVER_INK, lines=1, grow=1.0)
        if tool.server
        else None
    )
    return Row(
        Image(tokens.icon(tool.icon), tint=ink, size=TOOL_ICON),
        Text(text, font=tokens.CAPTION, color=ink, lines=1, grow=0.0 if server else 1.0),
        server,
        gap=tokens.SM,
        align=CENTER,
        height=TOOL_ROW,
    )


def _tile(source, size):
    """A picture in the thread: an image inside its own rounded, clipping box.

    ``Image`` has no corner radius of its own — rounding is a property of a
    rectangle, so the box is what carries it and the clip is what applies it.
    A source Skia cannot decode leaves the tile as its own surface with the
    media glyph on it, the way the composer's tray does, because an empty
    square reads as a bug.
    """
    body = (
        Image(source, fit=COVER, grow=1.0)
        if source
        else Stack(
            Image(tokens.icon("image-sparkle"), tint=tokens.ICON_SECONDARY, size=13),
            align=CENTER,
            justify=CENTER,
            grow=1.0,
        )
    )
    return Stack(
        body,
        align=STRETCH,
        size=size,
        style=Style(fill=tokens.SURFACE, radius=tokens.RADIUS_CHIP, clip=True),
    )


def _results(message):
    """Generations the turn produced, once their jobs landed.

    They arrive minutes after the text — the tool output carries job ids and
    nothing else — so they are appended under the answer rather than woven
    into it.
    """
    if not message.images:
        return None
    return Row(
        *[_tile(source, RESULT_TILE) for source in message.images],
        gap=tokens.XS,
        wrap=True,
    )


def _message(message, width, step=0, working=False, index=0, highlights=()):
    """One turn. The user is a bubble; the assistant is bare type.

    Only one side needs a container. Giving both one turns the thread into a
    ladder of boxes and halves the room the answer — which is the long half of
    every exchange — has to be read in.

    ``working`` is the thread already carrying a live "Working for" line: an
    empty streaming turn then stays out of the column, so Thinking and the
    clock do not both claim the same wait.

    The text column carries ``msg:index`` so the host can select and copy it.
    Tools and thumbnails stay out of that hit target.
    """
    node = message_id(index)
    if message.role == USER:
        body = []
        if message.tags:
            body.append(
                Row(
                    *[
                        controls.mention(tag, "settings/mesh")
                        for tag in message.tags
                    ],
                    gap=tokens.XS,
                    wrap=True,
                )
            )
        if message.attachments:
            body.append(
                Row(
                    *[_tile(source, ATTACH_TILE) for source in message.attachments],
                    gap=tokens.XS,
                    wrap=True,
                )
            )
        # What the bubble leaves the text once it has taken its own padding.
        # The user rarely writes markdown, but the same renderer runs on both
        # sides: two answers to "what is a list" in one thread is worse than
        # the odd bullet on a typed line.
        lines = _blocks(
            message.text,
            tokens.TEXT_PRIMARY,
            wrap_width(USER, width),
        )
        if lines:
            body.append(selectable_text(lines, node, highlights))
        return Row(
            Column(
                *body,
                gap=tokens.SM,
                pad=edges(x=tokens.LG, y=tokens.MD),
                align=STRETCH,
                max_width=width * BUBBLE_SHARE,
                style=Style(fill=tokens.SURFACE, radius=tokens.RADIUS_BUTTON),
            ),
            justify=END,
        )
    body = [_tool_line(tool, step) for tool in message.tools]
    lines = _blocks(
        message.text,
        tokens.DANGER if message.failed else tokens.TEXT_PRIMARY,
        wrap_width(ASSISTANT, width),
    )
    if not lines and not body and message.streaming and not working:
        # The row the answer will fill is already here, so the first token
        # does not shove the thread. Only when there is nothing else to
        # show — a tool line says the same thing better. The working line
        # under the thread is that row when the clock is up.
        lines = [
            Text(
                THINKING + ellipsis(step),
                font=tokens.BODY,
                color=tokens.TEXT_SECONDARY,
                grow=1.0,
            )
        ]
    elif lines and message.text:
        body.append(selectable_text(lines, node, highlights))
        lines = []
    body.extend(lines)
    results = _results(message)
    if results is not None:
        body.append(results)
    if not body:
        return None
    return Column(*body, gap=tokens.SM, align=START)


def thread_body(state, width):
    """The messages as one column, at the width the panel gives them.

    Solved on its own by the host so it knows how far the thread can scroll,
    then placed inside the clipped viewport below. Same tree both times, so
    the measurement and the drawing cannot disagree.

    A live turn pins its clock on the live edge of the thread, after the last
    message: the header is the title, and the thing that is still moving
    belongs with the answer it is writing. At the end it stays in view as the
    reply grows instead of riding the first token off the top.
    """
    live = bool(state.working)
    clock = working_line(state.working, state.activity)
    rows = []
    selected = state.thread_select
    for index, message in enumerate(state.messages):
        highlights = (
            state.thread_select_rects
            if selected is not None and selected[0] == index
            else ()
        )
        node = _message(
            message, width, state.ellipsis, live, index=index, highlights=highlights
        )
        if node is not None:
            rows.append(node)
    if clock is not None:
        rows.append(clock)
    return Column(*rows, gap=tokens.LG, align=STRETCH, width=width)


def _thread(state, width, height):
    body = thread_body(state, width)
    scroll = max(0.0, float(state.scroll or 0.0))
    return Stack(
        replace(body, dy=-scroll),
        align=START,
        justify=START,
        width=width,
        height=height,
        style=Style(clip=True),
        id=THREAD,
    )


def _tray(state, pointer=None):
    """Pending attachments, above the prompt. ``chat:attachment:<i>:remove``."""
    if not state.attachments:
        return None
    tiles = [
        controls.thumbnail(
            f"chat:attachment:{index}", source, pointer=pointer, size=ATTACH_TILE
        )
        for index, source in enumerate(state.attachments)
    ]
    return Row(*tiles, gap=tokens.XS, wrap=True)


def _send(state, pointer=None):
    """The submit disc, or the stop square while a reply is landing.

    Same slot either way: a dim button that does nothing is worse than a
    control that cuts the turn. The square is ``controls.stop_glyph`` so the
    composer's Stop and this one are the same mark.
    """
    if state.busy:
        return Stack(
            controls.stop_glyph(tokens.BACKGROUND, 8.0),
            align=CENTER,
            justify=CENTER,
            size=SEND,
            style=Style(
                fill=tokens.TEXT_PRIMARY,
                radius=SEND / 2.0,
                pressed=Override(fill=tokens.ICON_SECONDARY),
            ),
            id=STOP,
        )
    enabled = bool(state.prompt.strip() or state.attachments)
    ink = tokens.ON_BRAND if enabled else tokens.ICON_SECONDARY
    return Stack(
        Image(tokens.icon("settings/arrow-up"), tint=ink, size=13),
        align=CENTER,
        justify=CENTER,
        size=SEND,
        style=Style(
            fill=tokens.BRAND if enabled else tokens.SURFACE,
            radius=SEND / 2.0,
            pressed=Override(fill=tokens.BRAND_BEVEL) if enabled else None,
        ),
        id=SEND_ID if enabled else None,
    )


def _attach():
    return Stack(
        Image(tokens.icon("plus"), tint=tokens.ICON_PRIMARY, size=13),
        align=CENTER,
        justify=CENTER,
        size=ACTIONS,
        style=Style(
            fill=tokens.SURFACE,
            radius=ACTIONS / 2.0,
            hover=Override(fill=tokens.SURFACE_HOVER),
            pressed=Override(fill=tokens.SURFACE_PRESSED),
        ),
        id=ATTACH,
    )


def input_block(state, pointer=None, prompt_height=None):
    """The prompt, its tray and its two buttons, as one rounded surface.

    Public because the host solves it on its own to find out how tall it came
    to: the tray wraps, so a fifth attachment adds a row, and the thread has to
    give up exactly that much rather than the panel growing past its cap.
    """
    return Column(
        _tray(state, pointer),
        controls.prompt_field(
            PROMPT,
            state.prompt,
            PLACEHOLDER,
            lines=0,
            editing=state.editing,
            scroll=state.prompt_scroll,
            height=prompt_height,
            caret=state.caret_pos,
            caret_on=state.caret_on,
            selection=state.selection_rects or (),
        ),
        Row(
            _attach(),
            View(grow=1.0),
            _send(state, pointer),
            align=CENTER,
            height=ACTIONS,
        ),
        gap=tokens.SM,
        pad=INPUT_PAD,
        align=STRETCH,
        style=Style(
            fill=tokens.SURFACE_HOVER if state.drop_target else tokens.SURFACE,
            radius=tokens.RADIUS_SURFACE,
            border=Border(
                1.2 if state.drop_target else 1,
                tokens.TEXT_PRIMARY if state.drop_target else tokens.BORDER_FAINT,
            ),
        ),
    )


def inner_width():
    return WIDTH - 2 * PAD


def thread_height(state, input_height):
    """What is left for the conversation once the chrome has taken its share.

    An empty thread takes nothing. The panel is one fixed size *for a
    conversation* — tracking content reply by reply would walk it up the
    viewport, which is the whole reason for the fixed height — but before there
    is a conversation, holding 246pt of empty open reads as a panel that failed
    to load. It is one step, on the first send, and the panel is anchored at
    its bottom corner, so what grows is the empty half above the prompt.
    """
    if not state.messages:
        return 0.0
    return max(60.0, state.height - 2 * PAD - HEADER - 2 * tokens.LG - input_height)


def build(state, pointer=None, prompt_height=None, thread=None):
    """The whole panel, at a fixed width and whatever height the host allows."""
    width = inner_width()
    prompt_height = prompt_height if prompt_height is not None else 24.0
    ask = (
        ask_card(state.ask, pointer)
        if state.ask is not None and not state.ask.custom
        else None
    )
    prompt = (
        None
        if ask_hides_prompt(state.ask)
        else input_block(state, pointer, prompt_height)
    )
    if thread is None:
        thread = thread_height(
            state,
            0.0
            if prompt is None
            else 2 * INPUT_PAD + prompt_height + tokens.SM + ACTIONS,
        )
    return Column(
        _header(state, pointer),
        _thread(state, width, thread),
        ask,
        prompt,
        gap=tokens.LG,
        align=STRETCH,
        width=WIDTH,
        # No conversation, no height to hold: an empty panel sizes to its own
        # header and prompt rather than to the cap the host allows it.
        height=state.height if state.messages else None,
        pad=PAD,
        style=Style(
            fill=tokens.BACKGROUND,
            radius=tokens.RADIUS_CARD,
            shadows=tokens.CARD_SHADOW,
            clip=True,
        ),
        id=PANEL,
    )
