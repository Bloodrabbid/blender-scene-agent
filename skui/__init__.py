"""skui — a small declarative UI core that renders through Skia.

Three primitives described with named arguments, one pure layout pass, one pure
paint pass:

    from blender_scene_agent import skui

    tree = skui.Row(
        skui.Text("Generate", font=skui.Font("Inter", 13, 600), color=skui.BLACK),
        pad=skui.edges(x=16, y=10),
        style=skui.Style(fill=skui.hexa("#D1FE17"), radius=999),
    )

    measure = skui.Measure()
    frame = skui.solve(tree, measure, width=320)
    skui.paint(canvas, frame, measure)
    frame.hit(mouse_x, mouse_y)  # -> node id or None

``Row``, ``Column`` and ``Stack`` are ``View`` with a direction; ``View`` itself
is there for the rare tree that picks its direction at runtime. Layout is flat
keyword arguments because it is always local to one node; paint is a ``Style``
because it is the part worth naming once and sharing. ``replace`` covers the
few places that decide a property after the node exists.

The core has no state, no tokens and no interaction: it turns a description
into rectangles and pixels, and answers what is under a point. Everything else
— hover, press, focus, animation, component vocabulary — belongs to the layer
above.
"""

from .measure import FontBook, ImageBook, Measure, available, skia_module
from .nodes import (
    BETWEEN,
    BLACK,
    CENTER,
    COLUMN,
    CONTAIN,
    COVER,
    END,
    FILL,
    HOVER,
    PRESSED,
    ROW,
    STACK,
    START,
    TRANSPARENT,
    UNSET,
    WHITE,
    Border,
    Column,
    Edges,
    Font,
    Gradient,
    Image,
    Layout,
    Override,
    Row,
    Shadow,
    Stack,
    Style,
    Tail,
    Text,
    View,
    edges,
    hexa,
    in_state,
    replace,
    respec,
    rgba,
    stroke_box,
    wash,
)
from .paint import paint
from .solve import STRETCH, Frame, Placed, solve

__all__ = [
    "BETWEEN",
    "BLACK",
    "Border",
    "CENTER",
    "COLUMN",
    "CONTAIN",
    "COVER",
    "Column",
    "END",
    "Edges",
    "FILL",
    "Font",
    "FontBook",
    "Frame",
    "Gradient",
    "HOVER",
    "Image",
    "ImageBook",
    "Layout",
    "Measure",
    "Override",
    "PRESSED",
    "Placed",
    "ROW",
    "Row",
    "STACK",
    "STRETCH",
    "START",
    "Shadow",
    "Stack",
    "Style",
    "TRANSPARENT",
    "Tail",
    "Text",
    "UNSET",
    "View",
    "WHITE",
    "available",
    "edges",
    "hexa",
    "in_state",
    "paint",
    "replace",
    "respec",
    "rgba",
    "skia_module",
    "solve",
    "stroke_box",
    "wash",
]
