# SPDX-FileCopyrightText: 2026 Blender Authors
#
# SPDX-License-Identifier: GPL-3.0-or-later

"""Blender-side bridge for the bundled Blender MCP tool server.

Adapted from Blender Lab's ``blender_mcp_addon``. The Higgsfield extension
owns lifecycle/UI integration while these modules retain the upstream socket
protocol, main-thread execution, output capture, weak sandbox, and deferred
response support.
"""

from . import execute_interactive, mcp_to_blender_server

DEFAULT_HOST = mcp_to_blender_server.DEFAULT_HOST
DEFAULT_PORT = mcp_to_blender_server.DEFAULT_PORT


def is_running():
    return mcp_to_blender_server.is_running()


def start(host=DEFAULT_HOST, port=DEFAULT_PORT):
    """Start the localhost bridge and its Blender main-thread polling timer."""
    if is_running():
        return
    mcp_to_blender_server.start(host, port)
    if not __import__("bpy").app.timers.is_registered(execute_interactive.run):
        __import__("bpy").app.timers.register(
            execute_interactive.run,
            first_interval=mcp_to_blender_server.TIMER_INTERVAL_ACTIVE,
            persistent=True,
        )


def stop():
    """Stop the bridge, clients, deferred work, and polling timer."""
    bpy = __import__("bpy")
    mcp_to_blender_server.stop()
    if bpy.app.timers.is_registered(execute_interactive.run):
        bpy.app.timers.unregister(execute_interactive.run)
