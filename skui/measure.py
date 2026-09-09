"""Fonts and images: everything the solver cannot compute from arithmetic alone.

``Measure`` is the object the solver asks for sizes. It is also the cache the
painter draws from, so a string is shaped once per frame at most and an SVG is
parsed once per process.

Bundled TTFs under ``resources/fonts`` win over system faces so the overlay
matches the design on every machine; when they are absent the book falls back
through a per-platform family list, and then per character for scripts the
chosen face cannot render.
"""

from __future__ import annotations

import logging
import os

from .nodes import Font

logger = logging.getLogger(__name__)

_skia = None
_skia_failed = False

FONT_DIR = os.path.join(os.path.dirname(__file__), "..", "resources", "fonts")

# Used when a requested family has neither a bundled file nor a system match.
_FALLBACK_FAMILIES = (
    "Inter",
    "Helvetica Neue",
    "Segoe UI",
    "Ubuntu",
    "DejaVu Sans",
    "Arial",
    "Helvetica",
)


def skia_module():
    """Import skia once; ``None`` when the wheel is unavailable."""
    global _skia, _skia_failed
    if _skia is None and not _skia_failed:
        try:
            import skia
        except Exception:
            _skia_failed = True
            logger.exception("skia-python is unavailable")
        else:
            _skia = skia
    return _skia


def available():
    return skia_module() is not None


# Distinct strings the width memo holds before it starts over. A wrap measures
# one entry per word and a selection sweep one per prefix, so the working set
# is small and bounded by what is on screen; the cap only exists so that an
# afternoon of editing cannot grow it without limit. Clearing wholesale beats
# evicting — the next frame re-measures what it still needs, and nothing has
# to be tracked per entry to make that work.
WIDTH_CACHE = 4096
ZERO_WIDTH_BREAK = "\u200b"


def _wrap_words(value):
    """Return ``(word, separator)`` while treating U+200B as a zero-width gap."""
    words = []
    word = []
    separator = None
    for character in str(value or ""):
        if character == ZERO_WIDTH_BREAK or character.isspace():
            if word:
                words.append(("".join(word), separator))
                word = []
                separator = None
            if character == ZERO_WIDTH_BREAK:
                if separator is None:
                    separator = ""
            else:
                separator = " "
            continue
        word.append(character)
    if word:
        words.append(("".join(word), separator))
    return words


class FontBook:
    """(family, weight) → typeface, and (typeface, size) → sized font."""

    def __init__(self, directory=FONT_DIR):
        self.directory = os.path.normpath(directory)
        self._manager = None
        self._files = None
        self._typefaces = {}
        self._sized = {}
        self._per_character = {}
        self._ascii = {}
        self._widths = {}
        self._metrics = {}

    @property
    def manager(self):
        if self._manager is None:
            self._manager = skia_module().FontMgr()
        return self._manager

    @property
    def files(self):
        """Lower-cased file stem → path, for the bundled TTFs."""
        if self._files is None:
            found = {}
            if os.path.isdir(self.directory):
                for name in sorted(os.listdir(self.directory)):
                    if name.lower().endswith((".ttf", ".otf")):
                        stem = os.path.splitext(name)[0].lower()
                        found[stem] = os.path.join(self.directory, name)
            self._files = found
        return self._files

    def _bundled(self, family, weight, size):
        """Match ``Inter-SemiBold.ttf`` first, then a variable ``Inter*.ttf``."""
        slug = family.replace(" ", "").lower()
        named = _WEIGHT_NAMES.get(weight, "")
        for stem, path in self.files.items():
            flat = stem.replace(" ", "").replace("_", "")
            if named and flat == f"{slug}-{named}".lower():
                return self._instance(path, weight, size)
        for stem, path in self.files.items():
            if stem.replace(" ", "").startswith(slug):
                return self._instance(path, weight, size)
        return None

    def _instance(self, path, weight, size):
        """Load a face and pin its variable axes to this weight and size."""
        skia = skia_module()
        typeface = None
        try:
            typeface = self.manager.makeFromFile(path, 0)
            axes = {
                axis.tag: axis
                for axis in (typeface.getVariationDesignParameters() or [])
            }
            wanted = []
            if _tag("wght") in axes:
                wanted.append((_tag("wght"), float(weight)))
            optical = axes.get(_tag("opsz"))
            if optical is not None:
                # Optical sizing on auto, the way Figma and browsers do it.
                wanted.append(
                    (_tag("opsz"), min(max(float(size), optical.min), optical.max))
                )
            if wanted:
                # The position is referenced, not copied: keep it alive until
                # makeClone has run.
                coordinates = skia.FontArguments.VariationPosition.Coordinates(
                    [
                        skia.FontArguments.VariationPosition.Coordinate(tag, value)
                        for tag, value in wanted
                    ]
                )
                position = skia.FontArguments.VariationPosition(coordinates)
                arguments = skia.FontArguments()
                arguments.setVariationDesignPosition(position)
                clone = typeface.makeClone(arguments)
                if clone is not None:
                    return clone
        except Exception:
            logger.debug("variable instancing failed for %s", path, exc_info=True)
        if typeface is None:
            try:
                typeface = skia.Typeface.MakeFromFile(path)
            except Exception:
                typeface = None
        return typeface

    def typeface(self, family, weight=400, size=12.0):
        key = (family, int(weight), round(float(size), 1))
        if key in self._typefaces:
            return self._typefaces[key]
        skia = skia_module()
        style = skia.FontStyle(
            int(weight), skia.FontStyle.kNormal_Width, skia.FontStyle.kUpright_Slant
        )
        found = self._bundled(family, weight, size)
        if found is None:
            found = self.manager.matchFamilyStyle(family, style)
        if found is None:
            for candidate in _FALLBACK_FAMILIES:
                found = self.manager.matchFamilyStyle(candidate, style)
                if found is not None:
                    break
        if found is None:
            found = skia.Typeface.MakeEmpty()
        self._typefaces[key] = found
        return found

    def sized(self, font: Font):
        """A ``skia.Font`` for a token, ready to measure or draw with."""
        key = (font.family, int(font.weight), round(float(font.size), 2))
        cached = self._sized.get(key)
        if cached is None:
            skia = skia_module()
            cached = skia.Font(
                self.typeface(font.family, font.weight, font.size), float(font.size)
            )
            cached.setSubpixel(True)
            cached.setEdging(skia.Font.Edging.kSubpixelAntiAlias)
            self._sized[key] = cached
        return cached

    def _character_face(self, font, character):
        key = (font.family, int(font.weight), character)
        if key in self._per_character:
            return self._per_character[key]
        skia = skia_module()
        found = None
        try:
            found = self.manager.matchFamilyStyleCharacter(
                "", skia.FontStyle.Normal(), ["en"], character
            )
        except Exception:
            found = None
        self._per_character[key] = found
        return found

    def covers_ascii(self, font: Font):
        """Whether the base face can render printable ASCII on its own."""
        key = (font.family, int(font.weight), round(float(font.size), 2))
        answer = self._ascii.get(key)
        if answer is None:
            base = self.sized(font)
            answer = all(base.unicharToGlyph(code) != 0 for code in range(32, 127))
            self._ascii[key] = answer
        return answer

    def runs(self, value, font: Font):
        """Split ``value`` into ``(skia.Font, text)`` runs that can render.

        The general path asks the base face about every character to find the
        ones it has to hand off, which is a Python loop over the whole string
        on every measurement and every paint. Latin text is the overwhelming
        majority of what the overlay draws and it never needs a hand-off, so
        the answer for the whole face is worked out once and a plain ASCII
        string then skips the walk entirely.
        """
        base = self.sized(font)
        if value.isascii() and self.covers_ascii(font):
            return ((base, value),) if value else ()
        runs = []
        current = []
        current_font = base
        for character in value:
            face = base
            if base.unicharToGlyph(ord(character)) == 0 and not character.isspace():
                fallback = self._character_face(font, ord(character))
                if fallback is not None:
                    skia = skia_module()
                    face = skia.Font(fallback, float(font.size))
                    face.setSubpixel(True)
                    face.setEdging(skia.Font.Edging.kSubpixelAntiAlias)
            if face is not current_font and current:
                runs.append((current_font, "".join(current)))
                current = []
            current_font = face
            current.append(character)
        if current:
            runs.append((current_font, "".join(current)))
        return runs

    def width(self, value, font: Font):
        if not value:
            return 0.0
        key = (value, font)
        total = self._widths.get(key)
        if total is None:
            total = sum(face.measureText(text) for face, text in self.runs(value, font))
            if font.tracking:
                total += font.tracking * len(value)
            if len(self._widths) >= WIDTH_CACHE:
                self._widths.clear()
            self._widths[key] = total
        return total

    def metrics(self, font: Font):
        """``(ascent, descent, line_height)``, all positive except ascent."""
        cached = self._metrics.get(font)
        if cached is None:
            raw = self.sized(font).getMetrics()
            ascent, descent = abs(raw.fAscent), abs(raw.fDescent)
            height = font.line_height if font.line_height else ascent + descent
            cached = self._metrics[font] = (ascent, descent, height)
        return cached

    def ellipsize(self, value, font: Font, limit):
        # Half a pixel of slack: callers derive the limit from width() itself
        # and an exact comparison loses a character to float error.
        if self.width(value, font) <= limit + 0.5:
            return value
        low, high = 0, len(value)
        while low < high:
            middle = (low + high + 1) // 2
            if self.width(value[:middle] + "…", font) <= limit:
                low = middle
            else:
                high = middle - 1
        return (value[:low] + "…") if low else ""

    def _break_word(self, word, font: Font, limit):
        """Pieces of ``word`` that each fit ``limit``. One piece if it already does."""
        if limit is None or self.width(word, font) <= limit + 0.5:
            return [word]
        pieces = []
        start = 0
        while start < len(word):
            remaining = word[start:]
            if self.width(remaining, font) <= limit + 0.5:
                pieces.append(remaining)
                break
            # Longest prefix of remaining that still fits. At least one character,
            # even if that character alone is wider than the line — otherwise an
            # unbreakable glyph would loop forever.
            low, high = 1, len(remaining)
            best = 1
            while low <= high:
                middle = (low + high) // 2
                if self.width(remaining[:middle], font) <= limit + 0.5:
                    best = middle
                    low = middle + 1
                else:
                    high = middle - 1
            pieces.append(remaining[:best])
            start += best
        return pieces

    def wrap(self, value, font: Font, limit, max_lines):
        """Greedy word wrap; words wider than ``limit`` break mid-word.

        Line width is accumulated per word rather than re-measured for every
        candidate: measuring the whole line again for each word made wrapping
        quadratic, which showed up as a stall on a long prompt. Widths are
        additive here because ``width`` sums per-run measurements and tracking
        without kerning across the join.

        The last allowed line is ellipsized when ``max_lines`` truncates the
        rest — including the case where a single overlong word is what spills.
        """
        words = _wrap_words(value)
        if not words:
            return [""]
        space = self.width(" ", font)
        lines = []
        current = ""
        used = 0.0
        for word, separator in words:
            for index, piece in enumerate(self._break_word(word, font, limit)):
                piece_width = self.width(piece, font)
                if index > 0:
                    # Forced break inside an overlong word: the previous piece
                    # already filled the line.
                    lines.append(current)
                    current, used = piece, piece_width
                    continue
                if not current:
                    current, used = piece, piece_width
                else:
                    gap = space if separator else 0.0
                    if limit is None or used + gap + piece_width <= limit + 0.5:
                        current = f"{current}{separator or ''}{piece}"
                        used += gap + piece_width
                        continue
                    lines.append(current)
                    current, used = piece, piece_width
        if current or not lines:
            lines.append(current)
        if max_lines and len(lines) > max_lines:
            head = lines[: max_lines - 1]
            rest = " ".join(lines[max_lines - 1 :])
            return head + [self.ellipsize(rest, font, limit)]
        return lines


_WEIGHT_NAMES = {
    100: "Thin",
    200: "ExtraLight",
    300: "Light",
    400: "Regular",
    500: "Medium",
    600: "SemiBold",
    700: "Bold",
    800: "ExtraBold",
    900: "Black",
}


def _tag(name):
    """Four-character OpenType axis tag as the integer Skia expects."""
    return (
        (ord(name[0]) << 24) | (ord(name[1]) << 16) | (ord(name[2]) << 8) | ord(name[3])
    )


class Loaded:
    """A decoded image plus the size it wants to be at scale 1."""

    __slots__ = ("kind", "handle", "width", "height")

    def __init__(self, kind, handle, width, height):
        self.kind = kind
        self.handle = handle
        self.width = width
        self.height = height


# A user's reference photo can be 12 megapixels, which is 48 MB of decoded
# bitmap held for the life of the session to fill a thumbnail. Nothing this
# layer draws needs more than a screen's worth of detail.
MAX_PIXELS = 2048.0


def _bucket(target):
    """The next power of two at or above ``target``, floored at 64.

    Bucketed rather than exact so a surface that animates the size it draws an
    image at — or draws the same image at two sizes — reuses one copy instead
    of resampling into a new one every frame.
    """
    size = 64
    while size < target:
        size *= 2
    return size


class ImageBook:
    """Decode-once cache for raster files, SVG files and in-memory bytes.

    With ``deferred`` set, a raster that would need file I/O, a decode or a
    resample on this call returns ``None`` instead, and the miss lands in
    ``missing`` (source → the largest bucket anything asked for). The surface
    that set the flag harvests that dict after the draw and hands it to its
    warm timer, which calls back with ``force=True`` — so the cost of a cold
    image is paid on a frame nobody is watching, never inside a draw handler.
    SVGs stay synchronous: they are the UI's own icons, parse in microseconds,
    and a chrome glyph that pops in late reads as a glitch.
    """

    def __init__(self):
        self._cache = {}
        self._scaled = {}
        self.deferred = False
        self.missing = {}

    @staticmethod
    def _key(source):
        return source if isinstance(source, (str, bytes)) else id(source)

    @staticmethod
    def _defers(source):
        """Only rasters defer; an SVG parse is cheap and icons cannot pop in."""
        if isinstance(source, str):
            return not source.lower().endswith(".svg")
        if isinstance(source, bytes):
            head = source.lstrip()[:5].lower()
            return not (head.startswith(b"<svg") or head.startswith(b"<?xml"))
        return False

    def load(self, source, force=False):
        if source is None:
            return None
        key = self._key(source)
        if key in self._cache:
            return self._cache[key]
        if self.deferred and not force and self._defers(source):
            self.missing.setdefault(source, 0.0)
            return None
        loaded = self._load(source)
        self._cache[key] = loaded
        return loaded

    def scaled(self, source, target, force=False):
        """The decoded image, downsampled to roughly the size it is drawn at.

        Decoding once is not enough on its own. A generation thumbnail is 896
        to 2048 pixels on disk and lands in a 96-device-pixel tile, and Skia
        was resampling the full bitmap on every frame: four of them made the
        island's paint 1.6 ms, most of the cost of a morph frame. Resampling
        once into a bucket and letting the draw scale the small copy the rest
        of the way is visually identical at these sizes.
        """
        loaded = self.load(source, force=force)
        if loaded is None:
            if (
                self.deferred
                and not force
                and source is not None
                and target > 0
                and self._key(source) not in self._cache
            ):
                # Bucketed, so the warm resamples the exact copy the next
                # paint will ask for rather than a float away from it.
                self.missing[source] = max(
                    self.missing.get(source, 0.0), float(_bucket(target))
                )
            return None
        if loaded.kind != "raster" or target <= 0:
            return loaded
        longest = max(loaded.width, loaded.height)
        bucket = _bucket(target)
        if bucket >= longest:
            return loaded
        key = (self._key(source), bucket)
        hit = self._scaled.get(key)
        if hit is not None:
            return hit
        if self.deferred and not force:
            self.missing[source] = max(self.missing.get(source, 0.0), float(bucket))
            return None
        skia = skia_module()
        ratio = bucket / longest
        width = max(1, int(round(loaded.width * ratio)))
        height = max(1, int(round(loaded.height * ratio)))
        # Mipmapped, not the default: this is a 10x reduction, and nearest
        # sampling at that ratio throws away most of the picture and keeps
        # whichever pixels it landed on. It costs 0.4 ms once.
        image = loaded.handle.resize(
            width,
            height,
            skia.SamplingOptions(skia.FilterMode.kLinear, skia.MipmapMode.kLinear),
        )
        if image is None:
            return loaded
        hit = Loaded("raster", image, float(width), float(height))
        self._scaled[key] = hit
        return hit

    def _load(self, source):
        skia = skia_module()
        try:
            if isinstance(source, str):
                if source.lower().endswith(".svg"):
                    with open(source, "rb") as handle:
                        return self._svg(handle.read())
                return self._raster(skia.Image.open(source))
            if isinstance(source, bytes):
                head = source.lstrip()[:5].lower()
                if head.startswith(b"<svg") or head.startswith(b"<?xml"):
                    return self._svg(source)
                data = skia.Data.MakeWithoutCopy(source)
                return self._raster(skia.Image.MakeFromEncoded(data))
            if isinstance(source, skia.Image):
                return self._raster(source)
        except Exception:
            logger.warning("image failed to load: %r", _short(source), exc_info=True)
        return None

    def _raster(self, image):
        """Adopt a decoded image, shrinking anything larger than the cap."""
        if image is None:
            return None
        width, height = float(image.width()), float(image.height())
        longest = max(width, height)
        if longest > MAX_PIXELS:
            ratio = MAX_PIXELS / longest
            width, height = round(width * ratio), round(height * ratio)
            image = image.resize(int(width), int(height))
            if image is None:
                return None
        return Loaded("raster", image, float(width), float(height))

    def _svg(self, payload):
        skia = skia_module()
        stream = skia.MemoryStream(payload)
        dom = skia.SVGDOM.MakeFromStream(stream)
        if dom is None:
            return None
        size = dom.containerSize()
        width, height = float(size.width()), float(size.height())
        if width <= 0 or height <= 0:
            width = height = 24.0
            dom.setContainerSize(skia.Size(width, height))
        return Loaded("svg", dom, width, height)

    def clear(self):
        self._cache.clear()
        self._scaled.clear()
        self.missing.clear()


def _short(source):
    return source if isinstance(source, str) else f"<{len(source)} bytes>"


class Measure:
    """What ``solve`` asks for sizes, and what ``paint`` draws from."""

    def __init__(self, fonts=None, images=None, scale=1.0):
        self.fonts = fonts or FontBook()
        self.images = images or ImageBook()
        self.scale = float(scale)

    def text(self, value, font: Font, limit, max_lines, truncate):
        ascent, descent, line_height = self.fonts.metrics(font)
        if not value:
            return [""], 0.0, line_height

        if max_lines == 1 or limit is None:
            line = value
            if limit is not None and truncate:
                line = self.fonts.ellipsize(value, font, limit)
            return [line], self.fonts.width(line, font), line_height

        lines = self.fonts.wrap(value, font, limit, max_lines if truncate else 0)
        width = max((self.fonts.width(line, font) for line in lines), default=0.0)
        return lines, width, line_height * len(lines)

    def image(self, source):
        loaded = self.images.load(source)
        if loaded is None:
            return 0.0, 0.0
        return loaded.width, loaded.height
