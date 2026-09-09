"""Reading what the backend said went wrong.

`_error_message` is the one place an FNF failure is turned into something a
person should read: the body's `detail` field, not the raw exception. Always
surface that. `_write_error_report` dumps the whole exchange to a file when
the detail is not enough to debug from.

These sat inside the realtime block for historical reasons and are shared by
`run_async`, the job pipeline and the explorer surface.
"""

import contextlib
import json
import logging
import time
import uuid

from . import diagnostics, paths

logger = logging.getLogger(__name__)


def _error_message(error):
    """Human-readable detail from an FNF error body, falling back to str()."""
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        for key in ("detail", "message", "error"):
            value = body.get(key)
            if isinstance(value, dict):
                # Validation envelopes nest the readable text one level down.
                text = value.get("text") or value.get("message")
                errors = value.get("errors")
                if isinstance(errors, list) and errors:
                    first = errors[0] if isinstance(errors[0], dict) else {}
                    loc = ".".join(str(part) for part in first.get("loc") or ())
                    msg = first.get("msg")
                    if msg:
                        detail = f"{loc}: {msg}" if loc else str(msg)
                        return f"{text} — {detail}" if text else detail
                if text:
                    return str(text)
            if value:
                return str(value)
    return str(error)


_ERROR_REPORT_BODY_LIMIT = 20000

# One file per failed request, and the diagnostics pack only ever ships the 20
# most recent — past this many the rest are dead weight in a folder the user is
# told is disposable.
_ERROR_REPORT_KEEP = 200


def _write_error_report(label, error):
    """Persist a shareable diagnostic file for a failed FNF API request.

    Captures the exact request/response pair plus the server ``request_id`` so
    a failure can be handed to the backend team without reproducing it. Returns
    the report path, or None when the error carries no API response (local
    errors have nothing useful to report).
    """
    response = getattr(error, "response", None)
    if response is None and getattr(error, "status", None) is None:
        return None
    report = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "label": str(label),
        # Blender's extension loader strips module-level bl_info at runtime.
        "addon_version": ".".join(
            map(str, (globals().get("bl_info") or {}).get("version", ()))
        )
        or "unknown",
        "message": str(error),
        "status": getattr(error, "status", None),
        "error_type": getattr(error, "error_type", None),
        "request_id": getattr(error, "request_id", None),
        "response_body": getattr(error, "body", None),
    }
    with contextlib.suppress(Exception):
        request = response.request if response is not None else None
        if request is not None:
            content = ""
            if request.content:
                content = request.content.decode("utf-8", "replace")
                if len(content) > _ERROR_REPORT_BODY_LIMIT:
                    content = content[:_ERROR_REPORT_BODY_LIMIT] + "…(truncated)"
                else:
                    with contextlib.suppress(Exception):
                        content = json.loads(content)
            report["request"] = {
                "method": str(request.method),
                "url": str(request.url),
                "body": content,
            }
    try:
        report_dir = paths.error_reports_dir()
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = report_dir / f"{stamp}_{uuid.uuid4().hex[:6]}.json"
        path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        paths.prune(report_dir, keep=_ERROR_REPORT_KEEP, pattern="*.json")
        diagnostics.event(
            "job",
            "error_report",
            label=label,
            path=path,
            request_id=getattr(error, "request_id", None),
        )
        return path
    except Exception:
        logger.exception("Could not write the FNF error report")
        return None
