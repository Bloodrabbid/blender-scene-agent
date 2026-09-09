"""Skia-rendered viewport overlays."""

from ...hfui import field as field_module
from ...root import addon  # noqa: F401  (re-exported; every surface host calls it)


class _Clipboard(field_module.Clipboard):
    """The host's clipboard, for `hfui.TextField`.

    The field is in `hfui` and `hfui` does not import bpy — that is what lets
    the snapshot scripts render every surface with no Blender — so the one
    thing a text field needs from the host arrives as this object. Both fields
    share it; there is nothing to configure.
    """

    def read(self):
        import bpy

        return str(bpy.context.window_manager.clipboard or "")

    def write(self, text):
        import bpy

        bpy.context.window_manager.clipboard = text


CLIPBOARD = _Clipboard()


# `addon()` is re-exported from `..root` above rather than defined here. A
# surface takes its host as a parameter instead of importing it, which is what
# keeps `*_surface.py` free of bpy and lets `scripts/*_snapshot.py` render them
# with no Blender at all; `mount` and `modal` are where the handing happens, so
# this is where they reach for it. It used to be `sys.modules[__name__]`, which
# was correct while they lived in the add-on root and became the wrong module
# the moment they moved — quietly, because the surfaces reach the host through
# `getattr` and the miss shows up as a draw that raises, not an import that
# fails.


def redraw_viewports():
    """Tag the 3D viewports, and only those.

    The add-on's ``request_redraw`` invalidates every area of every window —
    the outliner, the properties editor, the timeline — which is right when a
    property both surfaces share has changed, and much too broad for a
    capsule that is animating. At sixty frames a second the areas dragged
    along are the expensive part of a morph by a wide margin; the overlays
    themselves cost about a millisecond.
    """
    import bpy

    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == "VIEW_3D":
                area.tag_redraw()
