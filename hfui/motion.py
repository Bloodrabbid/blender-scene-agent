"""Animation, kept out of the core the same way interaction is.

A surface describes itself from values, so animating means moving values over
time and rebuilding. ``Motion`` is a bag of named numbers that ease toward
targets:

    motion.to("open", 1.0, duration=0.22)
    t = motion.value("open")          # 0 -> 1 over 220ms
    if motion.tick():                 # something moved, ask for another redraw
        area.tag_redraw()

Nothing here reads a clock on its own except through ``now``, so a test can step
time by hand instead of sleeping.
"""

from __future__ import annotations

import time


def linear(t):
    return t


def ease_out(t):
    """Fast start, soft landing — the default for UI that reacts to a pointer."""
    return 1.0 - (1.0 - t) ** 3


def ease_in_out(t):
    """Symmetric cubic. Clamped, because `Loop` hands it an unbounded phase.

    A `_Track` only ever produces `t` in 0..1, but a loop's rise and fall are
    slices of a period and the arithmetic that maps them runs slightly past
    both ends. This is also a close enough stand-in for the
    `cubic-bezier(0.42, 0, 0.58, 1)` a browser runs that the difference across
    a two-second breath is not a thing anyone can see — which is what lets the
    Supercomputer's dots share it rather than carry their own copy.
    """
    if t <= 0.0:
        return 0.0
    if t >= 1.0:
        return 1.0
    return 4 * t * t * t if t < 0.5 else 1.0 - (-2 * t + 2) ** 3 / 2


def ease_out_back(t, overshoot=1.4):
    """Overshoots slightly before settling. Good for things that pop open."""
    scaled = t - 1.0
    return 1.0 + (overshoot + 1) * scaled**3 + overshoot * scaled**2


# Half of ``Motion.key``'s quantum. A track closer to its target than this is
# finished: letting it linger produced values like 0.0004 that ``key`` rounds
# to the settled 0.0 while the tree still sees "not quite zero" — and a node
# gated on ``value > 0`` (the launcher's View-all chip) then bakes a not-quite-
# settled layout into the cache under the settled key, where it sticks until an
# unrelated state change. See the launcher's stuck-label bug.
SNAP = 1e-3


class _Track:
    __slots__ = ("start", "target", "began", "duration", "easing")

    def __init__(self, start, target, began, duration, easing):
        self.start = start
        self.target = target
        self.began = began
        self.duration = duration
        self.easing = easing

    def at(self, now):
        if self.duration <= 0:
            return self.target, True
        progress = (now - self.began) / self.duration
        if progress >= 1.0:
            return self.target, True
        if progress <= 0.0:
            return self.start, False
        eased = self.easing(progress)
        value = self.start + (self.target - self.start) * eased
        if abs(value - self.target) < SNAP:
            return self.target, True
        return value, False


class Motion:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self._tracks = {}
        self._values = {}
        self.running = False

    def set(self, key, value):
        """Jump to a value with no animation."""
        self._values[key] = float(value)
        self._tracks.pop(key, None)

    def to(self, key, target, duration=0.2, easing=ease_out):
        """Ease toward ``target``. Re-aiming mid-flight starts from where it is."""
        target = float(target)
        current = self.value(key, target)
        if abs(current - target) < 1e-4:
            self.set(key, target)
            return
        existing = self._tracks.get(key)
        if existing is not None and abs(existing.target - target) < 1e-4:
            return
        self._tracks[key] = _Track(current, target, self.clock(), duration, easing)
        self.running = True

    def value(self, key, default=0.0):
        """The value as of the last ``tick`` — deliberately not live.

        A draw reads a track several times: once for the cache key, once or
        twice building the tree. Evaluating the clock on each read let those
        disagree by whatever time passed between them, and a disagreement that
        straddles ``key``'s rounding poisons the cache: a frame built from
        "almost settled" gets stored under the settled key and is never
        rebuilt. Values advance only in ``tick``, so one frame is one instant.
        """
        if key in self._values:
            return self._values[key]
        track = self._tracks.get(key)
        if track is not None:
            return track.start
        return default

    def tick(self):
        """Advance every track. Returns whether a redraw is still needed."""
        if not self._tracks:
            self.running = False
            return False
        now = self.clock()
        for key in list(self._tracks):
            value, done = self._tracks[key].at(now)
            self._values[key] = value
            if done:
                del self._tracks[key]
        self.running = bool(self._tracks)
        return True

    @property
    def key(self):
        """Cache key contribution: quantised so a still surface stops redrawing."""
        return tuple(
            (name, round(self.value(name), 3)) for name in sorted(self._values)
        )


class Loop:
    """A periodic animation, quantised into a whole number of steps.

    `Motion` is target-seeking and a loop has no target to hand it, so the
    breathing dots, the waiting ellipsis and the caret blink were each a plain
    function of `time.monotonic()`. They agreed on the arithmetic and not much
    else: `int((now % period) / period * steps) % steps` appeared twice, the
    interval between steps was re-derived from the same two constants in the
    pump, in *another file*, and turning a step back into seconds for the
    curve was a third spelling of it. Five places to keep in step, and nothing
    naming what they were about.

    **Quantising is not a detail of these animations, it is what makes them
    affordable.** A browser repainting a 16px nav span costs nothing; every
    frame here is a full `VIEW_3D` redraw of whatever the user is modelling.
    Left continuous, the Supercomputer's disc missed its tree cache and its
    texture cache on every single frame — 3.3 ms and 8.9% of the main thread,
    for as long as Blender is open. Rounding the phase makes the set of frames
    *finite*, and a finite loop is the one thing a cache serves perfectly:
    after one period every frame is a texture the `Layer` already holds. So
    the step count is a memory budget rather than a smoothness dial, and the
    pump wants to wake once per step and not once per frame.

    Time is absolute `monotonic`, with no origin recorded, because a loop has
    no beginning worth keeping. Pass `now` to step it by hand in a test.
    """

    __slots__ = ("period", "steps", "clock")

    def __init__(self, period, steps, clock=time.monotonic):
        self.period = float(period)
        self.steps = int(steps)
        self.clock = clock

    @property
    def interval(self):
        """How long one step lasts."""
        return self.period / self.steps

    def step(self, now=None):
        """Which step is showing: 0 to ``steps - 1``."""
        now = self.clock() if now is None else now
        return int((now % self.period) / self.interval) % self.steps

    def at(self, step):
        """Seconds into the period at the start of ``step``.

        The quantised clock, for a curve that wants to be asked in seconds.
        """
        return step * self.interval

    def phase(self, elapsed):
        """Where ``elapsed`` sits in the period, 0 to 1.

        Unquantised on purpose: a caller staggering several things off one
        clock shifts `elapsed` and asks again, and the quantisation has
        already happened upstream in whatever produced it.
        """
        return (elapsed % self.period) / self.period

    def due(self, now=None):
        """Seconds until the next step.

        **To the boundary, not a flat `interval`.** Sleeping a whole step from
        an arbitrary wake drifts against a phase that is read from absolute
        time, so a pump could tick twice inside one step or skip past one.
        This lands on the edge and stays there: an early wake sleeps the
        remainder and every wake after it is aligned.
        """
        now = self.clock() if now is None else now
        return self.interval - (now % self.interval)


def mix(a, b, t):
    return a + (b - a) * t


def mix_color(a, b, t):
    return tuple(mix(first, second, t) for first, second in zip(a, b))
