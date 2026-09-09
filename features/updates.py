"""The version, and no updater.

This module used to check `hf-adobe-updates.higgsfield.ai` on every register,
download a newer .zip and install it over the running add-on. That is removed,
and it is the one removal that would undo all the others: an updater pointed at
the vendor's manifest reinstalls the vendor's build, auth and all, the next
time Blender starts.

What is left is the version string and the shape of `update_state`, because the
sidebar reads both. `has_update` is permanently false, so the update banner and
its two operators never draw.

To update this build, install the new .zip yourself.
"""

from __future__ import annotations


def _plugin_version_string():
    # Imported at call time: `bl_info` is captured in the root, which is still
    # executing when this module loads.
    from .. import PLUGIN_VERSION

    return PLUGIN_VERSION


# Read by the sidebar's banner, which draws nothing while `has_update` is
# false. Kept as a dict rather than deleted so that code stays a plain read.
update_state = {
    "status": "idle",
    "manifest": None,
    "has_update": False,
    "error": None,
    "install_status": "idle",
    "install_error": None,
    "install_progress": 0.0,
    "install_path": None,
    "auto_opened_version": None,
    "available_tracked": False,
}


def check_for_updates(*_args, **_kwargs):
    """No manifest to check. Kept so a stray call site is not a crash."""
    return None


def _auto_open_update_dialog():
    """Never registered as a timer any more; returns None to unregister."""
    return None
