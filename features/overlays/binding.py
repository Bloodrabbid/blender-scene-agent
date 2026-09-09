"""What the composer needs to know about the files dropped on it.

This module used to be the bridge between the composer's chips and the
Higgsfield generation catalogs: eleven hundred lines of schema decoding, media
fields, cost estimates and per-mode preparation, one binding object per
generation tab. Those tabs are gone with the account, and so is all of it.

What is left is the part that was never about a catalog — which kinds of file
the composer will take, and what to say when it will not take one. Scene
Builder attaches files by path (`composer_surface.attach_scene_builder`) and
needs no binding at all, so the three binding entry points below exist only so
the call sites that still name them read honestly instead of failing with an
AttributeError somewhere deeper.
"""

from __future__ import annotations

from pathlib import Path

_COMPOSER_IMAGE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
)
_COMPOSER_VIDEO_SUFFIXES = frozenset({".mp4", ".mov", ".webm", ".m4v", ".avi"})
_COMPOSER_AUDIO_SUFFIXES = frozenset({".wav", ".mp3", ".m4a", ".aac", ".flac", ".ogg"})

_COMPOSER_MEDIA_SUFFIXES = {
    "image": _COMPOSER_IMAGE_SUFFIXES,
    "video": _COMPOSER_VIDEO_SUFFIXES,
    "audio": _COMPOSER_AUDIO_SUFFIXES,
}

#: What the coding CLI can actually open when it is handed a path, and so the
#: only thing the prompt will take: the four image formats the model can see,
#: plus PDF. Checked against the CLI rather than assumed — a BMP comes back
#: "not supported by vision system", a TIFF and an MP4 come back as binary it
#: will not read. Accepting those made a chip whose only possible outcome was a
#: failed tool row, which is worse than a drop that declines on the spot.
AGENT_READABLE_SUFFIXES = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".pdf"}
)
AGENT_READABLE_HINT = "Attach a PNG, JPG, GIF, WEBP or PDF — the CLI reads those."

_COMPOSER_MEDIA_HINT = {
    "image": "That file is not an image.",
    "video": "That file is not a video.",
    "audio": "That file is not an audio clip.",
}


def _composer_media_kind(path):
    """``image`` / ``video`` / ``audio`` for a path, or ``None``."""
    suffix = Path(str(path)).suffix.lower()
    for kind, suffixes in _COMPOSER_MEDIA_SUFFIXES.items():
        if suffix in suffixes:
            return kind
    return None


def _composer_binding(props=None):
    """There are no generation modes left to bind to."""
    return None


def _composer_prepare(binding):
    """Nothing to prepare: the only mode is a chat."""
    return True


def _composer_add_image_paths(binding, paths):
    raise RuntimeError("The composer is not accepting references.")


def forget_decodes():
    """No decode caches any more. Kept for teardown and the hot reload."""
    return None
