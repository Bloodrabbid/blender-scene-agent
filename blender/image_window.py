"""A floating Image Editor window, for looking at a result full size.

Realtime keeps reloading the *same* datablock, so an editor left open on it
shows every new frame without being told. One window is reused across calls.
"""

import contextlib
import logging

import bpy

from .viewport import request_redraw

logger = logging.getLogger(__name__)

preview_state = {"window": None}


def _largest_area(window):
    areas = list(window.screen.areas)
    return max(areas, key=lambda a: a.width * a.height) if areas else None


def _fit_image(window, area):
    for region in area.regions:
        if region.type == "WINDOW":
            with contextlib.suppress(Exception):
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.image.view_all(fit_view=True)
            return


def _show_preview(image, *, fit=True):
    """Open (or reuse) a floating Image Editor window showing `image`.

    Returns the window, or None if one could not be opened. Safe to call from a
    timer/handler: window creation is wrapped in a context override and guarded.
    """
    if image is None:
        return None
    wm = bpy.context.window_manager
    if not wm.windows:
        return None

    # Reuse the previous preview window if it is still open.
    win = preview_state.get("window")
    if win is not None and win in list(wm.windows):
        area = _largest_area(win)
        if area is not None:
            if area.type != "IMAGE_EDITOR":
                area.type = "IMAGE_EDITOR"
            area.spaces.active.image = image
            if fit:
                _fit_image(win, area)
            request_redraw()
            return win
    preview_state["window"] = None

    # Open a fresh window (duplicates the active area) and convert it.
    before = list(wm.windows)
    base_win = wm.windows[0]
    base_area = _largest_area(base_win)
    try:
        with bpy.context.temp_override(window=base_win, area=base_area):
            bpy.ops.wm.window_new()
    except Exception:
        logger.exception("Could not open preview window")
        return None
    new_windows = [w for w in wm.windows if w not in before]
    if not new_windows:
        return None
    win = new_windows[0]
    area = _largest_area(win)
    if area is None:
        return None
    area.type = "IMAGE_EDITOR"
    area.spaces.active.image = image
    if fit:
        _fit_image(win, area)
    preview_state["window"] = win
    request_redraw()
    return win
