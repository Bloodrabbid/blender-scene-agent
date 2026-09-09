"""The add-on's `bpy.utils.previews` collection.

Blender will only draw a custom icon that came from a preview collection, so
this one holds all of them: the logo tiles, the provider marks rendered through
Skia, and a thumbnail per generation.

**Read it as `collection`, never `from ... import collection`.** It is
None until `register()` calls `start()` and None again after `stop()`, so a
from-import binds the value that existed at import time and reads stale
forever. Going through the module is late-bound and always current — which is
also what lets a feature package reach it without importing the add-on root.
"""

import logging
import os
from pathlib import Path

import bpy

from .. import paths
from ..hfui import providers

logger = logging.getLogger(__name__)

collection = None


def start():
    global collection
    collection = bpy.utils.previews.new()
    return collection


def stop():
    global collection
    if collection is not None:
        bpy.utils.previews.remove(collection)
        collection = None
    # An icon_id is only meaningful while the collection that issued it lives.
    _provider_icon_ids.clear()


PROVIDER_ICON_PREFIX = "hf_provider_"


PROVIDER_ICON_PX = 64


_provider_icon_ids = {}  # "providers/google" -> preview icon_id, 0 once it failed


def _provider_icon_id(model):
    """Preview icon id for a model's brand mark, 0 when it has none.

    Blender previews only take raster files, so the bundled SVG the composer
    draws is rendered once per mark through Skia and cached on disk for the
    session — twelve marks at most, however large the catalog grows. It is
    drawn as the file ships it, white, which sits at the same weight as the
    stock monochrome icons beside it in the dropdown.
    """
    return _mark_icon_id(providers.for_model(model))


def _mark_icon_id(mark):
    """Preview icon id for one bundled SVG mark, 0 when it cannot be drawn."""
    if not mark:
        return 0
    if mark in _provider_icon_ids:
        return _provider_icon_ids[mark]
    _provider_icon_ids[mark] = 0
    if collection is None:
        return 0
    try:
        from ..skui.measure import skia_module

        skia = skia_module()
        source = providers.path(mark)
        if skia is None or not source or not os.path.exists(source):
            return 0
        with open(source, "rb") as handle:
            payload = handle.read()
        # MemoryStream borrows the buffer rather than copying it, so the bytes
        # have to outlive the parse — hand it a temporary and SVGDOM reads
        # freed memory and quietly answers None.
        dom = skia.SVGDOM.MakeFromStream(skia.MemoryStream(payload))
        if dom is None:
            return 0
        # The marks declare their own width/height, and a container size only
        # resolves percentages — so filling the tile is the canvas's job.
        box = dom.containerSize()
        surface = skia.Surface(PROVIDER_ICON_PX, PROVIDER_ICON_PX)
        with surface as canvas:
            canvas.clear(skia.Color4f(0, 0, 0, 0))
            canvas.scale(
                PROVIDER_ICON_PX / (box.width() or PROVIDER_ICON_PX),
                PROVIDER_ICON_PX / (box.height() or PROVIDER_ICON_PX),
            )
            dom.render(canvas)
        name = mark.rpartition("/")[2]
        target = paths.provider_icons_dir() / f"{name}.png"
        surface.makeImageSnapshot().save(str(target), skia.kPNG)
        key = f"{PROVIDER_ICON_PREFIX}{name}"
        if key in collection:
            del collection[key]
        collection.load(key, str(target), "IMAGE")
        _provider_icon_ids[mark] = collection[key].icon_id
    except Exception:
        logger.debug("Scene Agent: icon %s unavailable", mark, exc_info=True)
    return _provider_icon_ids[mark]


LOGO_KEY = "hf_logo"
LOGO_DIR = Path(__file__).resolve().parents[1] / "resources" / "logo"


def _logo_tiles():
    """The banner tiles, left to right."""
    return sorted(
        LOGO_DIR.glob("tile_*.png"), key=lambda p: int(p.stem.rsplit("_", 1)[1])
    )


def load_logo():
    """Register the brand banner tiles as preview icons.

    They were base64 in the source for a long time, decoded and written into
    the user's home on every load. They are eight small PNGs and the add-on
    already ships a `resources/` tree, so they are read straight out of it —
    240 lines of the root gone, and nothing written to the user's home to draw
    a header.
    """
    if collection is None or f"{LOGO_KEY}_0" in collection:
        return
    try:
        for index, tile in enumerate(_logo_tiles()):
            collection.load(f"{LOGO_KEY}_{index}", str(tile), "IMAGE")
    except Exception:
        logger.exception("Scene Agent: failed to load brand logo")


def logo_icon_ids():
    """``icon_id`` per tile, left to right; empty until all of them loaded."""
    if collection is None:
        return []
    ids = []
    for index in range(len(_logo_tiles())):
        key = f"{LOGO_KEY}_{index}"
        if key not in collection:
            return []
        ids.append(collection[key].icon_id)
    return ids


def brand_icon_id():
    """The compact brand mark, for native Blender controls."""
    return _mark_icon_id("brand")
