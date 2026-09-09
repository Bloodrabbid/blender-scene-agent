"""Finding the 3D viewport, and asking Blender to draw again."""

import contextlib
import os
import shutil
import tempfile
import uuid
from pathlib import Path

import bpy

from .. import paths

# A capture is a reference we send with one job. Only the failure path used to
# clean up after itself, so every successful animation left a 720p MP4 behind
# forever.
VIEWPORT_CAPTURE_MAX_AGE_DAYS = 7


def find_view3d():
    """Return (window, area, region) for the largest visible 3D viewport.

    Used to pin OpenGL captures to an actual VIEW_3D region. Without this, a
    capture triggered from a timer/panel may run with the active area being the
    sidebar (a UI region with no RegionView3D), and `render.opengl` then
    silently falls back to the scene camera instead of the viewport the user
    sees.
    """
    wm = bpy.context.window_manager
    best = (None, None, None)
    best_size = -1
    for window in wm.windows:
        screen = window.screen
        if screen is None:
            continue
        for area in screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next((r for r in area.regions if r.type == "WINDOW"), None)
            if region is None:
                continue
            size = area.width * area.height
            if size > best_size:
                best_size = size
                best = (window, area, region)
    return best


def request_redraw():
    with contextlib.suppress(Exception):
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                area.tag_redraw()


def view3d_for_region(region):
    """The ``RegionView3D`` that ``region`` draws through, or None.

    A `Region` carries no back-pointer to its area, and the view matrices live
    on the area's space rather than on the region, so the only way across is to
    find the area holding it. Compared by `as_pointer`: the RNA wrappers are
    rebuilt freely and two of them for the same region are not `is`-identical.
    """
    with contextlib.suppress(Exception):
        wanted = region.as_pointer()
        for window in bpy.context.window_manager.windows:
            screen = window.screen
            if screen is None:
                continue
            for area in screen.areas:
                if area.type != "VIEW_3D":
                    continue
                if not any(r.as_pointer() == wanted for r in area.regions):
                    continue
                return getattr(area.spaces.active, "region_3d", None)
    return None


def view3d_rotation():
    """How the largest visible viewport is oriented, or None if there is none.

    For the callers that want to face something at the viewer but are not
    running inside the region — anything delivered by `run_on_main_thread` has
    no `context.region_data` to read.
    """
    _window, area, _region = find_view3d()
    if area is None:
        return None
    rv3d = getattr(area.spaces.active, "region_3d", None)
    return None if rv3d is None else rv3d.view_rotation.copy()


# How far down a ray that grazes the ground plane a hit is still worth having.
# Past this the intersection is arithmetically true and useless: a horizon
# drop would put the object kilometres away, out of every view including the
# one it was dropped into.
_GRAZE_LIMIT = 1.0e4


def pick_in_view3d(region, x, y):
    """Where a region point lands in the scene: ``(location, normal, object)``.

    The object is None when the ray hit nothing, and then the location is the
    ground plane — which is where a viewport with nothing in it has to put
    something, and matches where Blender's own Add menu would. Two ways that
    fails: a view looking along Z=0 or up away from it, and a view grazing it
    so shallowly that the intersection is off in the distance. Both fall back
    to the orbit pivot's depth, which is the distance the view is already
    about.
    """
    from bpy_extras import view3d_utils
    from mathutils import Vector
    from mathutils.geometry import intersect_line_plane

    rv3d = view3d_for_region(region)
    if rv3d is None:
        return None
    coord = (float(x), float(y))
    origin = view3d_utils.region_2d_to_origin_3d(region, rv3d, coord)
    direction = view3d_utils.region_2d_to_vector_3d(region, rv3d, coord)
    if origin is None or direction is None:
        return None

    depsgraph = bpy.context.evaluated_depsgraph_get()
    hit, location, normal, _index, obj, _matrix = bpy.context.scene.ray_cast(
        depsgraph, origin, direction
    )
    if hit:
        # `ray_cast` answers with the evaluated copy; a caller that wants to
        # put a material on it needs the one in `bpy.data`.
        return location.copy(), normal.copy(), (obj.original if obj else None)

    up = Vector((0.0, 0.0, 1.0))
    ground = intersect_line_plane(
        origin, origin + direction, Vector((0.0, 0.0, 0.0)), up
    )
    if ground is not None:
        away = ground - origin
        if away.dot(direction) > 0.0 and away.length <= _GRAZE_LIMIT:
            return ground, up, None
    distance = float(getattr(rv3d, "view_distance", 0.0) or 10.0)
    return origin + direction * distance, up, None


def render_viewport_to_file(max_dim=None, camera=None):
    """Render a viewport or scene camera to a temp PNG (main thread).

    Without ``camera``, always captures the actual VIEW_3D the user sees (so
    realtime/pre-render use the live viewport as their input image), by
    overriding the operator context onto a real 3D viewport region. With a
    camera, captures that camera through Blender's OpenGL render instead.
    ``max_dim`` temporarily scales the render so its longest side is at most
    that many pixels (via resolution_percentage). All settings are restored.
    """
    fd, path = tempfile.mkstemp(suffix=".png", prefix="scene_agent_view_")
    os.close(fd)
    scene = bpy.context.scene
    render = scene.render
    previous_path = render.filepath
    previous_pct = render.resolution_percentage
    previous_camera = scene.camera if camera is not None else None
    render.filepath = path
    if max_dim:
        longest = max(render.resolution_x, render.resolution_y) * (previous_pct / 100.0)
        if longest > max_dim:
            render.resolution_percentage = max(1, int(previous_pct * max_dim / longest))
    try:
        if camera is not None:
            if camera.type != "CAMERA" or camera.name not in scene.objects:
                raise RuntimeError("Choose a camera from the active scene.")
            scene.camera = camera
            bpy.ops.render.opengl(write_still=True, view_context=False)
        else:
            window, area, region = find_view3d()
            if window is not None:
                with bpy.context.temp_override(window=window, area=area, region=region):
                    bpy.ops.render.opengl(write_still=True, view_context=True)
            else:
                # No 3D viewport open (e.g. headless) — fall back to a camera render.
                bpy.ops.render.opengl(write_still=True, view_context=False)
    finally:
        render.filepath = previous_path
        render.resolution_percentage = previous_pct
        if camera is not None:
            scene.camera = previous_camera
    return path


@contextlib.contextmanager
def clean_viewport(space):
    """Take Blender's own UI out of an OpenGL capture, then put it back.

    `render.opengl` draws through the viewport's own pipeline, so whatever the
    user has switched on lands in the pixels: the floor grid, object outlines,
    relationship lines, the corner text, the navigation gizmo. None of that is
    the shot, and a model asked to animate the shot should never be shown it.

    Two flags cover all of it — `show_overlays` is the master switch behind the
    Overlays popover, and `show_gizmo` the one behind the gizmo popover — so
    this does not have to know the two dozen sub-toggles or track new ones.
    A None space is the headless case and a no-op.
    """
    if space is None:
        yield
        return
    overlay = space.overlay
    saved = (overlay.show_overlays, space.show_gizmo)
    overlay.show_overlays = False
    space.show_gizmo = False
    try:
        yield
    finally:
        overlay.show_overlays, space.show_gizmo = saved


_PLAYBLAST_SUFFIXES = frozenset({".mp4", ".mkv", ".mov", ".avi", ".webm"})


def _set_output_format(render, media_type, file_format):
    """Point the render output at one format. Order matters here.

    Blender 4.5 split video out of the image formats, so `file_format`'s enum is
    filtered by `media_type` and FFMPEG simply does not exist as a choice until
    the media type says VIDEO. Setting them the other way round raises.
    """
    render.image_settings.media_type = media_type
    render.image_settings.file_format = file_format


def _playblast_output(directory):
    """The video Blender just wrote into ``directory``.

    Blender names a rendered video after the frame range it covered, so the path
    handed to `render.filepath` is a prefix and not the file that turns up —
    which is why the capture gets a directory of its own.
    """
    found = sorted(
        path
        for path in directory.iterdir()
        if path.is_file() and path.suffix.lower() in _PLAYBLAST_SUFFIXES
    )
    if not found:
        raise RuntimeError(
            "Blender's viewport render wrote no video. Check the system console."
        )
    return found[0]


def render_viewport_animation_to_video(frame_start, frame_end, fps, camera=None):
    """Playblast the viewport (or a camera) to a clean 720p MP4 reference.

    One `render.opengl(animation=True)` — Blender's own Viewport Render
    Animation — rather than a frame-at-a-time loop. Blender muxes the H.264
    itself, which is both what the feature is called everywhere else and several
    times quicker: the loop this replaced paid a PNG write, a decode and a
    re-encode on every single frame.

    Rendered at exactly 1280x720, so there is nothing to letterbox — with
    `view_context=True` the framing follows the *render* aspect, not the shape
    of the region the view happens to be sitting in.
    """
    scene = bpy.context.scene
    render = scene.render
    ffmpeg = render.ffmpeg
    captures = paths.viewport_captures_dir()
    paths.prune(captures, max_age_days=VIEWPORT_CAPTURE_MAX_AGE_DAYS)
    output_dir = captures / uuid.uuid4().hex
    output_dir.mkdir(parents=True, exist_ok=True)
    video_path = output_dir / "viewport_reference.mp4"
    first_frame_path = output_dir / "viewport_first_frame.png"

    window, area, region = find_view3d()
    space = area.spaces.active if area is not None else None
    saved = {
        "frame": scene.frame_current,
        "frame_start": scene.frame_start,
        "frame_end": scene.frame_end,
        "preview": scene.use_preview_range,
        "resolution_x": render.resolution_x,
        "resolution_y": render.resolution_y,
        "percentage": render.resolution_percentage,
        "media_type": render.image_settings.media_type,
        "format": render.image_settings.file_format,
        "filepath": render.filepath,
        "stamp": render.use_stamp,
        "overwrite": render.use_overwrite,
        "camera": scene.camera,
        "ffmpeg": (
            ffmpeg.format,
            ffmpeg.codec,
            ffmpeg.constant_rate_factor,
            ffmpeg.ffmpeg_preset,
            ffmpeg.audio_codec,
        ),
    }
    try:
        render.resolution_x = 1280
        render.resolution_y = 720
        render.resolution_percentage = 100
        # Burnt-in metadata is the one "overlay" that lives on the render rather
        # than on the viewport, so `clean_viewport` cannot reach it.
        render.use_stamp = False
        render.use_overwrite = True
        if camera is not None:
            scene.camera = camera
        with clean_viewport(space):
            # The still goes first and forces PNG: `render_viewport_to_file`
            # writes through `image_settings`, and that is FFMPEG for the rest
            # of this function.
            _set_output_format(render, "IMAGE", "PNG")
            scene.frame_set(int(frame_start))
            still = render_viewport_to_file(max_dim=1280, camera=camera)
            try:
                shutil.copyfile(still, first_frame_path)
            finally:
                with contextlib.suppress(OSError):
                    os.remove(still)

            # `animation=True` renders the scene's range, not arguments, and it
            # honours the preview range — which the caller has already folded
            # into the frames it asked for.
            scene.use_preview_range = False
            scene.frame_start = int(frame_start)
            scene.frame_end = int(frame_end)
            _set_output_format(render, "VIDEO", "FFMPEG")
            ffmpeg.format = "MPEG4"
            ffmpeg.codec = "H264"
            ffmpeg.constant_rate_factor = "HIGH"
            ffmpeg.ffmpeg_preset = "GOOD"
            ffmpeg.audio_codec = "NONE"
            render.filepath = str(output_dir / "playblast")
            if window is not None:
                with bpy.context.temp_override(
                    window=window, area=area, region=region
                ):
                    bpy.ops.render.opengl(animation=True, view_context=True)
            else:
                # No 3D viewport open — the scene camera is the only view there
                # is, and `view_context=True` would silently fall back to it.
                bpy.ops.render.opengl(animation=True, view_context=False)
        _playblast_output(output_dir).rename(video_path)
        return {
            "video_path": str(video_path),
            "first_frame_path": str(first_frame_path),
            "frame_start": int(frame_start),
            "frame_end": int(frame_end),
            "fps": int(fps),
            "duration_seconds": (frame_end - frame_start + 1) / fps,
            "resolution": [1280, 720],
        }
    except Exception:
        shutil.rmtree(output_dir, ignore_errors=True)
        raise
    finally:
        render.resolution_x = saved["resolution_x"]
        render.resolution_y = saved["resolution_y"]
        render.resolution_percentage = saved["percentage"]
        _set_output_format(render, saved["media_type"], saved["format"])
        render.filepath = saved["filepath"]
        render.use_stamp = saved["stamp"]
        render.use_overwrite = saved["overwrite"]
        (
            ffmpeg.format,
            ffmpeg.codec,
            ffmpeg.constant_rate_factor,
            ffmpeg.ffmpeg_preset,
            ffmpeg.audio_codec,
        ) = saved["ffmpeg"]
        scene.use_preview_range = saved["preview"]
        scene.frame_start = saved["frame_start"]
        scene.frame_end = saved["frame_end"]
        scene.camera = saved["camera"]
        scene.frame_set(saved["frame"])
