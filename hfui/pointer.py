"""Hover and press, kept out of the core.

The core answers "what is under this point"; this turns that into the two
states a widget cares about, and holds the last solved frame so the caller can
feed it raw mouse coordinates.

It used to pick colours too — ``fill(node_id, base, hover, pressed)``, called
from inside every widget. Widgets now *declare* their states on the ``Style``
and ``paint`` resolves them against ``state``, which is what lets a surface
choose whether to raster the change or draw it on a plane of its own. The
pointer is only read directly where a state changes something a style cannot
carry: a press that shifts padding, a glyph's tint, a badge that appears.
"""

from __future__ import annotations

# Shared with the painter, which resolves a node's declared ``hover`` and
# ``pressed`` styles against whatever ``state`` answers. Two spellings of these
# would fail silently: the state simply would never match.
from ..skui import HOVER, PRESSED


class Pointer:
    def __init__(self):
        self.hovered = None
        self.pressed = None
        self.frame = None

    def track(self, frame):
        """Adopt the frame that hit tests resolve against."""
        self.frame = frame

    def _hit(self, x, y):
        return self.frame.hit(x, y) if self.frame is not None else None

    def move(self, x, y):
        """Returns whether the hovered node changed, i.e. whether to redraw."""
        target = self._hit(x, y)
        changed = target != self.hovered
        self.hovered = target
        return changed

    def leave(self):
        changed = self.hovered is not None or self.pressed is not None
        self.hovered = self.pressed = None
        return changed

    def press(self, x, y):
        self.pressed = self._hit(x, y)
        return self.pressed

    def release(self, x, y):
        """Returns the node id that was clicked, if press and release agree."""
        target = self._hit(x, y)
        clicked = target if target is not None and target == self.pressed else None
        self.pressed = None
        return clicked

    def state(self, node_id):
        if node_id is not None and node_id == self.pressed:
            return PRESSED
        if node_id is not None and node_id == self.hovered:
            return HOVER
        return None

    @property
    def key(self):
        """Cache key contribution: what a redraw depends on."""
        return (self.hovered, self.pressed)
