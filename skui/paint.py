"""Draw a solved frame onto a Skia canvas.

Nothing is decided here — every rectangle already has its position, every string
its final characters. Painting only translates description into Skia calls, in
tree order, so a later sibling covers an earlier one.

Coordinates are logical pixels with the origin top-left. Retina is the caller's
job: scale the canvas before painting and everything follows.
"""

from __future__ import annotations

import math

from .nodes import (
    COVER,
    END,
    FILL,
    START,
    Gradient,
    Image,
    Text,
    View,
    in_state,
    respec,
    stroke_box,
)
from .measure import skia_module


def _color(value):
    return skia_module().Color4f(*value)


def _rrect(x, y, width, height, radius):
    skia = skia_module()
    radius = max(0.0, min(radius, width * 0.5, height * 0.5))
    rect = skia.Rect.MakeXYWH(x, y, width, height)
    if radius <= 0:
        return skia.RRect.MakeRect(rect)
    return skia.RRect.MakeRectXY(rect, radius, radius)


def _callout(x, y, width, height, radius, tail):
    """The rounded rect and its tail, unioned into one closed outline.

    A union rather than a rectangle with a triangle drawn beside it: two
    shapes are two outlines, so the border draws a line straight across the
    mouth of the tail and leaves the tail's own two edges bare. Skia's path
    ops absorb the shared edge and hand back the silhouette, which fills and
    strokes as the single shape it looks like.
    """
    skia = skia_module()
    body = skia.Path()
    body.addRRect(_rrect(x, y, width, height, radius))
    half = tail.width * 0.5
    if half <= 0.0 or tail.height <= 0.0:
        return body
    radius = max(0.0, min(radius, width * 0.5, height * 0.5))
    if tail.align == END:
        center = x + width - tail.offset
    elif tail.align == START:
        center = x + tail.offset
    else:
        center = x + width * 0.5 + tail.offset
    # Keep the mouth on the straight part of the edge. Over a rounded corner
    # the union takes a bite out of the curve and the tail looks dropped on.
    low, high = x + radius + half, x + width - radius - half
    if low <= high:
        center = min(max(center, low), high)
    base = y + height
    point = skia.Path()
    point.moveTo(center - half, base)
    point.lineTo(center, base + tail.height)
    point.lineTo(center + half, base)
    point.close()
    return skia.Op(body, point, skia.PathOp.kUnion_PathOp)


def _inset_tail(tail, distance):
    """The tail of a callout whose every edge has moved inward by ``distance``.

    Not the same as shrinking its bounding box. Both slant edges move
    perpendicular to themselves, which pulls the apex up by more than
    ``distance`` — by the ratio of the slant to the half-width — and draws the
    mouth in by less. Getting this wrong puts the stroke off the fill at the
    point, which is the one part of the shape the eye is on.
    """
    half, height = tail.width * 0.5, tail.height
    if half <= 0.0 or height <= 0.0 or distance <= 0.0:
        return tail
    slant = math.hypot(half, height)
    # An offset measured from an edge has to follow that edge inward, or the
    # stroked tail sits a stroke-width off the filled one.
    offset = tail.offset
    if tail.align in (START, END):
        offset = max(0.0, offset - distance)
    return respec(
        tail,
        {
            "width": 2.0 * max(0.0, half - distance * (slant - half) / height),
            "height": max(0.0, height + distance - distance * slant / half),
            "offset": offset,
        },
    )


def _shader(fill, x, y, width, height):
    """Gradient → Skia shader. ``angle`` is degrees clockwise from 3 o'clock."""
    skia = skia_module()
    radians = math.radians(fill.angle)
    half_x, half_y = width * 0.5, height * 0.5
    center_x, center_y = x + half_x, y + half_y
    # Reach to the box edge so the last stop lands exactly on the corner side.
    span = abs(math.cos(radians)) * half_x + abs(math.sin(radians)) * half_y
    dx, dy = math.cos(radians) * span, math.sin(radians) * span
    positions = [float(stop[0]) for stop in fill.stops]
    colors = [_color(stop[1]) for stop in fill.stops]
    return skia.GradientShader.MakeLinear(
        points=[(center_x - dx, center_y - dy), (center_x + dx, center_y + dy)],
        colors=colors,
        positions=positions,
    )


def _fill_paint(fill, x, y, width, height):
    skia = skia_module()
    paint = skia.Paint(AntiAlias=True)
    if isinstance(fill, Gradient):
        paint.setShader(_shader(fill, x, y, width, height))
        if fill.blend:
            paint.setBlendMode(getattr(skia.BlendMode, fill.blend))
    else:
        paint.setColor4f(_color(fill))
    return paint


def _paint_view(canvas, placed, measure, style):
    skia = skia_module()
    x, y, width, height = placed.rect

    # A tail turns the outline into a path. Everything else keeps `drawRRect`:
    # it is Skia's fast case and the composer paints dozens of boxes a raster,
    # so this branch buys the callout without charging the rest of the product
    # for it.
    outline = (
        None
        if style.tail is None
        else _callout(x, y, width, height, style.radius, style.tail)
    )

    if style.shadows:
        # A blurred shadow is priced by the pixels it fills, and nearly all of
        # them sit under the box that casts it: the explorer's card shadows
        # were ~6 ms of a 13 ms raster spent painting an interior the opaque
        # card covered on the very next draw call. Clipping the inside out —
        # inset a pixel so the card's own antialiased edge still lands on
        # shadow — leaves the visible ring, 0.43 ms for the same pixels. Only
        # for solid opaque fills: through a translucent one the shadow really
        # does show.
        covered = (
            width > 2.0
            and height > 2.0
            and style.fill is not None
            and not isinstance(style.fill, Gradient)
            and style.fill[3] >= 0.999
            and style.opacity >= 1.0
        )
        if covered:
            canvas.save()
            canvas.clipRRect(
                _rrect(x + 1.0, y + 1.0, width - 2.0, height - 2.0, style.radius),
                skia.ClipOp.kDifference,
                True,
            )
        for shadow in style.shadows:
            paint = skia.Paint(AntiAlias=True, Color4f=_color(shadow.color))
            if shadow.blur > 0:
                paint.setMaskFilter(
                    skia.MaskFilter.MakeBlur(skia.kNormal_BlurStyle, shadow.blur * 0.5)
                )
            if outline is None:
                canvas.drawRRect(
                    _rrect(x + shadow.dx, y + shadow.dy, width, height, style.radius),
                    paint,
                )
            else:
                moved = skia.Path(outline)
                moved.offset(shadow.dx, shadow.dy)
                canvas.drawPath(moved, paint)
        if covered:
            canvas.restore()

    if style.fill is not None:
        paint = _fill_paint(style.fill, x, y, width, height)
        if outline is None:
            canvas.drawRRect(_rrect(x, y, width, height, style.radius), paint)
        else:
            canvas.drawPath(outline, paint)

    border = style.border
    if border is not None and border.width > 0:
        paint = skia.Paint(
            AntiAlias=True,
            Color4f=_color(border.color),
            Style=skia.Paint.kStroke_Style,
            StrokeWidth=border.width,
        )
        inset = border.width * 0.5
        stroke_x, stroke_y, inner_width, inner_height, inner_radius = stroke_box(
            x, y, width, height, style.radius, border
        )
        if outline is None:
            canvas.drawRRect(
                _rrect(stroke_x, stroke_y, inner_width, inner_height, inner_radius),
                paint,
            )
        else:
            # The whole outline at once, tail included, so the border wraps the
            # point and never crosses its mouth. The stroke is centred, hence
            # the inset — same convention as the rounded-rect case above.
            canvas.drawPath(
                _callout(
                    x + inset,
                    y + inset,
                    inner_width,
                    inner_height,
                    inner_radius,
                    _inset_tail(style.tail, inset),
                ),
                paint,
            )


def _tracked_run(canvas, skia, face, text, x, baseline, paint, tracking):
    """Draw one run with letter spacing, as a single positioned blob.

    Skia puts no tracking on `Font`, so it has to come out of where each glyph
    is placed. Spelling that as a `drawString` per character costs two Skia
    calls per character, and `BODY`, `BODY_MEDIUM`, `BODY_STRONG` and `LABEL`
    all declare tracking — so it was the path nearly every string in the
    product took, the chat thread most of all.

    The positions are the same cumulative sum the per-character loop walked,
    which is also what `FontBook.width` charges for, so measurement and paint
    still agree to the pixel.
    """
    glyphs = face.textToGlyphs(text)
    if len(glyphs) != len(text):
        # Shaping did not give one glyph per character, so a per-character
        # position array would not line up with it. Rare; just walk it.
        for character in text:
            canvas.drawString(character, x, baseline, face, paint)
            x += face.measureText(character) + tracking
        return x
    positions = []
    for advance in face.getWidths(glyphs):
        positions.append(x)
        x += advance + tracking
    canvas.drawTextBlob(
        skia.TextBlob.MakeFromPosTextH(text, positions, baseline, face), 0, 0, paint
    )
    return x


def _paint_text(canvas, placed, measure):
    skia = skia_module()
    node = placed.node
    font = node.font
    ascent, descent, line_height = measure.fonts.metrics(font)
    paint = skia.Paint(AntiAlias=True, Color4f=_color(node.color))
    leading = (line_height - ascent - descent) * 0.5
    for index, line in enumerate(placed.lines):
        if not line:
            continue
        baseline = placed.y + index * line_height + leading + ascent
        cursor = placed.x
        for face, text in measure.fonts.runs(line, font):
            if font.tracking:
                cursor = _tracked_run(
                    canvas, skia, face, text, cursor, baseline, paint, font.tracking
                )
            else:
                canvas.drawString(text, cursor, baseline, face, paint)
                cursor += face.measureText(text)


def _device_scale(canvas):
    """How many device pixels one logical unit is, at this point in the draw.

    The surfaces scale the canvas once for the display, so a 60pt tile is 96
    real pixels on a retina Mac. Bucketing on the logical size alone would
    resample the image to half the resolution it is about to be drawn at.
    """
    try:
        matrix = canvas.getTotalMatrix()
        return max(abs(matrix.getScaleX()), abs(matrix.getScaleY())) or 1.0
    except Exception:
        return 1.0


def _paint_image(canvas, placed, measure):
    skia = skia_module()
    node = placed.node
    x, y, width, height = placed.rect
    if width <= 0 or height <= 0:
        return
    loaded = measure.images.scaled(
        node.source, max(width, height) * _device_scale(canvas)
    )
    if loaded is None or loaded.width <= 0 or loaded.height <= 0:
        return

    scale_x = width / loaded.width
    scale_y = height / loaded.height
    if node.fit == FILL:
        pass
    elif node.fit == COVER:
        scale_x = scale_y = max(scale_x, scale_y)
    else:
        scale_x = scale_y = min(scale_x, scale_y)
    drawn_width = loaded.width * scale_x
    drawn_height = loaded.height * scale_y
    left = x + (width - drawn_width) * 0.5
    top = y + (height - drawn_height) * 0.5

    layers = 0
    if node.tint is not None:
        tint = skia.Paint(
            ColorFilter=skia.ColorFilters.Blend(
                _color(node.tint).toColor(), skia.BlendMode.kSrcIn
            )
        )
        # Bounded to the glyph, not left at None: an unbounded saveLayer
        # allocates an offscreen the size of the whole surface, and the
        # composer tints thirteen 16px icons per frame into a 1529x345 layer.
        canvas.saveLayer(skia.Rect.MakeXYWH(left, top, drawn_width, drawn_height), tint)
        layers += 1
    if node.fit == COVER:
        canvas.save()
        canvas.clipRect(skia.Rect.MakeXYWH(x, y, width, height), doAntiAlias=True)
        layers += 1

    if loaded.kind == "svg":
        canvas.save()
        canvas.translate(left, top)
        canvas.scale(scale_x, scale_y)
        loaded.handle.setContainerSize(skia.Size(loaded.width, loaded.height))
        loaded.handle.render(canvas)
        canvas.restore()
    else:
        # Bilinear, no mipmaps. The bucketed copy from ``ImageBook.scaled`` is
        # already within 2x of the size it is drawn at — the heavy reduction
        # was done once there, with mipmaps — so trilinear here paid ~0.45 ms
        # a tile to filter between levels it never needed: half the explorer's
        # warm paint. At ratios of 0.5 and up the two are indistinguishable.
        canvas.drawImageRect(
            loaded.handle,
            skia.Rect.MakeXYWH(left, top, drawn_width, drawn_height),
            skia.SamplingOptions(skia.FilterMode.kLinear),
        )

    for _ in range(layers):
        canvas.restore()


def _paint(canvas, placed, measure, interact, state):
    node = placed.node
    if isinstance(node, Text):
        _paint_text(canvas, placed, measure)
        return
    if isinstance(node, Image):
        _paint_image(canvas, placed, measure)
        return
    if not isinstance(node, View):
        return

    style = node.style
    # A published id covers its whole subtree, exactly as hit testing sees it,
    # so a box inside an id-bearing node answers the pointer with it. The
    # explorer's tiles need that: the square that carries the fill is a child
    # of the column that carries the id.
    if interact is not None and node.id is not None:
        state = interact(node.id)
    if state is not None and (style.hover is not None or style.pressed is not None):
        style = in_state(style, state)

    transparent = style.opacity < 1.0
    if transparent:
        canvas.saveLayerAlpha(None, int(round(style.opacity * 255)))
    _paint_view(canvas, placed, measure, style)
    if style.clip:
        canvas.save()
        canvas.clipRRect(
            _rrect(placed.x, placed.y, placed.width, placed.height, style.radius),
            doAntiAlias=True,
        )
    for child in placed.children:
        _paint(canvas, child, measure, interact, state)
    if style.clip:
        canvas.restore()
    if transparent:
        canvas.restore()


def paint(canvas, frame, measure, interact=None):
    """Draw a solved ``frame``. The canvas keeps whatever transform it had.

    ``interact`` answers ``node_id -> None | "hover" | "pressed"``, and is what
    turns a node's declared ``hover`` and ``pressed`` styles into the one it is
    drawn with. Leave it out and every node paints at rest, which is what a
    surface wants when it draws its hover on a separate plane.
    """
    if frame.root is not None:
        _paint(canvas, frame.root, measure, interact, None)
