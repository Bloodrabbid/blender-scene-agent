# SPDX-License-Identifier: GPL-3.0-or-later

"""Lifecycle for the Blender MCP execution bridge.

This used to be two things: the localhost TCP exec bridge, and an outbound
``bridge_runner.py`` holding a WebSocket open to Higgsfield's MCP worker so
their hosted agent could drive this Blender. The second half is gone with the
account — nothing dials out any more, and there is no token left to dial with.

What remains is the half that was always local. `mcp_bridge` listens on
``127.0.0.1:9876`` and runs what it is handed on Blender's main thread; the
`blmcp` stdio server bundled in the wheels is what speaks MCP to a client and
forwards to that port. Scene Builder starts one of those servers per turn, as a
child of the user's own CLI (`features/agent.py`). The panel at the bottom of
this file hands over the same config for a CLI the user runs themselves, in
their own terminal, against the Blender they already have open.
"""

from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import sys
import logging
import threading

import bpy

from . import diagnostics, paths
from .features import agent

logger = logging.getLogger(__name__)

from . import mcp_bridge

BRIDGE_HOST = "127.0.0.1"
BRIDGE_PORT = 9876
BRIDGE_PORT_FALLBACKS = 3

_log_file = None
_lock = threading.Lock()
_last_error = ""
# The port the bridge actually bound. Not always `BRIDGE_PORT`: a second
# Blender, or anything else already on 9876, pushes it up the fallback range,
# and every MCP config handed out has to name the one in use.
_bridge_port = BRIDGE_PORT


def bridge_port() -> int:
    return _bridge_port


def _extension_site_packages() -> str:
    """Find Blender's shared directory where extension wheels are installed."""
    for entry in sys.path:
        normalized = entry.replace("\\", "/")
        if "/extensions/.local/lib/" in normalized and normalized.endswith(
            "/site-packages"
        ):
            return entry
    raise RuntimeError("Blender extension site-packages directory was not found")


def _log_path() -> Path:
    return paths.mcp_log_file()


def _set_private_permissions(path: Path) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def start() -> None:
    """Start the TCP exec bridge."""
    global _log_file, _last_error, _bridge_port
    with _lock:
        if mcp_bridge.is_running():
            return

        _last_error = ""
        try:
            bound = None
            bridge_error = None
            for candidate in range(
                BRIDGE_PORT, BRIDGE_PORT + BRIDGE_PORT_FALLBACKS + 1
            ):
                try:
                    mcp_bridge.start(BRIDGE_HOST, candidate)
                    bound = candidate
                    break
                except OSError as error:
                    bridge_error = error
            if bound is None:
                raise RuntimeError(
                    f"Could not bind MCP bridge ports {BRIDGE_PORT}-"
                    f"{BRIDGE_PORT + BRIDGE_PORT_FALLBACKS}: {bridge_error}"
                )
            _bridge_port = bound

            log_path = _log_path()
            if _log_file is None:
                _log_file = log_path.open("ab", buffering=0)
                _set_private_permissions(log_path)
        except Exception as error:
            _last_error = str(error)
            mcp_bridge.stop()
            if _log_file is not None:
                with contextlib.suppress(Exception):
                    _log_file.close()
                _log_file = None
            raise


def stop() -> None:
    """Stop the exec bridge."""
    global _log_file
    with _lock:
        if _log_file is not None:
            with contextlib.suppress(Exception):
                _log_file.close()
            _log_file = None
        mcp_bridge.stop()


def restart() -> None:
    stop()
    start()


def status() -> tuple[str, str]:
    """Return ``(state, detail)`` for the local exec bridge."""
    with _lock:
        if not mcp_bridge.is_running():
            return ("error", _last_error) if _last_error else ("stopped", "")
        return ("running", f"{BRIDGE_HOST}:{_bridge_port}")


def is_running() -> bool:
    return status()[0] == "running"


# ---------------------------------------------------------------------------
# Handing the same server to a CLI the user drives themselves
# ---------------------------------------------------------------------------


def mcp_server_spec() -> dict:
    """The stdio server entry, as an MCP client config wants it."""
    return agent.mcp_config(BRIDGE_HOST, _bridge_port)["mcpServers"][
        agent.MCP_SERVER_NAME
    ]


def claude_add_command() -> str:
    """One line to paste into a terminal to give `claude` this Blender."""
    spec = json.dumps(mcp_server_spec(), separators=(",", ":"))
    return f"claude mcp add-json {agent.MCP_SERVER_NAME} '{spec}'"


def codex_config_snippet() -> str:
    """The `~/.codex/config.toml` block for the same server."""
    spec = mcp_server_spec()
    lines = [
        f"[mcp_servers.{agent.MCP_SERVER_NAME}]",
        f"command = {json.dumps(spec['command'])}",
        f"args = {json.dumps(spec['args'])}",
        "env = {"
        + ", ".join(
            f"{json.dumps(key)} = {json.dumps(value)}"
            for key, value in spec["env"].items()
        )
        + "}",
    ]
    return "\n".join(lines)


def start_mcp_service():
    """Start the bundled Blender MCP bridge."""
    if bpy.app.background:
        return
    diagnostics.event("mcp", "service_start_attempt")
    try:
        start()
        diagnostics.event("mcp", "service_start_ok", port=_bridge_port)
    except Exception as error:
        logger.exception("Blender MCP bridge failed to start")
        diagnostics.event("mcp", "service_start_failed", error=str(error))


def stop_mcp_service():
    diagnostics.event("mcp", "service_stopped")
    with contextlib.suppress(Exception):
        stop()


class SCENEAGENT_OT_restart_mcp(bpy.types.Operator):
    bl_idname = "scene_agent.restart_mcp"
    bl_label = "Restart Bridge"
    bl_description = "Restart the local Blender execution bridge"

    def execute(self, context):
        try:
            restart()
        except Exception as error:
            self.report({"ERROR"}, f"The bridge could not start: {error}")
            return {"CANCELLED"}
        self.report({"INFO"}, "Bridge restarted.")
        return {"FINISHED"}


class SCENEAGENT_OT_copy_mcp_config(bpy.types.Operator):
    bl_idname = "scene_agent.copy_mcp_config"
    bl_label = "Copy"
    bl_description = (
        "Copy the setup for your own CLI: the `claude mcp add-json` line, or "
        "the Codex config.toml block"
    )

    def execute(self, context):
        from .props import addon_preferences

        prefs = addon_preferences()
        provider = getattr(prefs, "agent_provider", agent.CLAUDE) if prefs else agent.CLAUDE
        payload = (
            codex_config_snippet() if provider == agent.CODEX else claude_add_command()
        )
        context.window_manager.clipboard = payload
        self.report({"INFO"}, "Copied to the clipboard")
        return {"FINISHED"}


# ---------------------------------------------------------------------------
# The panel
#
# It sits with the service rather than in `sidebar/panels.py` because every
# name on it is one of this module's: the bridge status, the agent's own
# readiness, and the two operators.
# ---------------------------------------------------------------------------


class SCENEAGENT_PT_agent(bpy.types.Panel):
    bl_idname = "SCENEAGENT_PT_agent"
    bl_parent_id = "SCENEAGENT_PT_panel"
    bl_label = "Agent"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Scene Agent"

    def draw_header_preset(self, context):
        # Right-side status (draw_header would sit left of the title).
        self.layout.label(
            text="",
            icon="CHECKMARK" if is_running() else "ERROR",
        )

    def draw(self, context):
        from .props import addon_preferences

        layout = self.layout
        prefs = addon_preferences()
        provider = getattr(prefs, "agent_provider", agent.CLAUDE) if prefs else agent.CLAUDE
        override = getattr(prefs, "agent_binary", "") if prefs else ""
        binary = agent.find_binary(provider, override)
        state, detail = status()

        box = layout.box()
        box.label(text="Blender bridge", icon="NETWORK_DRIVE")
        labels = {
            "running": (f"Listening on {detail}", "LINKED"),
            "stopped": ("Not running", "UNLINKED"),
            "error": (str(detail)[:48] or "Error", "ERROR"),
        }
        text, icon = labels.get(state, ("Bridge", "DOT"))
        box.label(text=text, icon=icon)
        box.operator(SCENEAGENT_OT_restart_mcp.bl_idname, icon="FILE_REFRESH")

        box = layout.box()
        box.label(text="Scene Builder agent", icon="CONSOLE")
        if binary:
            box.label(text=f"{provider}: ready", icon="CHECKMARK")
            box.label(text=str(binary)[-46:])
        else:
            warn = box.row()
            warn.alert = True
            warn.label(text=f"{provider} CLI not found", icon="ERROR")
            box.label(text="Install it, or set its path in Preferences.")
        box.operator("scene_agent.open_prefs", text="Preferences", icon="PREFERENCES")

        box = layout.box()
        box.label(text="Use this Blender from your own terminal")
        box.label(text="Copy, paste into a shell, then talk to Blender")
        box.label(text="from any session of your CLI.")
        box.operator(SCENEAGENT_OT_copy_mcp_config.bl_idname, icon="COPYDOWN")
