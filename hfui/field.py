"""One editable string: where the caret is, what is selected, and the keymap.

The product has two text fields — the composer's prompt and the chat's — and
they were two implementations of the same three hundred lines. That is worse
than it sounds: two fields in one application that disagree about what
Option+Left does are a bug the user cannot name, and they had already drifted
(a word-delete coalesced into the surrounding run of backspaces in one of them
and not the other).

**It does not own the string.** The composer's prompt lives in a scene property
that the N-panel also writes, and the chat's is a module field; every operation
therefore takes the current text and returns what it should become, and the
surface stores it. That is the one genuine difference between the two, and it
is the only thing left in the surfaces.

Everything here is bpy-free, like the rest of `hfui`. The two things a text
field needs from the host arrive as hooks: `measure`, which answers the font,
the wrap width and the font book (all three change with the card's size, so it
is a callable and not three fields), and `clipboard`, which is an object with
`read()` and `write(text)`.

Conventions follow the host OS rather than Blender: `MAC` picks Cmd for
select-all, clipboard and undo, and Option for word jumps; elsewhere Ctrl does
both. Cmd+arrows are line and document edges on macOS, Home/End (plus Ctrl)
elsewhere. Lines are **visual** lines, since a prompt soft-wraps and holds no
newlines of its own.
"""

from __future__ import annotations

import sys

from . import motion

MAC = sys.platform == "darwin"

# How long a run of typing may get before it stops being one undo step is not
# the question; the question is how many steps are kept.
UNDO_LIMIT = 100

# The caret is a rule drawn on and off rather than a glyph, so the blink is
# a function of the clock and nothing has to be measured to run it. It is a
# `Loop` of two steps because that is exactly what it is, and because the pumps
# then ask `CARET.due()` for how long to sleep instead of each spelling out the
# same subtraction against `BLINK`.
BLINK = 0.53
CARET = motion.Loop(BLINK * 2, 2)
ZERO_WIDTH_BREAK = "\u200b"


def _wrap_space(character):
    return character == ZERO_WIDTH_BREAK or character.isspace()


def caret_on(now=None):
    """Is the caret showing at ``now``? Half a period on, half off."""
    return CARET.step(now) == 0


# ---------------------------------------------------------------------------
# Pure text geometry. No state, no host: given a string and a font book, where
# do the characters land. Both surfaces measure their cards with these.
# ---------------------------------------------------------------------------


def caret_xy(text, index, font, limit, fonts):
    """Logical ``(x, y)`` of a caret parked at ``index`` (y down from the top)."""
    _ascent, _descent, line_height = fonts.metrics(font)
    text = text or ""
    index = max(0, min(len(text), int(index)))
    if not text or index <= 0:
        return 0.0, 0.0
    spans = line_spans(text, font, limit, fonts)
    # Work backwards so a hard-wrap boundary shared by two lines belongs to
    # the start of the later line. Prefix-only wrapping cannot make that
    # decision: until the next glyph is present it still reports the previous
    # line, which made chips and the caret jump after the text had already
    # wrapped underneath them.
    for row in range(len(spans) - 1, -1, -1):
        line, bounds = spans[row]
        if not bounds or not (bounds[0] <= index <= bounds[-1]):
            continue
        column = min(
            range(len(bounds)),
            key=lambda offset: abs(bounds[offset] - index),
        )
        return fonts.width(line[:column], font), row * line_height

    # An index inside whitespace discarded by the wrapper sits at the end of
    # the preceding visual line. The next non-space boundary will map to the
    # following line through the branch above.
    for row in range(len(spans) - 1, -1, -1):
        line, bounds = spans[row]
        if bounds and bounds[-1] < index:
            return fonts.width(line, font), row * line_height
    return 0.0, 0.0


def line_spans(text, font, limit, fonts):
    """``(line, boundaries)`` per wrapped line.

    ``boundaries[i]`` is the source index in ``text`` before the line's i-th
    character, so a hit on a line maps straight back to a caret index. Wrapping
    drops the whitespace it broke on, hence the walk rather than an offset sum.
    """
    lines = fonts.wrap(text, font, limit, 0) or [""]
    spans = []
    cursor = 0
    length = len(text)
    for line in lines:
        while cursor < length and _wrap_space(text[cursor]):
            cursor += 1
        bounds = [cursor]
        for character in line:
            if character.isspace():
                while cursor < length and _wrap_space(text[cursor]):
                    cursor += 1
            else:
                while cursor < length and text[cursor] == ZERO_WIDTH_BREAK:
                    cursor += 1
                # The wrapper omits a zero-width break from the painted line.
                # Move the boundary before this glyph past the omitted source
                # character so a projected chip start still has an exact hit.
                bounds[-1] = cursor
                if cursor < length and text[cursor] == character:
                    cursor += 1
                else:
                    cursor = min(length, cursor + 1)
            bounds.append(cursor)
        spans.append((line, bounds))
    return spans


def selection_rects(text, start, end, font, limit, fonts):
    """One ``(x, y, w, h)`` per visual line the selection covers, text space."""
    if not text or start >= end:
        return ()
    _ascent, _descent, line_height = fonts.metrics(font)
    rects = []
    for row, (line, bounds) in enumerate(line_spans(text, font, limit, fonts)):
        if not line or bounds[-1] <= start or bounds[0] >= end:
            continue
        first = next((i for i in range(len(line)) if bounds[i + 1] > start), 0)
        last = next(
            (i + 1 for i in range(len(line) - 1, -1, -1) if bounds[i] < end),
            len(line),
        )
        left = fonts.width(line[:first], font) if first else 0.0
        right = fonts.width(line[:last], font)
        if right > left:
            rects.append((left, row * line_height, right - left, line_height))
    return tuple(rects)


def caret_index_at(text, font, limit, local_x, local_y, fonts):
    """Character index for a point inside the text box (y down, pad already out).

    Only the clicked line is measured: scanning every caret boundary in the
    whole string re-wrapped per boundary, which froze the UI on a long prompt.
    """
    text = text or ""
    if not text:
        return 0
    _ascent, _descent, line_height = fonts.metrics(font)
    spans = line_spans(text, font, limit, fonts)
    index = int(max(0.0, local_y) // max(line_height, 1.0))
    index = max(0, min(len(spans) - 1, index))
    line, bounds = spans[index]
    if not line:
        return bounds[0]

    best = bounds[0]
    best_dist = abs(local_x)
    width = 0.0
    for offset, character in enumerate(line):
        width += fonts.width(character, font)
        if font.tracking:
            width += font.tracking
        distance = abs(local_x - width)
        if distance < best_dist:
            best_dist = distance
            best = bounds[offset + 1]
    return max(0, min(len(text), best))


def word_left(text, index):
    """Start of the word before ``index`` (skips the run of spaces first)."""
    cursor = max(0, min(len(text), index))
    while cursor > 0 and text[cursor - 1].isspace():
        cursor -= 1
    while cursor > 0 and not text[cursor - 1].isspace():
        cursor -= 1
    return cursor


def word_right(text, index):
    """End of the word after ``index``."""
    length = len(text)
    cursor = max(0, min(length, index))
    while cursor < length and text[cursor].isspace():
        cursor += 1
    while cursor < length and not text[cursor].isspace():
        cursor += 1
    return cursor


def word_at(text, index):
    """``(start, end)`` of the word under ``index``, for a double click."""
    length = len(text)
    if not length:
        return 0, 0
    cursor = max(0, min(length - 1, index))
    if text[cursor].isspace():
        start = cursor
        while start > 0 and text[start - 1].isspace():
            start -= 1
        end = cursor
        while end < length and text[end].isspace():
            end += 1
        return start, end
    start = cursor
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    end = cursor
    while end < length and not text[end].isspace():
        end += 1
    return start, end


# ---------------------------------------------------------------------------
# The platform's modifiers
# ---------------------------------------------------------------------------


def command(event):
    """The platform's "this is a command, not typing" modifier."""
    return bool(event.oskey) if MAC else bool(event.ctrl)


# `event.type` is the physical QWERTY name on some platforms; on others
# (macOS + a Russian layout is the one that bit us) the same key arrives as
# the glyph that layout types, in `unicode` / `ascii`, and `type` is not
# `C`. Look up both so Cmd+C is Cmd+C in either layout.
_COMMAND_LETTER = {
    "A": "A",
    "C": "C",
    "V": "V",
    "X": "X",
    "Y": "Y",
    "Z": "Z",
    "Ф": "A",
    "С": "C",
    "М": "V",
    "Ч": "X",
    "Н": "Y",
    "Я": "Z",
}

# JCUKEN on the QWERTY key names Blender uses. Only reached when unicode
# and ascii are both empty — that is how a Russian layout shows up here.
_JCUKEN = {
    "Q": "й",
    "W": "ц",
    "E": "у",
    "R": "к",
    "T": "е",
    "Y": "н",
    "U": "г",
    "I": "ш",
    "O": "щ",
    "P": "з",
    "A": "ф",
    "S": "ы",
    "D": "в",
    "F": "а",
    "G": "п",
    "H": "р",
    "J": "о",
    "K": "л",
    "L": "д",
    "Z": "я",
    "X": "ч",
    "C": "с",
    "V": "м",
    "B": "и",
    "N": "т",
    "M": "ь",
    "LEFT_BRACKET": "х",
    "RIGHT_BRACKET": "ъ",
    "SEMI_COLON": "ж",
    "QUOTE": "э",
    "ACCENT_GRAVE": "ё",
    "COMMA": "б",
    "PERIOD": "ю",
}


def command_letter(event):
    """The A–Z command letter, or None.

    Same physical key in English or Russian: `C`, `С`, and `с` all mean C.
    """
    for raw in (
        event.type,
        getattr(event, "unicode", None) or "",
        getattr(event, "ascii", None) or "",
    ):
        token = str(raw)
        if not token:
            continue
        letter = _COMMAND_LETTER.get(token) or _COMMAND_LETTER.get(token.upper())
        if letter:
            return letter
    return None


def typed_character(event):
    """The printable character this key should insert, or empty.

    Prefer `unicode` / `ascii`. When both are empty the layout is not US —
    Blender still names the physical key, so JCUKEN can recover the glyph.
    """
    typed = str(getattr(event, "unicode", None) or "") or str(
        getattr(event, "ascii", None) or ""
    )
    if typed:
        return typed if typed.isprintable() else ""
    glyph = _JCUKEN.get(event.type)
    if not glyph:
        return ""
    return glyph.upper() if event.shift else glyph


def word_modifier(event):
    """The modifier that turns a motion into a word-sized one."""
    return bool(event.alt) if MAC else bool(event.ctrl)


def line_modifier(event):
    """The modifier that turns a motion into a line-sized one."""
    return bool(event.oskey) if MAC else False


class Clipboard:
    """What a field needs from the host's clipboard, so `hfui` stays bpy-free."""

    def read(self):
        return ""

    def write(self, text):
        pass


# ---------------------------------------------------------------------------
# The field
# ---------------------------------------------------------------------------


class TextField:
    """Caret, selection, undo history and the editing keymap for one string.

    Every editing method takes the current text and returns what it should
    become — see the module docstring for why the field does not hold it. The
    caret and the selection *are* held here, because they are about the act of
    editing rather than about the value.
    """

    __slots__ = (
        "caret",
        "anchor",
        "dragging",
        "claimed",
        "undo",
        "redo",
        "undo_kind",
        "cap",
        "measure",
        "clipboard",
        "on_caret",
        "on_overflow",
    )

    def __init__(
        self,
        cap=1000,
        measure=None,
        clipboard=None,
        on_caret=None,
        on_overflow=None,
    ):
        # ``None`` means "park at the end", which is what an untouched field
        # does and what `index` resolves against the text it is given.
        self.caret = None
        # The other end of the selection. None when there is none; equal to
        # `caret` after a click, which is also "no selection".
        self.anchor = None
        # True while a click-drag is sweeping a selection out.
        self.dragging = False
        # Keys consumed on press, so their release is swallowed too.
        self.claimed = set()
        self.undo = []
        self.redo = []
        self.undo_kind = None
        # Longest string the field will hold. The composer's prompt is a job
        # parameter and the chat's is a message, so the two differ — and the
        # composer's differs again per model, which is why this may also be a
        # callable, for the reason `measure` is one.
        self.cap = cap
        # () -> (font, limit, fonts). A callable because the wrap width
        # changes with the card, and the font book is rebuilt on teardown.
        self.measure = measure
        self.clipboard = clipboard or Clipboard()
        # Called after any caret move. The composer re-arms its scroller from
        # this; the chat chases the caret from a key instead.
        self.on_caret = on_caret
        # Called ``(dropped, limit)`` when the cap refused part of an edit. The
        # cap is the host's number and `hfui` has no way to tell the user
        # anything, so silently losing the tail of a paste is all this layer
        # could otherwise do.
        self.on_overflow = on_overflow

    # -- reading ------------------------------------------------------------

    def index(self, text):
        """The caret, clamped to a text that may have changed under it."""
        if self.caret is None:
            return len(text or "")
        return max(0, min(len(text or ""), int(self.caret)))

    def selection(self):
        """``(start, end)`` of the selected run, or None when there is none."""
        if self.anchor is None or self.caret is None or self.anchor == self.caret:
            return None
        return (min(self.anchor, self.caret), max(self.anchor, self.caret))

    def selected(self, text):
        span = self.selection()
        return text[span[0] : span[1]] if span else ""

    def editing_run(self):
        """Whether a coalescing edit run is open. Only undo cares."""
        return self.undo_kind is not None

    def limit(self):
        """Characters this field will hold, asking ``cap`` if it is callable."""
        value = self.cap() if callable(self.cap) else self.cap
        return max(1, int(value or 1))

    # -- the caret ----------------------------------------------------------

    def set_caret(self, index, extend=False):
        """Move the caret, keeping or dropping the selection anchor."""
        if extend and self.anchor is None:
            self.anchor = self.caret
        elif not extend:
            self.anchor = None
        self.caret = int(index)
        # Moving is what ends a typing run: the next character belongs
        # somewhere else, so undo must be able to stop between the two.
        self.undo_kind = None
        if self.on_caret is not None:
            self.on_caret()
        return self.caret

    def select_all(self, text):
        self.anchor = 0
        self.caret = len(text)
        if self.on_caret is not None:
            self.on_caret()
        return self.caret

    def move(self, text, delta=0, to=None, extend=False):
        """Move by ``delta`` or jump to ``to``."""
        if to is not None:
            target = int(to)
        else:
            target = self.index(text) + int(delta)
            # An unextended arrow with a selection up collapses to its edge,
            # the way every other text field behaves.
            span = self.selection()
            if span is not None and not extend and delta:
                target = span[0] if delta < 0 else span[1]
        return self.set_caret(max(0, min(len(text), target)), extend)

    def move_by_word(self, text, forward=False, extend=False):
        index = self.index(text)
        target = word_right(text, index) if forward else word_left(text, index)
        return self.set_caret(target, extend)

    def line_bounds(self, text, index):
        """``(start, end)`` of the visual line ``index`` sits on."""
        if not text:
            return 0, 0
        font, limit, fonts = self.measure()
        spans = line_spans(text, font, limit, fonts)
        for row, (_line, bounds) in enumerate(spans):
            if not bounds:
                continue
            # Wrapping eats the whitespace it broke on, so the last line's
            # span stops short of a trailing space. End should still go past.
            last = len(text) if row == len(spans) - 1 else bounds[-1]
            if bounds[0] <= index <= last:
                return bounds[0], last
        return 0, len(text)

    def move_to_line_edge(self, text, end=False, extend=False):
        """Home / End on the *visual* line — the field soft-wraps."""
        start, stop = self.line_bounds(text, self.index(text))
        return self.set_caret(stop if end else start, extend)

    def move_by_line(self, text, down=False, extend=False):
        """Up / down a visual line, keeping the caret's x where it can."""
        if not text:
            return self.caret
        font, limit, fonts = self.measure()
        _ascent, _descent, line_height = fonts.metrics(font)
        x, y = caret_xy(text, self.index(text), font, limit, fonts)
        target_y = y + (line_height if down else -line_height)
        if target_y < 0:
            return self.set_caret(0, extend)
        if target_y >= line_height * len(fonts.wrap(text, font, limit, 0) or [""]):
            return self.set_caret(len(text), extend)
        return self.set_caret(
            caret_index_at(text, font, limit, x, target_y, fonts), extend
        )

    def index_at(self, text, local_x, local_y):
        """Caret index for a point inside the field, padding already removed."""
        font, limit, fonts = self.measure()
        return caret_index_at(text, font, limit, local_x, local_y, fonts)

    # -- editing ------------------------------------------------------------

    def apply(self, text, mutate, coalesce=None):
        """Run ``mutate(text, caret)`` with undo around it. Returns the text.

        ``coalesce`` labels the edit so a run of the same kind — typing a word,
        holding backspace — collapses into one undo step instead of one a key.
        """
        caret = self.index(text)
        self.push(text, caret, coalesce)
        text, caret = mutate(text, caret)
        # Every mutating method comes through here, so this is the one place
        # the cap has to be enforced and the one place that can report it.
        edited, limit = (text or ""), self.limit()
        text = edited[:limit]
        self.caret = max(0, min(len(text), int(caret)))
        self.anchor = None
        if len(edited) > limit and self.on_overflow is not None:
            self.on_overflow(len(edited) - limit, limit)
        return text

    def replacing(self, chunk):
        """A mutate that drops the selection (if any) and inserts ``chunk``."""
        span = self.selection()

        def mutate(text, caret):
            if span is None:
                return text[:caret] + chunk + text[caret:], caret + len(chunk)
            start = max(0, min(len(text), span[0]))
            end = max(0, min(len(text), span[1]))
            return text[:start] + chunk + text[end:], start + len(chunk)

        return mutate

    def insert(self, text, chunk):
        """Type ``chunk`` in, replacing the selection."""
        kind = "type" if len(chunk) == 1 and not chunk.isspace() else None
        if self.selection() is not None:
            # Replacing a selection is one discrete act, not part of the run
            # of typing before it: undo has to bring the replaced text back.
            kind = None
        return self.apply(text, self.replacing(chunk), coalesce=kind)

    def backspace(self, text, word=False, line=False):
        """Delete backwards: the selection, a word, to the line start, or one."""
        if self.selection() is not None:
            return self.apply(text, self.replacing(""))
        start_of_line = self.line_bounds(text, self.index(text))[0] if line else None

        def mutate(value, caret):
            if caret <= 0:
                return value, caret
            if line:
                start = start_of_line
            elif word:
                start = word_left(value, caret)
            else:
                start = caret - 1
            return value[:start] + value[caret:], start

        # A word or line delete is one deliberate act and must not fold into
        # the run of single backspaces around it.
        return self.apply(text, mutate, coalesce=None if (word or line) else "erase")

    def delete_forward(self, text, word=False):
        if self.selection() is not None:
            return self.apply(text, self.replacing(""))

        def mutate(value, caret):
            if caret >= len(value):
                return value, caret
            end = word_right(value, caret) if word else caret + 1
            return value[:caret] + value[end:], caret

        return self.apply(text, mutate, coalesce=None if word else "erase")

    # -- the clipboard ------------------------------------------------------

    def copy(self, text):
        chunk = self.selected(text)
        if not chunk:
            return False
        self.clipboard.write(chunk)
        return True

    def cut(self, text):
        """Returns ``(text, cut)``; the text is unchanged when nothing was."""
        if not self.copy(text):
            return text, False
        return self.apply(text, self.replacing("")), True

    def paste(self, text):
        """Returns ``(text, pasted)``."""
        chunk = str(self.clipboard.read() or "")
        # A pasted newline would be a line the field cannot show: Enter is
        # either a commit or a send, never a break.
        chunk = chunk.replace("\r\n", " ").replace("\n", " ").replace("\r", " ")
        if not chunk:
            return text, False
        return self.insert(text, chunk), True

    # -- undo ---------------------------------------------------------------

    def push(self, text, caret, coalesce=None):
        self.redo.clear()
        if self.undo and coalesce is not None and coalesce == self.undo_kind:
            # Same kind of edit continuing: the entry already on the stack is
            # the state this run started from.
            return
        self.undo.append((text, caret))
        del self.undo[:-UNDO_LIMIT]
        self.undo_kind = coalesce

    def _land(self, entry):
        text, caret = entry
        self.caret = max(0, min(len(text), int(caret)))
        self.anchor = None
        self.undo_kind = None
        return text

    def step_back(self, text):
        """Returns ``(text, moved)``."""
        if not self.undo:
            return text, False
        self.redo.append((text, self.index(text)))
        return self._land(self.undo.pop()), True

    def step_forward(self, text):
        """Returns ``(text, moved)``."""
        if not self.redo:
            return text, False
        self.undo.append((text, self.index(text)))
        return self._land(self.redo.pop()), True

    def forget(self):
        """Drop the history — a commit, or a field that has been emptied."""
        self.undo.clear()
        self.redo.clear()
        self.undo_kind = None

    # -- the keymap ---------------------------------------------------------

    def keys(self, text, event, submit=None, cancel=None):
        """One key press. Returns ``(text, handled)``.

        Everything the field does not claim is left for Blender, so holding
        focus costs the user their editing keys and nothing else — the whole
        keymap is here rather than in the modal so the platform conventions
        are in one readable place. `submit` is Enter and `cancel` is Escape or
        Tab; both are the surface's, because committing a value and sending a
        message are not the same act.
        """
        if event.value not in {"PRESS", "REPEAT"}:
            # A key swallowed on press must not resurface on release.
            return text, event.value == "RELEASE" and event.type in self.claimed
        kind = event.type
        letter = command_letter(event)
        shift = bool(event.shift)
        cmd = command(event)
        handled = True

        if kind in {"RET", "NUMPAD_ENTER"}:
            if submit is not None:
                submit()
        elif kind in {"ESC", "TAB"}:
            if cancel is not None:
                cancel()
        elif letter == "A" and cmd:
            self.select_all(text)
        elif letter == "C" and cmd:
            self.copy(text)
        elif letter == "X" and cmd:
            text, _cut = self.cut(text)
        elif letter == "V" and cmd:
            text, _pasted = self.paste(text)
        elif letter == "Z" and cmd:
            text, _moved = self.step_forward(text) if shift else self.step_back(text)
        elif letter == "Y" and cmd and not MAC:
            text, _moved = self.step_forward(text)
        elif kind == "LEFT_ARROW":
            if line_modifier(event):
                self.move_to_line_edge(text, end=False, extend=shift)
            elif word_modifier(event):
                self.move_by_word(text, forward=False, extend=shift)
            else:
                self.move(text, delta=-1, extend=shift)
        elif kind == "RIGHT_ARROW":
            if line_modifier(event):
                self.move_to_line_edge(text, end=True, extend=shift)
            elif word_modifier(event):
                self.move_by_word(text, forward=True, extend=shift)
            else:
                self.move(text, delta=1, extend=shift)
        elif kind == "UP_ARROW":
            if cmd:
                self.move(text, to=0, extend=shift)
            else:
                self.move_by_line(text, down=False, extend=shift)
        elif kind == "DOWN_ARROW":
            if cmd:
                self.move(text, to=len(text), extend=shift)
            else:
                self.move_by_line(text, down=True, extend=shift)
        elif kind == "HOME":
            if cmd:
                self.move(text, to=0, extend=shift)
            else:
                self.move_to_line_edge(text, end=False, extend=shift)
        elif kind == "END":
            if cmd:
                self.move(text, to=len(text), extend=shift)
            else:
                self.move_to_line_edge(text, end=True, extend=shift)
        elif kind == "BACK_SPACE":
            text = self.backspace(
                text, word=word_modifier(event), line=line_modifier(event)
            )
        elif kind == "DEL":
            text = self.delete_forward(text, word=word_modifier(event))
        elif not cmd and not event.alt and not event.oskey:
            typed = typed_character(event)
            if typed:
                # Consumed, so typing "g" edits the field instead of starting
                # a grab in the viewport.
                text = self.insert(text, typed)
            else:
                handled = False
        else:
            handled = False

        if handled:
            self.claimed.add(kind)
        return text, handled

    # -- pointer ------------------------------------------------------------

    def press(self, index, extend=False, drag=False):
        """A click at ``index``. ``drag`` arms the sweep, so only a press does.

        A click that merely focuses the field must not leave the pointer
        selecting on the next move.
        """
        self.set_caret(index, extend)
        if drag:
            self.dragging = True
        return self.caret

    def drag_to(self, index):
        """Sweep the selection out to ``index``. True if the caret moved."""
        if not self.dragging or index == self.caret:
            return False
        self.set_caret(index, extend=True)
        return True

    def end_drag(self):
        was, self.dragging = self.dragging, False
        return was

    def select_word(self, text, index):
        self.anchor, self.caret = word_at(text, index)
        self.dragging = False
        if self.on_caret is not None:
            self.on_caret()
        return self.caret
