# SPDX-FileCopyrightText: 2026 Higgsfield Inc.
# SPDX-License-Identifier: GPL-3.0-or-later
"""Pictures and files on the OS clipboard, as paths on disk.

An add-on gets `window_manager.clipboard` from Blender, which is text and only
text. So Cmd+V over the prompt could paste the *name* of a screenshot and never
the screenshot — while pasting a picture into a chat is the whole of what the
gesture means to the person doing it. The image is on a pasteboard flavour bpy
cannot reach, and all three platforms keep it somewhere different, so each one
gets the single command that reads it.

Two things can be on there worth having: a file somebody copied in a file
manager, which is already a path, and raw image data, which is written into
the add-on's cache and becomes one. Everything else — text, nothing at all, a
platform whose tool is not installed — comes back empty, and the caller pastes
text exactly as it did before this module existed.

Only the macOS path is tested. The other two are written from documented
behaviour and are built to fail quietly rather than cleverly.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

from .. import paths

#: A clipboard read is on the main thread between a key press and a redraw.
#: Long enough for a cold `osascript`, short enough that a wedged helper is a
#: hitch rather than a hang.
TIMEOUT = 4.0

#: What we will write clipboard image data as. PNG because it is lossless, is
#: what a screenshot already is on every platform here, and is one of the five
#: formats the CLI can read — see `overlays.binding.AGENT_READABLE_SUFFIXES`.
_SUFFIX = ".png"


def _run(argv):
    """One short helper process. Empty string for every kind of failure."""
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        done = subprocess.run(
            argv,
            input="",
            capture_output=True,
            timeout=TIMEOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            creationflags=creationflags,
        )
    except Exception:
        return ""
    if done.returncode != 0:
        return ""
    return (done.stdout or "").strip()


def _scratch():
    """A fresh path in the add-on's cache for one pasted picture.

    Named by the clock rather than by a counter so two Blenders pasting at
    once do not fight, and so the folder reads as a history of what was
    pasted rather than as a pile of `image-3.png`.
    """
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    return paths.cache_dir("pasted") / f"clipboard-{stamp}{_SUFFIX}"


def _existing(candidate):
    """``[path]`` if that is a real file, else ``[]``.

    Worth its own function because the coercions below lie. AppleScript will
    happily turn the plain text "hello world" into the file URL
    `file:///hello world` and hand it over without erroring, so the answer is
    only believable if something is actually there.
    """
    text = str(candidate or "").strip()
    if not text:
        return []
    path = Path(text).expanduser()
    return [str(path.resolve())] if path.is_file() else []


# ---------------------------------------------------------------------------
# macOS
# ---------------------------------------------------------------------------

_MAC_FILE = """try
\tPOSIX path of (the clipboard as «class furl»)
on error
\t""
end try"""

# `set eof` first: `open for access` on an existing path appends, so pasting a
# second smaller picture into a reused name would leave the tail of the first.
_MAC_IMAGE = """on run argv
\tset target to item 1 of argv
\ttry
\t\tset payload to the clipboard as «class PNGf»
\ton error
\t\treturn ""
\tend try
\tset handle to open for access (POSIX file target) with write permission
\tset eof handle to 0
\twrite payload to handle
\tclose access handle
\treturn target
end run"""


def _mac_paths():
    advertised = _run(["osascript", "-e", "clipboard info"])
    if "«class furl»" in advertised:
        found = _existing(_run(["osascript", "-e", _MAC_FILE]))
        if found:
            return found
    if not any(
        flavour in advertised
        for flavour in ("«class PNGf»", "TIFF picture", "«class 8BPS»")
    ):
        return []
    target = _scratch()
    written = _run(["osascript", "-e", _MAC_IMAGE, str(target)])
    return _existing(written)


# ---------------------------------------------------------------------------
# Windows
# ---------------------------------------------------------------------------

# `-sta`: the clipboard classes are single-threaded-apartment only and return
# nothing at all from the default MTA, silently.
_WIN_SCRIPT = """
Add-Type -AssemblyName System.Windows.Forms, System.Drawing
$files = [Windows.Forms.Clipboard]::GetFileDropList()
if ($files.Count -gt 0) { Write-Output $files[0]; exit }
$image = [Windows.Forms.Clipboard]::GetImage()
if ($image -ne $null) {
  $image.Save($args[0], [System.Drawing.Imaging.ImageFormat]::Png)
  Write-Output $args[0]
}
"""


def _windows_paths():
    target = _scratch()
    written = _run(
        [
            "powershell",
            "-sta",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            _WIN_SCRIPT,
            str(target),
        ]
    )
    return _existing(written)


# ---------------------------------------------------------------------------
# Linux
# ---------------------------------------------------------------------------


def _linux_paths():
    """Wayland first, then X11; whichever tool is installed answers."""
    readers = (
        (["wl-paste", "--list-types"], ["wl-paste", "--no-newline", "--type"]),
        (
            ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"],
            ["xclip", "-selection", "clipboard", "-o", "-t"],
        ),
    )
    for list_argv, read_argv in readers:
        offered = _run(list_argv)
        if not offered:
            continue
        if "text/uri-list" in offered:
            uris = _run(read_argv + ["text/uri-list"])
            for line in uris.splitlines():
                found = _existing(line.strip().removeprefix("file://"))
                if found:
                    return found
        if "image/png" not in offered:
            continue
        target = _scratch()
        try:
            with open(target, "wb") as handle:
                subprocess.run(
                    read_argv + ["image/png"],
                    stdout=handle,
                    stderr=subprocess.DEVNULL,
                    timeout=TIMEOUT,
                    check=True,
                )
        except Exception:
            continue
        found = _existing(target)
        if found:
            return found
    return []


def clipboard_paths():
    """Files the clipboard is offering, as paths. ``[]`` when it holds none.

    Never raises: a paste is a reflex, and a reflex that can throw a traceback
    into the viewport is worse than one that quietly falls back to text.
    """
    try:
        if sys.platform == "darwin":
            return _mac_paths()
        if sys.platform == "win32":
            return _windows_paths()
        return _linux_paths()
    except Exception:
        return []
