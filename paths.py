# SPDX-FileCopyrightText: 2026 Higgsfield Inc.
# SPDX-License-Identifier: GPL-3.0-or-later

"""Every location the add-on writes to, resolved per platform convention.

Four roots, and which one a file belongs in is a decision about what the user
is allowed to do to it.

- ``outputs_dir()`` — ``<Downloads>/SceneAgent/Blender``. Files the agent is
  and nothing else. It sits in Downloads because that is the one folder whose
  social contract already reads "yours, and safe to throw away", which is
  exactly the promise we want to make about generated media.
- ``cache_dir()`` — thumbnails, model catalogs, brand icons, update zips,
  viewport captures. Losing it costs a re-download and nothing else.
- ``state_dir()`` — the local Scene Builder threads. Never
  Downloads: that folder is shared, cloud-synced and broadly readable by
  other apps, and a bearer token has no business in it. Created ``0700``
  with ``0600`` files.
- ``logs_dir()`` — session log, MCP log, per-failure error reports.

**Any of these may be gone at any moment.** The user is actively invited to
delete them, so nothing may assume a directory it wrote to last time still
exists. That is why every accessor here creates as it resolves: there is no way
to obtain a path from this module without having just ensured its parent. The
one case the pattern cannot cover is a long-lived open handle — ``diagnostics``
holds the log file open and re-opens it when the inode disappears underneath.

This module imports nothing but the standard library, deliberately.
``bridge_runner.py`` runs as a bare script in its own subprocess, with no
``bpy`` and no package context, and reads the OAuth token through here.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path
import shutil
import sys
import time

# The add-on's own folder under the platform's data/cache/log roots. Renamed
# from Higgsfield/Blender with the account: this build stores no token and
# belongs to nobody's product, and a folder named after a vendor whose service
# it cannot reach is a confusing thing to leave on someone's disk.
VENDOR = "SceneAgent"
PRODUCT = "Blender"

_WINDOWS = sys.platform == "win32"
_MACOS = sys.platform == "darwin"

# ``FOLDERID_Downloads``. Downloads is a Known Folder on Windows and is
# routinely redirected (OneDrive does it by default), so ``~/Downloads`` is a
# guess, not an answer.
_FOLDERID_DOWNLOADS = "{374DE290-123F-4565-9164-39C4925E467B}"


def _env_path(name: str) -> Path | None:
    """An absolute path from the environment, or None when unset or relative."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return None
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else None


def _ensure(path: Path, *, private: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if private:
        harden(path)
    return path


def harden(path: Path) -> None:
    """Restrict a credential file or directory to its owner where the OS can."""
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700 if path.is_dir() else 0o600)


# ---------------------------------------------------------------------------
# The four roots
# ---------------------------------------------------------------------------


def _state_root() -> Path:
    if _MACOS:
        return Path.home() / "Library" / "Application Support" / VENDOR / PRODUCT
    if _WINDOWS:
        local = _env_path("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return local / VENDOR / PRODUCT / "Data"
    xdg = _env_path("XDG_DATA_HOME") or (Path.home() / ".local" / "share")
    return xdg / VENDOR.lower() / PRODUCT.lower()


def _cache_root() -> Path:
    if _MACOS:
        return Path.home() / "Library" / "Caches" / VENDOR / PRODUCT
    if _WINDOWS:
        local = _env_path("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return local / VENDOR / PRODUCT / "Cache"
    xdg = _env_path("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return xdg / VENDOR.lower() / PRODUCT.lower()


def _logs_root() -> Path:
    if _MACOS:
        # The place a Mac user is told to look, and the one Console.app reads.
        return Path.home() / "Library" / "Logs" / VENDOR / PRODUCT
    if _WINDOWS:
        local = _env_path("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
        return local / VENDOR / PRODUCT / "Logs"
    # XDG grew a state dir precisely for logs and history; it is not cache
    # (losing it mid-session is not free) and not data (nothing here is
    # portable to another machine).
    xdg = _env_path("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return xdg / VENDOR.lower() / PRODUCT.lower() / "logs"


def _windows_downloads() -> Path | None:
    try:
        import winreg
    except ImportError:
        return None
    subkey = r"Software\Microsoft\Windows\CurrentVersion\Explorer"
    # "User Shell Folders" is authoritative and stores the unexpanded form;
    # "Shell Folders" is a resolved cache that can be missing entries.
    for name in ("User Shell Folders", "Shell Folders"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, f"{subkey}\\{name}") as key:
                value, _ = winreg.QueryValueEx(key, _FOLDERID_DOWNLOADS)
        except OSError:
            continue
        if not value:
            continue
        path = Path(os.path.expandvars(str(value))).expanduser()
        if path.is_absolute():
            return path
    return None


def _xdg_downloads() -> Path | None:
    direct = _env_path("XDG_DOWNLOAD_DIR")
    if direct is not None:
        return direct
    config = _env_path("XDG_CONFIG_HOME") or (Path.home() / ".config")
    user_dirs = config / "user-dirs.dirs"
    try:
        text = user_dirs.read_text(encoding="utf-8")
    except OSError:
        return None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("XDG_DOWNLOAD_DIR"):
            continue
        _, _, value = line.partition("=")
        value = value.strip().strip('"').strip("'")
        if not value:
            continue
        # The file is shell syntax and localized: `XDG_DOWNLOAD_DIR="$HOME/Загрузки"`.
        expanded = os.path.expandvars(value.replace("$HOME", str(Path.home())))
        path = Path(expanded).expanduser()
        if path.is_absolute():
            return path
    return None


def downloads_root() -> Path:
    """The user's Downloads folder, as the OS has it configured.

    Only macOS can be assumed to be `~/Downloads`. Windows keeps it as a Known
    Folder that OneDrive redirects by default, and on Linux it is an
    XDG user dir that is localized — `~/Загрузки` on a Russian desktop. Writing
    to a guessed `~/Downloads` on either would put files somewhere the user
    never looks.
    """
    if _WINDOWS:
        configured = _windows_downloads()
    elif _MACOS:
        configured = None
    else:
        configured = _xdg_downloads()
    return configured if configured is not None else Path.home() / "Downloads"


def outputs_root() -> Path:
    """Where generations go, *without* creating it.

    For read-only probes and for showing the path. Nothing should put a folder
    in someone's Downloads before they have generated anything — an empty
    `SceneAgent/Blender` appearing on install is the exact clutter this layout
    exists to avoid.
    """
    return downloads_root().joinpath(VENDOR, PRODUCT)


def outputs_dir(*parts: str) -> Path:
    """Where finished generations land, and the folder we tell users to delete."""
    return _ensure(outputs_root().joinpath(*parts))


def cache_dir(*parts: str) -> Path:
    return _ensure(_cache_root().joinpath(*parts))


def state_dir(*parts: str) -> Path:
    return _ensure(_state_root().joinpath(*parts), private=True)


def logs_dir(*parts: str) -> Path:
    return _ensure(_logs_root().joinpath(*parts), private=True)


# ---------------------------------------------------------------------------
# Named locations
# ---------------------------------------------------------------------------

#: Output subfolder per asset kind, keyed by ``asset_kind`` from the catalog.
_OUTPUT_KINDS = {
    "image": "images",
    "video": "videos",
    "mesh": "3d",
    "motion": "motion",
}


def outputs_kind_dir(asset_kind: str | None) -> Path:
    return outputs_dir(_OUTPUT_KINDS.get(asset_kind or "", "other"))


def auth_file() -> Path:
    return state_dir() / "auth.json"


def device_id_file() -> Path:
    return state_dir() / "device.json"


def catalog_file(kind: str) -> Path:
    """Offline copy of a model catalog — ``image``, ``video`` or ``3d``."""
    return cache_dir("catalogs") / f"{kind}-models.json"


def thumbs_dir() -> Path:
    return cache_dir("thumbs")


def provider_icons_dir() -> Path:
    return cache_dir("provider-icons")


def updates_dir() -> Path:
    return cache_dir("updates")


def viewport_captures_dir() -> Path:
    return cache_dir("viewport-captures")


def realtime_dir() -> Path:
    return cache_dir("realtime")


def log_file() -> Path:
    return logs_dir() / "blender.log"


def mcp_log_file() -> Path:
    return logs_dir() / "mcp.log"


def error_reports_dir() -> Path:
    return logs_dir("error-reports")


# ---------------------------------------------------------------------------
# Keeping the footprint bounded
# ---------------------------------------------------------------------------


def prune(
    directory: Path,
    *,
    keep: int | None = None,
    max_age_days: float | None = None,
    pattern: str = "*",
) -> int:
    """Trim a cache directory, newest kept. Returns how many entries went.

    Everything under `cache_dir()` and the error reports are append-only by
    nature — one file per failed request, one zip per version, one directory
    per viewport capture. Without this they only ever grow, and a folder we
    tell the user is disposable should not quietly reach a size where deleting
    it is the only sane option.
    """
    stamped = []
    try:
        for entry in directory.glob(pattern):
            with contextlib.suppress(OSError):
                stamped.append((entry.stat().st_mtime, entry))
    except OSError:
        return 0
    stamped.sort(key=lambda item: item[0], reverse=True)

    doomed = {}
    if keep is not None and len(stamped) > keep:
        doomed.update((entry, None) for _, entry in stamped[keep:])
    if max_age_days is not None:
        cutoff = time.time() - max_age_days * 86400
        doomed.update((entry, None) for mtime, entry in stamped if mtime < cutoff)

    removed = 0
    for entry in doomed:
        try:
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def local_data_roots() -> tuple[Path, ...]:
    """The three roots "remove my data" clears. Outputs are the user's, not ours."""
    return (_state_root(), _cache_root(), _logs_root())


def remove_local_data() -> tuple[list[Path], list[Path]]:
    """Delete the cache, state and logs trees. Returns ``(removed, failed)``.

    Deliberately not the outputs folder: those are the user's generations, and
    a button labelled "remove the add-on's data" quietly eating them would be
    the worst thing this module could do. It sits in Downloads precisely so
    that deleting it stays the user's own decision.
    """
    removed: list[Path] = []
    failed: list[Path] = []
    for root in local_data_roots():
        if not root.exists():
            continue
        shutil.rmtree(root, ignore_errors=True)
        (failed if root.exists() else removed).append(root)
        _rmdir_empty_parents(root.parent)
    return removed, failed


def _rmdir_empty_parents(directory: Path) -> None:
    """Walk off the `SceneAgent/Blender` wrapper dirs once they hold nothing.

    On Windows all three roots are siblings under one folder; without this the
    user is left staring at an empty `SceneAgent` tree after asking us to go.
    """
    for candidate in (directory, directory.parent):
        if candidate.name not in (PRODUCT, VENDOR, VENDOR.lower(), PRODUCT.lower()):
            return
        try:
            candidate.rmdir()
        except OSError:
            return


def describe() -> dict[str, str]:
    """Where everything lives, for the preferences panel and the support pack."""
    return {
        "outputs": str(outputs_root()),
        "cache": str(_cache_root()),
        "state": str(_state_root()),
        "logs": str(_logs_root()),
    }
