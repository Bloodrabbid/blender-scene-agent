"""Render a tree to a PNG without Blender.

The whole point of keeping the core free of ``bpy`` is that a widget can be
looked at in a fraction of a second instead of a rebuild-reinstall-relaunch
cycle. Layout and paint here are the same code paths the viewport uses.

    python3 -m blender_scene_agent.skui.snapshot   # renders the demo below
"""

from __future__ import annotations

import os

from .measure import Measure, skia_module
from .paint import paint
from .solve import solve


def render(
    tree,
    path,
    width=None,
    height=None,
    scale=2.0,
    background=(0, 0, 0, 0),
    measure=None,
    interact=None,
):
    """Solve and draw ``tree`` into a PNG. Returns the solved frame.

    ``interact`` is passed straight to ``paint``, so a hover or a press can be
    looked at here rather than only in Blender: pass ``lambda id: "hover"`` for
    a whole tree, or a dict's ``get`` for one node.
    """
    skia = skia_module()
    measure = measure or Measure()
    frame = solve(tree, measure, width=width, height=height)

    surface = skia.Surface(
        max(1, int(round(frame.width * scale))),
        max(1, int(round(frame.height * scale))),
    )
    canvas = surface.getCanvas()
    canvas.clear(skia.Color4f(*background))
    canvas.scale(scale, scale)
    paint(canvas, frame, measure, interact)

    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    surface.makeImageSnapshot().save(path, skia.kPNG)
    return frame
