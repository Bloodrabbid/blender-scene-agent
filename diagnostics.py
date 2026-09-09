# SPDX-FileCopyrightText: 2026 Higgsfield Inc.
# SPDX-License-Identifier: GPL-3.0-or-later

"""Diagnostics, session logging, and the support pack exporter."""

from __future__ import annotations

import contextlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import platform
import re
import sys
import threading
import time
import zipfile

from . import paths

# Prefer the live package name (Blender extensions load as
# ``bl_ext.user_default.blender_scene_agent``); fall back for direct imports.
ADDON_LOGGER_NAME = __package__ or "blender_scene_agent"

_file_handler: RotatingFileHandler | None = None
_sample_counters: dict[str, int] = {}
_sample_lock = threading.Lock()

# Cap HTTP body dumps so a large catalog / job payload cannot flood the log.
_HTTP_BODY_LOG_CHARS = 8000


def _addon_logger() -> logging.Logger:
    return logging.getLogger(ADDON_LOGGER_NAME)


# Redaction patterns
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9-_=]+\.[A-Za-z0-9-_=]+\.?[A-Za-z0-9-_.+/=]*")
_BEARER_RE = re.compile(r"(?i)bearer\s+[a-z0-9\-\._~\+\/]+=*")
_DATA_URI_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")

SENSITIVE_KEYS = {
    "access_token",
    "refresh_token",
    "id_token",
    "token",
    "authorization",
    "auth",
    "api_key",
    "secret",
    "password",
    "prompt",
    "negative_prompt",
    "user_prompt",
    "system_prompt",
    "text_prompt",
    "input_prompt",
}


def get_logs_dir() -> Path:
    return paths.logs_dir()


def get_current_log_path() -> Path:
    return paths.log_file()


_set_private_permissions = paths.harden


class _SelfHealingRotatingFileHandler(RotatingFileHandler):
    """A rotating log that survives its own directory being deleted.

    The log lives in a folder the user is explicitly invited to throw away, and
    the handler is the one thing in the add-on holding a path open across a
    whole session. On POSIX a delete leaves us writing into an unlinked inode:
    every line vanishes silently and the next rollover raises from inside
    `logging`. So before writing we check the file is still where we left it,
    and if it is not, drop the stale handle — `FileHandler.emit` reopens.

    The check is a `stat` and this logger is verbose, so it is throttled;
    losing a few seconds of lines to a folder the user just deleted is fine.
    """

    _RECHECK_INTERVAL_S = 5.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._next_check = 0.0

    def emit(self, record: logging.LogRecord) -> None:
        now = time.monotonic()
        if now >= self._next_check:
            self._next_check = now + self._RECHECK_INTERVAL_S
            with contextlib.suppress(OSError):
                if self.stream is not None and not os.path.exists(self.baseFilename):
                    stream, self.stream = self.stream, None
                    with contextlib.suppress(Exception):
                        stream.close()
                    Path(self.baseFilename).parent.mkdir(parents=True, exist_ok=True)
        super().emit(record)


def sanitize_val(val: object) -> str:
    """Sanitize a value for logging / breadcrumbs (strip home dir, tokens, data URIs)."""
    if val is None:
        return "none"
    s = str(val)
    home_str = str(Path.home())
    if home_str in s:
        s = s.replace(home_str, "~")
    s = _DATA_URI_RE.sub("[data URI redacted]", s)
    s = _JWT_RE.sub("[JWT REDACTED]", s)
    s = _BEARER_RE.sub("Bearer [REDACTED]", s)
    if len(s) > 500:
        s = s[:500] + "...(truncated)"
    return s


def redact_data(data: object) -> object:
    """Recursively redact sensitive keys/content from dicts, lists, and strings."""
    if isinstance(data, dict):
        cleaned = {}
        for k, v in data.items():
            k_lower = str(k).lower()
            if any(sk in k_lower for sk in SENSITIVE_KEYS):
                cleaned[k] = "[REDACTED]"
            else:
                cleaned[k] = redact_data(v)
        return cleaned
    if isinstance(data, list):
        return [redact_data(item) for item in data]
    if isinstance(data, str):
        return sanitize_val(data)
    return data


def setup_logging(session_info: dict | None = None) -> None:
    """Initialize the rotating file logger for this add-on's logger namespace.

    Logging is always DEBUG (verbose).
    """
    global _file_handler

    log_path = paths.log_file()

    # Close existing handler if re-initializing
    shutdown_logging()

    handler = _SelfHealingRotatingFileHandler(
        log_path,
        maxBytes=5 * 1024 * 1024,  # 5 MB
        backupCount=5,
        encoding="utf-8",
    )
    _set_private_permissions(log_path)

    formatter = logging.Formatter(
        "%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)
    handler.setLevel(logging.DEBUG)

    addon_logger = _addon_logger()
    addon_logger.setLevel(logging.DEBUG)
    # Avoid duplicate handlers if setup is called twice in one process.
    addon_logger.addHandler(handler)
    # Child loggers (``...analytics``, etc.) propagate here by default.
    addon_logger.propagate = False

    _file_handler = handler

    # Write session header
    _write_session_header(addon_logger, session_info)


def shutdown_logging() -> None:
    """Detach and close the file handler."""
    global _file_handler
    if _file_handler is not None:
        addon_logger = _addon_logger()
        addon_logger.removeHandler(_file_handler)
        with contextlib.suppress(Exception):
            _file_handler.flush()
            _file_handler.close()
        _file_handler = None


def _body_size(body: object) -> int | None:
    if body is None:
        return None
    if isinstance(body, (bytes, bytearray, str)):
        return len(body)
    return None


def _parse_json_body(body: object, content_type: str | None = None) -> object | None:
    """Return a parsed JSON value when the body is JSON I/O; otherwise None."""
    if isinstance(body, (dict, list)):
        return body
    ctype = (content_type or "").lower()
    looks_json_type = "json" in ctype
    if isinstance(body, (bytes, bytearray)):
        if not body:
            return None
        head = bytes(body[:1])
        if not looks_json_type and head not in (b"{", b"["):
            return None
        try:
            return json.loads(bytes(body).decode("utf-8"))
        except Exception:
            return None
    if isinstance(body, str):
        if not body:
            return None
        if not looks_json_type and body[:1] not in ("{", "["):
            return None
        try:
            return json.loads(body)
        except Exception:
            return None
    return None


def _format_json_body(value: object) -> str:
    text = json.dumps(redact_data(value), ensure_ascii=False, default=str)
    if len(text) > _HTTP_BODY_LOG_CHARS:
        return text[:_HTTP_BODY_LOG_CHARS] + f"...({len(text)} chars truncated)"
    return text


def log_http(
    direction: str,
    method: str,
    url: str,
    *,
    status: int | None = None,
    body: object = None,
    body_size: int | None = None,
    streaming: bool = False,
    content_type: str | None = None,
    error: object = None,
) -> None:
    """Always log one HTTP request/response line.

    JSON request/response bodies are redacted and written in full (with a size
    cap). Everything else (GLB uploads, images, HTML, empty) is a short
    ``body=<N bytes>`` summary so the log stays readable. Streaming request
    bodies must pass ``streaming=True`` / ``body_size`` — never read the stream.
    """
    parts = [
        f"[http] {direction}",
        f"{method.upper()} {sanitize_val(url)}",
    ]
    if status is not None:
        parts.append(f"status={status}")
    if streaming or body_size is not None:
        if body_size is not None:
            parts.append(f"body=<{body_size} bytes>")
        else:
            parts.append("body=<streaming>")
    else:
        parsed = _parse_json_body(body, content_type)
        if parsed is not None:
            parts.append(f"body={_format_json_body(parsed)}")
        else:
            size = _body_size(body)
            if size:
                parts.append(f"body=<{size} bytes>")
    if error is not None:
        parts.append(f"error={sanitize_val(error)}")
    _addon_logger().info(" ".join(parts))


def clear_logs(session_info: dict | None = None) -> Path:
    """Wipe the session log (+ rotated backups) and rewrite the session header."""
    log_path = paths.log_file()
    for path in log_path.parent.glob("blender.log.*"):
        with contextlib.suppress(OSError):
            path.unlink()

    if _file_handler is not None and getattr(_file_handler, "stream", None) is not None:
        stream = _file_handler.stream
        with contextlib.suppress(Exception):
            stream.flush()
            stream.seek(0)
            stream.truncate()
    else:
        log_path.write_text("", encoding="utf-8")
        _set_private_permissions(log_path)

    _write_session_header(_addon_logger(), session_info)
    event("diagnostics", "logs_cleared")
    return log_path


def _write_session_header(
    logger: logging.Logger, session_info: dict | None = None
) -> None:
    info = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "platform": platform.platform(),
        "python_version": sys.version.split()[0],
    }
    if session_info:
        info.update(session_info)

    header_lines = [
        "=== SCENE AGENT SESSION START ===",
        *[f"  {k}: {sanitize_val(v)}" for k, v in info.items()],
        "===============================================",
    ]
    for line in header_lines:
        logger.info(line)


def event(area: str, action: str, **fields) -> None:
    """Emit a structured INFO breadcrumbs log line."""
    field_strs = [f"{k}={sanitize_val(v)}" for k, v in fields.items()]
    msg = f"[breadcrumb] area={area} action={action}"
    if field_strs:
        msg += " " + " ".join(field_strs)
    _addon_logger().info(msg)


def sample(area: str, action: str, *, every_n: int = 30, **fields) -> None:
    """Rate-limited breadcrumb for hot loops."""
    key = f"{area}:{action}"
    with _sample_lock:
        count = _sample_counters.get(key, 0) + 1
        _sample_counters[key] = count
        should_emit = (count == 1) or (count % every_n == 0)

    if should_emit:
        fields["sample_count"] = count
        event(area, action, **fields)


def export_diagnostics_pack(
    target_zip_path: Path, session_info: dict | None = None
) -> Path:
    """Create a shareable, redacted zip pack containing logs, error reports, and meta."""
    target_zip_path = Path(target_zip_path)
    target_zip_path.parent.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(target_zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        # 1. Session logs
        logs_dir = get_logs_dir()
        if logs_dir.exists():
            for log_file in sorted(logs_dir.glob("blender.log*")):
                if log_file.is_file():
                    zf.write(log_file, arcname=f"session/{log_file.name}")

        # 2. MCP log
        mcp_log = paths.mcp_log_file()
        if mcp_log.exists() and mcp_log.is_file():
            zf.write(mcp_log, arcname=f"mcp/{mcp_log.name}")

        # 3. Error reports (last 20, redacted)
        reports_dir = paths.error_reports_dir()
        if reports_dir.exists():
            reports = sorted(
                [p for p in reports_dir.glob("*.json") if p.is_file()],
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )[:20]
            for report_path in reports:
                try:
                    raw_data = json.loads(report_path.read_text(encoding="utf-8"))
                    cleaned_data = redact_data(raw_data)
                    cleaned_json = json.dumps(
                        cleaned_data, indent=2, ensure_ascii=False
                    )
                    zf.writestr(f"error-reports/{report_path.name}", cleaned_json)
                except Exception as err:
                    zf.writestr(
                        f"error-reports/{report_path.name}.raw_err",
                        f"Failed to read/redact report: {err}",
                    )

        # 4. Meta JSON
        meta = {
            "exported_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
            "platform": platform.platform(),
            "python_version": sys.version.split()[0],
            "paths": {k: sanitize_val(v) for k, v in paths.describe().items()},
        }
        if session_info:
            meta.update(redact_data(session_info))
        zf.writestr("meta.json", json.dumps(meta, indent=2, ensure_ascii=False))

        # 5. README.txt
        readme_text = (
            "Scene Agent - Diagnostics Support Pack\n"
            "==================================================\n\n"
            "This archive contains anonymized log files and error reports to help diagnose issues.\n"
            "Attach this zip file when reporting a bug.\n"
        )
        zf.writestr("README.txt", readme_text)

    _set_private_permissions(target_zip_path)
    return target_zip_path
