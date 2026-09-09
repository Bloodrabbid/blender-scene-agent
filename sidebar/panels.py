"""The sidebar panels behind the add-on's tab in the N region.

Blender's own name for this region is the sidebar. It used to be a complete
second interface — image, video, 3D, animation, the galleries, the phone
camera, each with its own tab and its own generate button — and every one of
those was a view of a Higgsfield account. What is left is the header, whatever
the overlay needs to say when it has tripped, and the agent panel next door in
`mcp_service.py`.

**A panel reads the add-on through `hb`, late.** `draw` is called by Blender,
so there is nobody to hand it the root module, and the root is still executing
when this module is imported — so the module *object* is bound at import and
its attributes are read at draw time, when the root is complete.

**Operators are addressed by `bl_idname`, never called.** A panel names a class
only to read that string; the operator's own module is free to move.

**Nothing here may write.** A draw handler that changes a property re-enters
the notifier and Blender will either warn or redraw forever.
"""

import bpy

from .. import root
from ..mcp_service import SCENEAGENT_PT_agent
from ..work import tasks

# The add-on root, late-bound. See the module docstring.
hb = root.addon()


def _draw_overlay_guard_notice(layout):
    """Tell the user why the composer vanished, and offer it back."""
    breaker = hb.composer_breaker if hb.composer_breaker.tripped else hb.modal_breaker
    if not breaker.tripped:
        return
    box = layout.box()
    box.alert = True
    box.label(text="Viewport composer disabled", icon="ERROR")
    box.label(text=str(breaker.reason or "unknown reason"))
    box.operator(hb.SCENEAGENT_OT_reset_overlay.bl_idname, icon="FILE_REFRESH")


class SCENEAGENT_PT_panel(bpy.types.Panel):
    bl_idname = "SCENEAGENT_PT_panel"
    bl_label = "Scene Agent"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Scene Agent"

    def draw(self, context):
        layout = self.layout

        # Brand header: the wordmark, embedded as square tiles drawn
        # edge-to-edge. template_icon can't be used here: it draws previews
        # shrunk by PREVIEW_PAD (0.15 unit per side) and centered inside the
        # cell, so adjacent tiles can never touch and the banner renders with
        # seams. Plain icon labels draw the icon at exactly 0.8 UI-unit with no
        # padding, so each tile gets a fixed cell and the tiles line up
        # pixel-perfect.
        header = layout.column(align=True)
        tile_ids = hb.logo_icon_ids()
        if tile_ids:
            logo = header.row(align=True)
            logo.alignment = "LEFT"
            for icon_id in tile_ids:
                cell = logo.row(align=True)
                cell.ui_units_x = 0.8
                cell.label(icon_value=icon_id)
        else:
            badge = header.row()
            badge.alignment = "LEFT"
            badge.label(text="SCENE AGENT")
        layout.separator()
        _draw_overlay_guard_notice(layout)

        layout.label(text=f"Version {hb._plugin_version_string()}")

        active = [task for task in tasks.values() if task["status"] == "running"]
        if active:
            status = layout.box()
            for task in active:
                pct = task["progress"]
                suffix = f" · {int(pct * 100)}%" if pct >= 0 else ""
                status.label(
                    text=f"{task['label']}: {task['message']}{suffix}"[:48],
                    icon="SORTTIME",
                )


PANELS = (
    SCENEAGENT_PT_panel,
    SCENEAGENT_PT_agent,
)
