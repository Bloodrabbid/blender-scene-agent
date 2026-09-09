"""Skia → Blender GPU plumbing for the viewport overlays.

Skia rasterizes a widget into a CPU surface; this module gets those pixels onto
the screen as a single textured quad and keeps them cached so a redraw that
changed nothing costs one draw call.

Two Blender constraints shape everything here:

* ``gpu.types.GPUTexture`` only accepts ``FLOAT`` buffers, so the uint8 surface
  has to be converted every time it is re-rastered (~2 ms for a retina panel).
* Skia hands back **premultiplied** RGBA, so the quad is drawn with
  ``ALPHA_PREMULT`` blending — plain ``ALPHA`` double-darkens the edges.
"""

from __future__ import annotations

import logging

from ... import perf

logger = logging.getLogger(__name__)

_skia = None
_skia_failed = False


def skia_module():
    """Import skia once; ``None`` when the wheel is unavailable."""
    global _skia, _skia_failed
    if _skia is None and not _skia_failed:
        try:
            import skia
        except Exception:
            _skia_failed = True
            logger.exception("skia-python is unavailable; overlays fall back to quads")
        else:
            _skia = skia
    return _skia


def available():
    return skia_module() is not None


class Layer:
    """A cached Skia surface uploaded to a GPU texture.

    ``ensure`` re-rasterizes only when ``key`` changes, so a hover that does not
    touch this layer costs nothing. The last few textures are kept in a small
    LRU beside the current one, because interaction is full of A-to-B-and-back
    flips — hover on and off a control, a menu opened and closed, a page
    visited and returned to — and each direction of the flip is the *same*
    raster every time. Re-rendering it cost the full describe-paint-convert-
    upload (~10 ms on the composer, ~35 ms on the explorer); replaying it from
    the cache costs a dictionary hit. The capacity stays small deliberately:
    a texture the explorer's size is ~19 MB of GPU memory, so this holds the
    flip everyone does, not the history nobody revisits.
    """

    KEEP = 3

    def __init__(self, name, keep=KEEP):
        self.name = name
        self.width = 0
        self.height = 0
        self.key = None
        self.texture = None
        self.keep = keep
        self._surface = None
        self._buffer = None
        self._view = None
        self._kept = {}  # key → texture, insertion-ordered; oldest evicted.

    def _resize(self, width, height):
        import numpy as np
        from gpu.types import Buffer

        skia = skia_module()
        self.width, self.height = width, height
        # Explicitly RGBA, never ``skia.Surface(w, h)``, which is N32 — and N32
        # is *BGRA* on Windows. ``tobytes()`` honours the surface's colour type,
        # so the bytes handed to a texture Blender reads as RGBA arrived with
        # red and blue swapped: brand green #d1fe17 came out as #17fed1, a cyan.
        # Asking for the non-native order costs nothing measurable (10.0 ms a
        # composer raster against 10.7 for BGRA — Skia's raster pipeline does
        # not care), and it makes the contract with ``GPUTexture`` explicit
        # rather than dependent on how Skia happened to be built.
        self._surface = skia.Surface.MakeRaster(
            skia.ImageInfo.Make(
                width, height, skia.kRGBA_8888_ColorType, skia.kPremul_AlphaType
            )
        )
        count = width * height * 4
        self._buffer = Buffer("FLOAT", count)
        self._view = np.asarray(self._buffer)
        self.key = None
        # Every kept texture was rendered at the old size; none can be blitted
        # at the new one.
        self._kept.clear()

    def ensure(self, width, height, key, render):
        """Rasterize ``render(canvas)`` when the size or ``key`` changed."""
        import numpy as np
        from gpu.types import GPUTexture

        skia = skia_module()
        if skia is None:
            return False
        width, height = max(1, int(width)), max(1, int(height))
        perf.pixels(width, height)
        if width != self.width or height != self.height or self._surface is None:
            started = perf.now()
            self._resize(width, height)
            perf.mark("resize", started)
            perf.count("resizes")
        if key == self.key and self.texture is not None:
            return False
        replay = self._kept.get(key)
        if replay is not None:
            # Re-inserting marks it most recently used — dicts keep insertion
            # order, and eviction below pops from the front.
            del self._kept[key]
            self._kept[key] = replay
            self.texture = replay
            self.key = key
            perf.count("texture_hits")
            return False

        canvas = self._surface.getCanvas()
        # The canvas outlives the render, so any transform a caller applies has
        # to be undone here: left to compound, a per-frame ``scale`` pushes the
        # content off the surface within a few frames and the layer silently
        # goes blank.
        started = perf.now()
        canvas.save()
        canvas.clear(skia.Color4f(0, 0, 0, 0))
        try:
            render(canvas)
        finally:
            canvas.restoreToCount(1)
        perf.mark("paint", started)

        started = perf.now()
        # A view over the surface's own pixels. `makeImageSnapshot().tobytes()`
        # allocated and memcpy'd the whole surface every raster and threw it
        # away a line later — 0.35 ms and 15 MB at the explorer's size. Skia is
        # permitted to pad rows and a flat read would shear the image, so the
        # snapshot stays as the fallback for a platform that does.
        pixmap = skia.Pixmap()
        if self._surface.peekPixels(pixmap) and pixmap.rowBytes() == self.width * 4:
            raw = pixmap
        else:
            raw = self._surface.makeImageSnapshot().tobytes()
        perf.mark("snapshot", started)
        # Straight into the buffer's own view. Converting via a scratch array
        # and copying across cost a second full-size float32 allocation — 8.4 MB
        # for the composer's layer — to reach a bit-identical result.
        started = perf.now()
        np.divide(
            np.frombuffer(raw, dtype=np.uint8), 255.0, out=self._view, dtype=np.float32
        )
        perf.mark("convert", started)
        started = perf.now()
        self.texture = GPUTexture(
            (self.width, self.height), format="RGBA16F", data=self._buffer
        )
        perf.mark("upload", started)
        perf.count("rasters")
        self.key = key
        self._kept[key] = self.texture
        while len(self._kept) > self.keep:
            del self._kept[next(iter(self._kept))]
        return True

    def blit(self, x, y, alpha=1.0):
        """Composite the cached texture at region-space ``x, y`` (bottom-left).

        ``alpha`` fades the whole layer without re-rastering it, which is what
        makes a surface's entrance cost one textured quad a frame instead of a
        full describe-solve-paint. Scaling every channel keeps the texture
        premultiplied, so the blend mode below is still the right one.

        **The origin is snapped to whole pixels, and it has to be.** The quad is
        exactly ``self.width`` by ``self.height``, so a texel is a pixel — but
        only if the corner sits on the grid. Blender binds a ``GPUTexture``
        with linear filtering, so a fractional origin makes every output pixel
        a blend of two neighbouring texels in each axis, over the whole layer.
        Measured against a 1px stripe pattern: an offset of 0.4 takes a 0-to-1
        edge down to 0.4-to-0.6, and 0.5 erases it completely. The callers'
        origins are logical constants times the display scale, so on a 1×
        display they land off the grid almost every time — the explorer blits
        at x=46.4 — and the whole panel reads as soft. Retina hid it: the same
        arithmetic leaves 0.2 of a pixel that is half the size to begin with.
        """
        import gpu
        from gpu_extras.batch import batch_for_shader

        if self.texture is None:
            return
        alpha = max(0.0, min(1.0, float(alpha)))
        if alpha <= 0.0:
            return
        started = perf.now()
        faded = alpha < 0.999
        shader = gpu.shader.from_builtin("IMAGE_COLOR" if faded else "IMAGE")
        x, y = round(x), round(y)
        width, height = self.width, self.height
        batch = batch_for_shader(
            shader,
            "TRI_FAN",
            {
                "pos": (
                    (x, y),
                    (x + width, y),
                    (x + width, y + height),
                    (x, y + height),
                ),
                # Skia's origin is top-left, Blender's is bottom-left.
                "texCoord": ((0, 1), (1, 1), (1, 0), (0, 0)),
            },
        )
        gpu.state.blend_set("ALPHA_PREMULT")
        shader.bind()
        if faded:
            shader.uniform_float("color", (alpha, alpha, alpha, alpha))
        shader.uniform_sampler("image", self.texture)
        batch.draw(shader)
        gpu.state.blend_set("NONE")
        perf.mark("blit", started)

    def release(self):
        self.texture = None
        self._surface = None
        self._buffer = None
        self._view = None
        self.width = self.height = 0
        self.key = None
        self._kept.clear()


def color(rgba):
    """``(r, g, b, a)`` floats → ``skia.Color4f``."""
    skia = skia_module()
    return skia.Color4f(*rgba)


def rrect(x, y, width, height, radius):
    skia = skia_module()
    radius = max(0.0, min(radius, width * 0.5, height * 0.5))
    return skia.RRect.MakeRectXY(
        skia.Rect.MakeXYWH(x, y, width, height), radius, radius
    )
