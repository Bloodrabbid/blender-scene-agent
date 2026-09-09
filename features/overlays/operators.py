"""The buttons that belong to the viewport surfaces, not to the sidebar.

Three of them are the surfaces' own way in and out — resetting a surface a
breaker pulled, toggling the explorer, toggling the composer — and three are
file pickers the surfaces cannot draw themselves: a Skia overlay has no file
browser, so attaching a reference or a chat image hands off to Blender's.
"""

import contextlib
import os

import bpy

from ... import media
from ...blender.viewport import request_redraw
from ...hfui import composer as composer_module
from ...props import props as _props
from ...work import add_report
from .mount import (
    _hide_bar,
    _install_composer_overlay,
    _show_bar,
    _show_generate_3d_overlay,
    bar_state,
    chat_surface_module,
    composer_breaker,
    composer_surface_module,
    generate_3d_overlay_state,
    modal_breaker,
)
from .binding import (
    _COMPOSER_IMAGE_SUFFIXES,
    _COMPOSER_MEDIA_HINT,
    _COMPOSER_MEDIA_SUFFIXES,
    _composer_binding,
    _composer_add_image_paths,
    _composer_media_kind,
)


# Where an OS file drag is hovering, or None. The FileHandler writes this from
# ``poll_drop`` so the prompt can light the same way an explorer tile does;
# ``execute`` reads it to route chat vs composer. ``refuse`` means the pointer
# is on a prompt that cannot take a file (the phone-camera card).
_os_drop = None


def os_drop_surface():
    """Prompt a live OS drag would attach to, or ``None``."""
    if _os_drop in {"chat", "scene", "composer"}:
        return _os_drop
    return None


def _remember_os_drop(dest):
    global _os_drop
    if _os_drop == dest:
        return
    _os_drop = dest
    request_redraw()


def _drop_region_xy(context):
    """Region-local pointer during an OS file drag, or ``(None, None)``."""
    window = getattr(context, "window", None)
    mx = getattr(window, "mouse_x", None) if window is not None else None
    my = getattr(window, "mouse_y", None) if window is not None else None
    if mx is not None and my is not None:
        from .modal import _bar_view3d_region_xy

        region, local_x, local_y = _bar_view3d_region_xy(window, mx, my)
        if region is not None:
            return local_x, local_y
    last = bar_state.get("last_event_xy")
    if last is None or len(last) < 5:
        return None, None
    return last[3], last[4]


def _os_prompt_dest(context):
    """Where dropping OS files at the pointer would attach, or ``None``.

    ``refuse`` is a prompt that must swallow the drop so it does not land in
    the scene behind the card, but cannot stage the file.
    """
    mx, my = _drop_region_xy(context)
    if mx is None:
        return None
    chat = chat_surface_module()
    if chat.pointer_over(mx, my) and chat.state().open:
        return "chat"
    if not generate_3d_overlay_state.get("visible"):
        return None
    composer = composer_surface_module()
    if not composer.pointer_over(mx, my):
        return None
    current = composer.state()
    if current.camera is not None:
        return "refuse"
    if current.mode == composer_module.SCENE_BUILDER:
        return "scene"
    return "composer"


class SCENEAGENT_OT_reset_overlay(bpy.types.Operator):
    bl_idname = "scene_agent.reset_overlay"
    bl_label = "Re-enable Overlay"
    bl_description = (
        "Bring back the viewport composer after it was disabled for "
        "misbehaving. Report the cause if it keeps happening"
    )

    @classmethod
    def poll(cls, context):
        return composer_breaker.tripped or modal_breaker.tripped

    def execute(self, context):
        composer_breaker.reset()
        modal_breaker.reset()
        with contextlib.suppress(Exception):
            composer_surface_module().release()
        _install_composer_overlay()
        _show_generate_3d_overlay()
        request_redraw()
        return {"FINISHED"}


class SCENEAGENT_OT_toggle_bar(bpy.types.Operator):
    bl_idname = "scene_agent.toggle_bar"
    bl_label = "Toggle Generation Bar"
    bl_description = "Open/close the floating generation browser above the 3D viewport"

    def execute(self, context):
        if bar_state["visible"]:
            _hide_bar()
            return {"FINISHED"}
        _show_bar()
        return {"FINISHED"}


class SCENEAGENT_OT_toggle_generate_3d_overlay(bpy.types.Operator):
    bl_idname = "scene_agent.toggle_generate_3d_overlay"
    bl_label = "Toggle Composer"
    bl_description = "Expand or collapse the viewport composer"

    def execute(self, context):
        from . import composer_surface

        if not generate_3d_overlay_state["visible"]:
            _show_generate_3d_overlay()
        state = composer_surface.state()
        state.pinned = not state.pinned
        request_redraw()
        return {"FINISHED"}


class SCENEAGENT_OT_add_composer_images(bpy.types.Operator):
    bl_idname = "scene_agent.add_composer_images"
    bl_label = "Add References"
    bl_description = "Attach reference media to the model the composer has selected"

    # Empty means "whatever mode the composer is on", which is what a drop
    # onto the viewport should do.
    mode: bpy.props.StringProperty(default="", options={"SKIP_SAVE", "HIDDEN"})
    filepath: bpy.props.StringProperty(
        subtype="FILE_PATH", options={"SKIP_SAVE", "HIDDEN"}
    )
    directory: bpy.props.StringProperty(
        subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"}
    )
    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={"SKIP_SAVE", "HIDDEN"},
    )
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff;*.exr;*.hdr",
        options={"HIDDEN"},
    )

    def invoke(self, context, event):
        # Offer every media kind the selected model accepts.
        surface = composer_surface_module()
        scene_builder = self.mode == composer_module.SCENE_BUILDER or (
            not self.mode
            and surface.state().mode == composer_module.SCENE_BUILDER
        )
        retexture = self.mode == composer_module.RETEXTURE or (
            not self.mode and surface.state().mode == composer_module.RETEXTURE
        )
        if retexture:
            suffixes = _COMPOSER_IMAGE_SUFFIXES
        elif scene_builder:
            suffixes = surface.scene_builder_image_suffixes()
        else:
            binding = _composer_binding(_props(context), self.mode or None)
            suffixes = set().union(
                *(
                    _COMPOSER_MEDIA_SUFFIXES[_composer_media_kind(item)]
                    for item in binding.media_items()
                )
            )
        self.filter_glob = ";".join("*" + suffix for suffix in sorted(suffixes))
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        paths = [
            os.path.join(self.directory, file.name) for file in self.files if file.name
        ]
        if not paths and self.filepath:
            paths = [self.filepath]
        dest = _os_drop
        _remember_os_drop(None)
        try:
            if dest == "refuse":
                raise RuntimeError("Can't attach here.")
            if dest == "chat":
                added = chat_surface_module().attach(paths)
            elif dest in {"scene", "composer"}:
                added = composer_surface_module().attach_drop(paths, dest)
            else:
                surface = composer_surface_module()
                scene_builder = self.mode == composer_module.SCENE_BUILDER or (
                    not self.mode
                    and surface.state().mode == composer_module.SCENE_BUILDER
                )
                retexture = self.mode == composer_module.RETEXTURE or (
                    not self.mode
                    and surface.state().mode == composer_module.RETEXTURE
                )
                if retexture:
                    added = surface.attach_retexture(paths)
                elif scene_builder:
                    added = surface.attach_scene_builder(paths)
                else:
                    binding = _composer_binding(_props(context), self.mode or None)
                    added = _composer_add_image_paths(binding, paths)
        except Exception as error:
            self.report({"ERROR"}, str(error))
            add_report(str(error), type="ERROR")
            return {"CANCELLED"}
        request_redraw()
        message = f"Added {added} reference{'s' if added != 1 else ''}."
        self.report({"INFO"}, message)
        add_report(message, type="INFO", timeout=3.0)
        return {"FINISHED"}


class SCENEAGENT_FH_composer_images(bpy.types.FileHandler):
    bl_idname = "SCENEAGENT_FH_composer_images"
    bl_label = "Drop References into the Composer"
    bl_import_operator = SCENEAGENT_OT_add_composer_images.bl_idname
    bl_file_extensions = ";".join(
        sorted(
            _COMPOSER_IMAGE_SUFFIXES
            | media.VIDEO_SUFFIXES
            | media.AUDIO_SUFFIXES
            | media.FILE_SUFFIXES
        )
    )

    @classmethod
    def poll_drop(cls, context):
        if not (context.area and context.area.type == "VIEW_3D"):
            _remember_os_drop(None)
            return False
        dest = _os_prompt_dest(context)
        _remember_os_drop(dest)
        return dest is not None


class SCENEAGENT_OT_chat_attach(bpy.types.Operator):
    bl_idname = "scene_agent.chat_attach"
    bl_label = "Attach Image"
    bl_description = "Attach an image to the Supercomputer conversation"

    filepath: bpy.props.StringProperty(
        subtype="FILE_PATH", options={"SKIP_SAVE", "HIDDEN"}
    )
    directory: bpy.props.StringProperty(
        subtype="DIR_PATH", options={"SKIP_SAVE", "HIDDEN"}
    )
    files: bpy.props.CollectionProperty(
        type=bpy.types.OperatorFileListElement,
        options={"SKIP_SAVE", "HIDDEN"},
    )
    filter_glob: bpy.props.StringProperty(
        default="*.png;*.jpg;*.jpeg;*.webp;*.bmp;*.tif;*.tiff",
        options={"HIDDEN"},
    )

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        paths = [
            os.path.join(self.directory, file.name) for file in self.files if file.name
        ]
        if not paths and self.filepath:
            paths = [self.filepath]
        added = chat_surface_module().attach(paths)
        request_redraw()
        self.report({"INFO"}, f"Attached {added} image{'s' if added != 1 else ''}.")
        return {"FINISHED"}
