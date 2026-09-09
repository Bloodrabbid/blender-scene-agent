"""A height-indexed window over a long vertical thread.

The conversation remains whole in its owner and on disk.  This module only
decides which rows intersect the viewport and represents everything outside
that range as two spacers.  It deliberately knows nothing about Blender or
Skia, which makes the scroll arithmetic cheap to test and safe to use from a
draw handler.
"""

from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass


@dataclass(frozen=True)
class Slice:
    """A contiguous row range and the space hidden on either side."""

    first: int
    last: int
    before: float
    after: float
    total: float


class ThreadViewport:
    """Estimated row heights refined lazily as rows enter the viewport."""

    __slots__ = (
        "gap",
        "width",
        "keys",
        "indices",
        "heights",
        "exact",
        "prefix",
        "total",
    )

    def __init__(self, gap=0.0):
        self.gap = max(0.0, float(gap))
        self.width = None
        self.keys = ()
        self.indices = {}
        self.heights = []
        self.exact = []
        self.prefix = [0.0]
        self.total = 0.0

    def clear(self):
        self.width = None
        self.keys = ()
        self.indices = {}
        self.heights = []
        self.exact = []
        self.prefix = [0.0]
        self.total = 0.0

    def sync(self, keys, estimates, width, exact=None):
        """Adopt a transcript, retaining exact heights for unchanged rows."""
        keys = tuple(keys)
        estimates = tuple(max(1.0, float(value)) for value in estimates)
        exact = (
            tuple(False for _key in keys)
            if exact is None
            else tuple(bool(value) for value in exact)
        )
        width = round(float(width), 1)
        if len(keys) != len(estimates) or len(keys) != len(exact):
            raise ValueError("one height and exact flag are required per thread row")

        same_width = self.width == width
        if same_width and self.keys == keys:
            return False

        old = {
            key: (self.heights[index], self.exact[index])
            for index, key in enumerate(self.keys)
        }
        heights = []
        adopted_exact = []
        for key, estimate, already_exact in zip(keys, estimates, exact):
            previous = old.get(key)
            reusable = same_width and previous is not None and previous[1]
            heights.append(previous[0] if reusable else estimate)
            adopted_exact.append(bool(reusable or already_exact))

        self.width = width
        self.keys = keys
        self.indices = {key: index for index, key in enumerate(keys)}
        self.heights = heights
        self.exact = adopted_exact
        self._rebuild()
        return True

    def _rebuild(self):
        prefix = [0.0]
        count = len(self.heights)
        for index, height in enumerate(self.heights):
            prefix.append(
                prefix[-1] + height + (self.gap if index + 1 < count else 0.0)
            )
        self.prefix = prefix
        self.total = prefix[-1]

    def row_at(self, offset):
        """Index of the row containing ``offset``, clamped to the thread."""
        if not self.heights:
            return None
        offset = max(0.0, min(float(offset), max(0.0, self.total - 0.001)))
        return min(len(self.heights) - 1, bisect_right(self.prefix, offset) - 1)

    def window(
        self,
        offset,
        viewport,
        overscan=0.0,
        *,
        before=None,
        after=None,
    ):
        """Rows intersecting the viewport plus directional overscan."""
        if not self.heights:
            return Slice(0, -1, 0.0, 0.0, 0.0)
        overscan = max(0.0, float(overscan))
        before = overscan if before is None else max(0.0, float(before))
        after = overscan if after is None else max(0.0, float(after))
        start = max(0.0, float(offset) - before)
        end = min(
            self.total,
            float(offset) + max(0.0, float(viewport)) + after,
        )
        first = self.row_at(start)
        last = self.row_at(max(start, end - 0.001))
        first = 0 if first is None else first
        last = first if last is None else last
        before = self.prefix[first]
        visible_end = self.prefix[last] + self.heights[last]
        return Slice(
            first,
            last,
            before,
            max(0.0, self.total - visible_end),
            self.total,
        )

    def set_heights(self, measured):
        """Apply ``(index, height)`` measurements; return whether any changed."""
        changed = False
        for index, height in measured:
            if index < 0 or index >= len(self.heights):
                continue
            height = max(1.0, float(height))
            if not self.exact[index] or abs(self.heights[index] - height) >= 0.05:
                self.heights[index] = height
                changed = True
            self.exact[index] = True
        if changed:
            self._rebuild()
        return changed

    def position(self, index):
        """Top of a row, for preserving the first visible row as an anchor."""
        if index < 0:
            return 0.0
        if index >= len(self.heights):
            return self.total
        return self.prefix[index]

    def index(self, key):
        """Current index for a stable row key, or None after its removal."""
        return self.indices.get(key)


class ScrollSeek:
    """Velocity and hysteresis for a lightweight fast-scroll representation.

    Blender reports wheel and trackpad input rather than a browser-like scroll
    lifecycle.  This turns those deltas into the same contract used by mature
    virtualizers: enter above a high velocity, leave below a lower one, and
    guarantee one settled frame after input ends.
    """

    __slots__ = (
        "enter_velocity",
        "exit_velocity",
        "settle",
        "last_at",
        "velocity",
        "direction",
        "active",
        "until",
    )

    def __init__(self, enter_velocity=700.0, exit_velocity=180.0, settle=0.12):
        self.enter_velocity = max(1.0, float(enter_velocity))
        self.exit_velocity = max(0.0, min(float(exit_velocity), self.enter_velocity))
        self.settle = max(0.01, float(settle))
        self.reset()

    def reset(self):
        self.last_at = None
        self.velocity = 0.0
        self.direction = 0
        self.active = False
        self.until = 0.0

    def push(self, delta, now):
        """Observe one effective scroll delta; return whether seek is active."""
        delta = float(delta)
        now = float(now)
        if not delta:
            return self.is_active(now)

        elapsed = None if self.last_at is None else now - self.last_at
        self.direction = 1 if delta > 0.0 else -1
        self.last_at = now

        # One wheel notch has no elapsed sample to measure. Treat it as the
        # beginning of a gesture rather than inventing a 60 Hz interval that
        # makes every 20 px notch look like a 1,200 px/s fling. A second event
        # before the settle deadline supplies the first real velocity sample.
        fresh = elapsed is None or elapsed <= 0.0 or elapsed >= self.settle
        if fresh:
            self.velocity = 0.0
            self.active = False
            self.until = 0.0
            return False

        sample = abs(delta) / max(elapsed, 1.0 / 240.0)
        self.velocity = (
            sample if self.velocity <= 0.0 else self.velocity * 0.6 + sample * 0.4
        )
        self.until = now + self.settle

        if self.active:
            if self.velocity <= self.exit_velocity:
                self.active = False
        elif self.velocity >= self.enter_velocity:
            self.active = True
        return self.active

    def is_active(self, now):
        return bool(self.active and float(now) < self.until)

    def remaining(self, now):
        if not self.is_active(now):
            return None
        return max(0.0, self.until - float(now))
