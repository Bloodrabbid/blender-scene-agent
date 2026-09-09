"""Putting a result into the scene, and getting a picture back out.

Import a GLB, load an image, set the world's environment map, apply a texture
to the active mesh, play a video, decimate a mesh, render a studio thumbnail.
Every one of these is a Blender operation with a result on the other side of
it, and none of them knows where the file came from — which is what lets the
history, the explorer's action table and the job pipeline all call the same
ones.

**Main thread only.** These touch `bpy.data` and `bpy.ops`; reach them from a
`run_on_main_thread` callback, never from a worker.
"""

import contextlib
import logging
import math
import os
import subprocess
import tempfile
from pathlib import Path

import bpy
import mathutils

from ..media import VIDEO_SUFFIXES
from .viewport import find_view3d, request_redraw

logger = logging.getLogger(__name__)


def import_glb(path):
    before = set(bpy.data.objects)
    # TEMPERANCE sizes each bone by the distance to its child joint. The
    # default BLENDER heuristic points every tip down glTF +Y at a fixed
    # length, and generated rigs (Meshy bakes joints in huge units under a
    # 0.01-scaled node) came in as room-sized octahedra swallowing the mesh.
    bpy.ops.import_scene.gltf(filepath=path, bone_heuristic="TEMPERANCE")
    objects = [obj for obj in bpy.data.objects if obj not in before]
    _center_at_origin(objects)
    return objects


def _center_at_origin(objects):
    """Translate `objects` so the model's bounding-box centre sits at the
    world origin (0, 0, 0).

    Generated GLBs keep whatever baked node offsets the provider used, so
    without this a finished generation can appear far from the scene start
    point. Only root objects are moved, keeping hierarchies (e.g. rigged
    meshes parented to an armature) intact.
    """
    import mathutils

    geometry_types = {
        "MESH",
        "CURVE",
        "SURFACE",
        "FONT",
        "META",
        "VOLUME",
        "POINTCLOUD",
        "GREASEPENCIL",
        "GPENCIL",
    }
    corners = []
    for obj in objects:
        if obj.type not in geometry_types:
            continue
        for corner in obj.bound_box:
            corners.append(obj.matrix_world @ mathutils.Vector(corner))
    if not corners:
        return
    center = sum(corners, mathutils.Vector()) / len(corners)
    imported = set(objects)
    for root in (obj for obj in objects if obj.parent not in imported):
        root.location -= center


def render_studio_thumbnail(objects, *, max_dim=512):
    """Render a studio-framed OpenGL thumbnail of `objects` to a temp PNG.

    Uses a fixed 3/4 camera, Material Preview / studio lighting, and a neutral
    viewport background so Results thumbs look consistent — not a raw capture
    of whatever the user's viewport currently shows.
    """
    objects = [o for o in objects if o is not None]
    if not objects:
        return None
    window, area, region = find_view3d()
    if window is None:
        return None

    space = next((s for s in area.spaces if s.type == "VIEW_3D"), None)
    rv3d = getattr(space, "region_3d", None) if space is not None else None
    if space is None or rv3d is None:
        return None

    saved_view = (
        rv3d.view_location.copy(),
        rv3d.view_distance,
        rv3d.view_rotation.copy(),
        rv3d.view_perspective,
    )
    saved_shading = {
        "type": space.shading.type,
        "light": getattr(space.shading, "light", None),
        "color_type": getattr(space.shading, "color_type", None),
        "studio_light": getattr(space.shading, "studio_light", None),
        "studiolight_rotate_z": getattr(space.shading, "studiolight_rotate_z", None),
        "studiolight_intensity": getattr(space.shading, "studiolight_intensity", None),
        "background_type": getattr(space.shading, "background_type", None),
        "background_color": (
            tuple(space.shading.background_color)
            if hasattr(space.shading, "background_color")
            else None
        ),
        "show_xray": getattr(space.shading, "show_xray", None),
        "show_shadows": getattr(space.shading, "show_shadows", None),
        "show_cavity": getattr(space.shading, "show_cavity", None),
    }
    saved_overlay = getattr(space.overlay, "show_overlays", None)
    view_layer = bpy.context.view_layer
    prev_selected = list(bpy.context.selected_objects)
    prev_active = view_layer.objects.active
    hide_state = []

    fd, path = tempfile.mkstemp(suffix=".png", prefix="scene_agent_studio_")
    os.close(fd)
    render = bpy.context.scene.render
    prev_path = render.filepath
    prev_pct = render.resolution_percentage
    image_settings = render.image_settings
    prev_format = image_settings.file_format
    render.filepath = path
    image_settings.file_format = "PNG"
    longest = max(render.resolution_x, render.resolution_y) * (prev_pct / 100.0)
    if longest > max_dim:
        render.resolution_percentage = max(1, int(prev_pct * max_dim / longest))
    try:
        target_ids = {obj.as_pointer() for obj in objects}
        for obj in bpy.data.objects:
            with contextlib.suppress(Exception):
                hide_state.append(
                    (obj, obj.hide_get(), obj.hide_viewport, obj.hide_render)
                )
                is_target = obj.as_pointer() in target_ids or any(
                    p.as_pointer() in target_ids for p in _object_ancestors(obj)
                )
                obj.hide_set(not is_target)
                obj.hide_viewport = not is_target
                obj.select_set(obj.as_pointer() in target_ids)
        view_layer.objects.active = objects[0]

        # Solid + Studio reads much clearer for untextured / dark GLBs than
        # Material Preview on a near-black viewport background.
        space.shading.type = "SOLID"
        with contextlib.suppress(Exception):
            space.shading.light = "STUDIO"
        with contextlib.suppress(Exception):
            space.shading.color_type = "MATERIAL"
        with contextlib.suppress(Exception):
            space.shading.studio_light = "studio.exr"
        with contextlib.suppress(Exception):
            space.shading.studiolight_intensity = 1.15
        with contextlib.suppress(Exception):
            space.shading.studiolight_rotate_z = 0.4
        with contextlib.suppress(Exception):
            space.shading.background_type = "VIEWPORT"
            space.shading.background_color = (0.72, 0.73, 0.75)
        with contextlib.suppress(Exception):
            space.shading.show_xray = False
            space.shading.show_shadows = True
            space.shading.show_cavity = True
            space.shading.cavity_type = "BOTH"
        with contextlib.suppress(Exception):
            space.overlay.show_overlays = False

        _aim_view_at(rv3d, objects, studio=True)
        with bpy.context.temp_override(window=window, area=area, region=region):
            with contextlib.suppress(Exception):
                bpy.ops.view3d.view_selected()
            bpy.ops.render.opengl(write_still=True, view_context=True)
    except Exception:
        logger.exception("Studio thumbnail render failed")
        path = None
    finally:
        render.filepath = prev_path
        render.resolution_percentage = prev_pct
        image_settings.file_format = prev_format
        for obj, hide_get, hide_viewport, hide_render in hide_state:
            with contextlib.suppress(Exception):
                obj.hide_set(hide_get)
                obj.hide_viewport = hide_viewport
                obj.hide_render = hide_render
        for obj in bpy.data.objects:
            with contextlib.suppress(Exception):
                obj.select_set(obj in prev_selected)
        with contextlib.suppress(Exception):
            view_layer.objects.active = prev_active
        with contextlib.suppress(Exception):
            space.shading.type = saved_shading["type"]
            if saved_shading["light"] is not None:
                space.shading.light = saved_shading["light"]
            if saved_shading["color_type"] is not None:
                space.shading.color_type = saved_shading["color_type"]
            if saved_shading["studio_light"] is not None:
                space.shading.studio_light = saved_shading["studio_light"]
            if saved_shading["studiolight_rotate_z"] is not None:
                space.shading.studiolight_rotate_z = saved_shading[
                    "studiolight_rotate_z"
                ]
            if saved_shading["studiolight_intensity"] is not None:
                space.shading.studiolight_intensity = saved_shading[
                    "studiolight_intensity"
                ]
            if saved_shading["background_type"] is not None:
                space.shading.background_type = saved_shading["background_type"]
            if saved_shading["background_color"] is not None:
                space.shading.background_color = saved_shading["background_color"]
            if saved_shading["show_xray"] is not None:
                space.shading.show_xray = saved_shading["show_xray"]
            if saved_shading["show_shadows"] is not None:
                space.shading.show_shadows = saved_shading["show_shadows"]
            if saved_shading["show_cavity"] is not None:
                space.shading.show_cavity = saved_shading["show_cavity"]
            if saved_overlay is not None:
                space.overlay.show_overlays = saved_overlay
        loc, dist, rot, persp = saved_view
        rv3d.view_location = loc
        rv3d.view_distance = dist
        rv3d.view_rotation = rot
        rv3d.view_perspective = persp
    if path and (not os.path.exists(path) or os.path.getsize(path) == 0):
        logger.warning("Studio thumbnail render produced no image")
        return None
    return path


def _object_ancestors(obj):
    parent = getattr(obj, "parent", None)
    while parent is not None:
        yield parent
        parent = getattr(parent, "parent", None)


def _aim_view_at(rv3d, objects, *, studio=False):
    """Point a RegionView3D at the bounding box of `objects` (no operator)."""
    if rv3d is None:
        return
    coords = []
    for obj in objects:
        for corner in obj.bound_box:
            coords.append(obj.matrix_world @ mathutils.Vector(corner))
    if not coords:
        return
    center = sum(coords, mathutils.Vector()) / len(coords)
    radius = max((c - center).length for c in coords) or 1.0
    with contextlib.suppress(Exception):
        rv3d.view_perspective = "PERSP"
        rv3d.view_location = center
        rv3d.view_distance = max(radius * (2.7 if studio else 2.5), 0.1)
        if studio:
            # Fixed three-quarter orbit so every mesh thumb shares the same angle.
            rv3d.view_rotation = mathutils.Euler(
                (math.radians(65.0), 0.0, math.radians(35.0)), "XYZ"
            ).to_quaternion()


def load_image(path, name=None):
    image = bpy.data.images.load(path, check_existing=True)
    if name:
        image.name = name
    if Path(path).suffix.lower() in VIDEO_SUFFIXES:
        image.source = "MOVIE"
    else:
        image.pack()
    return image


def _play_video(path):
    """Play a local movie with Blender's animation player (Render → Play).

    The Image Editor can only scrub movie frames; real playback uses the
    standalone player launched as ``blender -a <file>``. Falls back to the
    OS default app if that cannot start.
    """
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isfile(path):
        raise RuntimeError(f"Video does not exist: {path}")
    if bpy.app.background:
        return False
    blender = getattr(bpy.app, "binary_path", None) or ""
    if blender and os.path.isfile(blender):
        try:
            subprocess.Popen(
                [blender, "-a", path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            return True
        except Exception:
            logger.exception("Blender animation player failed for %s", path)
    with contextlib.suppress(Exception):
        bpy.ops.wm.path_open(filepath=path)
        return True
    raise RuntimeError(f"Could not open video player for {path}")


def set_world_hdri(image_path):
    """Set the world environment texture to an image (equirect HDRI/skybox)."""
    world = bpy.context.scene.world
    if world is None:
        world = bpy.data.worlds.new("Agent World")
        bpy.context.scene.world = world
    world.use_nodes = True
    nodes = world.node_tree.nodes
    links = world.node_tree.links
    nodes.clear()

    output = nodes.new("ShaderNodeOutputWorld")
    background = nodes.new("ShaderNodeBackground")
    env = nodes.new("ShaderNodeTexEnvironment")
    tex_coord = nodes.new("ShaderNodeTexCoord")
    mapping = nodes.new("ShaderNodeMapping")

    env.image = load_image(image_path, name="Agent HDRI")
    links.new(tex_coord.outputs["Generated"], mapping.inputs["Vector"])
    links.new(mapping.outputs["Vector"], env.inputs["Vector"])
    links.new(env.outputs["Color"], background.inputs["Color"])
    links.new(background.outputs["Background"], output.inputs["Surface"])
    return world


def apply_image_as_material(
    obj, image_path, *, name="Agent Material", pbr=False
):
    """Apply an image as a material, optionally deriving a PBR node setup."""
    material = bpy.data.materials.new(name)
    material.use_nodes = True
    nodes = material.node_tree.nodes
    links = material.node_tree.links
    bsdf = nodes.get("Principled BSDF")
    tex = nodes.new("ShaderNodeTexImage")
    tex.image = load_image(image_path, name=name + " Texture")
    tex.label = "Generated Base Color"
    tex.location = (-620, 120)
    if Path(image_path).suffix.lower() in VIDEO_SUFFIXES:
        tex.image_user.use_auto_refresh = True
        tex.image_user.frame_start = bpy.context.scene.frame_start
    if bsdf is not None:
        bsdf.location = (180, 100)
        links.new(tex.outputs["Color"], bsdf.inputs["Base Color"])
        if pbr:
            roughness = nodes.new("ShaderNodeValToRGB")
            roughness.label = "Derived Roughness"
            roughness.location = (-360, -80)
            roughness.color_ramp.elements[0].position = 0.2
            roughness.color_ramp.elements[0].color = (0.82, 0.82, 0.82, 1.0)
            roughness.color_ramp.elements[1].position = 0.8
            roughness.color_ramp.elements[1].color = (0.28, 0.28, 0.28, 1.0)
            links.new(tex.outputs["Color"], roughness.inputs["Fac"])
            links.new(roughness.outputs["Color"], bsdf.inputs["Roughness"])

            bump = nodes.new("ShaderNodeBump")
            bump.label = "Derived Surface Detail"
            bump.location = (-100, -180)
            bump.inputs["Strength"].default_value = 0.22
            bump.inputs["Distance"].default_value = 0.08
            links.new(tex.outputs["Color"], bump.inputs["Height"])
            links.new(bump.outputs["Normal"], bsdf.inputs["Normal"])
            bsdf.inputs["Metallic"].default_value = 0.0

    # Drive the texture's UVs explicitly. Without a UV map the image samples at
    # a single point and looks like a flat color, so fall back to Generated
    # coordinates (a box-ish projection) for un-unwrapped meshes.
    has_uv = getattr(obj.data, "uv_layers", None) and len(obj.data.uv_layers) > 0
    tex_coord = nodes.new("ShaderNodeTexCoord")
    links.new(tex_coord.outputs["UV" if has_uv else "Generated"], tex.inputs["Vector"])

    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)
    return material


def _enable_material_shading(context):
    """Switch 3D viewports to Material Preview so node textures are visible.

    An image wired to Base Color shows nothing in Solid shading, which makes a
    freshly applied texture look like it "did nothing". Bump any Solid viewport
    to Material Preview (leave Rendered alone).
    """
    spaces = []
    space = getattr(context, "space_data", None)
    if space is not None and space.type == "VIEW_3D":
        spaces.append(space)
    else:
        for window in context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D":
                    spaces.extend(s for s in area.spaces if s.type == "VIEW_3D")
    for sp in spaces:
        with contextlib.suppress(Exception):
            if sp.shading.type not in {"MATERIAL", "RENDERED"}:
                sp.shading.type = "MATERIAL"
    request_redraw()


# ---------------------------------------------------------------------------
# Pure-bpy utilities (no API, no cost)
# ---------------------------------------------------------------------------


def decimate_object(obj, ratio=0.5):
    """Add and apply a decimate modifier -> cheap auto-LOD."""
    modifier = obj.modifiers.new(name="Agent LOD", type="DECIMATE")
    modifier.ratio = max(0.01, min(1.0, ratio))
    with bpy.context.temp_override(object=obj, active_object=obj):
        bpy.ops.object.modifier_apply(modifier=modifier.name)
    return obj


def export_selected_to_glb():
    """Export the current selection to a temp .glb and return its path.

    Main thread only (calls bpy.ops). Falls back to the active object if
    nothing is selected so a single right-clicked object still works.
    """
    obj = bpy.context.active_object
    selected = [o for o in bpy.context.selected_objects] or ([obj] if obj else [])
    if not selected:
        raise RuntimeError("Select a 3D object to export.")
    for o in selected:
        o.select_set(True)
    fd, path = tempfile.mkstemp(suffix=".glb", prefix="scene_agent_src_")
    os.close(fd)
    bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)
    return path


def export_object_to_glb(obj):
    """Export exactly one object while preserving the user's selection."""
    view_layer = bpy.context.view_layer
    previous_active = view_layer.objects.active
    previous_selected = list(bpy.context.selected_objects)
    fd, path = tempfile.mkstemp(suffix=".glb", prefix="scene_agent_src_")
    os.close(fd)
    try:
        bpy.ops.object.select_all(action="DESELECT")
        obj.select_set(True)
        view_layer.objects.active = obj
        bpy.ops.export_scene.gltf(filepath=path, export_format="GLB", use_selection=True)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(path)
        raise
    finally:
        bpy.ops.object.select_all(action="DESELECT")
        for selected in previous_selected:
            if selected.name in bpy.data.objects:
                selected.select_set(True)
        if previous_active is not None and previous_active.name in bpy.data.objects:
            view_layer.objects.active = previous_active
    return path


def apply_retextured_mesh(target, imported_objects):
    """Apply an imported retexture result's UVs and materials to ``target``."""
    source = max(
        (obj for obj in imported_objects if obj.type == "MESH"),
        key=lambda obj: len(obj.data.polygons),
        default=None,
    )
    if source is None:
        raise RuntimeError("Retexture result does not contain a mesh.")

    target_mesh = target.data
    source_mesh = source.data
    if len(target_mesh.loops) == len(source_mesh.loops):
        target_mesh.uv_layers.clear()
        for source_layer in source_mesh.uv_layers:
            target_layer = target_mesh.uv_layers.new(name=source_layer.name)
            for source_uv, target_uv in zip(
                source_layer.data, target_layer.data, strict=True
            ):
                target_uv.uv = source_uv.uv
    if len(target_mesh.polygons) == len(source_mesh.polygons):
        for source_polygon, target_polygon in zip(
            source_mesh.polygons, target_mesh.polygons, strict=True
        ):
            target_polygon.material_index = source_polygon.material_index

    target_mesh.materials.clear()
    for material in source_mesh.materials:
        target_mesh.materials.append(material)
    _enable_material_shading(bpy.context)
    return source


def _is_mesh_active(context):
    obj = context.active_object
    return obj is not None and obj.type == "MESH"
