"""The redraw pump the four Skia surfaces share.

A viewport overlay that animates cannot ask for its own next frame. Tagging a
redraw from inside a draw handler is swallowed — Blender clears the flag when
that draw finishes — so an animation would only advance on whatever event
happened to arrive next, and then *snap*, because the tracks run on wall time.
The answer everywhere is an app timer that tags the viewport and hands back how
long to sleep, and that retires the moment nothing is moving: a pump left
running is a full `VIEW_3D` redraw of the user's scene, sixty times a second,
for nothing.

All four surfaces had their own `park` / `_pulse` / `_pulse_stopped` /
`_ensure_pulse` / `safety.timer(...)` — twenty functions for five ideas, with
the interesting part buried in the middle of each. What actually differs
between them is two questions, and those are the two the caller answers:

- **`alive()`** — should this pump exist at all? Every surface says "the host
  is latched"; the chat also asks whether its draw handler is still installed
  and whether its breaker has tripped, because it is the only pump here that
  will never retire on its own.
- **`nap()`** — seconds until the next frame, or `None` when nothing is
  moving. The launcher and explorer are `1/60 while the morph runs`; the
  composer adds the caret, which sleeps to the next blink flip rather than
  running at 60fps for a rule that changes twice a second; the chat has five
  reasons to be awake and returns the soonest.

The two must stay separate, and the explorer is why: on `alive()` going false
the pump just stops, but on `nap()` answering `None` it fires `on_settle`,
which is the one extra frame that shows whatever was held back during a move.
Fold them together and a panel whose host went away paints one last time.

**The pump is module-level, beside the layers, not a field on the surface
object.** It owns a registration inside Blender, which is a lifetime, and
`release()` rebuilding the surface must not silently drop one on the floor —
the same reason `Layer` lives there. It also closes a hole by construction: a
pump replaced while its timer was still registered would let the next `wake()`
register a second one against the same surface.
"""

from ... import safety
from . import redraw_viewports


class Pump:
    """A redraw timer that runs while something moves and retires when nothing
    does.

    `wake()` from an event handler that started an animation — it is cheap and
    idempotent, so call it whenever in doubt rather than reasoning about
    whether one is already running.
    """

    __slots__ = ("running", "parked", "_nap", "_alive", "_settle", "_timer")

    def __init__(self, label, nap, alive, on_settle=None):
        self._nap = nap
        self._alive = alive
        self._settle = on_settle
        # True between `wake` and the tick that retires. Not "is the timer
        # registered" — only Blender knows that — but the flag `wake` checks so
        # a second registration cannot happen.
        self.running = False
        # Set after a draw raised. Without it a broken draw and its pump feed
        # each other at 60fps and the window stops responding. Ordinary events
        # still redraw, so the surface revives on its own once a draw succeeds.
        self.parked = False
        # A raise or a runaway interval in a pump is felt immediately, so it
        # goes through the safety wrapper: raises unregister rather than
        # repeat, and `on_stop` clears `running` on every path out — including
        # the ones this class never sees — so a dead pump can always restart.
        self._timer = safety.timer(self._tick, label, on_stop=self._stopped)

    def wake(self):
        """Start pumping, unless it already is or the surface is parked."""
        if self.running or self.parked:
            return
        import bpy

        self.running = True
        bpy.app.timers.register(self._timer, first_interval=0.0)

    def park(self):
        """Stop driving redraws after a failed draw."""
        self.parked = True

    def revive(self):
        """A draw got through: the surface is worth pumping for again."""
        self.parked = False

    def _stopped(self):
        self.running = False

    def _tick(self):
        if self.parked or not self._alive():
            self.running = False
            return None
        nap = self._nap()
        if nap is None:
            self.running = False
            if self._settle is not None:
                self._settle()
            return None
        redraw_viewports()
        return nap
