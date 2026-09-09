"""What kind of file this is, by its extension.

One table, because the answer has to be the same everywhere: the history
deciding how to badge a record, the file browser building a `filter_glob`, the
job pipeline naming a download, and the scene helpers deciding whether a path
goes to the image editor or the sequencer. Split across modules these drift,
and a `.m4v` that is a video to one surface and a mystery to another is a bug
nobody can see in a diff.

A leaf: stdlib only, no bpy, no network, so anything may depend on it.
"""

import urllib.parse
from pathlib import Path

MESH_SUFFIXES = {".glb", ".gltf"}
#: Character animation, not geometry: joint transforms on a known skeleton.
MOTION_SUFFIXES = {".npz"}
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".m4v"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}
#: Documents the Supercomputer `/file` upload accepts. Scene Builder offers PDF.
FILE_SUFFIXES = {".pdf"}


def _result_suffix(url, allowed, fallback):
    # A record with no result yet asks this about None, and urlparse answers a
    # bytes path for it rather than raising, which Path then rejects.
    suffix = Path(urllib.parse.urlparse(url or "").path).suffix.lower()
    return suffix if suffix in allowed else fallback


def image_result_suffix(url):
    """The suffix to save an image result under, defaulting to .png."""
    return _result_suffix(url, IMAGE_SUFFIXES, ".png")


def video_result_suffix(url):
    """The suffix to save a video result under, defaulting to .mp4."""
    return _result_suffix(url, VIDEO_SUFFIXES, ".mp4")


ALL_SUFFIXES = (
    MESH_SUFFIXES
    | MOTION_SUFFIXES
    | IMAGE_SUFFIXES
    | VIDEO_SUFFIXES
    | AUDIO_SUFFIXES
    | FILE_SUFFIXES
)


def kind(path):
    """``image`` / ``video`` / ``audio`` / ``file`` from the path's suffix."""
    suffix = Path(path).suffix.lower()
    if suffix in VIDEO_SUFFIXES:
        return "video"
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    if suffix in FILE_SUFFIXES:
        return "file"
    return "image"


def known_suffix(url, default=".bin"):
    """The url's own suffix if it names a kind we handle, else `default`."""
    return _result_suffix(url, ALL_SUFFIXES, default)
