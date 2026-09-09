"""Circuit breakers for the code that runs on Blender's main thread.

Draw handlers, app timers and modal operators all run inline with the UI. If
one of them raises on every frame, or simply takes too long, Blender has no way
to recover on its own: the window stops redrawing and the session is lost. That
already happened once here — a typo in a draw handler threw at the redraw
pump's 60fps and logging the traceback each time was itself enough work to lock
the app up.

Nothing here tries to repair a broken surface. It takes the surface off the
main thread and says so, on the principle that a missing overlay is a bug we
can fix tomorrow while a frozen Blender costs the user their work today.
"""

import logging
import time
from collections import deque

logger = logging.getLogger(__name__)

# Budget for one call. A single slow frame is normal (first raster, blur,
# font load); it is sustained cost that matters, so this only feeds the
# "is anything wrong at all" question, not the trip decision.
BUDGET = 0.05
# Fraction of wall time a surface may spend on the main thread before it counts
# as running away. Measured here: the composer idles near 7% and its open
# animation costs ~19 ms a frame, so a heavy-but-working overlay on a slow
# machine can legitimately sit around half. Only a surface that leaves Blender
# no room at all should lose its handler, hence the high bar.
DUTY = 0.75
# Wall time the duty measurement covers, and how long overload must remain
# continuous before it trips. A brief redraw burst may cross DUTY; only a
# surface that stays above it for this whole clock is running away.
WINDOW = 3.0
# Consecutive raises before a surface is considered permanently broken.
FAILURES = 5
# A single call this long has already frozen the window for a noticeable beat.
# The first call is exempt: cold Skia surfaces, font loading and the first blur
# are legitimately slow, and they happen once.
STALL = 2.0


class Breaker:
    """Trips when a surface eats the main thread or fails over and over.

    ``on_trip`` is called once, from whatever thread tripped it — in practice
    the main one, since that is the only place these surfaces run.
    """

    def __init__(
        self,
        label,
        *,
        budget=BUDGET,
        duty=DUTY,
        window=WINDOW,
        failures=FAILURES,
        stall=STALL,
        on_trip=None,
    ):
        self.label = label
        self.tripped = False
        self.reason = None
        self.slowest = 0.0
        self.calls = 0
        self._budget = budget
        self._duty = duty
        self._window = window
        self._failures = failures
        self._stall = stall
        self._on_trip = on_trip
        self._samples = deque()
        self._overloaded_since = None
        self._consecutive = 0

    def reset(self):
        """Re-arm after the user (or a reload) has dealt with the cause."""
        self.tripped = False
        self.reason = None
        self.slowest = 0.0
        self.calls = 0
        self._samples.clear()
        self._overloaded_since = None
        self._consecutive = 0

    def record(self, elapsed, failed=False):
        """Fold one call into the history. True once the breaker has tripped."""
        if self.tripped:
            return True
        now = time.monotonic()
        self.slowest = max(self.slowest, elapsed)
        self.calls += 1
        if self.calls > 1 and elapsed >= self._stall:
            return self._trip(f"blocked the window for {elapsed:.1f}s in one call")
        if failed:
            self._consecutive += 1
            if self._consecutive >= self._failures:
                return self._trip(f"failed {self._consecutive} times in a row")
        else:
            self._consecutive = 0

        if self.calls == 1:
            # The first call warms everything up — Skia surface, typefaces, the
            # first blur — and is not representative of anything.
            return False
        # Keep the actual busy interval. Stamping a call only at its end and
        # then counting its whole duration made the first retained sample sit
        # outside the measured span, enough to turn a real 73% duty into a
        # reported 78% on slow draws.
        self._samples.append((now - elapsed, now))
        cutoff = now - self._window
        while self._samples and self._samples[0][1] <= cutoff:
            self._samples.popleft()

        # Qualify overload from a meaningful window, then keep a separate
        # clock. Any recovery below DUTY closes the episode; only one that
        # remains open continuously for WINDOW may remove the surface.
        span_start = max(cutoff, self._samples[0][0])
        span = now - span_start
        overloaded = False
        spent = 0.0
        if span >= self._window * 0.5 and len(self._samples) >= 4:
            spent = sum(
                max(0.0, end - max(start, span_start))
                for start, end in self._samples
            )
            overloaded = spent > span * self._duty
        if not overloaded:
            self._overloaded_since = None
            return False
        if self._overloaded_since is None:
            # Qualification opens the episode; it does not retroactively claim
            # the whole measurement window was overloaded. The surface gets a
            # complete WINDOW to recover from this point.
            self._overloaded_since = now
        continuous = now - self._overloaded_since
        if continuous >= self._window:
            return self._trip(
                f"used {spent / span:.0%} of the main thread continuously "
                f"for {continuous:.1f}s"
            )
        return False

    def _trip(self, reason):
        self.tripped = True
        self.reason = reason
        logger.error("Scene Agent %s disabled: %s", self.label, reason)
        if self._on_trip is not None:
            try:
                self._on_trip(reason)
            except Exception:
                logger.exception("Scene Agent %s trip handler failed", self.label)
        return True


def call(breaker, function, *args, **kwargs):
    """Run ``function`` under ``breaker``; return the exception, or None.

    The exception is handed back rather than logged so the caller can decide
    how loud to be — repeating a traceback per frame is part of what freezes
    Blender in the first place.
    """
    if breaker.tripped:
        return None
    start = time.monotonic()
    try:
        function(*args, **kwargs)
    except Exception as error:
        breaker.record(time.monotonic() - start, failed=True)
        return error
    breaker.record(time.monotonic() - start)
    return None


def timer(function, label, *, floor=1.0 / 120.0, breaker=None, on_stop=None):
    """Wrap an app-timer callback so it cannot wedge the main thread.

    The wrapper unregisters itself (by returning None) when the callback raises
    or when its breaker trips, and it refuses to be scheduled faster than
    ``floor`` — a callback that returns 0 forever is a busy loop with extra
    steps. ``on_stop`` runs on every path that unregisters, which is where a
    caller clears the "already running" flag it used to avoid double
    registration; without it a crashed timer can never be restarted.
    """
    guard = breaker if breaker is not None else Breaker(label)

    def stop():
        if on_stop is not None:
            try:
                on_stop()
            except Exception:
                logger.exception("Scene Agent %s timer cleanup failed", label)
        return None

    def run():
        if guard.tripped:
            return stop()
        start = time.monotonic()
        try:
            interval = function()
        except Exception:
            guard.record(time.monotonic() - start, failed=True)
            logger.exception("Scene Agent %s timer failed", label)
            return stop()
        if guard.record(time.monotonic() - start) or interval is None:
            return stop()
        return max(float(interval), floor)

    run.__name__ = getattr(function, "__name__", label)
    run.breaker = guard
    return run
