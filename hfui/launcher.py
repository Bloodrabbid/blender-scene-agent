"""The generation launcher, borrowed from the iPhone's Dynamic Island.

The island is the right precedent for this widget: a small dark capsule that
sits alone at the top of the screen, says almost nothing at rest, and *grows*
when something happens rather than lighting up a second control. Its grammar is
what the tree follows — a leading glyph, a middle that opens, one trailing
indicator — and one rule underneath it: only one thing at a time, and the shape
moves rather than the contents piling up.

So there are no nested cards here and no decorative hover. The capsule is a
single filled shape; everything inside it is type, a glyph and a dot on that
fill. It has three states and animates between all of them:

- **at rest** it is the brand mark and a chevron, about the size of a toolbar
  button, saying only that it is there;
- **while a job runs** it widens and the trailing slot becomes the live status,
  with a breathing dot and the percentage in the label — never a progress bar,
  which is the one thing the island does not do;
- **under the pointer** it opens into a panel of the last few generations with
  a button to the full history, the same way the island trades its compact
  shape for a real surface when you reach for it.

- **while the explorer is open** it is not there at all: the explorer takes the
  bottom of the viewport and carries the island's own header row — mark, status,
  one trailing control — so the contents are found rather than lost.

Every one of those is a width, a height and a corner radius easing to a new
value, plus content cross-fading behind the capsule's own clip.

At rest there is no chevron. The island is a bare circle holding the mark, the
way the phone's is a bare cutout: everything that appears is news, and a glyph
that is always there is not. The affordance is that it reacts — reaching for it
opens it before a click is needed — and the one trailing control the island
ever draws is *View all* inside the panel that reaching for it opened.
"""

from __future__ import annotations

from ..skui import (
    CENTER,
    COVER,
    END,
    STRETCH,
    Border,
    Column,
    Image,
    Override,
    Row,
    Stack,
    Style,
    Text,
    edges,
)
from . import motion as motion_module
from . import tokens

# -- the resting capsule ---------------------------------------------------

HEIGHT = 26.0
MARK = 16.0
# Horizontal breathing room at rest — exactly what centres the mark in a circle
# of ``HEIGHT``. It eases to ``LG`` as the panel opens, and ``EXPANDED_WIDTH``
# is derived from ``LG``, so the tiles land symmetrically.
SIDE = (HEIGHT - MARK) / 2.0

# The two resting widths. At rest the island is a circle and nothing else, so
# the idle width is the height. Reporting is fixed rather than natural, because
# the status counts jobs and reports percent, and a capsule that resized with
# its text would twitch at the top of the viewport.
IDLE_WIDTH = HEIGHT
OPEN_WIDTH = 166.0

# How small it gets before it goes, when the explorer takes over. It shrinks
# back through its resting circle and past it rather than fading in place —
# a widget that dissolves without moving reads as a draw bug.
GONE_SCALE = 0.55

# -- the panel it opens into -----------------------------------------------

TILE = 48.0
RECENTS = 4
EXPANDED_WIDTH = RECENTS * TILE + (RECENTS - 1) * tokens.MD + 2 * tokens.LG
HEADER = 16.0
EXPANDED_HEIGHT = 2 * tokens.LG + HEADER + tokens.MD + TILE
EXPANDED_RADIUS = tokens.RADIUS_CARD

# The widest and tallest the layer ever has to hold. Exactly the open panel,
# with nothing set aside for overshoot, because nothing overshoots any more.
MAX_WIDTH = EXPANDED_WIDTH
MAX_HEIGHT = EXPANDED_HEIGHT

# Short, because a morph costs a full viewport redraw per frame and the island
# does not get to choose how expensive the scene under it is. Measured on a
# real session the pump lands around 50fps, so 0.2s is ten frames — enough for
# a size change to read as a move, and few enough that none of them is missed.
OPEN_DURATION = 0.22
EXPAND_DURATION = 0.20
# Shorter than the explorer's rise, and starting at the same moment: the island
# should be gone before the panel has finished arriving, not racing it.
AWAY_DURATION = 0.14

# How far into a move content starts arriving. Held back far enough that a
# swap is never half-drawn at rest, and no further: at 0.45 the first half of
# every expansion was an empty dark box, which is most of what read as lag —
# the capsule appeared to hesitate and then fill in. The clip is what reveals
# the panel; this only stops the fade from starting before there is room.
FADE = 0.15

RUNNING = "running"
PENDING = "pending"

BRAND_ICON = "brand"

# Published node ids. ``VIEW_ALL`` is the one control that opens the explorer;
# the capsule body answers hover with the panel and swallows its clicks, and a
# recent tile opens the explorer already looking at the record it showed.
VIEW_ALL = "launcher:view-all"


class LauncherState:
    """What the island is saying, and how far open it is being asked to be."""

    def __init__(self):
        # None when nothing is generating.
        self.status = None
        # ``RUNNING`` for work this session is doing, ``PENDING`` for jobs the
        # backend still owes us. The host reports which; the colour is ours.
        self.tone = RUNNING
        # The last status text, kept after ``status`` clears so the collapse can
        # still draw what it is collapsing away from. The island never empties
        # its trailing slot and then shrinks; it does both at once.
        self.last_status = None
        self.last_tone = RUNNING
        # Thumbnail paths for the most recent generations, newest first.
        self.recents = ()
        self.hovered = False
        # The explorer has the viewport. The island stands down rather than
        # sitting above a panel that already says everything it would.
        self.away = False

    def report(self, status, tone):
        self.status = status
        self.tone = tone or RUNNING
        if status:
            self.last_status = status
            self.last_tone = self.tone

    @property
    def key(self):
        return (
            self.status,
            self.tone,
            self.last_status,
            self.last_tone,
            self.recents,
            self.away,
        )


def _fade(value, edge=FADE):
    """0 below ``edge``, ramping to 1 at the end of the move."""
    return min(1.0, max(0.0, (value - edge) / (1.0 - edge)))


def _view_all(expanded):
    """Text only, and it is a real target.

    It exists solely in the open panel. At rest the trailing slot is empty —
    which is what lets one slot mean one thing, after a build where it meant
    "opens the dock", "closes the dock" and "full history" by turns. The
    expand glyph it used to carry went the way of the breathing dot: a second
    mark saying what the words already say.

    A chip with no resting fill: the panel's one control should not read as a
    button sitting on the panel until it is reached for. The surface it gains
    under the pointer is the same one every other chip in the product gains,
    and it is declared here rather than resolved here — see `docs/skui-api.md`.
    """
    if expanded <= 0.0:
        return None
    return Row(
        Text("View all", font=tokens.CAPTION, color=tokens.TEXT_SECONDARY),
        align=CENTER,
        pad=edges(x=tokens.MD, y=tokens.XS),
        style=Style(
            radius=tokens.RADIUS_CHIP,
            opacity=_fade(expanded),
            hover=Override(fill=tokens.SURFACE_HOVER),
            pressed=Override(fill=tokens.SURFACE_PRESSED),
        ),
        id=VIEW_ALL,
    )


def _header(state, opened, expanded):
    """Leading mark, the live status, and — only when open — one control.

    A stack of full-width layers, not a row. The row version moved the label
    with a pair of spacers whose grow weights traded places across the
    expansion — which put the label's position at the mercy of everything else
    in the row, including the *View all* chip that is present-but-invisible at
    every ``expanded > 0``. The label hung left of its resting place for the
    whole collapse and snapped right on the final frame. Layers make each
    element's position its own: the mark is pinned left, the resting status is
    centred on the capsule's own axis (optically — as if the mark were not
    there), the panel title sits against the mark, and the two label positions
    crossfade instead of travelling.

    No dot beside the status. It was a second thing saying what the words
    already say, and animating it cost a 20fps pump for the whole length of a
    job. Running against queued is carried by the ink instead.
    """
    ink = tokens.TEXT_PRIMARY if state.last_tone == RUNNING else tokens.TEXT_SECONDARY
    label = state.last_status if (opened > 0.0 and state.last_status) else None
    layers = [
        # The mark, pinned to the leading edge in every state.
        Row(
            Image(tokens.icon(BRAND_ICON), size=MARK, tint=tokens.BRAND),
            align=CENTER,
            grow=1.0,
        )
    ]
    resting_alpha = _fade(opened) * (1.0 - _fade(expanded))
    if label and resting_alpha > 0.0:
        layers.append(
            Row(
                Text(label, font=tokens.CAPTION, color=ink),
                justify=CENTER,
                align=CENTER,
                grow=1.0,
                style=Style(opacity=resting_alpha),
            )
        )
    if expanded > 0.0:
        # The panel's title: the live status when there is one, or "Recent" —
        # at rest the mark is the whole message and a word beside it would be
        # the thing to read first.
        title = label or "Recent"
        layers.append(
            Row(
                Text(
                    title,
                    font=tokens.CAPTION,
                    color=ink if label else tokens.TEXT_SECONDARY,
                ),
                align=CENTER,
                grow=1.0,
                pad=edges(left=MARK + tokens.MD),
                style=Style(opacity=_fade(expanded)),
            )
        )
        chip = _view_all(expanded)
        if chip is not None:
            layers.append(Row(chip, justify=END, align=CENTER, grow=1.0))
    return Stack(*layers, height=HEADER if expanded > 0.0 else MARK)


def _tile(source, index):
    """One recent generation, and now a target of its own.

    Four tiles under a single hit rect looked interactive and were not; the
    surface publishes a control list, so the id here finally answers a click.
    """
    node_id = f"recent:{index}"
    body = (
        Image(source, fit=COVER, grow=1.0)
        if source
        else Image(tokens.icon("image-sparkle"), size=16, tint=tokens.ICON_SECONDARY)
    )
    return Stack(
        body,
        align=CENTER,
        justify=CENTER,
        size=TILE,
        style=Style(
            fill=tokens.SURFACE,
            radius=tokens.RADIUS_SURFACE,
            clip=True,
            # A tile lifts and gains a ring together, which is more than a
            # fill, so the state is spelled out rather than named as a colour.
            #
            # The explorer's ring, down to the token, because these are the
            # same object in two places and a recent here is a record there.
            # ``outset`` for the reason it is one there too: the fill and an
            # inset border are both painted before the children, so under the
            # ``COVER`` thumbnail that fills a tile with a generation on it
            # neither was ever visible — every tile that had something to show
            # was the one that answered the pointer with nothing. Outside, the
            # ring is on the panel's own fill and reads the same whatever the
            # thumbnail is. It has the room: the stroke reaches ``RING_WIDTH``
            # into the panel's ``LG`` of padding and into the ``MD`` between
            # two tiles, and the capsule's clip is the far side of both.
            hover=Override(
                fill=tokens.SURFACE_HOVER,
                border=Border(
                    tokens.RING_WIDTH, tokens.RING_HOVER, outset=tokens.RING_GAP
                ),
            ),
        ),
        id=node_id,
    )


def _recents(state, expanded):
    """The tile row, or nothing at all.

    ``state.recents`` is empty in this build and the padding below used to turn
    that into four placeholder tiles — a tray of empty frames offering to show
    generations that no longer exist. Nothing to show is drawn as nothing.
    """
    if expanded <= 0.0 or not state.recents:
        return None
    sources = list(state.recents[:RECENTS])
    sources += [None] * (RECENTS - len(sources))
    return Row(
        *[_tile(source, index) for index, source in enumerate(sources)],
        gap=tokens.MD,
        style=Style(opacity=_fade(expanded)),
    )


def aim(state, motion):
    """Point both tracks at the shape the state is asking for.

    Separate from ``shape`` because the pointer handler has to aim before it
    starts the pump: the pump retires when nothing is running, so a track first
    aimed inside the next draw would have nothing driving it.
    """
    # ``ease_out`` on every track, including the ones that used to rubber-band.
    # An overshoot needs frames to read as a spring, and this pump has ten of
    # them: at that density the bulge and the settle are two visible steps, and
    # since the corner radius interpolates alongside the height, the capsule
    # briefly stopped being a capsule on the way through. It read as a wobble
    # rather than as weight.
    motion.to(
        "open",
        1.0 if state.status else 0.0,
        OPEN_DURATION,
        motion_module.ease_out,
    )
    motion.to(
        "expand",
        # Nothing opens under the pointer once the composer has the viewport,
        # or the island would spend its exit animation growing. And nothing
        # opens with no recents either: the panel exists to hold tiles, and an
        # empty one is a hover that grows a box saying "Recent" over nothing.
        1.0 if (state.hovered and state.recents and not state.away) else 0.0,
        EXPAND_DURATION,
        motion_module.ease_out,
    )
    motion.to(
        "away",
        1.0 if state.away else 0.0,
        AWAY_DURATION,
        motion_module.ease_out,
    )


def shape(state, motion):
    """Size, radius and the morph fractions, all from one place."""
    aim(state, motion)
    opened = motion.value("open", 1.0 if state.status else 0.0)
    expanded = motion.value("expand", 1.0 if state.hovered else 0.0)
    gone = motion.value("away", 1.0 if state.away else 0.0)
    resting = motion_module.mix(IDLE_WIDTH, OPEN_WIDTH, opened)
    width = motion_module.mix(resting, EXPANDED_WIDTH, expanded)
    height = motion_module.mix(HEIGHT, EXPANDED_HEIGHT, expanded)
    radius = motion_module.mix(HEIGHT / 2.0, EXPANDED_RADIUS, expanded)
    if gone > 0.0:
        # Shrink through the resting circle and past it, holding the capsule
        # shape the whole way, so the exit is a move rather than a dissolve.
        target = HEIGHT * GONE_SCALE
        width = motion_module.mix(width, target, gone)
        height = motion_module.mix(height, target, gone)
        radius = motion_module.mix(radius, target / 2.0, gone)
    return width, height, radius, opened, expanded, gone


def build(state, motion, node_id="launcher"):
    """The whole island. The capsule is a target, and so is everything on it.

    No pointer reaches this module. Every state the panel's controls have is
    on their ``Style``, and the surface resolves it at paint.
    """
    width, height, radius, opened, expanded, gone = shape(state, motion)
    # Content is described from the clamped fractions, never the raw ones. The
    # shell may be told to go past its target one day; a grow weight of
    # ``1 - expanded`` going negative would shove the header's contents
    # sideways at the very end of a move, which is the last place to put one.
    settled = min(1.0, max(0.0, expanded))
    reported = min(1.0, max(0.0, opened))
    return Column(
        _header(state, reported, settled),
        _recents(state, settled),
        # The header has to span the capsule, or the spacer inside it has no
        # width to take and the trailing glyph collapses against the mark.
        align=STRETCH,
        gap=tokens.MD,
        width=width,
        height=height,
        pad=edges(
            # The horizontal inset opens with *either* move. At rest it is
            # exactly what centres the mark in a circle; the moment the capsule
            # carries text pinned to both ends, six pixels reads as the label
            # falling off the edge.
            x=motion_module.mix(SIDE, tokens.LG, max(reported, settled)),
            y=motion_module.mix((HEIGHT - MARK) / 2.0, tokens.LG, settled),
        ),
        style=Style(
            fill=tokens.BACKGROUND,
            radius=radius,
            opacity=1.0 - gone,
            # The capsule masks its own contents, so the panel is revealed by
            # the height growing past it rather than by anything being laid
            # out twice.
            clip=True,
        ),
        # No id once it is leaving: the rect keeps shrinking for a few frames
        # after the explorer has the viewport, and a click landing on it then
        # would reopen what the user just closed.
        id=None if gone > 0.0 else node_id,
    )
