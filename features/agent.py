# SPDX-License-Identifier: GPL-3.0-or-later

"""The agent behind Scene Builder, running as the user's own CLI.

This is where the hosted Higgsfield agent used to be. Nothing here talks to a
Higgsfield service and nothing here holds a token: a turn is one child process
of the coding CLI the user already pays for — ``claude`` today, ``codex`` when
its wire format is wired up — and the only thing that reaches Blender is the
MCP server the add-on already runs on ``127.0.0.1:9876``.

The shape of a turn:

    add-on -> `claude -p --output-format stream-json` -> stdout JSON lines
                 |
                 +-> `python -m blmcp` (stdio MCP) -> TCP :9876 -> bpy

**The parts protocol is unchanged.** `conversation.py` was written against the
ai-sdk part dicts the hosted backend streamed (``text-delta``,
``tool-input-start``, ``tool-output-available``, ``finish``), and this module
keeps producing exactly those. That is deliberate: the panel, the tool rows,
the folding and the cancel path all still work, and the only thing that moved
is who is on the other end of the pipe.

Three things are load-bearing:

- **A page, not a part.** Every page the sink takes is a full re-raster of the
  chat surface, and a fast model emits hundreds of text deltas a second. Parts
  are batched on a `_FLUSH_INTERVAL` clock.
- **Text comes from the stream events, tools from the whole messages.** With
  ``--include-partial-messages`` the CLI sends both the deltas *and* the
  assembled `assistant` message. Reading text from both prints it twice, so
  text is taken from the deltas and the assembled message is read for its
  ``tool_use`` blocks alone.
- **Subagent output is dropped.** Anything carrying ``parent_tool_use_id`` is a
  nested agent talking to its parent, not to the user.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from .. import diagnostics, paths

# ---------------------------------------------------------------------------
# What the rest of the add-on still asks this module for
# ---------------------------------------------------------------------------

# The composer's model picker.
#
# Aliases first (`opus`, `sonnet`, `haiku`, `fable`), because the CLI resolves
# those to whatever is current — they keep pointing at the latest model without
# this file being touched. `[1m]` asks for the million-token context window.
# The dated ids below them are for pinning to a specific release.
#
# This list cannot be complete, and is not meant to be: a model that ships
# tomorrow goes in the add-on preferences' "Extra models" field and appears in
# the picker straight away. `Default` sends no `--model` at all, so the CLI's
# own configured model wins — which is the right answer for most people.
AUTO_MODEL = "default"
AUTO_MODEL_CHOICE = (AUTO_MODEL, "Default", "cli")

CLAUDE_MODELS = (
    AUTO_MODEL_CHOICE,
    ("opus", "Opus 5", "anthropic"),
    ("opus[1m]", "Opus 5 · 1M", "anthropic"),
    ("fable", "Fable 5.1", "anthropic"),
    ("fable[1m]", "Fable 5.1 · 1M", "anthropic"),
    ("sonnet", "Sonnet 5", "anthropic"),
    ("sonnet[1m]", "Sonnet 5 · 1M", "anthropic"),
    ("haiku", "Haiku 4.5", "anthropic"),
    ("opusplan", "Opus plan · Sonnet exec", "anthropic"),
    ("claude-opus-4-8", "Opus 4.8", "anthropic"),
    ("claude-opus-4-6", "Opus 4.6", "anthropic"),
    ("claude-opus-4-5", "Opus 4.5", "anthropic"),
    ("claude-sonnet-4-6", "Sonnet 4.6", "anthropic"),
    ("claude-sonnet-4-5", "Sonnet 4.5", "anthropic"),
)
CODEX_MODELS = (
    AUTO_MODEL_CHOICE,
    ("gpt-5.6-codex", "GPT-5.6 Codex", "openai"),
    ("gpt-5.6", "GPT-5.6", "openai"),
)

MODELS = CLAUDE_MODELS
MODEL = AUTO_MODEL

# Images staged onto one turn. The CLI reads them off disk by path, so this is
# a sanity bound rather than an upload limit.
MAX_MEDIAS = 14

# A turn with no output at all for this long is considered hung.
TURN_TIMEOUT = 900.0
# How often batched parts are handed to the sink.
_FLUSH_INTERVAL = 0.08
# Grace between terminate and kill when a turn is cancelled.
_KILL_GRACE_S = 2.0


class AgentError(RuntimeError):
    """The CLI could not be started, or ended a turn badly."""


# ---------------------------------------------------------------------------
# Providers
# ---------------------------------------------------------------------------

CLAUDE = "claude"
CODEX = "codex"

PROVIDERS = (
    (CLAUDE, "Claude Code", "claude"),
    (CODEX, "Codex", "codex"),
)

# Where a CLI is found when Blender was launched from the Finder and has none
# of the shell's PATH. Checked in order, after PATH itself.
_BINARY_HINTS = {
    CLAUDE: (
        "~/.local/bin/claude",
        "~/.claude/local/claude",
        "/opt/homebrew/bin/claude",
        "/usr/local/bin/claude",
        "~/.bun/bin/claude",
        "~/.npm-global/bin/claude",
        "%LOCALAPPDATA%/Programs/claude/claude.exe",
    ),
    CODEX: (
        "~/.local/bin/codex",
        "/opt/homebrew/bin/codex",
        "/usr/local/bin/codex",
        "~/.bun/bin/codex",
        "%LOCALAPPDATA%/Programs/codex/codex.exe",
    ),
}


def provider_models(provider):
    return CODEX_MODELS if provider == CODEX else CLAUDE_MODELS


def find_binary(provider, override=""):
    """The CLI's absolute path, or ``""``.

    An override from preferences wins and is not second-guessed — if the user
    named a path that is not there, the error should say that rather than
    silently running something else.
    """
    override = str(override or "").strip()
    if override:
        candidate = Path(os.path.expandvars(override)).expanduser()
        return str(candidate) if candidate.is_file() else ""
    found = shutil.which(provider)
    if found:
        return found
    for hint in _BINARY_HINTS.get(provider, ()):
        candidate = Path(os.path.expandvars(hint)).expanduser()
        if candidate.is_file():
            return str(candidate)
    return ""


# ---------------------------------------------------------------------------
# The MCP server the agent drives Blender through
# ---------------------------------------------------------------------------


def _extension_site_packages():
    """Blender's shared wheel directory for extensions.

    The same lookup `mcp_service` does for the exec bridge: the child is an
    ordinary interpreter and needs to be told where `blmcp` and its
    dependencies live.
    """
    for entry in sys.path:
        normalized = entry.replace("\\", "/")
        if "/extensions/.local/lib/" in normalized and normalized.endswith(
            "/site-packages"
        ):
            return entry
    return ""


def _child_python_path():
    parts = []
    site_packages = _extension_site_packages()
    if site_packages:
        parts.append(site_packages)
        if sys.platform == "win32":
            root = Path(site_packages)
            for relative in ("win32", "win32/lib", "pythonwin"):
                candidate = root / relative
                if candidate.is_dir():
                    parts.append(str(candidate))
    existing = os.environ.get("PYTHONPATH", "")
    if existing:
        parts.append(existing)
    return os.pathsep.join(parts)


MCP_SERVER_NAME = "blender"


def mcp_config(host="127.0.0.1", port=9876):
    """The ``--mcp-config`` payload: one stdio server, spoken to over the bridge.

    Blender's own interpreter runs it, which is what guarantees the bundled
    wheels are the ones imported. It never touches bpy — every call it makes
    goes down the TCP bridge to the add-on, which runs it on the main thread.
    """
    return {
        "mcpServers": {
            MCP_SERVER_NAME: {
                "command": sys.executable,
                "args": ["-m", "blmcp"],
                "env": {
                    "PYTHONPATH": _child_python_path(),
                    "BLENDER_MCP_HOST": str(host),
                    "BLENDER_MCP_PORT": str(port),
                    "PYTHONUNBUFFERED": "1",
                },
            }
        }
    }


SYSTEM_PROMPT = (
    "You are driving a live Blender session from inside Blender itself. The "
    f"`{MCP_SERVER_NAME}` MCP server is your hands: every change to the scene "
    "goes through its tools, which execute in the running Blender the user is "
    "looking at. Prefer its bpy execution tool over describing what to do, "
    "look at the scene before you change it, and keep replies short — they are "
    "read in a narrow panel over the viewport, not in a terminal.\n\n"
    "Code you send to the execution tool runs in a bare namespace: start every "
    "snippet with `import bpy` and import anything else you touch, or it fails "
    "with a NameError."
)

# Built-in CLI tools a scene agent has any use for. Shell and file writes are
# off unless the user turns them on in preferences: this agent is launched by
# clicking in a viewport, and a click is not consent to run commands.
SAFE_TOOLS = ("Read", "Glob", "Grep", "WebFetch", "WebSearch", "TodoWrite")
FULL_TOOLS = ("default",)


# ---------------------------------------------------------------------------
# The model picker (local; nothing to fetch)
# ---------------------------------------------------------------------------

_models_lock = threading.Lock()
_models = MODELS


def _parse_extra_models(raw):
    """The preferences' free-text model list, as picker rows.

    Comma or newline separated. ``id`` on its own, or ``id = Label`` when the
    id is not something anyone wants to read in a chip.
    """
    rows = []
    for chunk in re.split(r"[,\n]", str(raw or "")):
        entry = chunk.strip()
        if not entry:
            continue
        model_id, _, label = entry.partition("=")
        model_id = model_id.strip()
        if not model_id:
            continue
        rows.append((model_id, label.strip() or model_id, "anthropic"))
    return tuple(rows)


def set_provider_models(provider, extra=""):
    """Point the picker at the models the selected CLI understands."""
    global _models
    base = provider_models(provider)
    known = {model_id for model_id, _label, _provider in base}
    extras = tuple(
        row for row in _parse_extra_models(extra) if row[0] not in known
    )
    with _models_lock:
        _models = base + extras


def model_choices():
    with _models_lock:
        return _models


def default_model():
    return AUTO_MODEL


def remember_models(payload):
    """No catalog to remember: the CLI resolves its own aliases."""
    return False


def close():
    """Nothing is pooled — kept so lifecycle call sites read unchanged."""
    return None


# ---------------------------------------------------------------------------
# Odds and ends `conversation` and the composer still call
# ---------------------------------------------------------------------------

_REFERENCE_TOKEN = re.compile(
    r"<<<([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})>>>"
)


def display_reference_tokens(text):
    """History text as written. Reference tokens were a hosted-asset idea."""
    return str(text or "")


def reference_token_ids(text):
    return ()


def job_ids(output):
    """A local turn starts no hosted generations."""
    return []


def asks_user(name):
    """True when the tool is a parked questionnaire rather than an action."""
    lowered = str(name or "").lower().replace("-", "_")
    return "ask_user_question" in lowered or "askuserquestion" in lowered


# ---------------------------------------------------------------------------
# One turn
# ---------------------------------------------------------------------------


def working_dir(blend_path=""):
    """Where the CLI runs.

    Beside the .blend when the file has been saved, so the agent can read the
    textures and references that live with the project; otherwise a scratch
    directory of ours, so an unsaved scene never hands it the user's home.
    """
    blend_path = str(blend_path or "").strip()
    if blend_path:
        folder = Path(blend_path).expanduser().parent
        if folder.is_dir():
            return str(folder)
    return str(paths.state_dir("agent"))


class Session:
    """One thread's conversation with the CLI.

    ``session_id`` is the CLI's own id: the first turn names it, every turn
    after it resumes it. That is what makes the reply on screen and the
    transcript on disk the same conversation.
    """

    __slots__ = ("session_id", "cwd", "started")

    def __init__(self, session_id=None, cwd=""):
        self.session_id = str(session_id or "") or None
        self.cwd = cwd
        self.started = bool(session_id)


def _claude_argv(binary, session, text, *, model=None, mcp=None, full_access=False):
    argv = [
        binary,
        "--print",
        "--output-format",
        "stream-json",
        "--include-partial-messages",
        "--verbose",
        # Only the Blender server: the user's own connectors are their
        # business and have nothing to do with the scene in front of them.
        "--strict-mcp-config",
        "--mcp-config",
        json.dumps(mcp or mcp_config()),
        "--append-system-prompt",
        SYSTEM_PROMPT,
        "--permission-mode",
        "bypassPermissions",
        "--tools",
        ",".join(FULL_TOOLS if full_access else SAFE_TOOLS),
    ]
    if model and model != AUTO_MODEL:
        argv += ["--model", model]
    if session.session_id and session.started:
        argv += ["--resume", session.session_id]
    else:
        if not session.session_id:
            session.session_id = str(uuid.uuid4())
        argv += ["--session-id", session.session_id]
    argv.append(text)
    return argv


def _spawn(argv, cwd):
    """Start the CLI with a pipe on stdout and stderr kept separate.

    ``stdin`` is closed rather than inherited: a CLI that decides to prompt
    should fail at once instead of blocking a turn forever on a terminal that
    is not there.
    """
    env = os.environ.copy()
    # The child is a coding CLI, not a Blender process. Blender's own Python
    # environment leaking in confuses its own runtime.
    for key in ("PYTHONPATH", "PYTHONHOME"):
        env.pop(key, None)
    env["CLAUDE_CODE_ENTRYPOINT"] = "blender-scene-builder"
    creationflags = 0
    if sys.platform == "win32":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        return subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            close_fds=True,
            creationflags=creationflags,
        )
    except FileNotFoundError as error:
        raise AgentError(f"{argv[0]} is not there.") from error
    except OSError as error:
        raise AgentError(f"The agent CLI could not start: {error}") from error


def _tool_result_output(block):
    """A ``tool_result`` block as something `conversation._summarize` reads.

    Two shapes matter to the summariser: a dict with an ``error`` key is the
    only thing it marks a row failed for, and a dict it can find nothing to say
    about produces no detail at all — which is the right answer for a tool that
    simply worked. The bridge answers `{"status": "ok", "result": {...}}`, and
    printing that verbatim in a 34-character column is worse than printing
    nothing, so a successful envelope is reduced to exactly that: nothing.
    """
    content = block.get("content")
    text = ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        pieces = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                pieces.append(str(item.get("text") or ""))
            elif isinstance(item, dict) and item.get("type") == "image":
                pieces.append("image")
        text = "\n".join(piece for piece in pieces if piece)
    if block.get("is_error"):
        return {"error": _bridge_error(text) or text or "failed"}
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            envelope = json.loads(stripped)
        except ValueError:
            return text
        if isinstance(envelope, dict) and "status" in envelope:
            if str(envelope.get("status")).lower() == "ok":
                return {"status": "ok"}
            return {"error": _bridge_error(stripped) or "failed"}
    return text


def _bridge_error(text):
    """The readable half of a bridge error envelope, if that is what this is."""
    stripped = str(text or "").strip()
    if not stripped.startswith("{"):
        return ""
    try:
        envelope = json.loads(stripped)
    except ValueError:
        return ""
    if not isinstance(envelope, dict):
        return ""
    for key in ("message", "error", "detail"):
        value = envelope.get(key)
        if isinstance(value, str) and value.strip():
            return _error_line(value)
    return ""


def _error_line(text):
    """The one line of a failure worth putting in a 34-character row.

    A bpy failure arrives as a whole traceback, whose *first* line is always
    the word "Traceback" — which says nothing at all. The exception is the last
    line, and that is the one that tells you what went wrong.
    """
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    if lines[0].startswith("Traceback"):
        return lines[-1]
    return lines[0]


class _Pump:
    """Parts on their way to the panel, batched on a clock."""

    def __init__(self, sink):
        self._sink = sink
        self._parts = []
        self._names = {}
        self._last = 0.0

    def add(self, part):
        self._parts.append(part)

    def name(self, call_id, tool_name):
        if call_id and tool_name:
            self._names[call_id] = tool_name

    def flush(self, force=False):
        if not self._parts:
            return
        now = time.monotonic()
        if not force and now - self._last < _FLUSH_INTERVAL:
            return
        parts, self._parts = self._parts, []
        self._last = now
        self._sink(parts, dict(self._names))


def run_turn(
    session,
    text,
    sink,
    cancelled=None,
    *,
    model=None,
    provider=CLAUDE,
    binary="",
    attachments=(),
    full_access=False,
    mcp=None,
    on_start=None,
):
    """Run one turn to its end, handing pages of parts to ``sink``.

    Returns the CLI session id, so the next turn resumes this conversation.
    Raises `AgentError` before anything is streamed when the CLI is missing;
    once a turn is running, a failure arrives as an ``error`` part instead, so
    the panel shows it where the question was asked.

    ``on_start`` is handed the child process. Stop is a signal to the process,
    not a flag the reader checks: this loop blocks on the CLI's stdout, and a
    model that is thinking sends nothing for minutes — a Stop that only set a
    flag would not be felt until the answer it was cancelling arrived.
    """
    stop = cancelled or (lambda: False)
    if provider == CODEX:
        raise AgentError(
            "Codex is not wired up yet — pick Claude Code in the add-on "
            "preferences."
        )
    binary = binary or find_binary(provider)
    if not binary:
        raise AgentError(
            f"The {provider} CLI was not found. Install it, or set its path in "
            "the add-on preferences."
        )

    prompt = str(text or "")
    for path in attachments or ():
        prompt += f"\n\nAttached file: {path}"

    resumed = session.started
    finished, message = _stream_turn(
        session,
        prompt,
        sink,
        stop,
        binary=binary,
        model=model,
        mcp=mcp or mcp_config(),
        full_access=full_access,
        on_start=on_start,
    )
    if not finished and not stop() and resumed and _session_is_gone(message):
        # The CLI no longer has this conversation — its transcript was cleared,
        # or it was started somewhere this CLI cannot see. Resuming it will
        # fail forever, so the thread would be dead from here on. Answer the
        # question that was actually asked, in a new session, and let the
        # panel carry on.
        diagnostics.event("agent", "session-lost")
        session.session_id = None
        session.started = False
        finished, message = _stream_turn(
            session,
            prompt,
            sink,
            stop,
            binary=binary,
            model=model,
            mcp=mcp or mcp_config(),
            full_access=full_access,
            on_start=on_start,
        )
    if stop():
        return session.session_id
    if not finished:
        diagnostics.event("agent", "turn-failed", error=str(message)[:200])
        sink([{"type": "error", "errorText": message}], {})
    return session.session_id


_SESSION_GONE = "no conversation found with session id"


def _session_is_gone(message):
    return _SESSION_GONE in str(message or "").lower()


def _stream_turn(
    session, prompt, sink, stop, *, binary, model, mcp, full_access, on_start
):
    """Run the CLI once. Returns ``(finished, message)``.

    ``finished`` is True only when the CLI reached its own ``result`` event
    without an error; ``message`` is what to say when it did not.
    """
    argv = _claude_argv(
        binary, session, prompt, model=model, mcp=mcp, full_access=full_access
    )
    diagnostics.event("agent", "turn-start", resumed=session.started)
    process = _spawn(argv, session.cwd or working_dir())
    if on_start is not None:
        on_start(process)

    errors = []

    def drain_stderr():
        with contextlib.suppress(Exception):
            for line in process.stderr:
                piece = line.decode("utf-8", "replace").strip()
                if piece:
                    errors.append(piece)

    stderr_thread = threading.Thread(target=drain_stderr, daemon=True)
    stderr_thread.start()

    pump = _Pump(sink)
    finished = False
    failure = ""
    deadline = time.monotonic() + TURN_TIMEOUT
    try:
        for raw in process.stdout:
            if stop():
                break
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                if time.monotonic() > deadline:
                    break
                continue
            deadline = time.monotonic() + TURN_TIMEOUT
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("parent_tool_use_id"):
                # A subagent talking to its parent, not to the user.
                continue
            done, failed = _handle(event, pump, session)
            if failed:
                failure = failed
            finished = done or finished
            pump.flush()
            if finished or failure:
                break
    finally:
        pump.flush(force=True)
        if process.poll() is None:
            with contextlib.suppress(Exception):
                process.terminate()
                process.wait(timeout=_KILL_GRACE_S)
            if process.poll() is None:
                with contextlib.suppress(Exception):
                    process.kill()
        with contextlib.suppress(Exception):
            process.stdout.close()
        stderr_thread.join(timeout=1.0)
        with contextlib.suppress(Exception):
            process.stderr.close()

    if finished:
        return True, ""
    message = (
        failure
        or " ".join(errors[-3:]).strip()
        or f"The agent CLI stopped (exit {process.poll()})."
    )
    return False, message


def _handle(event, pump, session):
    """One CLI event.

    Returns ``(finished, failure)``: ``finished`` when the turn ended cleanly,
    ``failure`` as the message when it ended badly. The failure is handed back
    rather than pushed into the panel here, because a lost session is a failure
    the caller can recover from by starting a new one.
    """
    kind = event.get("type")

    if kind == "system":
        if event.get("subtype") == "init":
            # The turn is live and the CLI has named the conversation. Only
            # here is the id worth keeping: one made up before the spawn names
            # a transcript that may never exist, and resuming it later fails.
            got = str(event.get("session_id") or "").strip()
            if got:
                session.session_id = got
                session.started = True
        return False, ""

    if kind == "stream_event":
        inner = event.get("event") or {}
        if inner.get("type") == "content_block_delta":
            delta = inner.get("delta") or {}
            if delta.get("type") == "text_delta":
                pump.add({"type": "text-delta", "delta": delta.get("text") or ""})
        return False, ""

    if kind == "assistant":
        for block in (event.get("message") or {}).get("content") or ():
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            call_id = block.get("id")
            name = block.get("name")
            pump.name(call_id, name)
            pump.add(
                {"type": "tool-input-start", "toolCallId": call_id, "toolName": name}
            )
            pump.add(
                {
                    "type": "tool-input-available",
                    "toolCallId": call_id,
                    "toolName": name,
                    "input": block.get("input"),
                }
            )
        return False, ""

    if kind == "user":
        for block in (event.get("message") or {}).get("content") or ():
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            pump.add(
                {
                    "type": "tool-output-available",
                    "toolCallId": block.get("tool_use_id"),
                    "output": _tool_result_output(block),
                }
            )
        return False, ""

    if kind == "result":
        if event.get("is_error"):
            # `errors` carries the reason when the run never got far enough to
            # produce a `result` string — a lost session is the common one.
            reported = event.get("errors")
            detail = ""
            if isinstance(reported, list) and reported:
                detail = " ".join(str(item) for item in reported if item)
            return False, (detail or str(event.get("result") or "").strip()
                           or "The agent failed.")
        got = str(event.get("session_id") or "").strip()
        if got:
            session.session_id = got
            session.started = True
        pump.add({"type": "finish"})
        return True, ""

    return False, ""


# ---------------------------------------------------------------------------
# Readiness
#
# Scene Builder used to be gated on a hosted MCP connector being installed and
# OAuthed — `features/bridge_gate.py`, which polled a connector list and opened
# a browser. The gate is still there because the composer draws it, but what it
# now asks is the only question left: is there a CLI on this machine to run?
# ---------------------------------------------------------------------------

READY = "ready"
MISSING = "missing"

_gate = {"status": MISSING, "error": "", "binary": ""}


def _preferences():
    try:
        from ..props import addon_preferences

        return addon_preferences()
    except Exception:  # noqa: BLE001 — during register there may be none
        return None


def refresh_gate():
    """Look for the CLI and remember what was found."""
    prefs = _preferences()
    provider = getattr(prefs, "agent_provider", CLAUDE) if prefs else CLAUDE
    override = getattr(prefs, "agent_binary", "") if prefs else ""
    binary = find_binary(provider, override)
    _gate["binary"] = binary
    if binary:
        _gate["status"] = READY
        _gate["error"] = ""
    else:
        _gate["status"] = MISSING
        _gate["error"] = (
            f"No {provider} executable on PATH."
            if not override
            else f"Nothing at {override}."
        )
    set_provider_models(provider, getattr(prefs, "agent_extra_models", "") if prefs else "")
    return _gate["status"] == READY


def ready():
    return _gate["status"] == READY


def status():
    return _gate["status"]


def error():
    return _gate["error"]


def binary():
    return _gate["binary"]


def start():
    """Called once the add-on is up; cheap enough to be idempotent."""
    refresh_gate()


def ensure():
    """Scene Builder was opened. Look again — a CLI may have been installed."""
    if not ready():
        refresh_gate()


def act():
    """The gate's button: look again, then send them to preferences."""
    if refresh_gate():
        return
    import bpy

    with contextlib.suppress(Exception):
        bpy.ops.scene_agent.open_prefs()


def stop():
    _gate.update({"status": MISSING, "error": "", "binary": ""})
