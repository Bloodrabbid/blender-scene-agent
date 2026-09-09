"""The thread of messages, and the turn in flight.

This is what the user is saying to the agent and what it has said back. It lived in `overlays/chat_surface.py` for as long as the panel was the
only thing that could see it, which made a draw module the owner of a network
conversation: three hundred lines of poll pages, tool bookkeeping and job
following sat above the caret handling, and the surface's teardown was also
the only thing that could interrupt a running turn.

`agent.py` next door is the transport — the user's own coding CLI as a child
process, and its stream translated into parts. This is the state that
translation produces, and the main-thread rules that go with it:

- **Every `Message` is written here and only here.** The worker hands whole
  pages over `run_on_main_thread` and touches nothing else; `apply` is the
  single writer.
- **A turn is cancelled by killing its process**, and `cancelled` is set first
  so the pages already queued behind it land nowhere. The flag alone is not
  enough: the worker blocks on the CLI's stdout, which says nothing at all
  while the model is thinking.
- **The panel is a listener, not the owner.** `on_change` is how a landing
  page reaches the surface, so the conversation does not know a surface
  exists — which is what lets a turn keep running while the panel is shut,
  and lets teardown drop the panel without dropping the answer.
"""

from __future__ import annotations

import threading
import time

from ..hfui import chat as chat_module
from ..props import addon_preferences
from ..work import run_async, run_on_main_thread
from . import agent

# What a tool call is called in one line, and the glyph beside it. Matched on
# substrings of the backend's own tool name, longest-meaning first —
# a name carrying both words has to find the specific entry before the bare
# generate one. The label is a gerund and stays put for the life of the row:
# **the trailing ellipsis is what says it is still running**, and the detail
# after the middot is how it went.
_TOOL_LOOKS = (
    # The Blender bridge's own tools come first: their names contain words the
    # generic rules below would misread — `get_blendfile_summary_*` would match
    # the "file" rule and read "Reading" for what is really a look at the scene.
    (("execute_blender_code",), "Running code", "settings/prompt"),
    (("screenshot", "viewport_to_path", "thumbnail"), "Looking", "settings/capture"),
    (("render",), "Rendering", "settings/clapboard"),
    (
        ("objects_summary", "object_detail", "blendfile_summary"),
        "Reading the scene",
        "search",
    ),
    (("jump_to",), "Navigating", "search"),
    (("api_docs", "manual_docs"), "Reading docs", "settings/file"),
    (
        ("generate_image", "image_generation", "to_image", "image_edit"),
        "Generating image",
        "image-sparkle",
    ),
    (
        ("generate_video", "video_generation", "to_video", "lipsync"),
        "Generating video",
        "settings/clapboard",
    ),
    (("generate_3d", "to_3d", "mesh", "rig"), "Generating 3D", "settings/mesh"),
    (("speech", "audio", "music", "voice"), "Generating audio", "settings/audio"),
    # Ahead of the bare generate rule: a `models_explore` call is
    # the agent picking a model, not a generation, and reading "Generating"
    # twice for one image is a row that lies about what it cost.
    (
        ("models_explore", "model_list", "catalog", "options"),
        "Browsing models",
        "search",
    ),
    (("search", "browse", "fetch", "web"), "Searching", "search"),
    (
        ("skill", "read", "view", "list", "file", "storage", "memory"),
        "Reading",
        "settings/file",
    ),
    (("write", "edit", "save", "upload"), "Writing", "settings/prompt"),
    (("question", "ask", "clarify"), "Asking", "settings/prompt"),
    (("generate", "create"), "Generating", "settings/sparkles-three"),
)

# A whole tool output on one line, or nothing. Nothing is the common answer and
# the right one: a row with no detail and no ellipsis reads as "this ran, it
# was fine", which is all most calls are worth in a 356pt column.
_DETAIL_LIMIT = 34


def _tool_look(name):
    """``(label, icon, server)`` for a backend tool name.

    MCP tools arrive as ``mcp__<server>__<tool>``: the server is split off for
    the row to print after the label — the way the web chat shows
    "Bl screenshot  adobe_connector" — and the look is matched on the tool's
    own name, so ``mcp__adobe_connector__bl_generate_image`` still reads
    "Generating image".
    """
    lowered = str(name or "").lower()
    server = ""
    if lowered.startswith("mcp__"):
        head, _, tail = lowered.removeprefix("mcp__").partition("__")
        if tail:
            server, lowered = head, tail
    for keys, label, icon in _TOOL_LOOKS:
        if any(key in lowered for key in keys):
            return label, icon, server
    words = lowered.replace("_", " ").strip()
    return (
        (words[:1].upper() + words[1:]) if words else "Working",
        "settings/sparkles-three",
        server,
    )


def _shorten(value):
    text = " ".join(str(value or "").split())
    if len(text) <= _DETAIL_LIMIT:
        return text
    return text[: _DETAIL_LIMIT - 1].rstrip() + "…"


def _summarize(output):
    """``(detail, failed)`` for a ``tool-output-available``."""
    if isinstance(output, dict):
        for key in ("error", "error_message", "errorText"):
            value = output.get(key)
            if value:
                return _shorten(value), True
        if str(output.get("status") or "").lower() in {"failed", "error"}:
            return "failed", True
        jobs = agent.job_ids(output)
        if jobs:
            return ("queued" if len(jobs) == 1 else f"{len(jobs)} queued"), False
        for key in ("summary", "message", "text", "title"):
            value = output.get(key)
            if isinstance(value, str) and value.strip():
                return _shorten(value), False
        return "", False
    if isinstance(output, (list, tuple)):
        return (f"{len(output)} results" if len(output) != 1 else ""), False
    if isinstance(output, str) and output.strip():
        return _shorten(output.splitlines()[0]), False
    return "", False


def _option_label(option):
    if isinstance(option, str):
        return option.strip()
    if isinstance(option, dict):
        for key in ("label", "title", "text", "id"):
            value = option.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _parse_ask(payload):
    """``ask_user_question`` input as ``AskItem``s. Empty if it is not one."""
    if not isinstance(payload, dict):
        return []
    raw = payload.get("questions") or payload.get("items") or ()
    items = []
    for entry in raw:
        if isinstance(entry, str) and entry.strip():
            items.append(AskItem(entry.strip(), ()))
            continue
        if not isinstance(entry, dict):
            continue
        title = ""
        for key in ("question", "header", "title", "prompt"):
            value = entry.get(key)
            if isinstance(value, str) and value.strip():
                title = value.strip()
                break
        options = []
        for option in entry.get("options") or entry.get("choices") or ():
            label = _option_label(option)
            if label:
                options.append(label)
        if title:
            items.append(AskItem(title, tuple(options)))
    return items


class AskItem:
    """One question in a parked ``ask_user_question``."""

    __slots__ = ("title", "options", "selected")

    def __init__(self, title, options=()):
        self.title = title
        self.options = tuple(options)
        self.selected = None


class Ask:
    """A questionnaire the worker is blocked on until the user answers."""

    __slots__ = (
        "call_id",
        "tool_name",
        "questions",
        "index",
        "event",
        "result",
        "done",
        "custom",
    )

    def __init__(self, call_id, tool_name, questions):
        self.call_id = call_id
        self.tool_name = tool_name
        self.questions = list(questions)
        self.index = 0
        self.event = threading.Event()
        self.result = None
        self.done = False
        # True after "tell what to do differently": the questionnaire
        # stands down and the prompt comes back so they can type.
        self.custom = False

    def current(self):
        if not self.questions:
            return None
        self.index = max(0, min(self.index, len(self.questions) - 1))
        return self.questions[self.index]

    def answers(self):
        rows = []
        for item in self.questions:
            selected = ""
            if item.selected is not None and 0 <= item.selected < len(item.options):
                selected = item.options[item.selected]
            rows.append({"question": item.title, "selected": selected})
        return rows

    def snapshot(self):
        item = self.current()
        if item is None or self.done:
            return None
        return chat_module.AskState(
            item.title,
            item.options,
            item.selected,
            self.index + 1,
            len(self.questions),
            self.custom,
        )


def _wait_ask(turn, call_id, cancelled):
    """Block the worker until the user answers, skips, or the turn dies."""
    until = time.monotonic() + 8.0
    while turn.ask is None or turn.ask.call_id != call_id:
        if cancelled() or time.monotonic() > until:
            return None
        time.sleep(0.05)
    ask = turn.ask
    while not ask.event.wait(0.15):
        if cancelled():
            return None
    return ask.result


def messages_from_backend(payload):
    """A ``GET /chats/{id}/messages`` page as panel ``Message``s, oldest first.

    The web keeps the full ai-sdk part list; the panel keeps what it can show.
    Text parts join into the message body; tool parts become the same one-line
    rows a live turn produces, folded by label exactly as ``Turn.row`` folds
    them; reasoning and step markers are dropped. A row without either is
    dropped whole — the backend pads history with system bookkeeping.
    """
    stamped = []
    for entry in (payload or {}).get("results") or ():
        if not isinstance(entry, dict):
            continue
        role = entry.get("role")
        if role not in (chat_module.USER, chat_module.ASSISTANT):
            continue
        texts = []
        tools = []
        for part in entry.get("parts") or ():
            if not isinstance(part, dict):
                continue
            kind = str(part.get("type") or "")
            if kind == "text":
                value = agent.display_reference_tokens(
                    part.get("text") or ""
                ).strip()
                if value:
                    texts.append(value)
            elif kind.startswith("tool-") or kind == "dynamic-tool":
                name = part.get("toolName") or kind.removeprefix("tool-")
                label, icon, server = _tool_look(name)
                if tools and tools[-1].label == label and tools[-1].server == server:
                    tools[-1].count += 1
                else:
                    tools.append(
                        chat_module.Tool(
                            part.get("toolCallId"),
                            label,
                            icon,
                            status=chat_module.TOOL_DONE,
                            server=server,
                        )
                    )
        if not texts and not tools:
            continue
        failed = str(entry.get("status") or "").lower() in {"failed", "error"}
        stamped.append(
            (
                str(entry.get("created_at") or ""),
                chat_module.Message(
                    role, "\n\n".join(texts), tools=tools, failed=failed
                ),
            )
        )
    # One page, in time order. ISO timestamps sort as strings; the page's own
    # order is not documented and not relied on.
    stamped.sort(key=lambda pair: pair[0])
    return [message for _stamp, message in stamped]


class Turn:
    """One question and the reply streaming back for it.

    `cancelled` is read by the worker between polls and written by the main
    thread; everything else belongs to the main thread alone.
    """

    __slots__ = (
        "message",
        "cancelled",
        "chat",
        "tools",
        "job_tools",
        "started",
        "ask",
        "workspace_id",
        "process",
    )

    def __init__(self, message, chat_id, workspace_id=None):
        self.message = message
        self.cancelled = False
        self.chat = chat_id
        # Every request in this turn stays on the workspace where it began,
        # even if the account picker changes while the worker is polling.
        self.workspace_id = str(workspace_id or "") or None
        # When the question went up, for the thread's "Working for 9m 18s".
        self.started = time.time()
        # `toolCallId` → the row showing it. The backend names a tool once, on
        # `tool-input-start`, so both the approval and the output read it here.
        self.tools = {}
        # `job id` → the row that started it, for the follower to finish.
        self.job_tools = {}
        # A parked ``ask_user_question``. The worker waits on its event.
        self.ask = None
        # The CLI child running this turn, once it is up. Stop kills it.
        self.process = None

    def row(self, call_id, name=None):
        """The row for a tool call, made on first sight of the id.

        A call that repeats what the row above it already says folds into it
        as a count rather than starting a new line — the agent reads three of
        its own skill docs before it does anything, and three lines of
        "Reading" is the machinery crowding out the answer.
        """
        if not call_id:
            return None
        tool = self.tools.get(call_id)
        if tool is not None:
            return tool
        label, icon, server = _tool_look(name)
        rows = self.message.tools
        if (
            rows
            and rows[-1].label == label
            and rows[-1].server == server
            and not rows[-1].detail
        ):
            tool = rows[-1]
            tool.count += 1
        else:
            tool = chat_module.Tool(call_id, label, icon, server=server)
            rows.append(tool)
        self.tools[call_id] = tool
        return tool


class Conversation:
    """One thread with the agent, and the files staged for the next send.

    There is exactly one CLI session, named by the first send and resumed by
    every send after it. Clear drops the id rather than emptying anything: the
    CLI has no "forget", so a new thread is a new session.
    """

    def __init__(self, on_change=None):
        self.messages = []
        self.attachments = []
        # ``None`` means the next send opens a chat, which is the whole of what
        # "new chat" means here.
        self.chat_id = None
        self.turn = None
        # The surface, when there is one. Called on the main thread after
        # anything a reader would notice.
        self.on_change = on_change

    # -- what a reader asks -------------------------------------------------

    def busy(self):
        return self.turn is not None

    def working_seconds(self):
        """How long the active turn has been running, or ``None`` when idle."""
        if self.turn is None:
            return None
        return max(0.0, time.time() - self.turn.started)

    def waiting(self):
        """True while the last message is expecting something to arrive."""
        if not self.messages:
            return False
        last = self.messages[-1]
        return (last.streaming and not last.text and not last.tools) or any(
            tool.status == chat_module.TOOL_RUNNING for tool in last.tools
        )

    def asking(self):
        """True while a questionnaire is on screen waiting for a pick."""
        turn = self.turn
        return bool(turn is not None and turn.ask is not None and not turn.ask.done)

    def ask_state(self):
        """The current question as a tree-ready snapshot, or ``None``."""
        turn = self.turn
        if turn is None or turn.ask is None:
            return None
        return turn.ask.snapshot()

    def handle_ask(self, node_id):
        """Route an ``ask:*`` click. True when it belonged to a parked question."""
        if not isinstance(node_id, str) or not node_id.startswith("ask:"):
            return False
        if not self.asking():
            return False
        if node_id == chat_module.ASK_SKIP:
            return self.skip_ask()
        if node_id == chat_module.ASK_CONTINUE:
            return self.continue_ask()
        if node_id == chat_module.ASK_PREV:
            return self.step_ask(-1)
        if node_id == chat_module.ASK_NEXT:
            return self.step_ask(1)
        if node_id == chat_module.ASK_CUSTOM:
            self.turn.ask.custom = True
            self._changed()
            return True
        if node_id.startswith(chat_module.ASK_OPTION + ":"):
            try:
                index = int(node_id.rsplit(":", 1)[1])
            except ValueError:
                return True
            return self.select_ask(index)
        return True

    def select_ask(self, index):
        ask = self.turn.ask if self.turn is not None else None
        if ask is None or ask.done:
            return False
        item = ask.current()
        if item is None or index < 0 or index >= len(item.options):
            return False
        item.selected = index
        self._changed()
        return True

    def step_ask(self, delta):
        ask = self.turn.ask if self.turn is not None else None
        if ask is None or ask.done or not ask.questions:
            return False
        nxt = ask.index + int(delta)
        if nxt < 0 or nxt >= len(ask.questions):
            return False
        ask.index = nxt
        self._changed()
        return True

    def continue_ask(self):
        ask = self.turn.ask if self.turn is not None else None
        if ask is None or ask.done:
            return False
        item = ask.current()
        if item is None or item.selected is None:
            return False
        if ask.index + 1 < len(ask.questions):
            ask.index += 1
            self._changed()
            return True
        unanswered = next(
            (i for i, row in enumerate(ask.questions) if row.selected is None),
            None,
        )
        if unanswered is not None:
            ask.index = unanswered
            self._changed()
            return True
        return self._finish_ask(ask.answers())

    def skip_ask(self):
        if not self.asking():
            return False
        return self._finish_ask(None)

    def _reject_ask(self, text):
        if not text:
            return False
        self.messages.append(chat_module.Message(chat_module.USER, text))
        return self._finish_ask(("reject", text))

    def _finish_ask(self, result):
        turn = self.turn
        ask = turn.ask if turn is not None else None
        if ask is None or ask.done:
            return False
        ask.result = result
        ask.done = True
        ask.event.set()
        self._changed()
        return True

    def _changed(self):
        if self.on_change is not None:
            self.on_change()

    # -- the staged files ---------------------------------------------------

    def attach(self, paths):
        added = 0
        for path in paths:
            if path and path not in self.attachments:
                self.attachments.append(path)
                added += 1
        if added:
            self._changed()
        return added

    def detach(self, index):
        try:
            self.attachments.pop(int(index))
        except (ValueError, IndexError):
            return False
        self._changed()
        return True

    # -- the turn -----------------------------------------------------------

    def send(
        self,
        hb,
        text,
        attachments=None,
        model=None,
        *,
        request_text=None,
        tags=(),
        elements=(),
    ):
        """Commit a prompt: the turn goes up and the reply streams back.

        ``attachments=None`` consumes the files staged by the chat surface.
        Another prompt surface may pass an explicit tuple so invisible staged
        files do not hitchhike on a request made somewhere else.

        ``request_text`` may add host context for the agent without exposing
        implementation instructions as user-authored prose. ``tags`` keeps that
        context visible in the user's message.
        """
        text = (text or "").strip()
        if (
            self.turn is not None
            and self.turn.ask is not None
            and not self.turn.ask.done
        ):
            return self._reject_ask(text)
        request_text = (request_text if request_text is not None else text).strip()
        staged = attachments is None
        attachments = tuple(self.attachments if staged else attachments)
        if not request_text and not attachments:
            return False
        if self.turn is not None:
            return False

        self.messages.append(
            chat_module.Message(
                chat_module.USER,
                text,
                attachments=attachments,
                tags=tags,
                elements=elements,
            )
        )
        reply = chat_module.Message(chat_module.ASSISTANT, "", streaming=True)
        self.messages.append(reply)
        if staged:
            self.attachments.clear()
        self.turn = Turn(reply, self.chat_id, hb._active_workspace_id())
        self._changed()
        self._run(hb, self.turn, request_text, attachments, model)
        return True

    def _interrupt(self, hb, turn):
        """Flag the worker off, then end the process it is reading.

        The flag alone would not be felt: the worker is blocked on the CLI's
        stdout, which stays silent for as long as the model is thinking. It is
        still set first, so any page already queued for the main thread lands
        nowhere. Killing is safe — the CLI has written its transcript by the
        time each event reaches us, so the conversation is resumable.
        """
        turn.cancelled = True
        if turn.ask is not None:
            turn.ask.done = True
            turn.ask.event.set()
        process = turn.process
        turn.process = None
        if process is None:
            return
        import contextlib

        with contextlib.suppress(Exception):
            if process.poll() is None:
                process.terminate()

    def stop(self, hb):
        """Cut the turn in flight. The thread stays, including whatever arrived.

        Running tool rows settle as "stopped" so they do not keep counting
        dots after the clock has gone. An empty assistant turn stays in the
        list; the surfaces already skip a message with nothing to show.
        """
        turn = self.turn
        if turn is None:
            return False
        self._interrupt(hb, turn)
        turn.message.streaming = False
        if turn.ask is not None:
            turn.ask = None
        for tool in turn.message.tools:
            if tool.status == chat_module.TOOL_RUNNING:
                tool.status = chat_module.TOOL_DONE
                if not tool.detail:
                    tool.detail = "stopped"
        self.turn = None
        self._changed()
        return True

    def clear(self, hb):
        """Back to an empty thread, and a new chat on the next send.

        Whatever was in flight is interrupted first: leaving a turn running
        against a conversation nobody can see spends credits on an answer with
        nowhere to land.
        """
        turn, self.turn = self.turn, None
        if turn is not None:
            self._interrupt(hb, turn)
        self.chat_id = None
        self.messages = []
        self._changed()
        return True

    def abandon(self):
        """Teardown: the answer loses its destination, no network involved.

        The worker checks the flag between polls and stops rather than posting
        into a surface that is gone.
        """
        if self.turn is not None:
            self.turn.cancelled = True
            if self.turn.ask is not None:
                self.turn.ask.event.set()
        self.turn = None

    # -- what the worker hands back, all on the main thread -----------------

    def apply(self, turn, parts, names):
        """One poll page. The only writer of the thread."""
        if turn.cancelled:
            return
        message = turn.message
        for part in parts:
            kind = part.get("type")
            call_id = part.get("toolCallId")
            if kind == "text-delta":
                message.text += str(part.get("delta") or "")
            elif kind == "tool-input-start":
                tool = turn.row(call_id, part.get("toolName"))
                if tool is not None:
                    # A tool re-emitted after an approval comes back here.
                    tool.status = chat_module.TOOL_RUNNING
                    tool.detail = ""
            elif kind == "tool-input-available":
                name = names.get(call_id) or part.get("toolName")
                if agent.asks_user(name):
                    questions = _parse_ask(part.get("input"))
                    if questions:
                        turn.ask = Ask(call_id, name, questions)
            elif kind == "tool-output-available":
                tool = turn.row(call_id, names.get(call_id))
                if tool is not None:
                    detail, failed = _summarize(part.get("output"))
                    tool.detail = detail
                    tool.status = (
                        chat_module.TOOL_FAILED if failed else chat_module.TOOL_DONE
                    )
                    for job_id in agent.job_ids(part.get("output")):
                        turn.job_tools[job_id] = tool
                    if turn.ask is not None and turn.ask.call_id == call_id:
                        turn.ask = None
            elif kind == "error":
                self.fail(turn, part.get("errorText") or "The agent failed.")
            elif kind == "finish":
                self.settle(turn)
        self._changed()

    def settle(self, turn):
        """The turn's text is done. Jobs it started may still be running."""
        turn.message.streaming = False
        if self.turn is turn:
            self.turn = None
        self._changed()

    def fail(self, turn, message):
        """Say what went wrong where the question was asked."""
        reply = turn.message
        reply.streaming = False
        text = str(message or "").strip() or "The agent could not answer."
        if reply.text.strip() or reply.tools:
            self.messages.append(
                chat_module.Message(chat_module.ASSISTANT, text, failed=True)
            )
        else:
            reply.text = text
            reply.failed = True
        turn.cancelled = True
        if self.turn is turn:
            self.turn = None
        self._changed()

    def adopt(self, turn, chat_id):
        """Remember the chat the first send opened, so the next one extends it.

        **A cancelled turn may not adopt.** The worker opens the chat and posts
        the id before it starts polling, so a Clear in that window used to be
        undone a moment later by the id landing behind it: the thread looked
        empty, the next send extended the old conversation, and the agent
        answered with the whole of a history the user had just thrown away.
        """
        if turn.cancelled:
            return
        self.chat_id = chat_id

    def landed(self, turn, job_id, status, path):
        """A generation the turn started finished rendering."""
        tool = turn.job_tools.get(job_id)
        if path:
            turn.message.images.append(path)
            if tool is not None:
                tool.detail = "ready"
                tool.status = chat_module.TOOL_DONE
        elif tool is not None:
            tool.detail = str(status or "unavailable")
            tool.status = chat_module.TOOL_FAILED
        self._changed()

    # -- the worker ---------------------------------------------------------

    def _run(self, hb, turn, text, attachments, model=None):
        """Hand the turn to a worker thread and stream it back in pages."""
        import bpy

        prefs = addon_preferences()
        provider = getattr(prefs, "agent_provider", agent.CLAUDE) if prefs else agent.CLAUDE
        binary = getattr(prefs, "agent_binary", "") if prefs else ""
        full_access = bool(getattr(prefs, "agent_full_access", False)) if prefs else False
        session = agent.Session(
            session_id=turn.chat,
            cwd=agent.working_dir(bpy.data.filepath),
        )

        def cancelled():
            return turn.cancelled

        def sink(parts, names):
            # Worker thread: hand the page over and touch nothing else.
            run_on_main_thread(
                lambda parts=parts, names=names: self.apply(turn, parts, names)
            )

        def adopt_process(process):
            run_on_main_thread(lambda: self._own(turn, process))

        def work(_progress):
            agent.run_turn(
                session,
                text,
                sink,
                cancelled,
                model=model,
                provider=provider,
                binary=binary,
                attachments=attachments,
                full_access=full_access,
                on_start=adopt_process,
            )
            # Only a session the CLI itself confirmed is worth keeping.
            # `started` is set when its `init` event names the conversation; an
            # id we made up for a run that never started names a transcript
            # that does not exist, and resuming it would fail every time.
            chat_id = session.session_id if session.started else None
            if chat_id:
                run_on_main_thread(lambda chat_id=chat_id: self.adopt(turn, chat_id))
            return chat_id

        def failed(error):
            # `run_async` hands this back on the main thread. Bind before the
            # closure, never close over an `except ... as` name.
            self.fail(turn, str(error) or "The agent could not answer.")

        run_async("Scene Builder", work, on_error=failed, silent=True)

    def _own(self, turn, process):
        """Adopt the child running this turn, unless Stop got there first."""
        if turn.cancelled:
            import contextlib

            with contextlib.suppress(Exception):
                if process.poll() is None:
                    process.terminate()
            return
        turn.process = process


_live = None


def live():
    """The one conversation. Outlives any surface that shows it."""
    global _live

    if _live is None:
        _live = Conversation()
    return _live
