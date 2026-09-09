"""A viewport's offset into content that does not fit it.

Host-owned interaction state, the same category as `Pointer` and `Motion`: the
surface keeps one of these per scrollable region, the widget tree reads the
offset, and nothing here knows about Blender or Skia.

There are five in the product — the composer's prompt, chip strip and open
select, and the chat's thread and prompt — and they were five hand-rolled
copies of `max(0, min(overflow, offset))` with the interesting parts diverging.
This is those parts, named:

- **`follow`** pins the viewport to the far end, which is what makes a reply
  landing below the fold scroll into view. A wheel clears it; the caller
  re-arms it if being at the end means something (the chat thread does, the
  composer's prompt does not).
- **`reveal`** moves the *least* that brings a span into view, which is how a
  caret is chased without the field jumping to the bottom on every keystroke.
  Its `key` argument makes the chase an edge rather than a level — see the
  method.
- **`place`** answers the first frame only, for a popup that should open on the
  value it holds rather than at the top of a long list.

An offset is in logical pixels and always positive, however the surface draws
it: the chip strip scrolls sideways and its offset still counts up from zero.
"""

from __future__ import annotations

# `reveal` without a key chases on every call. A sentinel rather than None,
# because None is a perfectly good key for "nothing is being chased".
ALWAYS = object()


class Scroller:
    """How far into its content a viewport has been moved."""

    __slots__ = ("offset", "overflow", "follow", "placed", "_chased")

    def __init__(self, follow=False):
        self.offset = 0.0
        self.overflow = 0.0
        # Pin to the far end until something says otherwise.
        self.follow = follow
        # Whether `place` has answered yet.
        self.placed = False
        # What `reveal` was last aimed at.
        self._chased = None

    # -- measuring ----------------------------------------------------------

    def fit(self, content, viewport):
        """Take a fresh measurement of both sides. Returns the offset."""
        return self.extent(float(content) - float(viewport))

    def extent(self, overflow):
        """Take the overflow directly, for content measured as a difference."""
        self.overflow = max(0.0, float(overflow))
        return self.clamp()

    def clamp(self):
        self.offset = max(0.0, min(self.overflow, self.offset))
        return self.offset

    def at_end(self, slack=0.5):
        return self.offset >= self.overflow - slack

    # -- moving -------------------------------------------------------------

    def by(self, delta):
        """Shift by `delta` logical pixels. True if it moved.

        Clears `follow`: a deliberate scroll is the user saying they want to
        look at something other than wherever the content is going.
        """
        if self.overflow <= 0.0 or not delta:
            return False
        self.follow = False
        before = self.offset
        self.offset = max(0.0, min(self.overflow, self.offset + float(delta)))
        return self.offset != before

    def to_end(self):
        self.offset = self.overflow
        return self.offset

    def place(self, offset):
        """Set the offset, but only the first time content has been measured.

        A select opens on the value it holds rather than at the top of the
        catalog, and after that it is wherever the user left it.
        """
        if not self.placed:
            self.placed = True
            self.offset = float(offset)
        return self.clamp()

    def reveal(self, top, bottom, view, key=ALWAYS):
        """Move the least that brings `top`..`bottom` into a `view`-tall window.

        `key` makes the chase an **edge** rather than a level. Pass something
        that changes when the thing being revealed moves — for a caret, its
        index *and* the length of the text, since a forward delete moves the
        caret's position without moving its index — and the viewport is only
        aimed on the frames it actually moved. Run as a level, on every frame,
        it pins the offset to wherever the caret is, so a wheel moves the field
        and the very next draw puts it straight back: the field reads as not
        scrolling at all.
        """
        if key is not ALWAYS:
            if key == self._chased:
                return self.clamp()
            self._chased = key
        if top < self.offset:
            self.offset = float(top)
        elif bottom > self.offset + view:
            self.offset = float(bottom) - float(view)
        return self.clamp()

    # -- lifetime -----------------------------------------------------------

    def reset(self):
        """Back to the top, unplaced. Closing a popup, or changing what is in
        the viewport for something the old offset says nothing about."""
        self.offset = 0.0
        self.placed = False
        self._chased = None
        return self.offset
