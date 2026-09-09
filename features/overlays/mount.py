"""Hanging the Skia surfaces off a Blender viewport.

The composer, the launcher and the assistant are drawn by modules that never
import bpy. This is the other half: the draw handlers, the keep-alive timers,
the breakers that pull a surface that runs away with the main thread, and the
show/hide paths.

There used to be a fourth surface here — the assets browser that slid up over
the viewport — and it was a view of the account's generation history, so it
went with the account.

**The same three functions, three times, on purpose.** `_draw_*` /
`_install_*` / `_remove_*` repeat per surface, and they are together here so a
drift between them is visible. They differ in ways that matter: the chat is
re-added whenever the composer installs its own handler (`_raise_chat_overlay`),
because Blender draws handlers in registration order and the panel has to sit
over the composer; the launcher's install also starts the modal.

**`viewport_ui_enabled()` gates the entry points, not the handlers.** Gating
the handlers alone is useless — the launcher's 5 s timer and the composer's 2 s
keep-alive would put them straight back. `_apply_viewport_ui_preference` runs
the teardown in both directions and releases the layers, because hiding a
surface otherwise leaves its Skia surface, float32 scratch and GPU texture
allocated, 8.4 MB for the composer alone.

Input lives next door in `modal.py`, which imports this module and is not
imported back: `_ensure_bar_modal` reaches the operator through `bpy.ops`, a
string, so the dependency only runs one way.
"""

import contextlib
import logging
import os
import time
from pathlib import Path

import bpy

from ... import analytics, perf, safety
from ...blender.scene import _is_mesh_active
from ...blender.viewport import request_redraw
from ...props import addon_preferences
from ...work import add_report, run_async, tasks
from . import addon, redraw_viewports
from .binding import _composer_binding, _composer_prepare, forget_decodes

logger = logging.getLogger(__name__)


_BAR_ALERT_STATUSES = frozenset({"failed", "canceled"})


_generation_launcher_handler = None
_scene_change_owner = object()


generation_launcher_state = {
    "rect": (0, 0, 0, 0),
    "pressed": False,
    "ready_at": 0.0,
}


_generate_3d_overlay_handler = None


generate_3d_overlay_state = {
    "visible": False,
    "compact": False,
    "rect": (0, 0, 0, 0),
    "controls": [],
    "hover_control": None,
    "control_pressed": None,
    "control_press_xy": None,
    "prompt_active": False,
}


_chat_handler = None


# The assistant bubble in the bottom-right corner. Same shape as the composer's
# state: the rect the modal hit-tests, the controls the panel published, and
# whether its prompt has focus. Everything else — the conversation itself, the
# scroll offsets, the caret — belongs to the surface.
chat_state = {
    "rect": (0, 0, 0, 0),
    "controls": [],
    "prompt_active": False,
}


bar_state = {
    "visible": False,
    "handler": None,
    "scroll": 0,
    "kind": "image",
    "page": 0,
    "expanded": True,
    "dock_rows": 2,
    "rects": [],
    "controls": [],
    "band": (0, 0),
    "band_x": (0, 0),
    "control_pressed": False,
    "hover_rec": None,
    "hover_control": None,
    "selected_rec": None,
    "last_click_rec": None,
    "last_click_at": 0.0,
    "resizing": False,
    "resize_start_y": 0,
    "resize_start_rows": 1,
    "modal_running": False,
    "modal_heartbeat": 0.0,
    "event_scale": None,
    "last_event_xy": None,
    "avail": 0,
    "page_size": 12,
    "pan_accum": 0.0,
}


def _bar_ui_scale():
    """Blender UI scale for overlay widgets (same basis as BlenderKit: dpi/96).

    Do not multiply ``preferences.system.ui_scale`` on top — on retina that
    double-counts ``pixel_size`` and blows the strip up to ~2× tool-icon size.
    """
    with contextlib.suppress(Exception):
        dpi = float(bpy.context.preferences.system.dpi)
        if dpi > 0:
            return max(1.0, dpi / 96.0)
    with contextlib.suppress(Exception):
        return max(1.0, float(bpy.context.preferences.system.ui_scale))
    return 1.0


def _bar_region_margins(area):
    """Offsets so the bar clears overlapping HEADER / TOOLS / UI regions."""
    top_off = left_off = right_off = 0
    if area is None:
        return top_off, left_off, right_off
    use_overlap = False
    with contextlib.suppress(Exception):
        use_overlap = bool(bpy.context.preferences.system.use_region_overlap)
    if not use_overlap:
        return top_off, left_off, right_off
    for reg in area.regions:
        if reg.type in {"HEADER", "TOOL_HEADER"}:
            top_off += reg.height
        elif reg.type == "TOOLS":
            left_off = max(left_off, reg.width)
        elif reg.type == "UI":
            right_off = max(right_off, reg.width)
    return top_off, left_off, right_off


def _bar_modal_watchdog():
    """Keep the shared launcher/gallery modal alive while either UI exists."""
    if (
        not bar_state["visible"]
        and not generate_3d_overlay_state["visible"]
        and _generation_launcher_handler is None
    ):
        return None
    if bar_state.get("modal_running"):
        heartbeat = float(bar_state.get("modal_heartbeat") or 0.0)
        if time.monotonic() - heartbeat < 2.0:
            return 0.5
        bar_state["modal_running"] = False
    _invoke_bar_modal_in_view3d()
    return 0.5


def _ensure_bar_modal_watchdog():
    if not bpy.app.timers.is_registered(_bar_modal_watchdog):
        bpy.app.timers.register(
            _bar_modal_watchdog,
            first_interval=0.2,
            persistent=True,
        )


def _stop_bar_modal_watchdog():
    if bpy.app.timers.is_registered(_bar_modal_watchdog):
        with contextlib.suppress(Exception):
            bpy.app.timers.unregister(_bar_modal_watchdog)


def _ensure_bar_modal():
    if (
        not bar_state["visible"]
        and not generate_3d_overlay_state["visible"]
        and _generation_launcher_handler is None
    ) or bar_state.get("modal_running"):
        return
    _invoke_bar_modal_in_view3d()


def _invoke_bar_modal_in_view3d():
    """Start the modal in the same region coordinate space as POST_PIXEL."""
    for window in getattr(bpy.context.window_manager, "windows", ()):
        for area in window.screen.areas:
            if area.type != "VIEW_3D":
                continue
            region = next(
                (item for item in area.regions if item.type == "WINDOW"),
                None,
            )
            if region is None:
                continue
            with contextlib.suppress(Exception):
                with bpy.context.temp_override(
                    window=window,
                    screen=window.screen,
                    area=area,
                    region=region,
                ):
                    bpy.ops.scene_agent.bar_modal("INVOKE_DEFAULT")
                    return True
    return False


# Task labels are written for a list of jobs, so they name the action as a
# command: "Generate 3D · Meshy 6". The island is reporting something already
# under way, so the verb goes to its participle. Only the leading word is
# looked up, which covers every label the add-on submits; anything unmatched is
# left exactly as its task named it.
_LAUNCHER_GERUNDS = {
    "generate": "Generating",
    "remesh": "Remeshing",
    "import": "Importing",
    "apply": "Applying",
    "update": "Updating",
    "rig": "Rigging",
    "play": "Opening",
}


# Two labels that are nouns rather than commands and have no participle to
# take. Keyed on the whole head, not the first word.
_LAUNCHER_PHRASES = {
    "viewport vision": "Analyzing viewport",
    "results": "Loading results",
}


def _launcher_progressive(label):
    """ "Generate 3D" -> "Generating 3D". The model name is already gone."""
    head = str(label or "").split("·")[0].strip()
    if not head:
        return "Working"
    phrase = _LAUNCHER_PHRASES.get(head.lower())
    if phrase:
        return phrase
    first, _, rest = head.partition(" ")
    verb = _LAUNCHER_GERUNDS.get(first.lower())
    if not verb:
        return head
    return f"{verb} {rest}".strip()


def _generation_launcher_status():
    """Compact live status for the viewport launcher: ``(label, tone)``.

    A tone rather than a colour — ``running`` for work this session is doing,
    ``pending`` for jobs the backend still owes us. What those look like is the
    design system's business, not this function's.

    The label is written for a reader, which means it is built from the task's
    *label* and never from its ``message``: messages are progress strings a
    worker passes to its callback ("queued", "in progress", "starting") and they
    read like log output in a widget this size.

    It carries no percentage. The figure came from the worker's own callbacks,
    which report in coarse jumps and stall for most of a job at whatever they
    last said — so a number that looks precise was telling the user something
    the add-on does not actually know. That a job is running is the whole of
    what the island can honestly say.
    """
    running = [task for task in list(tasks.values()) if task.get("status") == "running"]
    if running:
        if len(running) > 1:
            return f"{len(running)} jobs running", "running"
        return _launcher_progressive(running[0].get("label")), "running"
    return None, None


def _generation_launcher_recents(limit=4):
    """Tiles under the launcher capsule. There is nothing to put in them.

    These were the last few generations, thumbnails and all. The capsule now
    shows only its mark and whatever the status line has to say.
    """
    return ()


def launcher_surface_module():
    from . import launcher_surface

    return launcher_surface


def viewport_ui_enabled():
    """Whether the Skia surfaces may install themselves at all.

    Defaults to on. The lookup fails during ``register()`` and under
    ``--factory-startup``, and a preference we cannot read is not a request to
    turn the product off.
    """
    prefs = addon_preferences()
    return True if prefs is None else bool(prefs.viewport_ui)


def _apply_viewport_ui_preference():
    """Bring every surface into line with the preference, both directions.

    Gating the installers is not enough on its own: the launcher and the
    composer each keep a timer that reinstalls them, so switching off has to
    take those down too, and switching back on has to put them back rather
    than wait for a timer that is no longer registered.
    """
    enabled = viewport_ui_enabled()
    analytics.track(
        analytics.AnalyticsEvent.SettingsChanged,
        {"setting": "viewport_ui", "value": enabled, "surface": "preferences"},
    )
    if enabled:
        _install_generation_launcher()
        _install_composer_overlay()
        _show_generate_3d_overlay()
    else:
        _remove_generation_launcher()
        _remove_composer_overlay()
        _remove_chat_overlay()
        _hide_generate_3d_overlay()
        _hide_bar()
        # Hiding a surface only stops it drawing; the layer behind it still
        # holds a Skia surface, a float32 scratch buffer and a GPU texture —
        # 8.4 MB for the composer alone. Somebody switching this off for
        # performance should get that back.
        with contextlib.suppress(Exception):
            composer_surface_module().release()
        # Whatever it cost while it was up is the most interesting thing about
        # a user who has just chosen to turn it off.
        with contextlib.suppress(Exception):
            perf.flush("viewport ui disabled", final=True)
    request_redraw()


def _viewport_ui_pref_update(self, context):
    with contextlib.suppress(Exception):
        _apply_viewport_ui_preference()


def _on_launcher_breaker_trip(reason):
    """Pull the badge rather than let a bad draw hold the main thread."""
    _remove_generation_launcher()
    perf.surface_disabled("launcher", reason, launcher_breaker)
    add_report(f"Launcher disabled ({reason}).", type="ERROR")


launcher_breaker = safety.Breaker("launcher badge", on_trip=_on_launcher_breaker_trip)


_launcher_draw_error = None


def _draw_generation_launcher():
    """POST_PIXEL handler: hand the badge to the skui renderer."""
    global _launcher_draw_error

    with perf.frame("launcher"):
        error = safety.call(launcher_breaker, launcher_surface_module().draw, addon())
        if error is not None:
            perf.fail()
    if error is None:
        _launcher_draw_error = None
        return
    # Same rule as the composer: report one signature once and stop pumping,
    # because logging a traceback at the redraw rate is itself enough work to
    # lock the window up.
    signature = (type(error).__name__, str(error))
    if signature != _launcher_draw_error:
        _launcher_draw_error = signature
        logger.error(
            "Launcher draw failed",
            exc_info=(type(error), error, error.__traceback__),
        )
    generation_launcher_state["rect"] = (0, 0, 0, 0)
    with contextlib.suppress(Exception):
        launcher_surface_module().park()


def _ensure_generation_launcher_timer():
    """Restore launcher draw state if Blender drops it during UI/file reloads."""
    if bpy.app.background:
        return None
    if _generation_launcher_handler is None:
        _install_generation_launcher()
    else:
        _ensure_bar_modal_watchdog()
        _ensure_bar_modal()
    return 5.0


def _install_generation_launcher():
    global _generation_launcher_handler
    if bpy.app.background or not viewport_ui_enabled():
        return
    if not bpy.app.timers.is_registered(_ensure_generation_launcher_timer):
        bpy.app.timers.register(
            _ensure_generation_launcher_timer,
            first_interval=1.0,
            persistent=True,
        )
    if _generation_launcher_handler is None:
        _generation_launcher_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_generation_launcher, (), "WINDOW", "POST_PIXEL"
        )
        # Ignore the app-activation mouse press that macOS can deliver while
        # Blender is opening; only intentional clicks after startup may open.
        generation_launcher_state["ready_at"] = time.monotonic() + 1.5
    _ensure_bar_modal_watchdog()
    _ensure_bar_modal()
    request_redraw()


def _remove_generation_launcher():
    global _generation_launcher_handler
    if bpy.app.timers.is_registered(_ensure_generation_launcher_timer):
        with contextlib.suppress(Exception):
            bpy.app.timers.unregister(_ensure_generation_launcher_timer)
    if _generation_launcher_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(
            _generation_launcher_handler, "WINDOW"
        )
        _generation_launcher_handler = None
    with contextlib.suppress(Exception):
        launcher_surface_module().release()
    generation_launcher_state["rect"] = (0, 0, 0, 0)
    generation_launcher_state["pressed"] = False
    generation_launcher_state["ready_at"] = 0.0


def _on_chat_breaker_trip(reason):
    """Pull the assistant rather than let a bad draw hold the main thread."""
    _remove_chat_overlay()
    perf.surface_disabled("chat", reason, chat_breaker)
    add_report(f"Assistant disabled ({reason}).", type="ERROR")


chat_breaker = safety.Breaker("assistant", on_trip=_on_chat_breaker_trip)


_chat_draw_error = None


def chat_surface_module():
    from . import chat_surface

    return chat_surface


def _draw_chat():
    """POST_PIXEL handler: hand the assistant to the skui renderer."""
    global _chat_draw_error

    with perf.frame("chat"):
        error = safety.call(chat_breaker, chat_surface_module().draw, addon())
        if error is not None:
            perf.fail()
    if error is None:
        _chat_draw_error = None
        return
    signature = (type(error).__name__, str(error))
    if signature != _chat_draw_error:
        _chat_draw_error = signature
        logger.error(
            "Assistant draw failed",
            exc_info=(type(error), error, error.__traceback__),
        )
    chat_state["rect"] = (0, 0, 0, 0)
    chat_state["controls"] = []
    with contextlib.suppress(Exception):
        chat_surface_module().park()


def _ensure_chat_timer():
    """Restore the assistant if Blender drops it during UI / file reloads."""
    if bpy.app.background:
        return None
    if _chat_handler is None:
        _install_chat_overlay()
    return 5.0


def _install_chat_overlay():
    global _chat_handler
    if bpy.app.background or chat_breaker.tripped or not viewport_ui_enabled():
        return
    if not bpy.app.timers.is_registered(_ensure_chat_timer):
        bpy.app.timers.register(_ensure_chat_timer, first_interval=1.0, persistent=True)
    if _chat_handler is None:
        _chat_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_chat, (), "WINDOW", "POST_PIXEL"
        )
    _ensure_bar_modal_watchdog()
    _ensure_bar_modal()
    request_redraw()


def _raise_chat_overlay():
    """Put the assistant back on top of the handler stack.

    Draw handlers run in the order they were added, and the composer's is
    added late — on the authenticated transition, and again by its own restore
    timer — so an open chat panel would end up *under* a composer card it
    overlaps on a narrow viewport. Re-adding is the only way to reorder.
    """
    global _chat_handler
    if _chat_handler is None:
        return
    with contextlib.suppress(Exception):
        bpy.types.SpaceView3D.draw_handler_remove(_chat_handler, "WINDOW")
    _chat_handler = bpy.types.SpaceView3D.draw_handler_add(
        _draw_chat, (), "WINDOW", "POST_PIXEL"
    )


def _remove_chat_overlay():
    global _chat_handler
    if bpy.app.timers.is_registered(_ensure_chat_timer):
        with contextlib.suppress(Exception):
            bpy.app.timers.unregister(_ensure_chat_timer)
    if _chat_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(_chat_handler, "WINDOW")
        _chat_handler = None
    with contextlib.suppress(Exception):
        chat_surface_module().release_resources()
    chat_state["rect"] = (0, 0, 0, 0)
    chat_state["controls"] = []
    chat_state["prompt_active"] = False


_overlay_draw_error = None


def _on_composer_breaker_trip(reason):
    """Take the composer down rather than let it hold the main thread."""
    _hide_generate_3d_overlay()
    _remove_composer_overlay()
    perf.surface_disabled(
        "composer" if composer_breaker.tripped else "input",
        reason,
        composer_breaker if composer_breaker.tripped else modal_breaker,
    )
    add_report(
        f"Viewport overlay disabled ({reason}). "
        "The N-panel still works; Re-enable Overlay to try again.",
        type="ERROR",
    )


composer_breaker = safety.Breaker("composer overlay", on_trip=_on_composer_breaker_trip)


# Input gets more rope than drawing: one bad event (a weird device, a missing
# region) should not cost the user their overlay, but a modal that raises on
# everything is worse than no modal at all.
modal_breaker = safety.Breaker(
    "overlay input", failures=12, on_trip=_on_composer_breaker_trip
)


def _draw_generate_3d_overlay():
    """POST_PIXEL handler: hand the composer to the skui renderer."""
    global _overlay_draw_error

    with perf.frame("composer"):
        error = safety.call(composer_breaker, composer_surface_module().draw, addon())
        if error is not None:
            perf.fail()
    if error is None:
        _overlay_draw_error = None
        return
    # A broken draw repeats at the redraw pump's rate, and logging a traceback
    # per frame is itself enough main-thread work to lock the UI up. Report the
    # same failure once, park the pump, and let the breaker pull the handler if
    # it keeps happening.
    signature = (type(error).__name__, str(error))
    if signature != _overlay_draw_error:
        _overlay_draw_error = signature
        logger.error(
            "Overlay draw failed",
            exc_info=(type(error), error, error.__traceback__),
        )
    with contextlib.suppress(Exception):
        composer_surface_module().park()


def composer_surface_module():
    from . import composer_surface

    return composer_surface


def _scene_context_changed():
    """A Blender window selected another scene; let Scene Builder follow it."""
    with contextlib.suppress(Exception):
        composer_surface_module().scene_context_changed()


def _install_scene_change_watch():
    if bpy.app.background:
        return
    bpy.msgbus.clear_by_owner(_scene_change_owner)
    bpy.msgbus.subscribe_rna(
        key=(bpy.types.Window, "scene"),
        owner=_scene_change_owner,
        args=(),
        notify=_scene_context_changed,
        options={"PERSISTENT"},
    )


def _remove_scene_change_watch():
    bpy.msgbus.clear_by_owner(_scene_change_owner)


def _ensure_composer_overlay_timer():
    """Keep the composer on screen.

    It is the add-on's primary surface, so it is not something the user opens:
    it lives in the viewport whenever the results bar is not covering the same
    space. The timer also restores it after file loads, which drop draw
    handlers.
    """
    if bpy.app.background:
        return None
    # The message bus is immediate; this is the recovery path for a file load
    # or a startup ordering where the first notification came too early.
    _scene_context_changed()
    if (
        not bar_state["visible"]
        and (
            _generate_3d_overlay_handler is None
            or not generate_3d_overlay_state["visible"]
        )
    ):
        with contextlib.suppress(Exception):
            _show_generate_3d_overlay()
    return 2.0


def _install_composer_overlay():
    if bpy.app.background or composer_breaker.tripped or not viewport_ui_enabled():
        return
    if not bpy.app.timers.is_registered(_ensure_composer_overlay_timer):
        bpy.app.timers.register(
            _ensure_composer_overlay_timer, first_interval=1.0, persistent=True
        )


def _remove_composer_overlay():
    if bpy.app.timers.is_registered(_ensure_composer_overlay_timer):
        with contextlib.suppress(Exception):
            bpy.app.timers.unregister(_ensure_composer_overlay_timer)


def _show_generate_3d_overlay():
    global _generate_3d_overlay_handler
    if composer_breaker.tripped or not viewport_ui_enabled():
        return
    _hide_bar()
    if _generate_3d_overlay_handler is None:
        _generate_3d_overlay_handler = bpy.types.SpaceView3D.draw_handler_add(
            _draw_generate_3d_overlay, (), "WINDOW", "POST_PIXEL"
        )
    generate_3d_overlay_state["visible"] = True
    _raise_chat_overlay()
    with contextlib.suppress(Exception):
        _composer_prepare(_composer_binding())
    _ensure_bar_modal_watchdog()
    _ensure_bar_modal()
    request_redraw()


def _hide_generate_3d_overlay():
    global _generate_3d_overlay_handler
    if _generate_3d_overlay_handler is not None:
        bpy.types.SpaceView3D.draw_handler_remove(
            _generate_3d_overlay_handler, "WINDOW"
        )
        _generate_3d_overlay_handler = None
    generate_3d_overlay_state.update(
        {
            "visible": False,
            "compact": False,
            "rect": (0, 0, 0, 0),
            "controls": [],
            "hover_control": None,
            "control_pressed": None,
            "control_press_xy": None,
            "prompt_active": False,
        }
    )
    if _generation_launcher_handler is None and not bar_state["visible"]:
        bar_state["modal_running"] = False
        _stop_bar_modal_watchdog()
    else:
        _ensure_bar_modal_watchdog()
        _ensure_bar_modal()
    request_redraw()


def _show_bar():
    """Bring the prompt forward.

    The "bar" was the assets browser that slid up over the viewport, and it
    was a view of the account's generation history, so it is gone. The name
    and its two entry points stayed — the toggle operator and the launcher
    both call them — and what they mean now is simply "open the composer".
    """
    if not viewport_ui_enabled():
        return
    _show_generate_3d_overlay()


def _hide_bar():
    bar_state["visible"] = False
    bar_state["resizing"] = False
    bar_state["control_pressed"] = False
    bar_state["hover_rec"] = None
    bar_state["hover_control"] = None
    if _generation_launcher_handler is None:
        bar_state["modal_running"] = False
        _stop_bar_modal_watchdog()
    else:
        _ensure_bar_modal_watchdog()
        _ensure_bar_modal()
    request_redraw()


def start_surfaces():
    """Bring the viewport surfaces up from scratch, on register and hot reload.

    Nothing is restored: a reload lands on the launcher alone, with the
    composer freshly allocated. The handlers are torn down first
    because a reload runs against the previous load's, which point at functions
    in modules that have been purged — calling one is a crash, not an error.
    """
    global _generate_3d_overlay_handler, _chat_handler

    if bar_state.get("handler") is not None:
        with contextlib.suppress(Exception):
            bpy.types.SpaceView3D.draw_handler_remove(bar_state["handler"], "WINDOW")
    bar_state.update(
        {
            "visible": False,
            "handler": None,
            "resizing": False,
            "control_pressed": False,
            "hover_rec": None,
            "hover_control": None,
        }
    )
    if _generate_3d_overlay_handler is not None:
        with contextlib.suppress(Exception):
            bpy.types.SpaceView3D.draw_handler_remove(
                _generate_3d_overlay_handler, "WINDOW"
            )
        _generate_3d_overlay_handler = None
    with contextlib.suppress(Exception):
        composer_surface_module().release()
    generate_3d_overlay_state.update(
        {
            "visible": False,
            "compact": False,
            "rect": (0, 0, 0, 0),
            "controls": [],
            "hover_control": None,
            "control_pressed": None,
            "control_press_xy": None,
            "prompt_active": False,
        }
    )
    generation_launcher_state["pressed"] = False
    if _chat_handler is not None:
        with contextlib.suppress(Exception):
            bpy.types.SpaceView3D.draw_handler_remove(_chat_handler, "WINDOW")
        _chat_handler = None
    with contextlib.suppress(Exception):
        chat_surface_module().release_resources()
    chat_state.update({"rect": (0, 0, 0, 0), "controls": [], "prompt_active": False})
    _install_scene_change_watch()
    _install_generation_launcher()
    _install_composer_overlay()


def stop_surfaces():
    """Take all four down and give their layers back."""
    _remove_scene_change_watch()
    _remove_generation_launcher()
    _remove_composer_overlay()
    _remove_chat_overlay()
    _hide_generate_3d_overlay()
    _hide_bar()
    with contextlib.suppress(Exception):
        forget_decodes()
