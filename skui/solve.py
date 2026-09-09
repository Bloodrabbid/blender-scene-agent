"""Layout: a tree plus a width becomes a flat table of rectangles.

Pure arithmetic. The only outside knowledge it needs is how wide a string and
how large an image are, which arrives as a ``measure`` object:

    measure.text(value, font, limit, max_lines, truncate) -> (lines, width, height)
    measure.image(source) -> (width, height)

Two passes. ``_measure`` walks bottom-up and memoises a natural size per node
under the width it was offered; ``_place`` walks top-down handing out final
rectangles. The result is a ``Frame``: a paint-ordered list of placed nodes, a
lookup by id, and hit testing.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .nodes import BETWEEN, CENTER, COLUMN, END, STACK, Image, Text, View

STRETCH = "stretch"


def _intersect(clip, rect):
    """``rect`` cropped to ``clip``. A None clip crops nothing."""
    if clip is None:
        return rect
    x, y, width, height = rect
    left = max(x, clip[0])
    top = max(y, clip[1])
    right = min(x + width, clip[0] + clip[2])
    bottom = min(y + height, clip[1] + clip[3])
    return (left, top, max(0.0, right - left), max(0.0, bottom - top))


@dataclass
class Placed:
    """One node's final rectangle. ``lines`` is set for text only."""

    node: object
    x: float
    y: float
    width: float
    height: float
    lines: tuple = ()
    children: tuple = ()
    # What a clipping ancestor leaves of this node, or None when nothing clips
    # it. Layout still runs at full size — only what reaches the screen shrinks.
    clip: tuple | None = None

    @property
    def rect(self):
        return (self.x, self.y, self.width, self.height)

    @property
    def visible(self):
        """The part of ``rect`` that is actually painted. Can be empty.

        A node scrolled out of a clipped viewport is still laid out and still
        has a rectangle; it is just not on screen. Hit testing has to agree
        with the paint, or a chip that scrolled away stays clickable.
        """
        return _intersect(self.clip, self.rect)

    def contains(self, x, y):
        left, top, width, height = self.visible
        if width <= 0 or height <= 0:
            return False
        return left <= x <= left + width and top <= y <= top + height


@dataclass
class Frame:
    """A solved tree: ``root`` for painting, ``items`` for hit testing."""

    width: float = 0.0
    height: float = 0.0
    root: object = None
    items: tuple = ()
    index: dict = field(default_factory=dict)

    def rect(self, node_id):
        placed = self.index.get(node_id)
        return placed.rect if placed else None

    def hit(self, x, y):
        """Topmost identified node under the point, or ``None``.

        Later siblings paint over earlier ones, so the scan runs backwards.
        """
        for placed in reversed(self.items):
            if placed.node.id is not None and placed.contains(x, y):
                return placed.node.id
        return None


@dataclass
class _Size:
    width: float
    height: float
    lines: tuple = ()
    rows: tuple = ()  # wrapped rows: tuple of tuples of (child, _Size)


def _clamp(value, low, high):
    if low is not None:
        value = max(value, low)
    if high is not None:
        value = min(value, high)
    return value


class _Solver:
    def __init__(self, measure):
        self.measure = measure
        self.memo = {}

    # -- pass 1 -----------------------------------------------------------

    def size(self, node, avail_width):
        key = (id(node), None if avail_width is None else round(avail_width, 2))
        cached = self.memo.get(key)
        if cached is None:
            cached = self._size(node, avail_width)
            self.memo[key] = cached
        return cached

    def _size(self, node, avail_width):
        layout = node.layout
        limit = layout.width if layout.width is not None else avail_width
        if layout.max_width is not None:
            limit = layout.max_width if limit is None else min(limit, layout.max_width)

        if isinstance(node, Text):
            budget = None if limit is None else max(0.0, limit)
            lines, width, height = self.measure.text(
                node.value, node.font, budget, node.lines, node.ellipsis
            )
            return _Size(
                width=layout.width if layout.width is not None else width,
                height=layout.height if layout.height is not None else height,
                lines=tuple(lines),
            )

        if isinstance(node, Image):
            width, height = self.measure.image(node.source)
            if layout.width is not None and layout.height is not None:
                width, height = layout.width, layout.height
            elif layout.width is not None:
                scale = layout.width / width if width else 1.0
                width, height = layout.width, height * scale
            elif layout.height is not None:
                scale = layout.height / height if height else 1.0
                width, height = width * scale, layout.height
            return _Size(width=width, height=height)

        inner_limit = None if limit is None else max(0.0, limit - layout.pad.x)
        rows = self._rows(node, inner_limit)

        if layout.direction == STACK:
            content_width = max((size.width for _, size in rows[0]), default=0.0)
            content_height = max((size.height for _, size in rows[0]), default=0.0)
        elif layout.direction == COLUMN:
            content_width = max((size.width for _, size in rows[0]), default=0.0)
            content_height = sum(size.height for _, size in rows[0])
            if rows[0]:
                content_height += layout.gap * (len(rows[0]) - 1)
        else:
            content_width = 0.0
            content_height = 0.0
            for index, row in enumerate(rows):
                row_width = sum(size.width for _, size in row)
                if row:
                    row_width += layout.gap * (len(row) - 1)
                content_width = max(content_width, row_width)
                content_height += max((size.height for _, size in row), default=0.0)
                if index:
                    content_height += layout.gap

        width = (
            layout.width if layout.width is not None else content_width + layout.pad.x
        )
        width = _clamp(width, layout.min_width, layout.max_width)
        height = (
            layout.height
            if layout.height is not None
            else content_height + layout.pad.y
        )
        return _Size(width=width, height=height, rows=rows)

    def _rows(self, node, inner_limit):
        """Children grouped into rows. Columns and non-wrapping rows give one."""
        layout = node.layout
        sized = [(child, self.size(child, inner_limit)) for child in node.children]
        if (
            layout.direction in (COLUMN, STACK)
            or not layout.wrap
            or inner_limit is None
        ):
            return (tuple(sized),)

        rows = []
        current = []
        used = 0.0
        for child, size in sized:
            step = size.width + (layout.gap if current else 0.0)
            if current and used + step > inner_limit + 0.5:
                rows.append(tuple(current))
                current, used = [(child, size)], size.width
            else:
                current.append((child, size))
                used += step
        if current:
            rows.append(tuple(current))
        return tuple(rows)

    # -- pass 2 -----------------------------------------------------------

    def place(self, node, x, y, width, height):
        size = self.size(node, width)
        layout = node.layout
        x, y = x + layout.dx, y + layout.dy
        placed = Placed(
            node=node, x=x, y=y, width=width, height=height, lines=size.lines
        )
        if not isinstance(node, View) or not node.children:
            return placed

        inner_x = x + layout.pad.left
        inner_y = y + layout.pad.top
        inner_width = max(0.0, width - layout.pad.x)
        inner_height = max(0.0, height - layout.pad.y)
        rows = size.rows or self._rows(node, inner_width)
        out = []

        if layout.direction == STACK:
            self._place_stack(
                layout, rows[0], inner_x, inner_y, inner_width, inner_height, out
            )
        elif layout.direction == COLUMN:
            self._place_column(
                layout, rows[0], inner_x, inner_y, inner_width, inner_height, out
            )
        else:
            cursor_y = inner_y
            for index, row in enumerate(rows):
                row_height = max((child.height for _, child in row), default=0.0)
                if len(rows) == 1:
                    row_height = inner_height
                elif index:
                    cursor_y += layout.gap
                self._place_row(
                    layout, row, inner_x, cursor_y, inner_width, row_height, out
                )
                cursor_y += row_height

        placed.children = tuple(out)
        return placed

    def _spread(self, layout, sizes, available, count):
        """Leading offset and inter-item gap for the main axis."""
        total = sum(sizes) + layout.gap * max(0, count - 1)
        slack = available - total
        if layout.justify == CENTER:
            return slack * 0.5, layout.gap
        if layout.justify == END:
            return slack, layout.gap
        if layout.justify == BETWEEN and count > 1:
            return 0.0, layout.gap + max(0.0, slack) / (count - 1)
        return 0.0, layout.gap

    def _grown(self, layout, row, available):
        """Main-axis sizes after flexible children absorb the slack.

        ``grow`` marks a child flexible in both directions: it takes a share of
        free space, and gives back a share when the row overflows. Shrinking is
        proportional to natural size, so a wide child yields more than a narrow
        one instead of everything collapsing at once.
        """
        sizes = [
            size.width if layout.direction != COLUMN else size.height for _, size in row
        ]
        weights = [child.layout.grow for child, _ in row]
        total_weight = sum(weights)
        if total_weight <= 0:
            return sizes
        slack = available - sum(sizes) - layout.gap * max(0, len(row) - 1)
        if slack == 0:
            return sizes
        if slack > 0:
            return [
                size + slack * (weight / total_weight)
                for size, weight in zip(sizes, weights)
            ]

        flexible = sum(size for size, weight in zip(sizes, weights) if weight > 0)
        if flexible <= 0:
            return sizes
        deficit = min(-slack, flexible)
        return [
            max(0.0, size - deficit * (size / flexible)) if weight > 0 else size
            for size, weight in zip(sizes, weights)
        ]

    def _place_row(self, layout, row, x, y, width, height, out):
        sizes = self._grown(layout, row, width)
        offset, gap = self._spread(layout, sizes, width, len(row))
        cursor = x + offset
        for (child, child_size), main in zip(row, sizes):
            cross = child_size.height
            if layout.align == STRETCH and child.layout.height is None:
                cross = height
            top = y
            if layout.align == CENTER:
                top = y + (height - cross) * 0.5
            elif layout.align == END:
                top = y + height - cross
            out.append(self.place(child, cursor, top, main, cross))
            cursor += main + gap

    def _place_column(self, layout, row, x, y, width, height, out):
        sizes = self._grown(layout, row, height)
        offset, gap = self._spread(layout, sizes, height, len(row))
        cursor = y + offset
        for (child, child_size), main in zip(row, sizes):
            cross = child_size.width
            if layout.align == STRETCH and child.layout.width is None:
                cross = width
            left = x
            if layout.align == CENTER:
                left = x + (width - cross) * 0.5
            elif layout.align == END:
                left = x + width - cross
            out.append(self.place(child, left, cursor, cross, main))
            cursor += main + gap

    def _place_stack(self, layout, row, x, y, width, height, out):
        """Every child gets the same box; ``grow`` fills it, otherwise natural."""
        for child, child_size in row:
            child_width = width if child.layout.grow else child_size.width
            child_height = height if child.layout.grow else child_size.height
            left, top = x, y
            if layout.justify == CENTER:
                left = x + (width - child_width) * 0.5
            elif layout.justify == END:
                left = x + width - child_width
            if layout.align == CENTER:
                top = y + (height - child_height) * 0.5
            elif layout.align == END:
                top = y + height - child_height
            elif layout.align == STRETCH:
                child_height = height
            out.append(self.place(child, left, top, child_width, child_height))


def _flatten(placed, out, clip=None):
    placed.clip = clip
    out.append(placed)
    node = placed.node
    if isinstance(node, View) and node.style.clip:
        clip = _intersect(clip, placed.rect)
    for child in placed.children:
        _flatten(child, out, clip)


def solve(tree, measure, width=None, height=None, x=0.0, y=0.0):
    """Lay ``tree`` out in the given space. Omit width/height to size to content."""
    solver = _Solver(measure)
    size = solver.size(tree, width)
    final_width = width if width is not None and tree.layout.grow else size.width
    if tree.layout.width is not None:
        final_width = tree.layout.width
    final_height = size.height if height is None else height

    root = solver.place(tree, x, y, final_width, final_height)
    items = []
    _flatten(root, items)
    index = {placed.node.id: placed for placed in items if placed.node.id is not None}
    return Frame(
        width=final_width,
        height=final_height,
        root=root,
        items=tuple(items),
        index=index,
    )
