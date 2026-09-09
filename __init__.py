bl_info = {
    "name": "Scene Agent",
    "author": "Scene Agent contributors",
    "version": (1, 0, 2),
    "blender": (5, 1, 0),
    "location": "View3D > Sidebar (N) > Scene Agent",
    "description": "Drive Blender from your own Claude Code or Codex CLI.",
    "category": "System",
}
# Capture before Blender's extension loader strips module-level bl_info.
PLUGIN_VERSION = ".".join(str(part) for part in bl_info["version"])

# ---------------------------------------------------------------------------
# Scene Agent
#
# Two halves, and the second is the only reason the first exists:
#
#   1. An in-process TCP bridge on 127.0.0.1:9876 that executes bpy on
#      Blender's main thread, with the bundled `blmcp` stdio MCP server in
#      front of it. That pair is what lets an agent drive this Blender.
#
#   2. A chat drawn over the viewport, answered by the user's own coding CLI —
#      `claude` today, `codex` when it is wired — started as a child process
#      per turn with that MCP server attached. See `features/agent.py`.
#
# The add-on holds no credential of any kind and talks to no service. The CLI
# authenticates itself, the way it does in a terminal.
#
# This is a fork of Higgsfield's Blender add-on with its account removed: the
# OAuth device flow, the hosted agent, the outbound bridge to their MCP worker,
# the Amplitude telemetry, the auto-updater and every generation feature that
# billed against a Higgsfield balance. See README.md.
# ---------------------------------------------------------------------------

import contextlib
import logging

import bpy
import bpy.utils.previews

from . import (
    analytics,
    diagnostics,
    events,
    paths,
    perf,
    runtime_config,
)

# Everything below is a package that used to be part of this file, re-exported
# under its old bare name so no call site had to move. It is only safe because
# nothing here is rebound after import — reach a lifecycle-owned singleton
# through its module instead (`previews.collection`), never through a
# from-import.
from .blender import previews
from .blender.scene import _is_mesh_active  # noqa: F401  (read through `hb`)
from .blender.image_window import preview_state
from .blender.viewport import request_redraw
from .blender.previews import (
    brand_icon_id,
    load_logo,
    logo_icon_ids,
)
from .features import agent
from .features.overlays import reports as reports_overlay
from .features.overlays import mount
from .features.overlays.mount import (
    _BAR_ALERT_STATUSES,
    _bar_region_margins,
    _generation_launcher_recents,
    _generation_launcher_status,
    _hide_bar,
    _show_bar,
    _show_generate_3d_overlay,
    _viewport_ui_pref_update,
    bar_state,
    chat_breaker,
    chat_state,
    composer_breaker,
    generate_3d_overlay_state,
    generation_launcher_state,
    modal_breaker,
)
from .features.overlays.modal import SCENEAGENT_OT_bar_modal
from .features.overlays.operators import (
    SCENEAGENT_FH_composer_images,
    SCENEAGENT_OT_add_composer_images,
    SCENEAGENT_OT_chat_attach,
    SCENEAGENT_OT_reset_overlay,
    SCENEAGENT_OT_toggle_bar,
    SCENEAGENT_OT_toggle_generate_3d_overlay,
)
from .features.overlays.binding import (
    _composer_binding,
    _composer_media_kind,
    _composer_prepare,
)
from .features.support import (
    SCENEAGENT_OT_clear_logs,
    SCENEAGENT_OT_copy_log_path,
    SCENEAGENT_OT_copy_ui_report,
    SCENEAGENT_OT_export_diagnostics,
    SCENEAGENT_OT_open_log_folder,
    SCENEAGENT_OT_open_output_folder,
    SCENEAGENT_OT_remove_local_data,
    _get_session_info,
)
from .features.updates import _plugin_version_string, update_state  # noqa: F401
from .mcp_service import (
    SCENEAGENT_OT_copy_mcp_config,
    SCENEAGENT_OT_restart_mcp,
    start_mcp_service,
    stop_mcp_service,
)
from .props import props as _props
from .props.group import SceneAgentProps
from .session import _active_workspace_id, is_authenticated
from .sidebar.panels import PANELS
from .work import (
    add_report,
    execute_queued_scripts,
    reports,
    run_async,  # noqa: F401  (read through `hb`)
    run_on_main_thread,  # noqa: F401
    tasks,
    watchdog_timer,
)

logger = logging.getLogger(__name__)

# The launcher capsule used to list recent generations. There are none, and
# `hfui/launcher.py` reads this through the host — an empty tuple is what tells
# it to draw the mark alone.
history = ()


def request_viewport_redraw():
    """Redraw the overlays without invalidating every other editor.

    For state only the viewport surfaces can show — a hover, a caret sweep, a
    scroll — the outliner and the properties editor have nothing new to draw,
    and these paths fire on every mouse move. Anything that changes a scene
    property still goes through ``request_redraw`` so the N-panel keeps up.
    """
    with contextlib.suppress(Exception):
        from .features.overlays import redraw_viewports

        redraw_viewports()


@bpy.app.handlers.persistent
def _wire_events():
    """Everything that reacts to add-on state changing, in one table.

    This table used to be long, and all of it hung off the session: signing in
    woke the MCP service, the catalogs and Results; signing out tore every
    feature down. With no account there is no such moment — `register()` starts
    what needs starting. Called from `register()` after `events.clear()`, so a
    hot reload rebuilds it rather than stacking a second copy on top.
    """

    @events.when(events.AUTH_SETTLED)
    def _settled(authenticated, changed):
        request_redraw()


# ---------------------------------------------------------------------------
# Operators
# ---------------------------------------------------------------------------


class SCENEAGENT_OT_open_prefs(bpy.types.Operator):
    bl_idname = "scene_agent.open_prefs"
    bl_label = "Preferences"
    bl_description = "Open this add-on's preferences"

    def execute(self, context):
        try:
            bpy.ops.preferences.addon_show(module=__name__)
        except Exception:  # noqa: BLE001 — fall back to the generic prefs window
            bpy.ops.screen.userpref_show()
            context.preferences.active_section = "ADDONS"
        return {"FINISHED"}


OPERATORS = (
    SCENEAGENT_OT_restart_mcp,
    SCENEAGENT_OT_copy_mcp_config,
    SCENEAGENT_OT_reset_overlay,
    SCENEAGENT_OT_open_prefs,
    SCENEAGENT_OT_toggle_bar,
    SCENEAGENT_OT_toggle_generate_3d_overlay,
    SCENEAGENT_OT_add_composer_images,
    SCENEAGENT_OT_chat_attach,
    SCENEAGENT_OT_bar_modal,
    SCENEAGENT_OT_open_log_folder,
    SCENEAGENT_OT_copy_log_path,
    SCENEAGENT_OT_copy_ui_report,
    SCENEAGENT_OT_clear_logs,
    SCENEAGENT_OT_export_diagnostics,
    SCENEAGENT_OT_open_output_folder,
    SCENEAGENT_OT_remove_local_data,
)

FILE_HANDLERS = (SCENEAGENT_FH_composer_images,)


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


def _agent_provider_items(self, context):
    return [
        (key, label, f"Run the chat on the {label} CLI")
        for key, label, _icon in agent.PROVIDERS
    ]


def _agent_setting_changed(self, context):
    """Re-look for the executable the moment the setting changes."""
    with contextlib.suppress(Exception):
        agent.refresh_gate()
    with contextlib.suppress(Exception):
        request_redraw()


class SceneAgentPreferences(bpy.types.AddonPreferences):
    bl_idname = __name__

    agent_provider: bpy.props.EnumProperty(
        name="Agent CLI",
        description=(
            "Which coding CLI answers the chat. It is spawned per turn with "
            "this Blender's MCP server attached, and authenticates itself — "
            "the add-on never sees your credentials"
        ),
        items=_agent_provider_items,
        update=_agent_setting_changed,
    )

    agent_binary: bpy.props.StringProperty(
        name="Executable",
        description=(
            "Full path to the CLI. Leave empty to find it on PATH and in the "
            "usual install locations — which is what you want unless Blender "
            "was launched from a desktop icon with no shell environment"
        ),
        subtype="FILE_PATH",
        default="",
        update=_agent_setting_changed,
    )

    agent_extra_models: bpy.props.StringProperty(
        name="Extra models",
        description=(
            "Models to add to the picker, beyond the ones shipped. Comma "
            "separated, `id` or `id = Label` — for example "
            "`claude-opus-4-7, claude-fable-5-mythos-5 = Mythos`. This is how a "
            "model released after this build reaches the picker"
        ),
        default="",
        update=_agent_setting_changed,
    )

    agent_full_access: bpy.props.BoolProperty(
        name="Give the agent shell and file access",
        description=(
            "Off, the agent gets Blender plus read-only file tools. On, it "
            "gets the CLI's full tool set — running commands and writing files "
            "on this machine — with permission prompts bypassed, because there "
            "is no terminal to answer them"
        ),
        default=False,
    )

    viewport_ui: bpy.props.BoolProperty(
        name="Viewport UI",
        description=(
            "Draw the prompt bar and launcher over the 3D viewport. Turn this "
            "off if the viewport feels slow"
        ),
        default=True,
        update=_viewport_ui_pref_update,
    )

    # Not drawn: the prompt's grip is the only thing that sets it, and 0 means
    # "as tall as the text needs". A size the user dragged belongs to them
    # rather than to a .blend, which is what makes it a preference.
    composer_prompt_height: bpy.props.FloatProperty(
        name="Composer Prompt Height",
        default=0.0,
        options={"HIDDEN"},
    )

    def draw(self, context):
        layout = self.layout

        box = layout.box()
        box.label(text="Agent", icon="CONSOLE")
        box.prop(self, "agent_provider")
        box.prop(self, "agent_binary")
        found = agent.find_binary(self.agent_provider, self.agent_binary)
        if found:
            box.label(text=f"Found: {found}", icon="CHECKMARK")
        else:
            warn = box.row()
            warn.alert = True
            warn.label(
                text=f"No {self.agent_provider} executable found", icon="ERROR"
            )
        box.prop(self, "agent_extra_models")
        box.label(text=f"Picker rows: {len(agent.model_choices())}", icon="PRESET")
        box.prop(self, "agent_full_access")
        if self.agent_full_access:
            warn = box.row()
            warn.alert = True
            warn.label(
                text="The agent can run commands on this machine.", icon="ERROR"
            )
        else:
            box.label(
                text="The agent can drive Blender and read files. Nothing else.",
                icon="INFO",
            )
        box.label(
            text="Sign in to the CLI itself, in a terminal, the usual way.",
            icon="INFO",
        )

        box = layout.box()
        box.label(text="Viewport", icon="OVERLAY")
        box.prop(self, "viewport_ui")
        if self.viewport_ui:
            box.label(
                text="Prompt bar and launcher draw over the viewport.", icon="INFO"
            )
        else:
            box.label(
                text="Viewport surfaces are off; use the sidebar tab (N).",
                icon="INFO",
            )
        row = box.row(align=True)
        row.operator(SCENEAGENT_OT_copy_ui_report.bl_idname, icon="SORTTIME")

        box = layout.box()
        box.label(text="Support Logs (always on, verbose)", icon="TEXT")
        box.label(text=f"Session log: {paths.log_file()}")
        row = box.row(align=True)
        row.operator(
            SCENEAGENT_OT_export_diagnostics.bl_idname,
            text="Export Logs",
            icon="EXPORT",
        )
        row.operator(SCENEAGENT_OT_clear_logs.bl_idname, text="Clear", icon="TRASH")
        row.operator(SCENEAGENT_OT_open_log_folder.bl_idname, icon="FILE_FOLDER")
        row.operator(SCENEAGENT_OT_copy_log_path.bl_idname, icon="COPYDOWN")

        box = layout.box()
        box.label(text="Files On This Machine", icon="FILE_FOLDER")
        located = paths.describe()
        box.label(text=f"Working files: {located['outputs']}")
        box.label(text=f"Cache: {located['cache']}")
        box.label(text=f"Threads: {located['state']}")
        box.label(
            text="All of it is safe to delete; the add-on rebuilds what it needs.",
            icon="INFO",
        )
        row = box.row(align=True)
        row.operator(
            SCENEAGENT_OT_open_output_folder.bl_idname,
            text="Open Folder",
            icon="FILE_FOLDER",
        )
        row.operator(
            SCENEAGENT_OT_remove_local_data.bl_idname,
            text="Remove Local Data",
            icon="TRASH",
        )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    SceneAgentProps,
    SceneAgentPreferences,
    *OPERATORS,
    *FILE_HANDLERS,
    *PANELS,
)


def register():
    # Before anything can announce, and from scratch: a hot reload re-runs this
    # against subscribers closed over the previous load's purged modules.
    events.clear()
    _wire_events()

    for cls in classes:
        bpy.utils.register_class(cls)

    diagnostics.setup_logging(session_info=_get_session_info())
    diagnostics.event(
        "addon",
        "registered",
        version=PLUGIN_VERSION,
        blender=str(bpy.app.version_string),
    )

    bpy.types.Scene.scene_agent = bpy.props.PointerProperty(type=SceneAgentProps)

    previews.start()
    load_logo()
    brand_icon_id()

    if not bpy.app.timers.is_registered(execute_queued_scripts):
        bpy.app.timers.register(execute_queued_scripts, persistent=True)
    if not bpy.app.timers.is_registered(watchdog_timer):
        bpy.app.timers.register(watchdog_timer, persistent=True)

    analytics.init_analytics(
        plugin_version=_plugin_version_string(),
        host_app_version=str(bpy.app.version_string),
        environment=runtime_config.environment(),
    )
    # Signing in used to be what started this. Nothing gates it now: the bridge
    # is a localhost socket and the agent is a process on this machine, so
    # there is nothing to wait for.
    start_mcp_service()
    agent.start()

    reports_overlay.install()
    # Never restore an open overlay: a reload lands on the launcher alone.
    mount.start_surfaces()


def unregister():
    diagnostics.event("addon", "unregistering")
    events.clear()
    # Ahead of the shutdown, and with a low floor: a short session on a slow
    # machine is the one we most want to hear about and the one least likely to
    # reach a scheduled rollup.
    with contextlib.suppress(Exception):
        perf.flush("shutdown", final=True)
    analytics.shutdown_analytics()
    with contextlib.suppress(Exception):
        agent.close()
    mount.stop_surfaces()
    preview_state["window"] = None

    reports_overlay.remove()

    if bpy.app.timers.is_registered(watchdog_timer):
        bpy.app.timers.unregister(watchdog_timer)
    if bpy.app.timers.is_registered(execute_queued_scripts):
        bpy.app.timers.unregister(execute_queued_scripts)

    stop_mcp_service()

    previews.stop()
    reports.clear()
    tasks.clear()

    del bpy.types.Scene.scene_agent
    diagnostics.shutdown_logging()

    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
