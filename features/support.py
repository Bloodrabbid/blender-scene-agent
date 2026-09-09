"""Support logs and the diagnostics bundle.

The five buttons in Preferences → Support Logs. `diagnostics.py` writes the log
and builds the bundle; this is the way in from Blender, plus `_get_session_info`,
the header that says which version of what was running against which
environment — the first thing to read on any report.
"""

import contextlib
import logging
import os
import subprocess
import sys
import time
from pathlib import Path

import bpy
from bpy_extras.io_utils import ExportHelper

from .. import diagnostics, paths, perf
from ..features import agent
from ..work import add_report


def _plugin_version_string():
    # Imported at call time: `bl_info` is captured in the root, which is still
    # executing when this module loads.
    from .. import PLUGIN_VERSION

    return PLUGIN_VERSION


logger = logging.getLogger(__name__)


def _get_session_info():
    """What a support log needs to know about this machine.

    The account fields are gone with the account; what matters now is which
    CLI is going to be spawned and whether it is actually on disk, because
    "the agent does nothing" is almost always one of those two.
    """
    from ..props import addon_preferences

    prefs = addon_preferences()
    provider = getattr(prefs, "agent_provider", agent.CLAUDE) if prefs else agent.CLAUDE
    override = getattr(prefs, "agent_binary", "") if prefs else ""
    return {
        "addon_version": _plugin_version_string(),
        "blender_version": str(bpy.app.version_string),
        "python_version": sys.version.split()[0],
        "agent_provider": provider,
        "agent_binary": agent.find_binary(provider, override) or "not found",
    }


class SCENEAGENT_OT_open_log_folder(bpy.types.Operator):
    bl_idname = "scene_agent.open_log_folder"
    bl_label = "Open Log Folder"
    bl_description = "Open the add-on logs folder in the file browser"

    def execute(self, context):
        log_dir = diagnostics.get_logs_dir()
        bpy.ops.wm.path_open(filepath=str(log_dir))
        return {"FINISHED"}


class SCENEAGENT_OT_copy_log_path(bpy.types.Operator):
    bl_idname = "scene_agent.copy_log_path"
    bl_label = "Copy Log Path"
    bl_description = "Copy the active session log file path to the clipboard"

    def execute(self, context):
        log_path = diagnostics.get_current_log_path()
        context.window_manager.clipboard = str(log_path)
        self.report({"INFO"}, f"Copied log path: {log_path}")
        add_report("Copied log path to clipboard", type="INFO")
        return {"FINISHED"}


class SCENEAGENT_OT_copy_ui_report(bpy.types.Operator):
    bl_idname = "scene_agent.copy_ui_report"
    bl_label = "Copy UI Performance Report"
    bl_description = (
        "Copy a breakdown of what the viewport UI costs on this machine — "
        "frame times, the slowest phase of each surface, and the GPU it ran "
        "on — to the clipboard"
    )

    def execute(self, context):
        report = perf.report()
        context.window_manager.clipboard = report
        # Into the support log too, so an exported bundle carries it without
        # the user having to think to press this first.
        logger.info("Viewport UI performance report\n%s", report)
        self.report({"INFO"}, "Copied UI performance report")
        add_report("Copied UI performance report to clipboard", type="INFO")
        return {"FINISHED"}


class SCENEAGENT_OT_open_output_folder(bpy.types.Operator):
    bl_idname = "scene_agent.open_output_folder"
    bl_label = "Open Generations Folder"
    bl_description = (
        "Open the Downloads folder generations are saved to. Delete anything "
        "in it whenever you like — the add-on re-downloads what it still needs"
    )

    def execute(self, context):
        bpy.ops.wm.path_open(filepath=str(paths.outputs_dir()))
        return {"FINISHED"}


class SCENEAGENT_OT_remove_local_data(bpy.types.Operator):
    bl_idname = "scene_agent.remove_local_data"
    bl_label = "Remove Local Data"
    bl_description = (
        "Delete everything the add-on keeps on this machine — cached "
        "thumbnails, logs, and the local chat threads. Files in the Downloads "
        "folder are left alone"
    )

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        # The log handler holds its file open, so it has to let go before the
        # tree goes; setup_logging puts it back afterwards.
        diagnostics.shutdown_logging()
        removed, failed = paths.remove_local_data()
        diagnostics.setup_logging(session_info=_get_session_info())
        if failed:
            names = ", ".join(str(path) for path in failed)
            self.report({"WARNING"}, f"Could not fully remove: {names}")
            add_report("Some local data could not be removed", type="WARNING")
            return {"FINISHED"}
        self.report({"INFO"}, f"Removed {len(removed)} local data folder(s)")
        add_report("Removed local add-on data", type="INFO")
        return {"FINISHED"}


class SCENEAGENT_OT_clear_logs(bpy.types.Operator):
    bl_idname = "scene_agent.clear_logs"
    bl_label = "Clear Logs"
    bl_description = "Erase the session log (and rotated backups) and start a fresh one"

    def execute(self, context):
        try:
            path = diagnostics.clear_logs(session_info=_get_session_info())
        except Exception as error:  # noqa: BLE001
            self.report({"ERROR"}, f"Failed to clear logs: {error}")
            return {"CANCELLED"}
        self.report({"INFO"}, f"Cleared logs: {path}")
        add_report("Session log cleared", type="INFO")
        return {"FINISHED"}


class SCENEAGENT_OT_export_diagnostics(bpy.types.Operator, ExportHelper):
    bl_idname = "scene_agent.export_diagnostics"
    bl_label = "Export Logs"
    bl_description = (
        "Export a shareable zip of session logs, MCP log, and recent error reports "
        "(always-on logging; sensitive fields are redacted)"
    )

    filename_ext = ".zip"
    filter_glob: bpy.props.StringProperty(
        default="*.zip",
        options={"HIDDEN"},
    )
    filepath: bpy.props.StringProperty(
        name="File Path",
        description="File path for exporting the diagnostics pack",
        subtype="FILE_PATH",
    )

    def invoke(self, context, event):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        self.filepath = f"scene-agent-diagnostics-{stamp}.zip"
        return super().invoke(context, event)

    def execute(self, context):
        try:
            session_info = _get_session_info()
            out_path = diagnostics.export_diagnostics_pack(
                Path(self.filepath), session_info=session_info
            )
            self.report({"INFO"}, f"Diagnostics pack exported: {out_path.name}")
            add_report(
                f"Exported diagnostics: {out_path.name}", type="INFO", timeout=10.0
            )
            bpy.ops.wm.path_open(filepath=str(out_path.parent))
            return {"FINISHED"}
        except Exception as err:
            logger.exception("Failed to export diagnostics pack")
            self.report({"ERROR"}, f"Failed to export diagnostics pack: {err}")
            return {"CANCELLED"}
