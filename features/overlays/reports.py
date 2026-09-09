"""The report toast: the add-on's own status line on the viewport.

`work.add_report` queues a line and this draws whatever has not expired, bottom
left. It is the one overlay drawn with GPU quads rather than Skia — it predates
`skui` and it is four rectangles and a string, which is not worth a layer.

It is also the *fallback*: when a Skia surface's breaker trips, this is still
standing, so it is where the failure gets said.
"""

import logging
import time

import bpy

from ...work import reports, tasks

logger = logging.getLogger(__name__)


_reports_handler = None


def _draw_reports():
    running = [t for t in tasks.values() if t["status"] == "running"]
    if not reports and not running:
        return
    try:
        import blf

        if bpy.context.region is None:
            return
        font_id = 0
        blf.size(font_id, 15)
        blf.enable(font_id, blf.SHADOW)
        blf.shadow(font_id, 3, 0.0, 0.0, 0.0, 0.9)

        now = time.time()
        x, base_y, line = 20, 24, 0

        # Transient notifications (newest at the bottom), fading in the last second.
        for rep in reversed(reports[-6:]):
            remaining = rep["timeout"] - (now - rep["start"])
            fade = max(0.0, min(1.0, remaining))
            r, g, b, a = rep["color"]
            blf.color(font_id, r, g, b, a * fade)
            blf.position(font_id, x, base_y + line * 24, 0)
            blf.draw(font_id, rep["text"][:90])
            line += 1

        # Live progress for active jobs.
        for task in running:
            pct = task["progress"]
            suffix = f" {int(pct * 100)}%" if pct >= 0 else ""
            blf.color(font_id, 0.5, 0.8, 1.0, 1.0)
            blf.position(font_id, x, base_y + line * 24, 0)
            blf.draw(font_id, f"• {task['label']}: {task['message']}{suffix}"[:90])
            line += 1

        blf.disable(font_id, blf.SHADOW)
    except Exception:
        logger.exception("Scene Agent reports draw failed")


def install():
    """Put the toast on the viewport, once."""
    global _reports_handler

    if _reports_handler is None:
        _reports_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_reports, (), "WINDOW", "POST_PIXEL"
        )


def remove():
    global _reports_handler

    if _reports_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_reports_handler, "WINDOW")
        _reports_handler = None
