"""Background work, and how it reports on itself.

Four things that only make sense together: the queue Blender's main thread is
fed from, the `run_async` that feeds it, the registry of what is currently
running, and the transient toasts painted over the viewport. `run_async` writes
to all three, and the pump drains all three, so splitting them by noun would
mean three modules importing each other.

**`execute_queued_scripts` is the main-thread pump and must never be deleted.**
The name is a leftover from a localhost automation bridge that was removed;
what it does now is drain `main_thread_tasks`, expire reports and give `perf` a
chance to flush. Every `run_async` callback in the add-on arrives through it.
`watchdog_timer` re-registers it if an unhandled error in a callback ever takes
it down, so the async layer cannot silently die.

**Never touch bpy off the main thread.** That is the rule this module exists to
make easy: network work goes to `run_async`, results come back through
`run_on_main_thread`.

**The PEP 3110 trap lives in `run_async` and must be preserved.** `except
Exception as error:` *deletes* `error` when the block exits, so a lambda closing
over it dies with `NameError` later. Bind it first — `caught = error` — then
close over `caught`.
"""

import logging
import queue
import threading
import time

import bpy

from . import diagnostics, perf
from .blender.viewport import request_redraw
from .errors import _error_message, _write_error_report

logger = logging.getLogger(__name__)


# Callbacks scheduled by background AI workers, so scene mutation always
# happens on the main thread.
main_thread_tasks = queue.Queue()

# One entry per in-flight job, each with its own progress, so several
# generations can run at once instead of behind one global lock. Mutation is
# all on the main thread (workers marshal through run_on_main_thread), so the
# id counter is the only thing that needs a lock.
tasks = {}  # id -> {id, label, message, progress, status, time}
_task_counter = 0
_tasks_lock = threading.Lock()

reports = []  # list of {text, color, timeout, start}

REPORT_COLORS = {
    "INFO": (0.6, 1.0, 0.6, 1.0),
    "WARNING": (1.0, 0.8, 0.3, 1.0),
    "ERROR": (1.0, 0.45, 0.45, 1.0),
}


def new_task(label):
    """Register a running async task and return its id (any thread)."""
    global _task_counter
    with _tasks_lock:
        _task_counter += 1
        tid = f"task_{_task_counter}"
    tasks[tid] = {
        "id": tid,
        "label": label,
        "message": "starting",
        "progress": -1.0,  # -1 = indeterminate
        "status": "running",
        "time": time.time(),
    }
    logger.info("Scene Agent: %s started", label)
    request_redraw()
    return tid


def update_task(tid, *, message=None, progress=None, status=None):
    """Update a task's live state (main thread only)."""
    task = tasks.get(tid)
    if task is None:
        return
    if message is not None:
        task["message"] = message
    if progress is not None:
        task["progress"] = progress
    if status is not None:
        task["status"] = status
    request_redraw()


def remove_task(tid):
    tasks.pop(tid, None)
    request_redraw()


def any_task_running():
    return any(task["status"] == "running" for task in tasks.values())


def add_report(text, *, type="INFO", timeout=None):
    """Queue a floating viewport notification. A duplicate text refreshes the
    existing report's timer instead of stacking (main thread only)."""
    if timeout is None:
        timeout = 8.0 if type == "ERROR" else 4.0
    logger.info("Scene Agent report [%s]: %s", type, text)
    for rep in reports:
        if rep["text"] == text:
            rep["start"] = time.time()
            rep["timeout"] = timeout
            request_redraw()
            return
    reports.append(
        {
            "text": text,
            "color": REPORT_COLORS.get(type, REPORT_COLORS["INFO"]),
            "timeout": timeout,
            "start": time.time(),
        }
    )
    request_redraw()


def _prune_reports():
    """Drop expired reports. Returns True while any report remains."""
    if not reports:
        return False
    now = time.time()
    live = [rep for rep in reports if now - rep["start"] < rep["timeout"]]
    if len(live) != len(reports):
        reports[:] = live
    return bool(reports)


def run_on_main_thread(func):
    """Schedule a zero-arg callable to run on Blender's main thread."""
    main_thread_tasks.put(func)


def run_async(label, work, on_success=None, on_error=None, *, silent=False):
    """Run `work()` on a daemon thread; deliver result to the main thread.

    Multiple jobs may run concurrently; each gets its own entry in `tasks` with
    independent progress. `work(progress)` does the network/IO and may call
    progress(message_str) or progress(fraction_float). `on_success(value)` and
    `on_error(exc)` run on the main thread so they can safely touch bpy.

    ``silent=True`` skips the intra-panel task strip and toast reports (used for
    background Results polling / history refresh).
    """
    tid = None if silent else new_task(label)
    if not silent:
        diagnostics.event("job", "start", label=label, tid=tid)

    def progress(state):
        if tid is None:
            return
        # Background thread: marshal the update onto the main thread.
        if isinstance(state, (int, float)):
            run_on_main_thread(lambda: update_task(tid, progress=float(state)))
        else:
            run_on_main_thread(lambda: update_task(tid, message=str(state)))

    def runner():
        try:
            value = work(progress)
        except Exception as error:  # noqa: BLE001 - surfaced to the user
            logger.exception("%s failed", label)
            caught_error = error
            report_path = None if silent else _write_error_report(label, error)
            request_id = getattr(caught_error, "request_id", None)
            if not silent:
                diagnostics.event(
                    "job",
                    "failed",
                    label=label,
                    tid=tid,
                    request_id=request_id,
                    error=str(caught_error),
                    report_path=report_path,
                )

            def deliver_error():
                if tid is not None:
                    remove_task(tid)
                if not silent:
                    message = _error_message(caught_error)
                    request_id = getattr(caught_error, "request_id", None)
                    if request_id:
                        message = f"{message} · request {request_id}"
                    add_report(f"{label} failed: {message}", type="ERROR")
                    if report_path:
                        add_report(
                            f"Error report saved: {report_path}",
                            type="WARNING",
                            timeout=12.0,
                        )
                if on_error:
                    on_error(caught_error)

            run_on_main_thread(deliver_error)
            return

        def deliver_success():
            try:
                if not silent:
                    diagnostics.event("job", "success", label=label, tid=tid)
                if on_success:
                    on_success(value)
                if tid is not None:
                    remove_task(tid)
                if not silent:
                    add_report(f"{label}: done", type="INFO")
            except Exception as error:  # noqa: BLE001
                if tid is not None:
                    remove_task(tid)
                if not silent:
                    add_report(f"{label} import failed: {error}", type="ERROR")
                logger.exception("%s post-processing failed", label)

        run_on_main_thread(deliver_success)

    threading.Thread(target=runner, daemon=True).start()


def execute_queued_scripts():
    did_work = False
    # Drain main-thread callbacks scheduled by async AI workers.
    while not main_thread_tasks.empty():
        task = main_thread_tasks.get()
        did_work = True
        try:
            task()
        except Exception:
            logger.exception("Main-thread task failed")

    have_reports = _prune_reports()
    if have_reports:
        request_redraw()

    # One comparison against a deadline on most ticks; a rollup every five
    # minutes. This pump is the only thing that runs whether or not the
    # viewport is being drawn, which is exactly what a flush needs.
    perf.maybe_flush()

    # Adaptive cadence: stay hot while callbacks are landing, tick steadily
    # while jobs or reports are live, idle down otherwise to save CPU.
    if did_work:
        return 0.02
    if any_task_running() or have_reports:
        return 0.1
    return 0.3


@bpy.app.handlers.persistent
def watchdog_timer():
    """Re-register the worker timer if it ever stops (e.g. an unhandled error
    in a callback unregistered it), so the async layer can't silently die."""
    if not bpy.app.timers.is_registered(execute_queued_scripts):
        bpy.app.timers.register(execute_queued_scripts, persistent=True)
    return 5.0
