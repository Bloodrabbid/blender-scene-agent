"""The generation launcher as a viewport overlay.

The composer's small sibling: same three coordinate spaces, same cached Skia
layer, same published-rect contract with the modal. Everything it draws comes
from ``hfui.launcher``; everything it reads about Blender is here.

It is simpler than ``composer_surface`` in one way and harder in another. It is
one button, so there is no hit list to publish — a single rect answers a click.
But it is never quite still: the capsule morphs between two widths and the live
dot breathes, so this module owns a ``Motion`` and a pump, and the layer is sized
for the widest the island can get rather than for whatever it happens to be.
"""

from __future__ import annotations

from ... import perf, safety, skui
from ...hfui import launcher as launcher_module
from ...hfui import motion as motion_module
from ...hfui import pointer as pointer_module
from . import pump as pump_module
from . import skia_runtime as sk

# The island has no shadow, so the only bleed to leave room for is the capsule
# overshooting its target size. ``TOP`` mirrors the composer's ``BOTTOM``, so
# the two surfaces are inset from their edges by the same amount.
MARGIN = 3.0
TOP = 10.0
DRAG_SLOP = 4.0

# A layer is a resource, not state: it owns a Skia surface and a GPU texture
# that have to be *released* in order, so it is not in the object below and
# teardown does not rebuild it. Same for the two timers further down.
_layer = sk.Layer("launcher")


class Launcher:
    """Everything the island remembers between frames.

    One object rather than ten globals, which buys three things: teardown is a
    reconstruction rather than a list of assignments that has to be kept in
    step with the declarations, the defaults are written once and here, and
    `__slots__` turns a misspelled field into an error instead of a new
    attribute nothing reads.

    Grouped by **kind**, in the order every surface uses — see
    `docs/state-model.md`. This one is worth reading for what is *missing*:
    the island has **nothing Published**. Its Model is the pointer plus the
    user-chosen offset and the short-lived state of a drag.
    """

    __slots__ = (
        # -- Resources: lifetime, released in order, never values.
        "measure",
        "host",
        # -- Model: what input writes, and only input.
        "pointer",
        "offset",
        "drag_start",
        "drag_origin",
        "dragging",
        # -- Anim: what the clock writes.
        "motion",
        # -- Work: what a background timer owns. Merge in; never replace.
        "warm_paths",
        # -- Derived: pure, and always safe to drop.
        "state",
        "recents",
        "frame_cache",
    )

    def __init__(self):
        # -- Resources ------------------------------------------------------
        self.measure = None
        # The add-on module, latched by each draw. Timers and event handlers
        # are handed no `hb` of their own and reach it through here.
        self.host = None

        # -- Model ----------------------------------------------------------
        # Which control the pointer is on, which is a finer question than the
        # island's own expansion (`state().hovered`, one bool): the panel it
        # opens carries the View all chip and four tiles, and each declares a
        # hover for `paint` to resolve. Only `hovered` is ever set — a press
        # is not routed here, so a held control reads as hovered, which is
        # what `Style.pressed` falls back to anyway.
        self.pointer = pointer_module.Pointer()
        # Offset from the normal top-centre anchor. Keeping this relative makes
        # the launcher follow viewport resizes instead of remembering stale
        # absolute coordinates.
        self.offset = (0.0, 0.0)
        self.drag_start = None
        self.drag_origin = (0.0, 0.0)
        self.dragging = False

        # -- Anim -----------------------------------------------------------
        self.motion = motion_module.Motion()

        # -- Work -----------------------------------------------------------
        # Paths the warm timer has yet to decode.
        self.warm_paths = ()

        # -- Derived --------------------------------------------------------
        self.state = None
        self.recents = ((), ())
        # The last solved island, under the layer's own key. Same reasoning as
        # the composer's: the texture was cached and the tree that produced it
        # was not.
        self.frame_cache = None


_launcher = Launcher()

# A morph runs at 60 like the composer's: at 30 a 0.28 s expansion is eight
# frames and reads as a stutter, and the island's own cost is not the reason to
# be frugal (~0.3 ms to solve, ~0.9 ms to raster, measured at every stage of the
# move). Nothing else animates: with the breathing dot gone, a running job no
# longer holds a timer open for its whole duration.
_MORPH_FPS = 60.0


def state():
    if _launcher.state is None:
        _launcher.state = launcher_module.LauncherState()
    return _launcher.state


def measure():
    if _launcher.measure is None:
        _launcher.measure = skui.Measure()
    return _launcher.measure


def ui_scale():
    """The composer's scale, deliberately.

    These are the same product at opposite edges of one viewport. If the pill
    were sized against Blender's widget scale and the composer against its own,
    the two would disagree on how big 12pt Inter is, and no amount of shared
    tokens would make them look related.
    """
    from . import composer_surface

    return composer_surface.ui_scale()


def _nap():
    """The morph, and nothing else — the island has no caret and no loop."""
    return 1.0 / _MORPH_FPS if _launcher.motion.running else None


_pump = pump_module.Pump(
    "launcher pump", _nap, alive=lambda: _launcher.host is not None
)
park = _pump.park
_ensure_pulse = _pump.wake


def _warm():
    """Decode and downsample the recents, away from any animation.

    A generation thumbnail is a 900-to-2000-pixel webp and Skia decodes it
    lazily, on the first draw that needs its pixels — which was always the
    first frame of the first expansion. Four of them landed as a ~90 ms stall
    at the exact moment the island started moving, and the morph then had to
    finish in whatever time was left. Doing it from a timer moves the cost to a
    frame nobody is watching.
    """

    paths, _launcher.warm_paths = _launcher.warm_paths, ()
    if paths:
        book = measure().images
        target = launcher_module.TILE * ui_scale()
        for path in paths:
            book.scaled(path, target)
    return None


_warm_timer = safety.timer(_warm, "launcher warm")


def recents(hb):
    """Thumbnail paths for the last few generations, memoised.

    ``collect`` runs on every draw, including the sixty a second a morph costs,
    and this walks the whole history and stats a file per hit. The signature is
    what actually changes when a job lands: how many records there are and which
    one is on top. The record ids ride along in the memo (``recent_ids``) so a
    click on a tile can name the record it showed.
    """

    signature = (len(hb.history), hb.history[0].get("id") if hb.history else None)
    if signature != _launcher.recents[0]:
        import bpy

        pairs = hb._generation_launcher_recents()
        _launcher.recents = (
            signature,
            tuple(thumb for _rec_id, thumb in pairs),
            tuple(rec_id for rec_id, _thumb in pairs),
        )
        _launcher.warm_paths = _launcher.recents[1]
        if not bpy.app.timers.is_registered(_warm_timer):
            bpy.app.timers.register(_warm_timer, first_interval=0.0)
    return _launcher.recents[1]


def recent_ids():
    """Record ids behind the tiles ``recents`` last offered, same order."""
    return _launcher.recents[2] if len(_launcher.recents) > 2 else ()


def collect(hb):
    """Read the live job state into the island's own state object."""
    current = state()
    current.recents = recents(hb)
    label, tone = hb._generation_launcher_status()
    current.report(label, tone)
    # The explorer has the viewport: stand down and let its header do the
    # reporting. See ``hfui.launcher`` — the island is never a second surface
    # above a panel that already says everything it would.
    current.away = bool(hb.bar_state.get("visible"))
    return current


def geometry(hb, region, area, width, height, offset=None):
    """Where the island of this size lands: top centre, clear of the header.

    Centred on the same axis as the composer, not on the region. The two are one
    surface split across the viewport's two edges, and the toolbar and the
    N-panel push the *usable* middle to the right of the region's middle — so
    centring on `region.width` left them visibly off from each other. It comes
    from `_bar_region_margins`, which is where the composer gets it.

    The top is worked out here rather than taken from that helper, which sums
    HEADER and TOOL_HEADER: clearing the taller of the two puts the island where
    BlenderKit sits, summing both drops it a row lower, and clearing neither
    lets the toolbar cover it.
    """
    import bpy

    _, left_off, right_off = hb._bar_region_margins(area)
    top_off = 0
    try:
        if bpy.context.preferences.system.use_region_overlap and area is not None:
            top_off = max(
                (
                    item.height
                    for item in area.regions
                    if item.type in {"HEADER", "TOOL_HEADER"}
                ),
                default=0,
            )
    except Exception:
        top_off = 0
    span = region.width - left_off - right_off
    scale = ui_scale()
    edge = TOP * scale
    base_x = left_off + (span - width) * 0.5
    base_y = region.height - top_off - edge - height
    dx, dy = _launcher.offset if offset is None else offset
    x = min(
        max(base_x + dx, left_off + edge),
        max(left_off + edge, region.width - right_off - edge - width),
    )
    y = min(
        max(base_y + dy, edge),
        max(edge, region.height - top_off - edge - height),
    )
    return x, y


def visible(hb):
    """The badge is permanent for a signed-in user.

    It used to hide behind the composer, which meant it was suppressed in
    exactly the situation it exists for — the composer is restored every two
    seconds, so a job submitted from it had nowhere to report. The two do not
    overlap: the composer is anchored to the bottom edge and the explorer opens
    as a dock above the timeline.
    """
    return bool(hb.is_authenticated() and sk.available())


def draw(hb):
    """Draw handler body. Publishes ``rect`` for the modal to hit-test."""
    import bpy

    _launcher.host = hb
    _pump.revive()
    if not visible(hb):
        hb.generation_launcher_state["rect"] = (0, 0, 0, 0)
        return
    region = bpy.context.region
    if region is None:
        return

    started = perf.now()
    current = collect(hb)
    perf.mark("collect", started)
    launcher_state = hb.generation_launcher_state

    scale = ui_scale()
    _launcher.motion.tick()
    # Aimed here as well as inside ``build``, because the exit has to start
    # even on the frame this returns early from.
    launcher_module.aim(current, _launcher.motion)
    gone = _launcher.motion.value("away", 1.0 if current.away else 0.0)
    if gone >= 0.999:
        # Fully stood down. The rect goes with it: the shrunken capsule is a
        # transparent seventeen pixels at top centre, and leaving it hittable
        # means a stray click there reopens what was just closed.
        launcher_state["rect"] = (0, 0, 0, 0)
        return
    # ``build`` aims both tracks through ``shape``, so the key has to miss
    # whenever they move — which it does, since a track that starts moving
    # changes the motion's key on the very next tick. ``hover`` aims for
    # itself before the pump starts, for exactly that reason.
    tree_key = (current.key, _launcher.motion.key, round(scale, 3))
    if _launcher.frame_cache is not None and _launcher.frame_cache[0] == tree_key:
        frame = _launcher.frame_cache[1]
        perf.count("tree_hits")
    else:
        started = perf.now()
        frame = skui.solve(launcher_module.build(current, _launcher.motion), measure())
        perf.mark("solve", started)
        _launcher.frame_cache = (tree_key, frame)
    card_width, card_height = frame.width * scale, frame.height * scale
    started = perf.now()
    card_x, card_y = geometry(hb, region, bpy.context.area, card_width, card_height)
    perf.mark("geometry", started)

    # Both of the capsule's dimensions change every frame while it morphs, so
    # the layer is allocated once for the largest it can ever be — overshoot
    # included — and the island is drawn into a corner of it. Sizing the layer
    # to the frame would reallocate the surface, the GPU buffer and the texture
    # thirty times a second for the length of the animation.
    box_width = (launcher_module.MAX_WIDTH + 2 * MARGIN) * scale
    box_height = (launcher_module.MAX_HEIGHT + 2 * MARGIN) * scale
    # Centred across, but hung from the top: the island grows downward into the
    # viewport and its top edge is the thing that must not move.
    inset = (box_width / scale - frame.width) * 0.5

    def render(canvas):
        canvas.scale(scale, scale)
        canvas.translate(inset, MARGIN)
        skui.paint(canvas, frame, measure(), _launcher.pointer.state)

    # The tree key, not a second tuple beside it: the raster is a function of
    # the tree and of geometry the tree already determines, so anything the
    # tree misses on the layer must miss on too. Written out separately they
    # agree only as long as somebody keeps them agreeing.
    #
    # The pointer is the one thing added to it rather than folded into it.
    # ``build`` never sees a pointer — a control's hover is declared on its
    # ``Style`` and resolved above, at paint — so a move under the open panel
    # changes the raster without changing a single node, and putting it in the
    # tree key would re-solve a tree that cannot have come out differently.
    _layer.ensure(box_width, box_height, (tree_key, _launcher.pointer.key), render)
    _layer.blit(
        card_x - inset * scale,
        card_y + card_height - box_height + MARGIN * scale,
    )

    launcher_state["rect"] = (card_x, card_y, card_width, card_height)
    if _launcher.motion.running:
        _ensure_pulse()


def hit(hb, mx, my):
    x, y, width, height = hb.generation_launcher_state.get("rect", (0, 0, 0, 0))
    return width > 0 and x <= mx <= x + width and y <= my <= y + height


def control_at(hb, mx, my):
    """The published node id under a region-space point, or ``None``.

    The frame is solved in logical units with the island hung from the top of
    its rect, so the mapping is the blit's, inverted: shift to the card's
    origin, divide by the scale, flip y.
    """
    if _launcher.frame_cache is None or not hit(hb, mx, my):
        return None
    x, y, _width, height = hb.generation_launcher_state.get("rect", (0, 0, 0, 0))
    scale = ui_scale() or 1.0
    frame = _launcher.frame_cache[1]
    return frame.hit((mx - x) / scale, (y + height - my) / scale)


def hover(hb, mx, my):
    """Mouse move: the island opens under the pointer. Did anything change?

    Hit-tested against the *current* rect, which is the one that is growing, so
    the pointer stays captured as the panel opens beneath it and is released the
    moment it leaves the larger shape.

    Two things can change and either is a redraw: the island's expansion, and
    which control is lit. The control is resolved against the frame the last
    draw solved, the same way a click is — so the chip and the tiles light on
    the first move after the panel has arrived under the pointer, never during
    the expansion that brought them there.
    """

    _launcher.host = hb
    inside = hit(hb, mx, my)
    control = control_at(hb, mx, my)
    moved = control != _launcher.pointer.hovered
    _launcher.pointer.hovered = control
    current = state()
    if inside == current.hovered:
        return moved
    current.hovered = inside
    # Aim before pumping. The pump retires the moment nothing is running, so
    # starting it against a track the next draw has not aimed yet kills it on
    # its first tick and the morph advances only on whatever redraw happens to
    # come along next — which is what made the expansion stutter.
    launcher_module.aim(current, _launcher.motion)
    _ensure_pulse()
    return True


def begin_drag(mx, my):
    """Remember a possible drag without stealing ordinary clicks."""
    _launcher.drag_start = (mx, my)
    _launcher.drag_origin = _launcher.offset
    _launcher.dragging = False


def drag_move(hb, region, area, mx, my):
    """Move the launcher after a small threshold; return whether it changed."""
    if _launcher.drag_start is None or region is None:
        return False
    dx = mx - _launcher.drag_start[0]
    dy = my - _launcher.drag_start[1]
    if not _launcher.dragging:
        slop = DRAG_SLOP * ui_scale()
        if dx * dx + dy * dy < slop * slop:
            return False
        _launcher.dragging = True

    candidate = (_launcher.drag_origin[0] + dx, _launcher.drag_origin[1] + dy)
    rect = hb.generation_launcher_state.get("rect", (0, 0, 0, 0))
    width, height = rect[2], rect[3]
    base_x, base_y = geometry(hb, region, area, width, height, offset=(0.0, 0.0))
    x, y = geometry(hb, region, area, width, height, offset=candidate)
    offset = (x - base_x, y - base_y)
    if offset == _launcher.offset:
        return False
    _launcher.offset = offset
    return True


def end_drag():
    """Finish a possible drag and report whether it became one."""
    dragged = _launcher.dragging
    _launcher.drag_start = None
    _launcher.dragging = False
    return dragged


def activate(hb, mx, my):
    """A click landed on the island. Which control decides what it does.

    There used to be two: *View all* opened the assets browser and a recent
    tile opened it looking at one generation. Both were the account's, and with
    them went the panel that held them — the island no longer expands, so there
    is no control to ask about. The capsule is the control, and the one thing
    it can do is bring the prompt forward.
    """
    hb._show_bar()
    return True


def release():
    """Drop GPU and Skia resources; called from unregister().

    The layer is released, everything else is rebuilt. The frame cache holds
    `Placed` nodes measured by the font book, and the two go together here
    because they are fields of one object rather than two assignments whose
    order somebody has to keep right.
    """
    global _launcher

    # A park is a fact about a draw that raised, and this rebuilds the thing
    # that raised. The pump outlives the surface object — it holds a timer
    # registration — so it has to be told, where before the flag simply went
    # with the object.
    _pump.revive()
    _layer.release()
    _launcher = Launcher()
