"""The three render primitives and the values they are described with.

A tree is immutable data, described with named arguments:

    Row(
        Image(logo, size=16),
        Text("Recraft V4.1", font=INTER_12),
        gap=4, pad=edges(x=8, y=4), height=32,
        style=Style(fill=rgba(1, 1, 1, 0.04), radius=8),
        id="model",
    )

Nodes are hashable, so a whole tree hashes structurally — that is what lets a
cached raster be reused when nothing about the description changed.

**Layout is flat, styling is a value.** The two have different lifetimes: how a
node sits among its siblings is always local to that node, while how a
rectangle is painted is the part worth naming once and sharing. So layout
arrives as keyword arguments and paint arrives as a ``Style``, which a design
system can hand out as a constant and a pointer can swap wholesale for hover.

There are no chained builders. Every one of them copied the node, and a control
averaged four and a half copies to describe one rectangle; naming the arguments
makes it one. It also lets a field and its argument share a name, which a
method never could — ``color`` is the field *and* the argument, where before it
was ``ink`` stored behind a ``.color()`` setter.

``Row``, ``Column`` and ``Stack`` are functions rather than subclasses on
purpose: ``solve`` and ``paint`` dispatch on ``isinstance(node, View)``, so a
subclass would make the layout direction part of the type and a stale class
identity after a reload would silently stop matching.

This module knows nothing about Skia, Blender, interaction or tokens.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

ROW = "row"
COLUMN = "column"
STACK = "stack"  # children share the same box, later ones on top

START = "start"
CENTER = "center"
END = "end"
BETWEEN = "between"

FILL = "fill"
CONTAIN = "contain"
COVER = "cover"


def rgba(red, green, blue, alpha=1.0):
    return (float(red), float(green), float(blue), float(alpha))


def hexa(value, alpha=1.0):
    """``hexa("#D1FE17")`` — the form the Figma tokens come in."""
    value = value.lstrip("#")
    channels = tuple(int(value[index : index + 2], 16) / 255.0 for index in (0, 2, 4))
    return (*channels, float(alpha))


TRANSPARENT = rgba(0, 0, 0, 0)
WHITE = rgba(1, 1, 1)
BLACK = rgba(0, 0, 0)


@dataclass(frozen=True)
class Gradient:
    """Linear gradient. ``angle`` in degrees; 90 runs top to bottom."""

    stops: tuple = ()
    angle: float = 90.0
    blend: str | None = None


@dataclass(frozen=True)
class Shadow:
    dx: float = 0.0
    dy: float = 0.0
    blur: float = 0.0
    color: tuple = (0.0, 0.0, 0.0, 0.25)


@dataclass(frozen=True)
class Border:
    """A stroke on the box's edge, inside it by default.

    ``outset`` is the gap between the box and the stroke's inner edge, and
    turning it on moves the whole stroke outside — a CSS ``outline`` rather
    than a CSS ``border``. It is what a ring around a picture wants: drawn
    inside, a ring is a ring over the top of the content, and its contrast
    then depends on whatever the content happens to be.

    Like a shadow, an outset stroke takes no part in layout and is not in the
    box's measured size. The caller leaves it room.
    """

    width: float = 0.0
    color: tuple = TRANSPARENT
    outset: float | None = None


def stroke_box(x, y, width, height, radius, border):
    """The rounded rect a ``border`` is stroked on, for the box it belongs to.

    Skia centres a stroke on its path, so a border that is to land *inside* the
    box is drawn on a rect inset by half its width — and one that is to land
    outside on a rect grown by the gap plus that same half. One function
    because two places stroke these: ``paint``, and the explorer's hover plane,
    which reads the very same declarations without going through it.
    """
    offset = (
        border.width * 0.5
        if border.outset is None
        else -(border.outset + border.width * 0.5)
    )
    return (
        x + offset,
        y + offset,
        max(0.0, width - 2.0 * offset),
        max(0.0, height - 2.0 * offset),
        max(0.0, radius - offset),
    )


@dataclass(frozen=True)
class Tail:
    """A pointer on the bottom edge, drawn as part of the box's own outline.

    It makes a box a callout. It is a property of the box rather than a child
    node for the same reason a border is: it has to be filled and stroked with
    the single outline, or the join draws as a line across the mouth of the
    tail and the tail's own edges come out bare.

    Like a shadow, it is drawn **outside** the box and takes no part in layout
    — a callout is `height` tall and points `height + tail.height` down. The
    caller places it.

    ``offset`` is the distance from the ``align`` edge to the tail's *centre*,
    which is what lets a callout sized by its own text aim at something it
    never measures.
    """

    width: float = 12.0
    height: float = 6.0
    offset: float = 0.0
    align: str = CENTER


@dataclass(frozen=True)
class Font:
    family: str = "Inter"
    size: float = 12.0
    weight: int = 400
    tracking: float = 0.0
    line_height: float | None = None


DEFAULT_FONT = Font()


@dataclass(frozen=True)
class Edges:
    top: float = 0.0
    right: float = 0.0
    bottom: float = 0.0
    left: float = 0.0

    @property
    def x(self):
        return self.left + self.right

    @property
    def y(self):
        return self.top + self.bottom


NO_EDGES = Edges()


def edges(value=None, *, x=None, y=None, top=None, right=None, bottom=None, left=None):
    """CSS-ish shorthand: ``edges(8)``, ``edges(x=8, y=4)``, ``edges(left=16)``."""
    base = 0.0 if value is None else float(value)
    horizontal = base if x is None else float(x)
    vertical = base if y is None else float(y)
    return Edges(
        top=vertical if top is None else float(top),
        right=horizontal if right is None else float(right),
        bottom=vertical if bottom is None else float(bottom),
        left=horizontal if left is None else float(left),
    )


def _pad(value):
    """``pad=`` takes a number, an ``Edges``, or nothing."""
    if value is None:
        return NO_EDGES
    if isinstance(value, Edges):
        return value
    return edges(value)


HOVER = "hover"
PRESSED = "pressed"


class _Unset:
    """Not "no value" but "no answer" — the field was never mentioned."""

    __slots__ = ()

    def __repr__(self):
        return "unset"


UNSET = _Unset()


class Style(NamedTuple):
    """How a rectangle is painted, resting and under the pointer. ``View`` only.

    Worth holding as a value rather than spreading over the node's arguments:
    it is the part a design system names once (``tokens.CHIP``) and hands to
    every chip, and the part a pointer swaps whole for hover rather than
    picking a colour out of.

    **A widget declares its states; it does not resolve them.** ``hover`` and
    ``pressed`` are ``Override`` values — the same fields again, defaulting
    to ``UNSET``, so a state names only what is *different* about this box
    under the pointer and keeps the rest — and *when* they apply is the
    surface's business: one that rasters hover into the card hands
    ``paint`` a state lookup and gets the swap for free, while one that cannot
    afford to — the explorer re-rasters ~19 MB per tile boundary otherwise —
    reads the pair off the node and washes the difference over the top. Both
    read one declaration, which is the point: hover used to be resolved inside
    the widget *and* re-derived outside it from the node id.

    A ``NamedTuple`` rather than a frozen dataclass, and the reason is the
    frame cache. Both give an immutable value with named fields and a
    constructor that rejects a typo; the difference is that this one is a tuple
    underneath, so building it is 180ns against 556 and hashing it is 44ns
    against 180. A composer frame builds one of these per styled node and then
    hashes the whole tree to decide whether to re-raster, so both numbers are
    paid on every frame the surface is on screen.
    """

    fill: object = None
    radius: float = 0.0
    border: Border | None = None
    tail: Tail | None = None
    shadows: tuple = ()
    opacity: float = 1.0
    clip: bool = False
    hover: object = None
    pressed: object = None


NO_STYLE = Style()


class Override(NamedTuple):
    """What a state changes: ``Override(fill=X)``, ``Override(fill=X, border=B)``.

    ``Style``'s visual fields again, defaulting to ``UNSET`` instead of to a
    real value — which is the whole trick. ``Style`` cannot serve as its own
    delta because its defaults *are* values: ``Style(fill=X)`` says radius 0,
    opacity 1 and clip off just as loudly as it says the fill, so applying one
    to a chip would quietly square its corners. Every field here starts as *no
    answer*, so an override carries only what it was given and ``in_state``
    steps over the rest.

    Three things fall out of it being a tuple rather than a bag of names. A
    typo is refused by the constructor, so ``Override(colour=X)`` raises instead of
    being dropped. A state cannot nest a state, because ``hover`` and
    ``pressed`` are not fields here. And it is hashable and cheap, so it rides
    in a layer key like everything else a frame is drawn from.
    """

    fill: object = UNSET
    radius: object = UNSET
    border: object = UNSET
    tail: object = UNSET
    shadows: object = UNSET
    opacity: object = UNSET
    clip: object = UNSET


# The two are one declaration split in half, so a field added to Style and
# forgotten here is a state that silently cannot change it.
assert Override._fields == Style._fields[: len(Override._fields)], (
    Override._fields,
    Style._fields,
)


def wash(base, over):
    """The colour that turns ``base`` into ``over`` when laid on top of it.

    For a surface that cannot afford to re-raster on hover and paints the
    change as a separate plane instead. The plane composites over pixels that
    already hold ``base``, so it carries the difference rather than the target:
    ``1 - (1 - over) / (1 - base)`` in alpha, which is exact when the two share
    a hue — and in this product they do, since every interaction token is white
    at a different alpha. ``base`` of ``None`` is bare backdrop, so the wash is
    just ``over``.
    """
    if over is None or isinstance(over, Gradient):
        return None
    if base is None or isinstance(base, Gradient):
        return over
    if base[3] >= 1.0:
        return over
    alpha = 1.0 - (1.0 - over[3]) / (1.0 - base[3])
    if alpha <= 0.0:
        return None
    return (over[0], over[1], over[2], min(1.0, alpha))


def in_state(style, state):
    """``style`` as it looks in ``state``, or unchanged if it says nothing.

    ``pressed`` falls back to ``hover``: a control that only declares a hover
    still has to look pressed while the button is down, and every one of them
    did before this was a declaration.
    """
    spec = None
    if state == PRESSED:
        spec = style.pressed if style.pressed is not None else style.hover
    elif state == HOVER:
        spec = style.hover
    if spec is None:
        return style
    if type(spec) is not Override:
        raise TypeError(
            f"Style.{state} must be an Override, got {type(spec).__name__}. "
            f"Write Override(fill={spec!r}) rather than the value on its own."
        )
    # Field by field rather than through respec: this runs per painted box per
    # hovered frame, and the pair are two tuples of known shape.
    return Style(
        style.fill if spec.fill is UNSET else spec.fill,
        style.radius if spec.radius is UNSET else spec.radius,
        style.border if spec.border is UNSET else spec.border,
        style.tail if spec.tail is UNSET else spec.tail,
        style.shadows if spec.shadows is UNSET else spec.shadows,
        style.opacity if spec.opacity is UNSET else spec.opacity,
        style.clip if spec.clip is UNSET else spec.clip,
        style.hover,
        style.pressed,
    )


class Layout(NamedTuple):
    """Where a node sits. Built from a node's keyword arguments, not passed in.

    A ``NamedTuple`` for the same reason as ``Style``, and more so: every node
    has one, where only a painted node has a style.
    """

    direction: str = ROW
    gap: float = 0.0
    pad: Edges = NO_EDGES
    align: str = START
    justify: str = START
    wrap: bool = False
    width: float | None = None
    height: float | None = None
    min_width: float | None = None
    max_width: float | None = None
    grow: float = 0.0
    # Painted-position nudge. Siblings do not see it, which is what makes a
    # sliding highlight or a popup possible without a second layout pass.
    dx: float = 0.0
    dy: float = 0.0


NO_LAYOUT = Layout()
_LAYOUT_FIELDS = Layout._fields

_fields = {}


def respec(value, changes):
    """``dataclasses.replace`` for a frozen value type, without the ceremony.

    The stock helper re-runs ``__init__`` through a ``getattr`` per field, which
    measured two thirds of the cost of describing a composer frame back when
    every property was a chained builder call. Named arguments took most of
    that traffic away, but the painter still reaches this per callout and the
    node-level ``replace`` below is built on it.

    Copying the fields directly stays correct because every field is set, so
    the generated ``__eq__`` and ``__hash__`` still see a fully formed
    instance.
    """
    kind = type(value)
    names = _fields.get(kind)
    if names is None:
        names = _fields[kind] = tuple(
            getattr(kind, "_fields", None) or kind.__dataclass_fields__
        )
    # ``Style`` and ``Layout`` are tuples underneath, so the stock ``_replace``
    # is a single ``_make`` over an iterator rather than a setattr per field.
    if issubclass(kind, tuple):
        return value._replace(**changes)
    clone = object.__new__(kind)
    for name in names:
        object.__setattr__(
            clone, name, changes[name] if name in changes else getattr(value, name)
        )
    return clone


def _layout(
    direction,
    gap,
    pad,
    align,
    justify,
    wrap,
    width,
    height,
    size,
    min_width,
    max_width,
    grow,
    dx,
    dy,
):
    """A ``Layout``, or the shared default when nothing was asked for.

    The default case is most of the tree — a text inside a padded row asks for
    no layout of its own — and returning the singleton keeps those nodes from
    allocating at all.
    """
    if size is not None:
        if width is None:
            width = size
        if height is None:
            height = size
    if (
        direction == ROW
        and not gap
        and pad is None
        and align == START
        and justify == START
        and not wrap
        and width is None
        and height is None
        and min_width is None
        and max_width is None
        and not grow
        and not dx
        and not dy
    ):
        return NO_LAYOUT
    return Layout(
        direction=direction,
        gap=float(gap),
        pad=_pad(pad),
        align=align,
        justify=justify,
        wrap=bool(wrap),
        width=None if width is None else float(width),
        height=None if height is None else float(height),
        min_width=None if min_width is None else float(min_width),
        max_width=None if max_width is None else float(max_width),
        grow=float(grow),
        dx=float(dx),
        dy=float(dy),
    )


@dataclass(frozen=True, init=False)
class View:
    """A rectangle that lays out children."""

    children: tuple = ()
    layout: Layout = NO_LAYOUT
    style: Style = NO_STYLE
    id: str | None = None

    def __init__(
        self,
        *children,
        direction=ROW,
        gap=0.0,
        pad=None,
        align=START,
        justify=START,
        wrap=False,
        width=None,
        height=None,
        size=None,
        min_width=None,
        max_width=None,
        grow=0.0,
        dx=0.0,
        dy=0.0,
        style=None,
        id=None,
    ):
        setattr_ = object.__setattr__
        # None children are dropped so `x if cond else None` reads inline.
        setattr_(
            self, "children", tuple(child for child in children if child is not None)
        )
        setattr_(
            self,
            "layout",
            _layout(
                direction,
                gap,
                pad,
                align,
                justify,
                wrap,
                width,
                height,
                size,
                min_width,
                max_width,
                grow,
                dx,
                dy,
            ),
        )
        setattr_(self, "style", NO_STYLE if style is None else style)
        setattr_(self, "id", id)


def Row(*children, **kwargs):
    """A ``View`` that lays its children out left to right."""
    kwargs["direction"] = ROW
    return View(*children, **kwargs)


def Column(*children, **kwargs):
    """A ``View`` that lays its children out top to bottom."""
    kwargs["direction"] = COLUMN
    return View(*children, **kwargs)


def Stack(*children, **kwargs):
    """A ``View`` whose children share one box, later ones painted on top."""
    kwargs["direction"] = STACK
    return View(*children, **kwargs)


@dataclass(frozen=True, init=False)
class Text:
    """A string. The only node whose size depends on font metrics."""

    value: str = ""
    font: Font = DEFAULT_FONT
    color: tuple = WHITE
    lines: int = 1
    ellipsis: bool = True
    layout: Layout = NO_LAYOUT
    id: str | None = None

    def __init__(
        self,
        value="",
        *,
        font=None,
        color=WHITE,
        lines=1,
        ellipsis=True,
        width=None,
        height=None,
        size=None,
        min_width=None,
        max_width=None,
        grow=0.0,
        dx=0.0,
        dy=0.0,
        id=None,
    ):
        setattr_ = object.__setattr__
        setattr_(self, "value", value)
        setattr_(self, "font", DEFAULT_FONT if font is None else font)
        setattr_(self, "color", color)
        setattr_(self, "lines", int(lines))
        setattr_(self, "ellipsis", bool(ellipsis))
        setattr_(
            self,
            "layout",
            _layout(
                ROW,
                0.0,
                None,
                START,
                START,
                False,
                width,
                height,
                size,
                min_width,
                max_width,
                grow,
                dx,
                dy,
            ),
        )
        setattr_(self, "id", id)


@dataclass(frozen=True, init=False)
class Image:
    """Pixels or vectors. ``source`` is opaque here; the loader resolves it.

    Tinting is what makes a separate icon primitive unnecessary: a monochrome
    SVG plus ``tint=color`` is an icon.
    """

    source: object = None
    fit: str = CONTAIN
    tint: tuple | None = None
    layout: Layout = NO_LAYOUT
    id: str | None = None

    def __init__(
        self,
        source=None,
        *,
        fit=CONTAIN,
        tint=None,
        width=None,
        height=None,
        size=None,
        min_width=None,
        max_width=None,
        grow=0.0,
        dx=0.0,
        dy=0.0,
        id=None,
    ):
        setattr_ = object.__setattr__
        setattr_(self, "source", source)
        setattr_(self, "fit", fit)
        setattr_(self, "tint", tint)
        setattr_(
            self,
            "layout",
            _layout(
                ROW,
                0.0,
                None,
                START,
                START,
                False,
                width,
                height,
                size,
                min_width,
                max_width,
                grow,
                dx,
                dy,
            ),
        )
        setattr_(self, "id", id)


_STYLE_FIELDS = frozenset(Style._fields)
_LAYOUT_ONLY = frozenset(_LAYOUT_FIELDS)


def replace(node, **changes):
    """A copy of ``node`` with some properties changed.

    For the handful of places that decide a single property *after* the node
    exists — a chip dimmed when the scene cannot service it, a paragraph given
    its width once the row it shares is known. Each name is routed to whichever
    of the node's three parts owns it, so the caller does not have to know
    which:

        replace(node, opacity=0.4)   # style
        replace(node, width=220)     # layout
        replace(node, id="asset:3")  # the node itself
    """
    if not changes:
        return node
    node_changes = {}
    layout_changes = {}
    style_changes = {}
    for name, value in changes.items():
        if name == "size":
            layout_changes["width"] = layout_changes["height"] = value
        elif name in _LAYOUT_ONLY:
            layout_changes[name] = value
        elif name in _STYLE_FIELDS:
            style_changes[name] = value
        else:
            node_changes[name] = value
    if layout_changes:
        if "pad" in layout_changes:
            layout_changes["pad"] = _pad(layout_changes["pad"])
        node_changes["layout"] = respec(node.layout, layout_changes)
    if style_changes:
        if not isinstance(node, View):
            raise TypeError(f"{type(node).__name__} has no style to change")
        node_changes["style"] = respec(node.style, style_changes)
    return respec(node, node_changes)
