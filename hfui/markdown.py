"""The agent's markdown, read down to the half of it that is *structure*.

One renderer for every conversation surface — the Supercomputer panel and the
Scene Builder thread both feed replies through here, so "what is a list" has
one answer no matter which chat asked.

The split is not arbitrary — it is where the layout engine stops. A `Text`
node carries one font for its whole string and wrapping is a word loop over
that one string, so a bold run inside a sentence cannot be expressed without
styled spans in `measure`, `solve` and `paint`. Structure needs none of that:
a list item is a marker node beside a paragraph node, a heading is the same
node at another weight, a table is a grid of cells. So blocks are rendered and
inline emphasis is flattened, which is also the right trade for what lands
here — the replies are lists, `**Model**: GPT Image 2` label lines and beat
tables, and at 11pt in a narrow column the hierarchy a reader needs comes from
indentation, not from weight inside a line. Links keep their text and lose
their URL: nothing here is clickable.
"""

from __future__ import annotations

import re

from ..skui import START, STRETCH, Column, Row, Style, Text, View, edges
from . import tokens

_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_MD_EMPHASIS = re.compile(r"\*\*|__|`|~~|(?<!\*)\*(?!\*)")
_MD_FENCE = re.compile(r"^\s*(?:```|~~~)")
_MD_HEADING = re.compile(r"^\s{0,3}#{1,6}\s+(.*)$")
_MD_BULLET = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_MD_NUMBER = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_MD_QUOTE = re.compile(r"^\s*>\s?(.*)$")
# Three or more of the same mark, nothing else on the line.
_MD_RULE = re.compile(r"^\s*([-*_])(?:\s*\1){2,}\s*$")
# A line that is *entirely* bold, or a bold run that is the whole line bar a
# trailing colon. Both are a heading in everything but syntax, and both are
# expressible: the weight applies to the node, not to a run inside it.
_MD_STRONG = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*(:?)\s*$")
# `**Orbit** — the camera circles the product` is what nearly every list item
# this agent writes looks like, and it is the one inline case worth recovering:
# a bold run at the *head* of a line is a heading in disguise, so it goes on
# its own line at `BODY_STRONG` with the description under it. Only when the
# description is long enough to have wrapped anyway — `**Model**: gpt_image_2`
# is one short fact, and setting it over two lines says it is two.
_MD_LABEL = re.compile(r"^\s*(?:\*\*|__)(.+?)(?:\*\*|__)\s*[:—–-]\s+(.*)$")
LABEL_SPLIT = 48

# A pipe table row, and the `|---|:--:|` rule under its header. The rule is
# what says "table" in markdown proper, but a streaming reply spends a whole
# frame with the header alone on screen, so any run of pipe lines with two or
# more columns is taken at its word.
_MD_TABLE = re.compile(r"^\s*\|.*\|\s*$")
_MD_TABLE_RULE = re.compile(r"^\s*[|:\s-]+$")

LIST_MARKER = 16.0  # the hanging indent a bullet or a number sits in
LIST_INDENT = 13.0  # one level of nesting
QUOTE_RULE = 2.0
CELL_GAP = tokens.SM
CELL_MIN = 24.0


def _plain(line):
    """A line with its inline markers taken out."""
    return _MD_EMPHASIS.sub("", _MD_LINK.sub(r"\1", line)).strip()


def _line(text, ink, font, width=None):
    """A wrapping paragraph.

    **A paragraph that shares a row needs its width spelled out.** The solver
    sizes every child of a row against the row's whole inner width and only
    then hands the flexible ones what is left, so a `grow` text beside a fixed
    marker wraps as if the marker were not there and overruns the card by
    exactly the marker's width. Nothing is wrong with that for a single-line
    label, but a wrapped one has to be told.
    """
    return Text(
        text,
        font=font,
        color=ink,
        lines=0,
        ellipsis=False,
        width=None if width is None else max(1.0, width),
        grow=1.0,
    )


def _labelled(raw, ink, font, width=None):
    """A bold label at the head of a line, set over its description.

    ``None`` when the line is not that shape, so callers can fall through to a
    plain paragraph.
    """
    found = _MD_LABEL.match(raw)
    if found is None or len(found.group(2)) <= LABEL_SPLIT:
        return None
    return Column(
        _line(_plain(found.group(1)), ink, tokens.BODY_STRONG, width),
        _line(_plain(found.group(2)), ink, font, width=width),
        align=STRETCH,
    )


def _body(raw, ink, font, width=None):
    return _labelled(raw, ink, font, width) or _line(
        _plain(raw), ink, font, width=width
    )


def _hanging(marker, raw, ink, font, depth, width):
    """A list item: the marker in its own column, the text wrapping beside it.

    The marker is a sibling rather than a prefix so the second and later lines
    of a long item line up under the first word instead of under the bullet.
    """
    indent = depth * LIST_INDENT
    return Row(
        Text(
            marker,
            font=font,
            color=tokens.TEXT_SECONDARY,
            lines=1,
            width=LIST_MARKER,
        ),
        _body(raw, ink, font, width - indent - LIST_MARKER),
        pad=edges(left=indent),
        align=START,
    )


def _code(lines, ink):
    """A fenced block. The box is what says "code" — the face only helps.

    Long lines wrap rather than truncate: an ellipsis in the middle of a
    command is worse than a second line, and there is no way to scroll one
    line sideways in here.
    """
    return Column(
        *[
            Text(line, font=tokens.CODE, color=ink, lines=0, ellipsis=False, grow=1.0)
            for line in lines
        ],
        pad=edges(x=tokens.SM, y=tokens.XS),
        align=STRETCH,
        style=Style(fill=tokens.SURFACE, radius=tokens.RADIUS_CHIP),
    )


def _quote(text, ink, font, width):
    return Row(
        View(width=QUOTE_RULE, style=Style(fill=tokens.BORDER_STRONG)),
        _line(
            text,
            tokens.TEXT_SECONDARY if ink is tokens.TEXT_PRIMARY else ink,
            font,
            width=width - QUOTE_RULE - tokens.SM,
        ),
        gap=tokens.SM,
        align=STRETCH,
    )


def _table(lines, ink, font, width):
    """A pipe table as a grid: every column the same share of the width.

    Equal shares rather than measured ones, deliberately. Sizing columns to
    their content means re-measuring the whole table on every streamed token,
    and a beat table's cells are short enough that an equal split wastes
    little. Cells wrap; the header sets its row apart by weight and a hairline
    rather than a fill.

    ``None`` when the run is not really a table — a single-column line that
    happens to start with a pipe — so the caller can fall back to paragraphs.
    """
    rows = []
    header = len(lines) > 1 and bool(
        _MD_TABLE_RULE.match(lines[1]) and "-" in lines[1]
    )
    for index, raw in enumerate(lines):
        if index and _MD_TABLE_RULE.match(raw) and "-" in raw:
            continue
        rows.append([_plain(cell) for cell in raw.strip().strip("|").split("|")])
    rows = [row for row in rows if any(row)]
    if not rows:
        return None
    columns = max(len(row) for row in rows)
    if columns < 2:
        return None
    cell = max(CELL_MIN, (width - (columns - 1) * CELL_GAP) / columns)

    def grid_row(cells, strong):
        cells = cells + [""] * (columns - len(cells))
        return Row(
            *[
                Text(
                    text,
                    font=tokens.BODY_STRONG if strong else font,
                    color=ink,
                    lines=0,
                    ellipsis=False,
                    width=cell,
                )
                for text in cells
            ],
            gap=CELL_GAP,
            align=START,
        )

    out = []
    for index, cells in enumerate(rows):
        out.append(grid_row(cells, header and index == 0))
        if header and index == 0:
            out.append(View(height=1.0, style=Style(fill=tokens.BORDER_STRONG)))
    # STRETCH, not START: the header's hairline is a `View` with no width of
    # its own, and it only spans the table because the column stretches it to
    # the widest row.
    return Column(*out, gap=tokens.XS, align=STRETCH)


def blocks(text, ink, width, font=None):
    """The reply as nodes, one per markdown block.

    ``FontBook.wrap`` treats a newline as one more space, so without this a
    numbered list comes back as a single running sentence. The caller's column
    gap is what makes a blank line read as a break, so blank lines are dropped
    here rather than emitted as empty rows.

    ``font`` is the body face — the composer's thread runs a step heavier than
    the Supercomputer panel's, and the weight belongs to the surface, not the
    markup.
    """
    font = font or tokens.BODY
    out = []
    fenced = None
    table = []

    def flush_table():
        if not table:
            return
        node = _table(tuple(table), ink, font, width)
        if node is not None:
            out.append(node)
        else:
            out.extend(_body(raw, ink, font) for raw in table)
        table.clear()

    for raw in (text or "").split("\n"):
        if fenced is None and _MD_TABLE.match(raw):
            table.append(raw)
            continue
        flush_table()
        if _MD_FENCE.match(raw):
            # A fence closes the block it opened; an unclosed one is flushed
            # at the end, because a reply cut off mid-stream is the normal
            # case here, not a malformed one.
            if fenced is None:
                fenced = []
            else:
                out.append(_code(fenced or [""], ink))
                fenced = None
            continue
        if fenced is not None:
            fenced.append(raw.rstrip())
            continue
        if not raw.strip():
            continue
        if _MD_RULE.match(raw):
            out.append(View(height=1.0, style=Style(fill=tokens.BORDER_STRONG)))
            continue
        quoted = _MD_QUOTE.match(raw)
        if quoted:
            out.append(_quote(_plain(quoted.group(1)), ink, font, width))
            continue
        heading = _MD_HEADING.match(raw)
        if heading:
            out.append(_line(_plain(heading.group(1)), ink, tokens.BODY_STRONG))
            continue
        bullet = _MD_BULLET.match(raw)
        if bullet:
            depth = len(bullet.group(1)) // 2
            out.append(_hanging("•", bullet.group(2), ink, font, depth, width))
            continue
        number = _MD_NUMBER.match(raw)
        if number:
            depth = len(number.group(1)) // 2
            marker = f"{number.group(2)}."
            out.append(_hanging(marker, number.group(3), ink, font, depth, width))
            continue
        strong = _MD_STRONG.match(raw)
        if strong:
            label = _plain(strong.group(1)) + strong.group(2)
            out.append(_line(label, ink, tokens.BODY_STRONG))
            continue
        out.append(_body(raw, ink, font))
    flush_table()
    if fenced is not None:
        out.append(_code(fenced or [""], ink))
    return out
