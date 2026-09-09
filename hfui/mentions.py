"""Atomic reference-element mentions inside a plain-text prompt.

Blender has no DOM and the viewport UI is Skia, so Lexical's decorator nodes
are represented by one private-use character in the editable string.  The
character is one caret step; the document keeps the stable element identity;
the surface projects it to a run of visible-width braille blanks and paints a
chip over that run.

The private marker never leaves the add-on.  ``visible`` turns it into
``@Display name`` for local history and clipboard, while ``wire`` turns it
into the platform token ``<<<uuid>>>`` for the Supercomputer request.
"""

from __future__ import annotations

from dataclasses import dataclass

MARKER_FIRST = 0xE000
MARKER_LAST = 0xF8FF
# A break opportunity with no painted glyph. Atomic chips need one on both
# sides: two adjacent markers must be allowed to wrap between their pills, and
# ordinary text touching a marker must not turn both into one unbreakable word.
SOFT_BREAK = "\u200b"


@dataclass(frozen=True)
class ElementMention:
    id: str
    # The backend's mention handle (``@alice``), not ``display_name``.
    name: str
    category: str = ""
    thumbnail: str = ""
    display_name: str = ""

    @property
    def title(self):
        return self.display_name or self.name or f"Element · {self.id[:8]}"

    @property
    def label(self):
        return "@" + (self.name or self.id[:8])

    def data(self):
        return {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "thumbnail": self.thumbnail,
            "display_name": self.display_name,
        }

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict):
            return None
        element_id = str(data.get("id") or "").strip()
        if not element_id:
            return None
        return cls(
            element_id,
            str(data.get("name") or "").strip(),
            str(data.get("category") or "").strip(),
            str(data.get("thumbnail") or "").strip(),
            str(data.get("display_name") or "").strip(),
        )


@dataclass(frozen=True)
class ProjectedMention:
    marker: str
    element: ElementMention
    logical_index: int
    start: int
    end: int


@dataclass(frozen=True)
class Projection:
    text: str
    boundaries: tuple[int, ...]
    mentions: tuple[ProjectedMention, ...]

    def display_index(self, logical_index):
        if not self.boundaries:
            return 0
        index = max(0, min(len(self.boundaries) - 1, int(logical_index)))
        return self.boundaries[index]

    def logical_index(self, display_index):
        """Map a caret in projected text back onto the logical document."""
        display_index = max(0, min(len(self.text), int(display_index)))
        for mention in self.mentions:
            if mention.start < display_index < mention.end:
                midpoint = mention.start + (mention.end - mention.start) / 2.0
                return mention.logical_index + (display_index >= midpoint)
        best = min(
            range(len(self.boundaries)),
            key=lambda index: abs(self.boundaries[index] - display_index),
        )
        return int(best)

    def insert(self, logical_index, reserve):
        """Insert a display-only token (the selected Blender object group)."""
        logical_index = max(
            0, min(len(self.boundaries) - 1, int(logical_index))
        )
        start = self.display_index(logical_index)
        reserve = str(reserve or "")
        if not reserve:
            return self, start, start
        inserted = SOFT_BREAK + reserve + SOFT_BREAK
        size = len(inserted)
        boundaries = tuple(
            value + size if index >= logical_index else value
            for index, value in enumerate(self.boundaries)
        )
        shifted = tuple(
            ProjectedMention(
                item.marker,
                item.element,
                item.logical_index,
                item.start + (size if item.start >= start else 0),
                item.end + (size if item.start >= start else 0),
            )
            for item in self.mentions
        )
        return (
            Projection(
                self.text[:start] + inserted + self.text[start:],
                boundaries,
                shifted,
            ),
            start + len(SOFT_BREAK),
            start + len(SOFT_BREAK) + len(reserve),
        )


class MentionDocument:
    """Registry for the private marker characters used by one draft."""

    __slots__ = ("entries", "next_codepoint")

    def __init__(self, entries=None, next_codepoint=MARKER_FIRST):
        self.entries = dict(entries or {})
        self.next_codepoint = max(MARKER_FIRST, int(next_codepoint))

    def _marker(self):
        for codepoint in range(self.next_codepoint, MARKER_LAST + 1):
            marker = chr(codepoint)
            if marker not in self.entries:
                self.next_codepoint = codepoint + 1
                return marker
        for codepoint in range(MARKER_FIRST, self.next_codepoint):
            marker = chr(codepoint)
            if marker not in self.entries:
                return marker
        raise RuntimeError("This prompt contains too many element mentions.")

    def add(self, element):
        marker = self._marker()
        self.entries[marker] = element
        return marker

    def element(self, marker):
        return self.entries.get(marker)

    def active(self, text):
        return tuple(
            self.entries[character]
            for character in str(text or "")
            if character in self.entries
        )

    def visible(self, text):
        return "".join(
            self.entries[character].label
            if character in self.entries
            else character
            for character in str(text or "")
        )

    def wire(self, text):
        return "".join(
            f"<<<{self.entries[character].id}>>>"
            if character in self.entries
            else character
            for character in str(text or "")
        )

    def project(self, text, reserve_for):
        shown = []
        boundaries = [0]
        projected = []
        position = 0
        for logical_index, character in enumerate(str(text or "")):
            element = self.entries.get(character)
            if element is None:
                shown.append(character)
                position += 1
            else:
                shown.append(SOFT_BREAK)
                position += len(SOFT_BREAK)
                start = position
                reserve = str(reserve_for(element) or "\u2800")
                shown.append(reserve)
                position += len(reserve)
                projected.append(
                    ProjectedMention(
                        character,
                        element,
                        logical_index,
                        start,
                        position,
                    )
                )
                shown.append(SOFT_BREAK)
                position += len(SOFT_BREAK)
            boundaries.append(position)
        return Projection("".join(shown), tuple(boundaries), tuple(projected))

    def data(self):
        return {
            "next": self.next_codepoint,
            "entries": [
                {"marker": ord(marker), **element.data()}
                for marker, element in self.entries.items()
            ],
        }

    @classmethod
    def from_data(cls, data):
        if not isinstance(data, dict):
            return cls()
        entries = {}
        for row in data.get("entries") or ():
            if not isinstance(row, dict):
                continue
            try:
                marker = chr(int(row.get("marker")))
            except (TypeError, ValueError, OverflowError):
                continue
            element = ElementMention.from_data(row)
            if element is not None and MARKER_FIRST <= ord(marker) <= MARKER_LAST:
                entries[marker] = element
        return cls(entries, data.get("next") or MARKER_FIRST)


def query_at(text, caret):
    """Return ``(start, end, query)`` for an unfinished ``@query`` at caret."""
    text = str(text or "")
    caret = max(0, min(len(text), int(caret)))
    start = caret
    while start > 0 and not text[start - 1].isspace():
        if MARKER_FIRST <= ord(text[start - 1]) <= MARKER_LAST:
            break
        start -= 1
    fragment = text[start:caret]
    if not fragment.startswith("@") or fragment.startswith("@@"):
        return None
    query = fragment[1:]
    if any(character in query for character in "<>@"):
        return None
    return start, caret, query
