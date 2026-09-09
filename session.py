"""What is left of the connection layer once the account is gone.

This module used to hold the Higgsfield session: an RFC 8628 device token in
`paths.auth_file()`, its refresh loop, and a pooled `Higgsfield` SDK client
keyed on the active workspace. All of that is removed. The add-on no longer
signs in to anything, holds no credential, and ships no path that could
acquire one — the agent in the viewport runs on the user's own coding CLI,
which brings its own authentication and keeps it to itself.

Three things stayed, because they are not about an account:

- **`auth_state` and `is_authenticated()`**, now constants. Roughly forty call
  sites and every sidebar panel are gated on them, and the gate still has a
  meaning worth keeping: "the add-on is up". Rather than unpick the gate in
  forty places, the answer is simply always yes.
- **The plain HTTP helpers**, which take a URL and add no credential. The
  updater used to use them; downloads still can.
- **`get_sdk_client()`**, which now raises. The generation features (image,
  video, 3D, history, credits) were the account's, and are unreachable from
  the UI — but their code is still on disk, so the one door they all went
  through is kept, closed and named, instead of failing with an ImportError
  somewhere further in.

If you are re-adding a backend, do it here. Nothing else in the add-on knows
how to open a connection.
"""

import contextlib
import json
import logging
import os
import socket
import tempfile

import httpx

from . import diagnostics, events, paths, runtime_config

logger = logging.getLogger(__name__)


BACKEND_REMOVED = (
    "The Higgsfield backend was removed from this build. Scene Builder runs on "
    "your own CLI; generation, credits and Results are gone with the account."
)

FNF_SDK_BASE_URL = ""
USER_AGENT = runtime_config.user_agent
# The ceiling on any one download.
JOB_TIMEOUT_SECONDS = 600


def auth_file():
    """Kept so housekeeping can still delete a token an older build wrote."""
    return paths.auth_file()


# There is no signed-out state to be in. The keys are the ones the panels and
# the composer read; they are here so a reader finds a shape, not a KeyError.
auth_state = {
    "status": "authenticated",
    "user_code": None,
    "verification_url": None,
    "error": None,
    "user": None,
    "balance": None,
    "workspaces": [],
    "workspace_error": None,
    "sdk_error": None,
    "mcp_error": None,
}


# ---------------------------------------------------------------------------
# HTTP utilities (httpx, no credentials)
# ---------------------------------------------------------------------------


def http_request(url, *, method="GET", headers=None, data=None, timeout=120):
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = data.encode("utf-8")
    req_ctype = (headers or {}).get("Content-Type") or (headers or {}).get(
        "content-type"
    )
    diagnostics.log_http("request", method, url, body=data, content_type=req_ctype)
    try:
        response = httpx.request(
            method,
            url,
            headers=headers,
            content=data,
            timeout=timeout,
            follow_redirects=True,
        )
    except httpx.HTTPError as error:
        diagnostics.log_http("response", method, url, error=error)
        raise RuntimeError(f"Request to {url} failed: {error}") from error
    diagnostics.log_http(
        "response",
        method,
        url,
        status=response.status_code,
        body=response.content,
        content_type=response.headers.get("content-type"),
    )
    if response.is_error:
        raise RuntimeError(f"HTTP {response.status_code} from {url}: {response.text}")
    return response.status_code, response.content


def http_json(url, *, method="GET", headers=None, payload=None, timeout=120):
    headers = dict(headers or {})
    data = None
    if payload is not None:
        headers.setdefault("Content-Type", "application/json")
        data = json.dumps(payload).encode("utf-8")
    _status, body = http_request(
        url, method=method, headers=headers, data=data, timeout=timeout
    )
    if not body:
        return {}
    return json.loads(body.decode("utf-8"))


def _is_transient_network_error(error):
    """Return True only for transport failures, never API/validation errors."""
    current = error
    while current is not None:
        if isinstance(
            current,
            (httpx.TransportError, socket.gaierror, ConnectionError, TimeoutError),
        ):
            return True
        current = current.__cause__
    return False


def _network_retry(call, *, progress=None, attempts=3):
    """Retry transient DNS/connectivity failures off Blender's main thread."""
    import time

    for attempt in range(1, attempts + 1):
        try:
            return call()
        except Exception as error:
            if attempt >= attempts or not _is_transient_network_error(error):
                raise
            if progress:
                progress(f"network unavailable · retrying ({attempt}/{attempts - 1})")
            time.sleep(1.5 * attempt)


def _download_to_file_once(url, suffix="", *, on_progress=None):
    """Stream a URL to a temporary file and return its path."""
    fd, path = tempfile.mkstemp(suffix=suffix, prefix="blender_agent_")
    os.close(fd)
    try:
        with httpx.stream(
            "GET",
            url,
            headers={"User-Agent": USER_AGENT()},
            timeout=JOB_TIMEOUT_SECONDS,
            follow_redirects=True,
        ) as response:
            if response.is_error:
                response.read()
                raise RuntimeError(
                    f"HTTP {response.status_code} from {url}: {response.text}"
                )
            total = int(response.headers.get("Content-Length") or 0)
            downloaded = 0
            with open(path, "wb") as handle:
                for chunk in response.iter_bytes(chunk_size=65536):
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if on_progress and total:
                        on_progress(downloaded / total)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        raise
    return path


def download_to_file(url, suffix="", *, on_progress=None):
    """Download with short retries for transient DNS and connection failures."""
    return _network_retry(
        lambda: _download_to_file_once(url, suffix=suffix, on_progress=on_progress),
        attempts=3,
    )


# ---------------------------------------------------------------------------
# The account, as it is now: absent
# ---------------------------------------------------------------------------


def is_authenticated():
    """Always true. There is no account to be outside of."""
    return True


# A workspace used to be the account's, and a good deal of the add-on is keyed
# on one — most importantly `features/scene_history.py`, which files the local
# Scene Builder threads under it. There is exactly one now, and it is this
# machine. Returning `None` here would be truthful and would silently stop the
# thread history from ever being written, because an empty key is how that
# module spells "no workspace, do not save".
LOCAL_WORKSPACE = "local"


def _active_workspace_id():
    return LOCAL_WORKSPACE


def _persisted_workspace_id():
    return LOCAL_WORKSPACE


def _auth_state_workspace_id():
    return LOCAL_WORKSPACE


def set_auth_state(**changes):
    """Record a change and announce it, without any session behind it.

    Nothing transitions any more — the add-on is authenticated from import —
    so this only ever emits ``AUTH_SETTLED``. It is kept because the panels
    write status strings through it and the event table still listens.
    """
    auth_state.update(changes)
    events.emit(
        events.AUTH_SETTLED,
        authenticated=True,
        changed=frozenset(changes),
    )


def get_valid_access_token():
    raise RuntimeError(BACKEND_REMOVED)


def get_valid_oauth_token():
    return None


def get_sdk_client(workspace_id=None):
    """The one door to the Higgsfield API, and it is shut."""
    raise RuntimeError(BACKEND_REMOVED)


def close_sdk_client():
    return None
