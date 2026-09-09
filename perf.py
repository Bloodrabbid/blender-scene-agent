"""Performance telemetry for the Skia viewport surfaces.

The overlays run inline with Blender's UI, and the only machine we have ever
measured them on is a fast Mac. ``safety`` already answers "did a surface run
away with the main thread"; this answers the question underneath it — *how
expensive is the custom UI on the hardware people actually have, and which
phase is the expensive one*.

Three constraints shaped the design:

* **It runs on every frame**, so it cannot allocate. Each surface owns one
  reusable ``_Frame`` and one ``Record``; a draw clears a dict and adds a
  handful of floats. Measured at ~4 us a frame against a 0.31 ms cache-hit
  composer frame, which is inside the noise of the thing it is measuring.
* **It cannot send an event per frame.** Sixty events a second is not
  telemetry, it is a denial of service. Samples fold into fixed-edge
  histograms and leave as one rollup per surface per five minutes, which
  keeps percentiles and peaks without keeping samples.
* **The interesting number is rarely the mean.** A surface that averages 2 ms
  and stalls for 300 ms twice a minute is the one users call laggy, so the
  histogram carries p50/p90/p99, the peak, and explicit counts of frames over
  the 60fps, 30fps and outright-jank thresholds.

Phases are non-overlapping segments of one draw, so ``other`` — total minus
the sum of the phases — is a real signal: it is the cost nobody instrumented.

Read it live with ``perf.report()`` through ``scripts/bridge.py``, or from the
add-on preferences, which copies the same text to the clipboard.
``perf.set_enabled(False)`` takes the whole thing out of the frame — the escape
hatch this needs for the same reason ``safety`` exists, and the right way to
get it out of the way while profiling something else.
"""

from __future__ import annotations

import bisect
import logging
import os
import platform
import time

logger = logging.getLogger(__name__)

# Upper bucket edges in milliseconds. Dense where the budget lives (a 60fps
# frame is 16.6 ms) and coarse out in the tail, where the only question is how
# bad it got rather than exactly how bad.
EDGES = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.0,
    4.0,
    8.0,
    16.6,
    33.3,
    50.0,
    100.0,
    250.0,
    500.0,
    1000.0,
)

# Frames slower than these are counted on their own, because "3% of frames
# missed 60fps" is a sentence about lag and a mean is not.
BUDGET_60 = 16.6
BUDGET_30 = 33.3
JANK = 100.0

# A rollup this short says nothing and every one of them costs an event.
MIN_FRAMES = 120
# Anything at all is worth reporting when the session is ending and this is
# the last chance to say it.
MIN_FRAMES_FINAL = 20
FLUSH_INTERVAL = 300.0

_records = {}
_current = None
_facts = None
_deadline = 0.0
_enabled = True


class Histogram:
    """Streaming distribution over ``EDGES``: count, mean, peak, quantiles."""

    __slots__ = ("count", "total", "peak", "buckets")

    def __init__(self):
        self.count = 0
        self.total = 0.0
        self.peak = 0.0
        self.buckets = [0] * (len(EDGES) + 1)

    def add(self, value):
        self.count += 1
        self.total += value
        if value > self.peak:
            self.peak = value
        self.buckets[bisect.bisect_left(EDGES, value)] += 1

    @property
    def mean(self):
        return self.total / self.count if self.count else 0.0

    def quantile(self, fraction):
        """Interpolated within the bucket the rank lands in.

        Approximate by construction — the samples are gone — but the edges are
        tight enough around the frame budget that the answer is good to a
        millisecond where a millisecond matters. Capped at the peak, which is
        exact: a surface whose every frame lands in the first bucket would
        otherwise report a p99 above anything it ever actually did.
        """
        if not self.count:
            return 0.0
        target = fraction * self.count
        seen = 0
        for index, hits in enumerate(self.buckets):
            if hits and seen + hits >= target:
                low = EDGES[index - 1] if index else 0.0
                high = EDGES[index] if index < len(EDGES) else max(self.peak, EDGES[-1])
                return min(low + (high - low) * ((target - seen) / hits), self.peak)
            seen += hits
        return self.peak


class Record:
    """Everything known about one surface since the last rollup."""

    def __init__(self, name):
        self.name = name
        self.frame = _Frame(self)
        self.reset()

    def reset(self):
        self.started = time.monotonic()
        self.frames = 0
        self.failed = 0
        self.busy = 0.0
        self.over_60 = 0
        self.over_30 = 0
        self.jank = 0
        self.width = 0
        self.height = 0
        self.last = 0.0
        self.total = Histogram()
        self.gap = Histogram()
        self.phases = {}
        self.counters = {}

    def phase(self, name):
        histogram = self.phases.get(name)
        if histogram is None:
            histogram = self.phases[name] = Histogram()
        return histogram

    def summary(self):
        """Flat, Amplitude-shaped dict. Milliseconds throughout."""
        window = max(time.monotonic() - self.started, 1e-6)
        total = self.total
        props = {
            "surface": self.name,
            "window_s": round(window, 1),
            "frames": self.frames,
            "frames_failed": self.failed,
            "frames_per_s": round(self.frames / window, 1),
            "duty": round(self.busy / window, 4),
            "ms_mean": round(total.mean, 3),
            "ms_p50": round(total.quantile(0.5), 3),
            "ms_p90": round(total.quantile(0.9), 3),
            "ms_p99": round(total.quantile(0.99), 3),
            "ms_max": round(total.peak, 3),
            "over_60fps": self.over_60,
            "over_30fps": self.over_30,
            "jank": self.jank,
            "gap_ms_p50": round(self.gap.quantile(0.5), 1),
            "layer_w": self.width,
            "layer_h": self.height,
            "layer_mpx": round(self.width * self.height / 1e6, 3),
        }
        accounted = 0.0
        for name, histogram in self.phases.items():
            accounted += histogram.total
            props[f"ms_{name}"] = round(histogram.mean, 3)
            props[f"ms_{name}_max"] = round(histogram.peak, 3)
            # Of the surface's whole cost, not of a frame — a phase that only
            # runs on a re-raster has a misleading per-frame mean and an
            # honest share.
            props[f"share_{name}"] = (
                round(histogram.total / total.total, 3) if total.total else 0.0
            )
        if total.total:
            props["share_other"] = round(
                max(total.total - accounted, 0.0) / total.total, 3
            )
        for name, value in self.counters.items():
            props[name] = value
        props.update(facts())
        return props


class _Frame:
    """One draw, reused. Bound to its record so a frame never allocates."""

    __slots__ = ("record", "started", "phases", "width", "height", "failed", "owned")

    def __init__(self, record):
        self.record = record
        self.started = 0.0
        self.phases = {}
        self.width = 0
        self.height = 0
        self.failed = False
        self.owned = False

    def __enter__(self):
        global _current

        # Non-reentrant by nature — draw handlers do not nest — but a surface
        # that ever drew another surface would otherwise silently attribute
        # its cost twice. The inner frame measures nothing and stays out of
        # the way.
        self.owned = _current is None
        if self.owned:
            self.phases.clear()
            self.width = self.height = 0
            self.failed = False
            self.started = time.perf_counter()
            _current = self
        return self

    def __exit__(self, kind, value, traceback):
        global _current, _facts

        if not self.owned:
            return False
        _current = None
        elapsed = (time.perf_counter() - self.started) * 1000.0
        record = self.record
        now = time.monotonic()
        if record.last:
            record.gap.add(min((now - record.last) * 1000.0, 10000.0))
        record.last = now
        record.frames += 1
        record.busy += elapsed / 1000.0
        record.total.add(elapsed)
        if kind is not None or self.failed:
            record.failed += 1
        if elapsed >= BUDGET_60:
            record.over_60 += 1
            if elapsed >= BUDGET_30:
                record.over_30 += 1
                if elapsed >= JANK:
                    record.jank += 1
        for name, seconds in self.phases.items():
            record.phase(name).add(seconds * 1000.0)
        if self.width:
            record.width, record.height = self.width, self.height
        if _facts is None:
            # From inside a draw handler, where there is a live GPU context to
            # ask which renderer this actually is. Once, and never at the cost
            # of the frame it happens on.
            try:
                _facts = _capture_facts()
            except Exception:
                logger.debug("UI telemetry facts unavailable", exc_info=True)
                _facts = {}
        return False


def record(name):
    existing = _records.get(name)
    if existing is None:
        existing = _records[name] = Record(name)
    return existing


def frame(name):
    """Bracket one draw or one input event: ``with perf.frame("composer"):``."""
    if not _enabled:
        return _IDLE
    return record(name).frame


def now():
    return time.perf_counter()


def mark(phase, started):
    """Fold ``started`` → now into a phase of the open frame."""
    open_frame = _current
    if open_frame is None:
        return
    phases = open_frame.phases
    phases[phase] = phases.get(phase, 0.0) + (time.perf_counter() - started)


def count(name, amount=1):
    open_frame = _current
    if open_frame is None:
        return
    counters = open_frame.record.counters
    counters[name] = counters.get(name, 0) + amount


def pixels(width, height):
    """Note the layer this frame rasterized into; area is most of the cost."""
    open_frame = _current
    if open_frame is None:
        return
    if width * height > open_frame.width * open_frame.height:
        open_frame.width, open_frame.height = int(width), int(height)


def fail():
    """The draw raised. ``safety.call`` swallows it, so the wrapper says so."""
    open_frame = _current
    if open_frame is not None:
        open_frame.failed = True


class _Idle:
    """Stands in for a frame while telemetry is off; measures nothing."""

    __slots__ = ()

    def __enter__(self):
        return self

    def __exit__(self, kind, value, traceback):
        return False


_IDLE = _Idle()


def set_enabled(value):
    global _enabled
    _enabled = bool(value)


def _capture_facts():
    """The machine, once. This is the dimension every rollup is sliced by."""
    facts = {"cpu_count": os.cpu_count()}
    facts["os_release"] = platform.release()
    facts["cpu_arch"] = platform.machine()
    try:
        import gpu

        facts["gpu_backend"] = str(gpu.platform.backend_type_get())
        facts["gpu_vendor"] = str(gpu.platform.vendor_get())[:100]
        facts["gpu_renderer"] = str(gpu.platform.renderer_get())[:100]
    except Exception:
        logger.debug("GPU platform unavailable for UI telemetry", exc_info=True)
    try:
        import bpy

        system = bpy.context.preferences.system
        facts["ui_scale"] = round(float(system.ui_scale), 3)
        facts["pixel_size"] = float(getattr(system, "pixel_size", 1.0))
        facts["dpi"] = int(getattr(system, "dpi", 0))
    except Exception:
        logger.debug("Blender UI scale unavailable for UI telemetry", exc_info=True)
    try:
        from .features.overlays import skia_runtime

        module = skia_runtime.skia_module()
        facts["skia_version"] = getattr(module, "__version__", None) if module else None
    except Exception:
        pass
    return {key: value for key, value in facts.items() if value not in (None, "")}


def facts():
    global _facts

    if _facts is None:
        _facts = _capture_facts()
    return _facts


def flush(reason="interval", final=False):
    """Emit a rollup per surface that has drawn enough to be worth one."""
    from . import analytics, diagnostics

    floor = MIN_FRAMES_FINAL if final else MIN_FRAMES
    sent = 0
    for entry in _records.values():
        if entry.frames < floor:
            continue
        props = entry.summary()
        props["reason"] = reason
        entry.reset()
        sent += 1
        try:
            analytics.track(analytics.AnalyticsEvent.UIPerformance, props)
        except Exception:
            logger.debug("UI performance event failed", exc_info=True)
        try:
            diagnostics.event("ui_perf", entry.name, **props)
        except Exception:
            logger.debug("UI performance log failed", exc_info=True)
    return sent


def maybe_flush():
    """Cheap enough to call from the main-thread pump on every tick."""
    global _deadline

    moment = time.monotonic()
    if moment < _deadline:
        return
    first = _deadline == 0.0
    # The deadline moves before the flush, and the flush is guarded: this runs
    # on the pump that drains every async callback in the add-on, and telemetry
    # is not allowed to be the reason that stops.
    _deadline = moment + FLUSH_INTERVAL
    if first:
        return
    try:
        flush("interval")
    except Exception:
        logger.debug("UI performance flush failed", exc_info=True)


def surface_disabled(surface, reason, breaker=None):
    """A breaker pulled a surface — the loudest "this was laggy" signal there is."""
    from . import analytics, diagnostics

    entry = _records.get(surface)
    props = entry.summary() if entry is not None else {"surface": surface}
    props["reason"] = str(reason)[:200]
    if breaker is not None:
        props["breaker_calls"] = getattr(breaker, "calls", 0)
        props["breaker_slowest_ms"] = round(
            getattr(breaker, "slowest", 0.0) * 1000.0, 1
        )
    if entry is not None:
        entry.reset()
    try:
        analytics.track(analytics.AnalyticsEvent.UISurfaceDisabled, props)
    except Exception:
        logger.debug("UI surface disabled event failed", exc_info=True)
    try:
        diagnostics.event("ui_perf", "disabled", **props)
    except Exception:
        logger.debug("UI surface disabled log failed", exc_info=True)


def report():
    """The same numbers as plain text, for the bridge and the clipboard."""
    lines = []
    machine = facts()
    lines.append(
        "Scene Agent viewport UI performance  ·  {} {}  ·  {}".format(
            platform.system(),
            machine.get("os_release", ""),
            machine.get("gpu_renderer", "unknown GPU"),
        )
    )
    lines.append(
        "ui_scale {}  pixel_size {}  cpus {}  skia {}".format(
            machine.get("ui_scale", "?"),
            machine.get("pixel_size", "?"),
            machine.get("cpu_count", "?"),
            machine.get("skia_version", "?"),
        )
    )
    if not _records:
        lines.append("")
        lines.append("No surface has drawn yet.")
        return "\n".join(lines)
    header = (
        f"{'surface':<10}{'frames':>9}{'per s':>9}{'duty':>7}"
        f"{'mean':>8}{'p50':>8}{'p90':>8}{'p99':>8}{'max':>9}"
        f"{'>16ms':>8}{'jank':>6}{'fail':>6}"
    )
    lines.append("")
    lines.append(header)
    lines.append("-" * len(header))
    for entry in _records.values():
        if not entry.frames:
            continue
        row = entry.summary()
        lines.append(
            f"{row['surface']:<10}{row['frames']:>9}{row['frames_per_s']:>9.1f}"
            f"{row['duty'] * 100:>6.1f}%{row['ms_mean']:>8.2f}{row['ms_p50']:>8.2f}"
            f"{row['ms_p90']:>8.2f}{row['ms_p99']:>8.2f}{row['ms_max']:>9.1f}"
            f"{row['over_60fps']:>8}{row['jank']:>6}{row['frames_failed']:>6}"
        )
    for entry in _records.values():
        if not entry.frames or not entry.phases:
            continue
        layer = f" into {entry.width}x{entry.height}" if entry.width else ""
        lines.append("")
        lines.append(f"{entry.name} phases over {entry.frames} frames{layer}")
        ranked = sorted(entry.phases.items(), key=lambda item: -item[1].total)
        for name, histogram in ranked:
            share = histogram.total / entry.total.total if entry.total.total else 0.0
            lines.append(
                f"  {name:<10}{share * 100:>6.1f}%  mean {histogram.mean:>7.2f} ms"
                f"  max {histogram.peak:>7.1f} ms  n {histogram.count}"
            )
        accounted = sum(item.total for item in entry.phases.values())
        other = max(entry.total.total - accounted, 0.0)
        share = other / entry.total.total if entry.total.total else 0.0
        lines.append(f"  {'other':<10}{share * 100:>6.1f}%")
        if entry.counters:
            lines.append(
                "  "
                + "  ".join(f"{key} {value}" for key, value in entry.counters.items())
            )
    return "\n".join(lines)


def reset():
    global _facts

    for entry in _records.values():
        entry.reset()
    _facts = None
