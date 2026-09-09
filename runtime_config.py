"""What is left of the packaged endpoint config.

Every hostname this module used to resolve belonged to Higgsfield: the FNF SDK
gateway, its notification mount, the device-auth worker, the hosted MCP worker
and its plugin WebSocket, the web origin used as an OAuth return URL, and the
phone camera's room server. None of them is reachable from this build, so none
of them is named here any more — a URL nothing calls is still a URL somebody
will one day call by accident.

The build-time bake (`build_config.py`, written by the vendor's packaging
script) is likewise not read. Three small things stayed, because they are about
this add-on rather than about a service:

- `environment()`, which some log lines still print.
- `user_agent()`, sent on plain downloads.
- `phonecam_origin()`, which now answers "" — the phone camera's Start path
  reads it and fails cleanly on an empty origin, which is exactly right for a
  hosted room that no longer exists.
"""

from __future__ import annotations

import os


def environment() -> str:
    """``dev`` or ``prod``, from the environment only. Cosmetic now."""
    value = os.environ.get("SCENE_AGENT_ENV", "").strip().lower()
    return "prod" if value in ("prod", "production") else "dev"


def phonecam_origin() -> str:
    """The hosted phone-camera origin. Empty: there is no longer one."""
    return ""


def user_agent() -> str:
    """Product User-Agent for plain downloads.

    The version is read late so this module can load during ``register()``.
    """
    try:
        from . import PLUGIN_VERSION
    except Exception:  # noqa: BLE001 — root still executing
        PLUGIN_VERSION = "0"
    return f"BlenderSceneAgent/{PLUGIN_VERSION}"
