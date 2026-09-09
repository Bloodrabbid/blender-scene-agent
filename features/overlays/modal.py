"""Routing viewport events to the four surfaces.

One modal operator, running for as long as any surface is up, deciding for every
event whether a surface wants it and whether Blender should still see it. The
order is fixed and load bearing: the launcher is asked first in all three
branches, then the assistant, then the composer — the composer's branch returns
for nearly every event, so anything checked after it is never reached.

**"Trapping" is two separate questions.** The *pointer* being over a card
(`blocks_viewport`) swallows navigation and region shortcuts the way hovering
the N-panel does, while window-level combos still pass through — except
Cmd/Ctrl C, V, A, X while a panel is open, which copy, paste, select-all or
cut prompt and thread text. *Focus* (`prompt_active`) only claims the keys a
text field owns; everything else reaches Blender. Never re-add a blanket "any
other key while editing is trapped".

**Region coordinates are the hard part.** `_bar_view3d_region_xy` maps a
window event onto the region the surfaces drew into, and `_bar_event_pixel_scale`
disambiguates the pixel scale from the surfaces’ own published rects — they
band for exactly that. Get it wrong and every hit rect is off by a factor on
Retina.

The one event the launcher never claims is `MOUSEMOVE`: the composer has to see
every one or it cannot tell that the pointer left its card.
"""

import contextlib
import logging
import time

import bpy

from ... import perf
from ...blender.viewport import request_redraw
from . import addon, mount, redraw_viewports

# `mount` is imported whole for the handler slots alone: install and remove
# rebind them, and a from-import would freeze this module on whatever they
# held when it loaded.
from .mount import (
    _bar_ui_scale,
    bar_state,
    chat_breaker,
    chat_state,
    chat_surface_module,
    composer_breaker,
    composer_surface_module,
    generate_3d_overlay_state,
    generation_launcher_state,
    launcher_surface_module,
    modal_breaker,
)

logger = logging.getLogger(__name__)






def _bar_event_pixel_scale():
    """Window-event points → region/framebuffer pixels (2× on macOS Retina)."""
    with contextlib.suppress(Exception):
        return max(1.0, float(bpy.context.preferences.system.pixel_size))
    return 1.0


def _bar_view3d_region_xy(window, mouse_x, mouse_y):
    """Map window mouse coords onto the VIEW_3D WINDOW region under the cursor."""
    if window is None:
        return None, 0, 0
    # Blender's event coordinates are inconsistent across macOS window modes:
    # some builds report logical points while Area/Region are framebuffer
    # pixels, while others report framebuffer pixels for both. Score both
    # interpretations against the actual overlay hit-zones and remember the
    # winner for drag/release events.
    pixel_scale = _bar_event_pixel_scale()
    remembered = bar_state.get("event_scale")
    scales = []
    for scale in (remembered, 1.0, pixel_scale):
        if scale is None or any(abs(scale - item) < 0.001 for item in scales):
            continue
        scales.append(float(scale))
    candidates = []
    for area in window.screen.areas:
        if area.type != "VIEW_3D":
            continue
        for region in area.regions:
            if region.type != "WINDOW":
                continue
            for scale in scales:
                window_x = float(mouse_x) * scale
                window_y = float(mouse_y) * scale
                if not (
                    region.x <= window_x < region.x + region.width
                    and region.y <= window_y < region.y + region.height
                ):
                    continue
                local_x = window_x - region.x
                local_y = window_y - region.y
                score = 0
                # The badge is checked first because it is the one surface that
                # is up in every state; the others are alternatives to it.
                if _generation_launcher_hit(local_x, local_y):
                    score = 6
                elif generate_3d_overlay_state.get("visible"):
                    hit_control = _generate_3d_overlay_hit_control(local_x, local_y)
                    if hit_control is not None:
                        score = 7
                    elif _generate_3d_overlay_hit(local_x, local_y):
                        score = 3
                elif _generation_launcher_hit(local_x, local_y):
                    score = 6
                if remembered is not None and abs(scale - remembered) < 0.001:
                    score += 1
                candidates.append((score, scale, region, local_x, local_y))
    if candidates:
        score, scale, region, local_x, local_y = max(
            candidates, key=lambda item: item[0]
        )
        if score >= 4:
            bar_state["event_scale"] = scale
        bar_state["last_event_xy"] = (
            float(mouse_x),
            float(mouse_y),
            scale,
            local_x,
            local_y,
            score,
        )
        return region, local_x, local_y
    return None, 0, 0


def _bar_event_xy(context, event):
    """Region-local bar coords from a modal event (window space → 3D WINDOW)."""
    if (
        getattr(context, "area", None) is not None
        and context.area.type == "VIEW_3D"
        and getattr(context, "region", None) is not None
        and context.region.type == "WINDOW"
    ):
        mx = float(getattr(event, "mouse_region_x", 0.0))
        my = float(getattr(event, "mouse_region_y", 0.0))
        bar_state["last_event_xy"] = (
            float(getattr(event, "mouse_x", 0.0)),
            float(getattr(event, "mouse_y", 0.0)),
            "region",
            mx,
            my,
            10,
        )
        return context.region, mx, my
    return _bar_view3d_region_xy(context.window, event.mouse_x, event.mouse_y)






def _generate_3d_overlay_hit(mx, my):
    x, y, width, height = generate_3d_overlay_state.get("rect", (0, 0, 0, 0))
    return width > 0 and x <= mx <= x + width and y <= my <= y + height


def _generate_3d_overlay_hit_control(mx, my):
    for action, value, x, y, width, height in reversed(
        generate_3d_overlay_state.get("controls") or ()
    ):
        if x <= mx <= x + width and y <= my <= y + height:
            return action, value
    return None


def _generation_launcher_hit(mx, my):
    x, y, width, height = generation_launcher_state.get("rect", (0, 0, 0, 0))
    return width > 0 and x <= mx <= x + width and y <= my <= y + height


def _update_resize_cursor(context, hovering_grip):
    """MOVE_Y over the thread grip and through its drag, DEFAULT on leaving.

    Set on transitions only: `cursor_set` is per window, so re-asserting
    DEFAULT on every move would fight any cursor Blender itself decides to
    show. The flag remembers which side this module last set.
    """
    from . import composer_surface

    want = bool(hovering_grip or composer_surface.resizing())
    if want == bool(generate_3d_overlay_state.get("resize_cursor")):
        return
    generate_3d_overlay_state["resize_cursor"] = want
    with contextlib.suppress(AttributeError, RuntimeError):
        context.window.cursor_set("MOVE_Y" if want else "DEFAULT")


def _activate_generate_3d_overlay_control(action, _value=None):
    """Route a released click. The composer owns every node id it publishes."""
    composer_surface_module().activate(addon(), action)
    request_redraw()


class SCENEAGENT_OT_bar_modal(bpy.types.Operator):
    """Tracks hover/click/drag for the floating generation bar.

    Uses window-space mouse coords mapped onto the 3D WINDOW region so hover
    still works when the operator was started from the N-panel.
    """

    bl_idname = "scene_agent.bar_modal"
    bl_label = "Generation Bar Modal"
    bl_options = {"INTERNAL"}

    def _finish(self, context):
        timer = getattr(self, "_timer", None)
        if timer is not None:
            with contextlib.suppress(Exception):
                context.window_manager.event_timer_remove(timer)
            self._timer = None
        bar_state["modal_running"] = False
        bar_state["modal_heartbeat"] = 0.0

    def invoke(self, context, event):
        if (
            not bar_state["visible"]
            and not generate_3d_overlay_state["visible"]
            and mount._generation_launcher_handler is None
            and mount._chat_handler is None
        ) or bar_state.get("modal_running"):
            return {"CANCELLED"}
        bar_state["modal_running"] = True
        bar_state["modal_heartbeat"] = time.monotonic()
        context.window_manager.modal_handler_add(self)
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        return {"RUNNING_MODAL"}

    # Pointer and timer traffic never reaches the prompt's keymap.
    _NON_KEY_EVENTS = frozenset(
        {
            "MOUSEMOVE",
            "INBETWEEN_MOUSEMOVE",
            "TIMER",
            "TIMER_REPORT",
            "TRACKPADPAN",
            "TRACKPADZOOM",
            "MOUSEROTATE",
            "MOUSESMARTZOOM",
            "NDOF_MOTION",
            "LEFTMOUSE",
            "RIGHTMOUSE",
            "MIDDLEMOUSE",
            "BUTTON4MOUSE",
            "BUTTON5MOUSE",
            "BUTTON6MOUSE",
            "BUTTON7MOUSE",
            "WHEELUPMOUSE",
            "WHEELDOWNMOUSE",
            "WHEELINMOUSE",
            "WHEELOUTMOUSE",
        }
    )

    def modal(self, context, event):
        # This modal can swallow events, so a raise inside it is the one bug
        # that could leave the user unable to click anything. Anything it does
        # not survive gives the event straight back to Blender.
        error = None
        try:
            # Input is measured beside the draws because it is the other half
            # of "does this feel slow": a MOUSEMOVE flood over the card runs
            # this on every one of them, ahead of the redraw it asks for.
            with perf.frame("input"):
                return self._modal(context, event)
        except Exception as caught:
            error = caught
        modal_breaker.record(0.0, failed=True)
        signature = (type(error).__name__, str(error))
        if signature != getattr(self, "_last_error", None):
            self._last_error = signature
            logger.error(
                "Overlay modal failed",
                exc_info=(type(error), error, error.__traceback__),
            )
        generate_3d_overlay_state["prompt_active"] = False
        return {"PASS_THROUGH"}

    def _launcher_event(self, event, mx, my, region=None, area=None):
        """Pointer handling for the island, in every state the viewport is in.

        The island outlives the composer — it is the one surface that is
        surface that is always up — so this runs ahead of either, and returns a
        modal result only when it actually took the event. A move is never
        taken — the composer has to see every one of them or it cannot tell that
        the pointer left the card — but it is acted on, because the pointer is
        what opens the island.
        """
        if mount._generation_launcher_handler is None:
            return None
        launcher = launcher_surface_module()
        if event.type == "MOUSEMOVE":
            if generation_launcher_state.get("pressed"):
                if launcher.drag_move(addon(), region, area, mx, my):
                    redraw_viewports()
                # Keep the state that was grabbed. Hit-testing against the old
                # rect before the moved frame is drawn would otherwise collapse
                # an expanded launcher as soon as the drag starts.
                return {"RUNNING_MODAL"}
            if launcher.hover(addon(), mx, my):
                redraw_viewports()
            return None
        over = _generation_launcher_hit(mx, my)
        if event.type == "LEFTMOUSE" and event.value == "PRESS" and over:
            # Ignore the app-activation press macOS delivers while Blender is
            # coming to the front; only deliberate clicks act.
            if time.monotonic() < float(
                generation_launcher_state.get("ready_at") or 0.0
            ):
                return {"RUNNING_MODAL"}
            generation_launcher_state["pressed"] = True
            launcher.begin_drag(mx, my)
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if not generation_launcher_state.get("pressed"):
                return None
            generation_launcher_state["pressed"] = False
            dragged = launcher.end_drag()
            if over and not dragged:
                launcher.activate(addon(), mx, my)
            elif launcher.hover(addon(), mx, my):
                redraw_viewports()
            return {"RUNNING_MODAL"}
        if not over:
            return None
        # Same split Blender makes for a panel under the cursor: the region's
        # own shortcuts and navigation do not fire, window-level combos do.
        if event.type not in self._NON_KEY_EVENTS and (event.ctrl or event.oskey):
            return None
        return {"RUNNING_MODAL"}

    def _chat_event(self, event, mx, my):
        """Pointer and keys for the assistant, ahead of the composer.

        Same contract as ``_launcher_event``: ``None`` means "not mine, carry
        on". A move is never claimed — the composer has to see every one of
        them — but it is acted on, because the pointer is what opens the panel.

        A press *outside* the panel is not claimed either. It dismisses the
        panel and then falls through, so the same click still reaches whatever
        it landed on: the composer, or the scene.
        """
        if mount._chat_handler is None or chat_breaker.tripped:
            return None
        chat = chat_surface_module()
        me = addon()
        if event.type == "MOUSEMOVE":
            if chat.hover(me, mx, my):
                redraw_viewports()
            if chat.drag_caret(me, mx, my):
                redraw_viewports()
                return {"RUNNING_MODAL"}
            return None
        over = chat.blocks_viewport(mx, my)
        if chat.scroll_event(me, event, mx, my):
            redraw_viewports()
            return {"RUNNING_MODAL"}
        # The focused prompt takes the keys a text field owns and no others,
        # exactly like the composer's — and it takes them first, because the
        # two fields can never be focused at the same time.
        if (
            chat_state.get("prompt_active")
            and event.type not in self._NON_KEY_EVENTS
            and chat.key_event(me, event)
        ):
            redraw_viewports()
            return {"RUNNING_MODAL"}
        if event.type not in self._NON_KEY_EVENTS and chat.clipboard_event(
            me, event, mx, my
        ):
            redraw_viewports()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "DOUBLE_CLICK" and over:
            node = chat.hit_control(mx, my)
            if node == "chat:prompt" or str(node or "").startswith("msg:"):
                chat.select_word_at(me, mx, my)
                redraw_viewports()
                return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            if not over:
                if chat.dismiss(me):
                    redraw_viewports()
                return None
            chat.press(me, mx, my, extend=event.shift)
            redraw_viewports()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE":
            if not chat.pressed():
                chat.end_drag()
                return None
            chat.release(me, mx, my)
            request_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "ESC" and event.value == "PRESS" and chat.state().open:
            if chat.skip_ask():
                redraw_viewports()
                return {"RUNNING_MODAL"}
            chat.dismiss(me)
            redraw_viewports()
            return {"RUNNING_MODAL"}
        if not over:
            return None
        if event.type not in self._NON_KEY_EVENTS and (event.ctrl or event.oskey):
            return None
        return {"RUNNING_MODAL"}

    def _modal(self, context, event):
        bar_state["modal_heartbeat"] = time.monotonic()
        if generate_3d_overlay_state["visible"] and not composer_breaker.tripped:
            if event.type == "TIMER":
                return {"RUNNING_MODAL"}
            region, mx, my = _bar_event_xy(context, event)
            from . import composer_surface

            if composer_surface.sync_context_selection():
                redraw_viewports()
            taken = self._launcher_event(event, mx, my, region, context.area)
            if taken is not None:
                return taken
            taken = self._chat_event(event, mx, my)
            if taken is not None:
                return taken
            hover_control = _generate_3d_overlay_hit_control(mx, my)
            in_overlay = _generate_3d_overlay_hit(mx, my)

            # Pointer business only: the composer eats navigation under its own
            # rectangle. Keys are a separate question, answered by the field.
            # The badge counts as part of that rectangle even though it is a
            # separate surface, or a click on it would orbit the scene.
            trapping = (
                composer_surface.blocks_viewport(mx, my)
                or _generation_launcher_hit(mx, my)
                or chat_surface_module().blocks_viewport(mx, my)
            )
            if event.type == "MOUSEMOVE":
                _update_resize_cursor(
                    context,
                    hover_control is not None
                    and hover_control[0] in composer_surface.composer_module.RESIZE_IDS,
                )
                changed = hover_control != generate_3d_overlay_state.get(
                    "hover_control"
                )
                generate_3d_overlay_state["hover_control"] = hover_control
                # Hovering the collapsed pill is what opens the composer, so the
                # move has to reach it even when it lands outside the rect.
                if composer_surface.hover(addon(), mx, my) or changed:
                    redraw_viewports()
                if composer_surface.drag_context(addon(), mx, my):
                    redraw_viewports()
                    return {"RUNNING_MODAL"}
                if composer_surface.drag_caret(addon(), mx, my):
                    redraw_viewports()
                    return {"RUNNING_MODAL"}
                if composer_surface.drag_slider(addon(), mx):
                    # Viewport only while the drag runs; the release below
                    # redraws everything so the N-panel lands on the value.
                    redraw_viewports()
                    return {"RUNNING_MODAL"}
                if composer_surface.drag_resize(my):
                    redraw_viewports()
                    return {"RUNNING_MODAL"}
                # Over the composer: swallow the move so a middle-drag orbit
                # cannot keep running underneath the card.
                # Outside, pass through so the viewport stays navigable.
                return {"RUNNING_MODAL"} if trapping else {"PASS_THROUGH"}
            # Wheel / trackpad scrolls whatever it is pointing at — the chip
            # strip sideways, an overflowing prompt down; otherwise the same
            # gesture over the composer is eaten so it cannot zoom the scene.
            if composer_surface.scroll_event(addon(), event, mx, my):
                redraw_viewports()
                return {"RUNNING_MODAL"}
            # The focused prompt behaves like a text field: it takes the keys a
            # text field owns — typing, caret motion, selection, clipboard —
            # and leaves every other shortcut to Blender.
            if (
                generate_3d_overlay_state.get("prompt_active")
                and event.type not in self._NON_KEY_EVENTS
                and composer_surface.key_event(addon(), event)
            ):
                request_redraw()
                return {"RUNNING_MODAL"}
            if event.type not in self._NON_KEY_EVENTS and composer_surface.clipboard_event(
                addon(), event, mx, my
            ):
                request_redraw()
                return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "DOUBLE_CLICK":
                if hover_control is not None and (
                    hover_control[0] == "prompt"
                    or str(hover_control[0]).startswith("msg:")
                ):
                    composer_surface.select_word_at(addon(), mx, my)
                    request_redraw()
                    return {"RUNNING_MODAL"}
                if (
                    hover_control is not None
                    and hover_control[0]
                    == composer_surface.composer_module.PROMPT_RESIZE_ID
                ):
                    # Double click is the way back to a prompt sized by its
                    # own text; a drag cannot land on that height by hand.
                    composer_surface.reset_prompt_height()
                    request_redraw()
                    return {"RUNNING_MODAL"}
            if event.type == "LEFTMOUSE" and event.value == "PRESS":
                if hover_control is not None:
                    generate_3d_overlay_state["control_pressed"] = hover_control
                    generate_3d_overlay_state["control_press_xy"] = (mx, my)
                    if composer_surface.press_context(hover_control[0]):
                        request_redraw()
                        return {"RUNNING_MODAL"}
                    # A slider sets its value on press and keeps setting it as
                    # the pointer moves, the way every other drag control does.
                    if composer_surface.press_slider(addon(), hover_control[0], mx):
                        request_redraw()
                        return {"RUNNING_MODAL"}
                    # The grip above the Scene Builder thread arms a height
                    # drag the same way.
                    if composer_surface.press_resize(hover_control[0], my):
                        request_redraw()
                        return {"RUNNING_MODAL"}
                    if hover_control[0] == "prompt":
                        # Focus and start sweeping on the first press, so a
                        # drag-select does not need a click to arm the field.
                        generate_3d_overlay_state["prompt_active"] = True
                        composer_surface.place_caret(
                            addon(),
                            mx,
                            my,
                            extend=event.shift,
                            drag=True,
                        )
                        request_redraw()
                    elif str(hover_control[0]).startswith("msg:"):
                        composer_surface.press_thread(
                            addon(),
                            hover_control[0],
                            mx,
                            my,
                            extend=event.shift,
                        )
                        request_redraw()
                    return {"RUNNING_MODAL"}
                if generate_3d_overlay_state.get("prompt_active"):
                    generate_3d_overlay_state["prompt_active"] = False
                if not in_overlay:
                    # Click outside: drop the pin so hover alone governs again.
                    composer_surface.activate(addon(), None)
                request_redraw()
                return {"RUNNING_MODAL"} if in_overlay else {"PASS_THROUGH"}
            if event.type == "LEFTMOUSE" and event.value == "RELEASE":
                pressed = generate_3d_overlay_state.get("control_pressed")
                press_xy = generate_3d_overlay_state.get("control_press_xy")
                generate_3d_overlay_state["control_pressed"] = None
                generate_3d_overlay_state["control_press_xy"] = None
                swept = composer_surface.end_drag()
                # The drag is over; unless the pointer let go still on the
                # grip, the arrows leave with it.
                _update_resize_cursor(
                    context,
                    hover_control is not None
                    and hover_control[0] in composer_surface.composer_module.RESIZE_IDS,
                )
                if pressed is not None:
                    if pressed == hover_control:
                        action, value = pressed
                        if action == "prompt" and press_xy is not None:
                            # A drag already placed the caret and swept a
                            # selection; re-placing it here would discard that.
                            composer_surface.activate(
                                addon(),
                                "prompt",
                                mx=None if swept else press_xy[0],
                                my=None if swept else press_xy[1],
                            )
                            request_redraw()
                        else:
                            _activate_generate_3d_overlay_control(action, value)
                    else:
                        # The press ended off its control — a slider drag that
                        # travelled past the chip. Nothing is activated, but a
                        # value did change, and the drag itself only redrew the
                        # viewport, so this is where the N-panel catches up.
                        request_redraw()
                    return {"RUNNING_MODAL"}
            if event.type == "ESC" and event.value == "PRESS":
                # The composer is permanent, so Escape collapses it back to the
                # pill instead of dismissing it.
                composer_surface.activate(addon(), None)
                request_redraw()
                return {"PASS_THROUGH"}
            # Pointer is on the composer: trap the rest (middle-orbit, right-
            # pan, wheel zoom, trackpad, NDOF, view shortcuts) so the scene
            # underneath cannot move. Outside the card, navigation is free —
            # even while the composer stays pinned open.
            if trapping:
                # Same split Blender makes for a panel under the cursor: the
                # region's own shortcuts (G, R, numpad views) do not fire, but
                # window-level ones — Cmd/Ctrl+S and friends — still do.
                if event.type not in self._NON_KEY_EVENTS and (
                    event.ctrl or event.oskey
                ):
                    return {"PASS_THROUGH"}
                return {"RUNNING_MODAL"}
            return {"PASS_THROUGH"}

        # The composer just went away (hidden, or its breaker tripped) — do not
        # leave the window stuck on the grip's resize arrows.
        if generate_3d_overlay_state.get("resize_cursor"):
            _update_resize_cursor(context, False)

        if not bar_state["visible"]:
            if (
                mount._generation_launcher_handler is None
                and mount._chat_handler is None
            ):
                self._finish(context)
                return {"FINISHED"}
            region, mx, my = _bar_event_xy(context, event)
            taken = self._launcher_event(event, mx, my, region, context.area)
            if taken is not None:
                return taken
            taken = self._chat_event(event, mx, my)
            return taken if taken is not None else {"PASS_THROUGH"}

        # Nothing else reads the pointer. The assets browser used to live
        # below here — six hundred lines of hover, drag and drop over a grid of
        # generations — and it went with the account that filled it.
        return {"PASS_THROUGH"}

    def cancel(self, context):
        self._finish(context)
