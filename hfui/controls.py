"""The widget vocabulary, composed from the three skui primitives.

Every control is a function returning a node, so a surface is written as an
expression and nothing is instantiated, registered or mutated. Interaction is
*declared*: a control names its hover and pressed appearance on the ``Style``
and never asks what the pointer is doing, because whether that swap is rastered
into the card or drawn on a plane above it is the surface's decision and not
the widget's. Three controls here still take a ``Pointer``, and they are the
three whose state changes something a ``Style`` cannot hold — ``generate`` and
``generate_round`` shift their padding to travel on a press, ``thumbnail``
grows a remove badge.

Shapes follow the prompt box in Figma (node 13787:64706): a floating card at
radius 24 holding a 5%-white surface at radius 20, chips at 28 high and radius
8, and a Generate button whose bottom bevel is a darker rectangle showing
through under the face.
"""

from __future__ import annotations

from ..skui import (
    BETWEEN,
    CENTER,
    COLUMN,
    COVER,
    END,
    START,
    STRETCH,
    Border,
    Column,
    Image,
    Override,
    Row,
    Stack,
    Style,
    Text,
    View,
    edges,
)
from . import tokens


def card(*children, radius=tokens.RADIUS_CARD, pad=tokens.XS, **layout):
    """The floating shell: opaque, rounded, lifted off the viewport.

    Extra layout arguments pass through, which is how a caller sizes the shell
    without reaching for ``replace``.
    """
    return Row(
        *children,
        gap=tokens.XS,
        pad=pad,
        style=Style(fill=tokens.BACKGROUND, radius=radius, shadows=tokens.CARD_SHADOW),
        **layout,
    )


def surface(
    *children, pad=tokens.LG, gap=tokens.LG, direction=COLUMN, armed=False, **layout
):
    """The inset panel inside a card.

    ``armed`` is a drop target: the prompt is about to receive a file, so the
    fill lifts a step and a ring appears. Not brand green — that fill is
    Generate's.
    """
    return View(
        *children,
        direction=direction,
        gap=gap,
        pad=pad,
        style=Style(
            fill=tokens.SURFACE_HOVER if armed else tokens.SURFACE,
            radius=tokens.RADIUS_SURFACE,
            border=Border(1.2, tokens.TEXT_PRIMARY) if armed else None,
        ),
        **layout,
    )


def _icon(name, size=13, color=tokens.ICON_PRIMARY):
    return Image(tokens.icon(name), size=size, tint=color)


def chip(
    node_id,
    label=None,
    icon=None,
    trailing=None,
    active=False,
    height=22.0,
    muted=False,
):
    """A small pill: optional icon, optional label, optional trailing icon.

    ``muted`` is the read-only variant — a cost or a duration the composer is
    reporting rather than offering. It keeps the chip shape so the toolbar
    stays one rhythm, but drops the hover response and the bright ink.
    """
    ink = tokens.TEXT_SECONDARY if muted and not active else tokens.TEXT_PRIMARY
    base = tokens.SELECTED if active else tokens.SURFACE
    hover = (
        None
        if muted
        else Override(fill=tokens.SELECTED_HOVER if active else tokens.SURFACE_HOVER)
    )
    press = (
        None
        if muted
        else Override(
            fill=tokens.SELECTED_PRESSED if active else tokens.SURFACE_PRESSED
        )
    )

    return Row(
        _icon(icon, 13, ink) if icon else None,
        Row(Text(label, font=tokens.CAPTION, color=ink), pad=edges(x=tokens.XS))
        if label
        else None,
        _icon(trailing, 13, ink) if trailing else None,
        gap=tokens.XS,
        align=CENTER,
        justify=CENTER,
        pad=edges(x=tokens.MD, y=tokens.XS),
        height=height,
        style=Style(fill=base, radius=tokens.RADIUS_CHIP, hover=hover, pressed=press),
        # A muted chip publishes no id, so the modal never sees a hit for it.
        id=None if muted else node_id,
    )


def toggle_chip(node_id, label, icon=None, active=False, height=22.0):
    """A chip with an explicit switch, so on/off is not encoded by tint alone."""
    ink = tokens.TEXT_PRIMARY
    track = Row(
        View(
            size=8.0,
            style=Style(
                fill=tokens.ICON_PRIMARY if active else tokens.ICON_SECONDARY,
                radius=4.0,
            ),
        ),
        align=CENTER,
        justify=END if active else START,
        width=20.0,
        height=12.0,
        pad=2.0,
        style=Style(
            fill=tokens.SELECTED_PRESSED if active else tokens.SURFACE_HOVER,
            radius=6.0,
        ),
    )
    return Row(
        _icon(icon, 13, ink) if icon else None,
        Row(Text(label, font=tokens.CAPTION, color=ink), pad=edges(x=tokens.XS)),
        track,
        gap=tokens.XS,
        align=CENTER,
        justify=CENTER,
        pad=edges(x=tokens.MD, y=tokens.XS),
        height=height,
        style=Style(
            fill=tokens.SELECTED if active else tokens.SURFACE,
            radius=tokens.RADIUS_CHIP,
            hover=Override(
                fill=tokens.SELECTED_HOVER if active else tokens.SURFACE_HOVER
            ),
            pressed=Override(
                fill=tokens.SELECTED_PRESSED if active else tokens.SURFACE_PRESSED
            ),
        ),
        id=node_id,
    )


MENTION_HEIGHT = 18.0
MENTION_CLOSE = 12.0


def mention(label, icon=None, node_id=None, pointer=None, dismiss=None):
    """Compact entity tag embedded in a prompt field.

    ``dismiss`` is its own control id, so a parent can keep owning the rest of
    the tag — the object-context group still drags from the pill, not the ✕.
    """
    interactive = node_id is not None
    avatar = Stack(
        _icon(icon, 10, tokens.ICON_PRIMARY) if icon else None,
        align=CENTER,
        justify=CENTER,
        size=14.0,
        style=Style(fill=tokens.SCRIM, radius=tokens.RADIUS_ROUND),
    )
    close = (
        Row(
            _icon("close", 8, tokens.ICON_PRIMARY),
            align=CENTER,
            justify=CENTER,
            size=MENTION_CLOSE,
            style=Style(
                radius=MENTION_CLOSE / 2,
                hover=Override(fill=tokens.SURFACE_HOVER),
                pressed=Override(fill=tokens.SURFACE_PRESSED),
            ),
            id=dismiss,
        )
        if dismiss
        else None
    )
    return Row(
        avatar,
        Text(str(label), font=tokens.CAPTION, color=tokens.TEXT_PRIMARY),
        close,
        gap=tokens.XS,
        align=CENTER,
        pad=edges(left=2.0, right=2.0 if dismiss else tokens.SM, y=2.0),
        height=MENTION_HEIGHT,
        style=Style(
            fill=tokens.SURFACE_HOVER,
            radius=tokens.RADIUS_ROUND,
            hover=Override(fill=tokens.SURFACE_PRESSED) if interactive else None,
            pressed=Override(fill=tokens.SELECTED) if interactive else None,
        ),
        id=node_id,
    )


def icon_chip(node_id, icon, active=False, size=22.0):
    """A square chip holding a single icon."""
    base = tokens.SELECTED if active else tokens.SURFACE
    hover = Override(fill=tokens.SELECTED_HOVER if active else tokens.SURFACE_HOVER)
    press = Override(fill=tokens.SELECTED_PRESSED if active else tokens.SURFACE_PRESSED)
    return Row(
        _icon(icon, 13, tokens.ICON_PRIMARY),
        align=CENTER,
        justify=CENTER,
        size=size,
        style=Style(fill=base, radius=tokens.RADIUS_CHIP, hover=hover, pressed=press),
        id=node_id,
    )


THUMB_SIZE = 45.0
THUMB_RADIUS = 11.0
THUMB_REMOVE = 14.0


def thumbnail(
    node_id, source=None, icon="image-sparkle", pointer=None, size=THUMB_SIZE
):
    """One attached reference, shown as itself.

    ``source`` is a path Skia can decode; anything it cannot — a video, a
    missing file — falls back to ``icon`` on the empty tile, because a blank
    square reads as a bug and a filename reads as a list.

    The remove button appears when the pointer is over the tile *or* over the
    button, since the button sits on top of the tile and taking the tile's
    hover away would make it vanish on approach.
    """
    remove = f"{node_id}:remove"
    reachable = pointer is not None and (
        pointer.state(node_id) is not None or pointer.state(remove) is not None
    )
    body = (
        Image(source, fit=COVER, grow=1.0)
        if source
        else Stack(
            _icon(icon, 16, tokens.ICON_SECONDARY),
            align=CENTER,
            justify=CENTER,
            grow=1.0,
        )
    )
    close = None
    if reachable:
        close = Stack(
            Stack(
                _icon("close", 10, tokens.TEXT_PRIMARY),
                align=CENTER,
                justify=CENTER,
                size=THUMB_REMOVE,
                style=Style(
                    fill=tokens.SCRIM,
                    radius=THUMB_REMOVE / 2,
                    hover=Override(fill=tokens.SCRIM_HOVER),
                    pressed=Override(fill=tokens.SCRIM_PRESSED),
                ),
                id=remove,
            ),
            align=START,
            justify=END,
            pad=tokens.XS,
            grow=1.0,
        )
    return Stack(
        body,
        close,
        align=STRETCH,
        size=size,
        style=Style(fill=tokens.SURFACE, radius=THUMB_RADIUS, clip=True),
        id=node_id,
    )


# The floor, not the width: a long title widens the chip rather than losing
# its value off the right edge. "Target Polycount 30000" needs 160-odd px.
SLIDER_WIDTH = 106.0


def slider(node_id, label, value, fraction, height=22.0, icon=None):
    """A chip whose fill *is* its value: press or drag anywhere in it to set.

    A bounded number used to be a stepper, which meant thirteen taps to cross a
    duration. The fill runs the chip's whole height rather than sitting in a
    thin track, so the value is readable at a glance from across the viewport.

    Both the track and the chip under it lift together, and neither names the
    pointer: the id is on the outer stack and a declared state covers its whole
    subtree, so the bar inside answers the same hover the chip does.
    """
    fraction = max(0.0, min(1.0, float(fraction)))
    bar = Row(
        # Weights rather than a measured width, because the chip sizes itself
        # to its title. No radius of its own either: the container's clip
        # rounds the left end and leaves the right a clean cut, which is what
        # a fill should look like.
        View(
            grow=fraction,
            style=Style(
                fill=tokens.TRACK_FILL, hover=Override(fill=tokens.TRACK_FILL_HOVER)
            ),
        )
        if fraction > 0.0
        else None,
        View(grow=1.0 - fraction) if fraction < 1.0 else None,
        align=STRETCH,
        grow=1.0,
    )
    content = Row(
        Row(
            _icon(icon, 13, tokens.ICON_SECONDARY) if icon else None,
            Row(
                Text(label, font=tokens.CAPTION, color=tokens.TEXT_PRIMARY),
                pad=edges(x=tokens.XS),
            ),
            gap=tokens.XS,
            align=CENTER,
        ),
        Text(value, font=tokens.CAPTION, color=tokens.TEXT_PRIMARY),
        align=CENTER,
        justify=BETWEEN,
        pad=edges(x=tokens.MD),
        grow=1.0,
    )
    return Stack(
        bar,
        content,
        align=STRETCH,
        height=height,
        min_width=SLIDER_WIDTH,
        style=Style(
            fill=tokens.SURFACE,
            radius=tokens.RADIUS_CHIP,
            clip=True,
            hover=Override(fill=tokens.SURFACE_HOVER),
        ),
        id=node_id,
    )


def mode_tab(node_id, icon, label, active=False, icon_size=14.0):
    """One entry in the mode bar: icon beside a caption, as a pill.

    Figma 4146:7944 — horizontal tabs above the prompt, not a vertical rail.
    """
    ink = tokens.TEXT_PRIMARY if active else tokens.TEXT_SECONDARY
    return Row(
        View(
            _icon(icon, icon_size, ink),
            width=20.0,
            height=20.0,
            align=CENTER,
            justify=CENTER,
        ),
        Row(Text(label, font=tokens.CAPTION, color=ink), pad=edges(x=tokens.XS)),
        align=CENTER,
        justify=CENTER,
        pad=edges(left=tokens.XS, right=tokens.MD, y=tokens.XS),
        style=Style(
            fill=tokens.SURFACE_HOVER if active else None,
            radius=14.0,
            border=Border(1, tokens.BORDER_SUBTLE),
            hover=Override(fill=tokens.SURFACE_HOVER),
            pressed=Override(fill=tokens.SURFACE_PRESSED),
        ),
        id=node_id,
    )


# Room for the widest two-digit cost, so the button holds still. Its natural
# width moves with what it is showing — 85 before an estimate lands, 105 at one
# digit, 108 to 112 across the two-digit range, because the brand face is not
# tabular — and the prompt wraps at whatever is left over, so every one of those
# steps re-flowed the text beside it. Three digits and up still extend it: a
# cost you cannot read is worse than a seam. Re-derive as the widest
# ``generate("x", "Generate", str(n))`` over n in 10..99, which is "40".
GENERATE_MIN_WIDTH = 112.0


def stop_glyph(ink, size=8.0):
    """The stop mark: a square, not an icon.

    At the 8–10pt the send disc can spare, a stop SVG turns to mush. A rounded
    square is the shape every chat uses for "cut this turn", and it stays
    crisp at both scales.
    """
    return View(size=size, style=Style(fill=ink, radius=1.5))


def generate(
    node_id,
    label="Generate",
    credits=None,
    enabled=True,
    pointer=None,
    height=None,
    icon="sparkle",
    tone="brand",
    leading_icon=None,
):
    """The brand button.

    The bevel is a darker rounded rectangle that the face sits on top of, which
    also gives the press state somewhere to travel. No drop shadow — the brand
    fill already carries the weight.

    ``credits`` is the settled cost estimate. It rides on the button because
    that is the moment it matters — anything unsettled is reported by the
    toolbar's status chip instead, so the button never shows a number that is
    about to change.

    ``tone="stop"`` is the live-turn swap: same slot, white face, no credits.
    Brand green is Generate's alone.
    """
    state = pointer.state(node_id) if pointer else None
    pressed = enabled and state == "pressed"
    stopping = tone == "stop"
    if stopping:
        face_fill = tokens.TEXT_PRIMARY
        ink = tokens.BACKGROUND
    else:
        face_fill = tokens.BRAND if enabled else tokens.BRAND_DIM
        if enabled and state == "hover":
            face_fill = tokens.BRAND
        ink = tokens.ON_BRAND
    trailing = None
    if stopping:
        trailing = stop_glyph(ink, 8.0)
    elif credits:
        trailing = Row(
            _icon(icon, 10, ink),
            Text(str(credits), font=tokens.BRAND_LABEL, color=ink),
            gap=2,
            align=CENTER,
        )

    leading = _icon(leading_icon, 14, ink) if leading_icon else None
    face = Row(
        leading,
        Text(label.upper(), font=tokens.BRAND_LABEL, color=ink),
        trailing,
        gap=tokens.XS,
        align=CENTER,
        justify=CENTER,
        grow=1.0,
        pad=edges(x=24),
        style=Style(
            fill=face_fill,
            radius=tokens.RADIUS_BUTTON,
            border=Border(1.2, tokens.BORDER_FAINT),
        ),
    )

    return Column(
        face,
        align=STRETCH,
        # The composer passes ``GENERATE_HEIGHT``; ``None`` hands the choice to
        # a stretching parent, which is only useful to a caller that wants the
        # button to span whatever box it is given.
        height=height,
        min_width=GENERATE_MIN_WIDTH,
        pad=edges(top=2 if pressed else 0, bottom=1 if pressed else 3),
        style=Style(
            fill=(
                tokens.ICON_SECONDARY
                if stopping
                else (tokens.BRAND_BEVEL if enabled else tokens.BRAND_DIM)
            ),
            radius=tokens.RADIUS_BUTTON,
        ),
        id=node_id,
    )


MENU_ITEM = 27.0
MENU_GAP = 2.0
# A name the chip no longer carries, sitting above the rows. Shorter than a
# row: it is a heading, not a choice.
MENU_TITLE = 22.0
# The tallest a select is allowed to get, whatever the catalog hands it. The
# video model list is twenty-seven entries — seven hundred logical pixels — and
# an unbounded popup ran off the top of the viewport with the rows the user
# most likely wanted somewhere above the window.
MENU_MAX_HEIGHT = 256.0
MENU_TRACK = 2.5
MENU_THUMB_MIN = 19.0


def menu_rows_height(count):
    """The rows' own height, before the popup's padding."""
    return count * MENU_ITEM + MENU_GAP * max(0, count - 1)


def menu_chrome(title=None):
    """Padding, plus a title row when the chip no longer carries the name."""
    extra = 2 * tokens.XS
    if title:
        extra += MENU_TITLE + MENU_GAP
    return extra


def menu_height(count, limit=None, title=None):
    """So a caller can place a menu before it is laid out."""
    height = menu_rows_height(count) + menu_chrome(title)
    return height if limit is None else min(height, float(limit))


def menu_viewport(limit, title=None):
    """The scrolling height a popup capped at ``limit`` leaves its rows."""
    return max(MENU_ITEM, float(limit) - menu_chrome(title))


def _menu_thumb(viewport, content, scroll):
    """The scroll position, as a rule down the right edge.

    A capped list has to say that it is capped: without an indicator the popup
    reads as the whole catalog and the rows past the cut simply do not exist.
    """
    span = max(1.0, content)
    height = max(MENU_THUMB_MIN, viewport * (viewport / span))
    travel = max(0.0, viewport - height)
    overflow = max(1.0, content - viewport)
    return Column(
        View(
            width=MENU_TRACK,
            height=height,
            dy=travel * min(1.0, scroll / overflow),
            style=Style(fill=tokens.SELECTED, radius=MENU_TRACK * 0.5),
        ),
        align=END,
        justify=START,
        grow=1.0,
    )


def menu(node_id, items, selected=None, width=208.0, scroll=0.0, limit=None, title=None):
    """A select popup. Items are ``(label, icon[, kind, detail, marker])``.

    ``detail`` is right-aligned secondary text. ``marker`` defaults true and
    controls the selected row's green dot; the selected fill and ink remain.

    Rows are addressed by position — ``menu:model:3`` — rather than by value,
    because the values are catalog data: an aspect ratio is "16:9" and a node
    id split on colons would read it as two fields.

    ``kind="heading"`` draws a non-interactive group label at the same row
    height, keeping scrolling and selected-row placement index-based.

    ``title`` is the field name when the chip shows only the value. It is not
    a row: it publishes no id, so a click on it closes the select rather than
    picking anything.

    ``limit`` caps the popup's height; past it the rows become a scrolling
    viewport offset by ``scroll``, both in logical pixels and both owned by the
    host, exactly like the chip strip's.

    Drawn as a sibling layer above the surface rather than inside it, so it can
    reach past the composer's edges — see ``composer.build``.
    """
    rows = []
    for index, entry in enumerate(items):
        label, icon = entry[:2]
        kind = entry[2] if len(entry) > 2 else None
        detail = entry[3] if len(entry) > 3 else None
        marker = entry[4] if len(entry) > 4 else True
        if kind == "heading":
            rows.append(
                Row(
                    Text(
                        str(label),
                        font=tokens.CAPTION,
                        color=tokens.TEXT_SECONDARY,
                        grow=1.0,
                    ),
                    align=CENTER,
                    height=MENU_ITEM,
                    pad=edges(x=tokens.MD),
                )
            )
            continue
        active = index == selected
        target = f"{node_id}:{index}"
        rows.append(
            Row(
                _icon(
                    icon, 16, tokens.ICON_PRIMARY if active else tokens.ICON_SECONDARY
                )
                if icon
                else None,
                Text(
                    label,
                    font=tokens.CAPTION,
                    color=tokens.TEXT_PRIMARY if active else tokens.TEXT_SECONDARY,
                    grow=1.0,
                ),
                Text(
                    str(detail),
                    font=tokens.CAPTION,
                    color=tokens.TEXT_SECONDARY,
                )
                if detail
                else None,
                View(size=5, style=Style(fill=tokens.BRAND, radius=2.5))
                if active and marker
                else None,
                gap=tokens.SM,
                align=CENTER,
                height=MENU_ITEM,
                pad=edges(x=tokens.MD),
                style=Style(
                    fill=tokens.SURFACE if active else None,
                    radius=tokens.RADIUS_CHIP,
                    hover=Override(fill=tokens.SURFACE_HOVER),
                    pressed=Override(fill=tokens.SURFACE_PRESSED),
                ),
                id=target,
            )
        )

    inner = max(MENU_ITEM, width - 2 * tokens.XS)
    # An explicit width rather than ``grow``: a stack sizes a growing child to
    # its whole box in *both* axes, which would squash the rows back into the
    # viewport they are meant to overflow.
    content = menu_rows_height(len(rows))
    viewport = menu_viewport(limit, title) if limit is not None else content
    if content > viewport + 0.5:
        scroll = max(0.0, min(content - viewport, float(scroll or 0.0)))
        body = Stack(
            Column(*rows, gap=MENU_GAP, align=STRETCH, width=inner, dy=-scroll),
            _menu_thumb(viewport, content, scroll),
            align=START,
            justify=START,
            width=inner,
            height=viewport,
            style=Style(clip=True),
        )
    else:
        body = Column(*rows, gap=MENU_GAP, align=STRETCH, width=inner)

    heading = (
        Row(
            Text(title, font=tokens.CAPTION, color=tokens.TEXT_SECONDARY, grow=1.0),
            align=CENTER,
            height=MENU_TITLE,
            pad=edges(x=tokens.MD),
        )
        if title
        else None
    )

    return Column(
        heading,
        body,
        gap=MENU_GAP if heading else 0.0,
        align=STRETCH,
        pad=tokens.XS,
        width=width,
        style=Style(
            fill=tokens.BACKGROUND,
            radius=tokens.RADIUS_BUTTON,
            border=Border(1, tokens.SURFACE),
            shadows=tokens.CARD_SHADOW,
        ),
        id=node_id,
    )


def generate_round(
    node_id,
    enabled=True,
    pointer=None,
    size=32.0,
    icon="sparkle",
    tone="brand",
):
    """The collapsed composer's submit: a lime disc inside a faint ring.

    ``tone="stop"`` swaps the sparkle for a square on a white disc — same
    slot, the live-turn cut. Brand green stays Generate's.
    """
    state = pointer.state(node_id) if pointer else None
    pressed = enabled and state == "pressed"
    stopping = tone == "stop"
    mark = (
        stop_glyph(tokens.BACKGROUND, 8.0)
        if stopping
        else _icon(icon, 13, tokens.ON_BRAND)
    )
    face = Row(
        mark,
        align=CENTER,
        justify=CENTER,
        grow=1.0,
        style=Style(
            fill=(
                tokens.TEXT_PRIMARY
                if stopping
                else (tokens.BRAND if enabled else tokens.BRAND_DIM)
            ),
            radius=tokens.RADIUS_ROUND,
            border=Border(1.2, tokens.BORDER_FAINT),
        ),
    )
    return Column(
        face,
        align=STRETCH,
        size=size,
        pad=edges(top=3 if pressed else 2, bottom=1 if pressed else 2),
        style=Style(fill=tokens.SURFACE, radius=tokens.RADIUS_ROUND),
        id=node_id,
    )


PROMPT_PAD_X = 4.0
PROMPT_PAD_Y = 4.0
CARET_WIDTH = 1.2


def prompt_text(value, placeholder):
    """Exactly the string the field draws — the caret is not part of it.

    Splicing a caret glyph in used to shift every character after it by that
    glyph's width and could re-wrap the last word, so the text jittered on each
    blink and slid sideways as the caret moved. Whatever sizes the field has to
    agree with what gets drawn, so both go through here.
    """
    return value or placeholder


def prompt_field(
    node_id,
    value,
    placeholder,
    lines=3,
    font=None,
    editing=False,
    scroll=0.0,
    height=None,
    caret=None,
    caret_on=True,
    selection=(),
    mentions=(),
    placeholder_ink=False,
    dimmed=False,
):
    """The text area. Text hangs from the top.

    The caret is a drawn rule positioned by ``caret`` — ``(x, y, height)`` in
    the text's own space, measured by the host, which is the only place that
    has a font book. It lives inside the scrolled body so it tracks the text
    without a second offset, and it is placed with ``dx``/``dy`` so it can
    never influence the layout it is pointing into. ``selection`` is the same
    idea for the highlight: one rect per visual line, painted under the glyphs.

    When ``height`` is set the field is a clipped viewport: the full string is
    laid out (``lines=0``) and shifted by ``-scroll`` so the host can page
    through it without growing the card past its max.

    ``dimmed`` is for the collapsed pill: prompt and placeholder both sit at
    placeholder opacity so the pill reads quieter than the open card.
    """
    showing = prompt_text(value, placeholder)
    if (dimmed or placeholder_ink) and not editing:
        ink = tokens.PLACEHOLDER
    else:
        ink = tokens.TEXT_PRIMARY if (value or editing) else tokens.PLACEHOLDER
    # A capped viewport must measure every line; the single-line collapsed
    # pill still truncates with an ellipsis.
    capped = height is not None
    text = Text(
        showing,
        font=font or tokens.BODY,
        color=ink,
        lines=0 if capped else lines,
        ellipsis=not capped,
    )
    inner = [
        View(
            width=float(width),
            height=float(rect_height),
            dx=float(x),
            dy=float(y),
            style=Style(fill=tokens.SELECTION, radius=2.0),
        )
        for x, y, width, rect_height in (selection or ())
    ]
    inner.append(text)
    inner.extend(mentions or ())
    if editing and caret_on and caret is not None:
        caret_x, caret_y, caret_h = caret
        inner.append(
            View(
                width=CARET_WIDTH,
                height=float(caret_h),
                dx=float(caret_x),
                dy=float(caret_y),
                style=Style(fill=tokens.TEXT_PRIMARY, radius=CARET_WIDTH / 2.0),
            )
        )
    return Stack(
        Stack(*inner, align=START, dy=-max(0.0, float(scroll or 0.0))),
        align=START,
        pad=edges(x=PROMPT_PAD_X, y=PROMPT_PAD_Y),
        height=float(height) if capped else None,
        grow=1.0,
        # Clip, not just wrap: a single glyph wider than the field, or a wrap
        # bug, must never paint across the Generate button next to us. Also
        # what makes scroll a viewport instead of an overflow.
        style=Style(clip=True),
        id=node_id,
    )
