"""Scene Builder's chat threads, on disk between Blender sessions.

The composer keeps the threads in memory (`composer_surface._composer`), which
survives a UI toggle through `renewed()` but not a Blender restart. This module
is the restart half: one JSON file under `paths.state_dir()` — private like the
OAuth token, because a chat is the user talking — with one thread bucket per
workspace, written whole on every save and read back once per workspace.
It also maps the stable UUID stored on each Blender scene to that scene's
primary local thread.

What is persisted is the *thread*, not the turn. A turn in flight cannot be
resumed across a restart — the worker, its poll cursor and its job followers
are all gone — so messages are saved as they stand and settle on load: a tool
still marked running when Blender died becomes failed, a reply still streaming
keeps whatever text had arrived. The backend `chat_id` is saved too, which is
what lets the next send extend the same server-side conversation rather than
opening a new one.

Deliberately `bpy`-free, like `conversation.py` next door: nothing here needs
Blender, and the snapshot scripts import the modules above this one headlessly.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile

from .. import paths
from ..hfui import chat as chat_module
from ..hfui import mentions as mentions_module
from . import conversation

_VERSION = 4
# Enough that "the chat from last week" is still there, small enough that the
# file stays a read of milliseconds. Oldest threads fall off first; within a
# thread the newest messages win.
_MAX_THREADS = 20
_MAX_MESSAGES = 200


def _history_file():
    return paths.state_dir() / "scene-chats.json"


def _message_data(message):
    return {
        "role": message.role,
        "text": message.text,
        "attachments": list(message.attachments),
        "elements": [element.data() for element in message.elements],
        "tags": list(message.tags),
        "images": list(message.images),
        "failed": message.failed,
        "tools": [
            {
                "label": tool.label,
                "icon": tool.icon,
                "detail": tool.detail,
                "status": tool.status,
                "count": tool.count,
                "server": tool.server,
            }
            for tool in message.tools
        ],
    }


def _restore_message(data):
    role = chat_module.USER if data.get("role") == chat_module.USER else chat_module.ASSISTANT
    tools = []
    for entry in data.get("tools") or ():
        status = str(entry.get("status") or "")
        if status not in (chat_module.TOOL_DONE, chat_module.TOOL_FAILED):
            # It was running when Blender died; it will never finish now.
            status = chat_module.TOOL_FAILED
        tools.append(
            chat_module.Tool(
                None,
                str(entry.get("label") or "Working"),
                str(entry.get("icon") or "settings/sparkles-three"),
                detail=str(entry.get("detail") or ""),
                status=status,
                count=max(1, int(entry.get("count") or 1)),
                server=str(entry.get("server") or ""),
            )
        )
    return chat_module.Message(
        role,
        str(data.get("text") or ""),
        attachments=tuple(str(path) for path in data.get("attachments") or ()),
        elements=tuple(
            element
            for element in (
                mentions_module.ElementMention.from_data(row)
                for row in data.get("elements") or ()
            )
            if element is not None
        ),
        tags=tuple(str(tag) for tag in data.get("tags") or ()),
        tools=tools,
        images=[str(path) for path in data.get("images") or ()],
        failed=bool(data.get("failed")),
    )


def _workspace_data(
    chats,
    drafts,
    active,
    next_id,
    thread_height=None,
    scene_bindings=None,
    mention_documents=None,
):
    """Serializable state for one workspace's local Scene Builder threads."""
    bound_ids = {str(thread_id) for thread_id in (scene_bindings or {}).values()}
    threads = []
    for thread_id, chat in tuple(chats.items())[-_MAX_THREADS:]:
        draft = str(drafts.get(thread_id) or "")
        document = (mention_documents or {}).get(str(thread_id))
        messages = [_message_data(message) for message in chat.messages[-_MAX_MESSAGES:]]
        if not messages and not draft and str(thread_id) not in bound_ids:
            # A fresh "New chat" costs nothing to recreate and would otherwise
            # accumulate one empty entry per session. A scene-bound empty chat
            # is meaningful, though: it must still open empty after a restart.
            continue
        threads.append(
            {
                "id": str(thread_id),
                "chat": chat.chat_id,
                "draft": {
                    "text": draft,
                    "mentions": document.data() if document is not None else {},
                },
                "messages": messages,
            }
        )
    saved_ids = {entry["id"] for entry in threads}
    bindings = {
        str(scene_id): str(thread_id)
        for scene_id, thread_id in (scene_bindings or {}).items()
        if str(scene_id) and str(thread_id) in saved_ids
    }
    data = {
        "active": str(active),
        "next": int(next_id),
        "threads": threads,
        "scenes": bindings,
    }
    if thread_height is not None:
        data["thread_height"] = float(thread_height)
    return data


def save(
    chats,
    drafts,
    active,
    next_id,
    thread_height=None,
    scene_bindings=None,
    mention_documents=None,
    *,
    workspace_id=None,
):
    """Write the threads whole. Never raises: losing one save loses nothing
    the next settle will not write again.

    ``thread_height`` is the user's own resize of the conversation viewport,
    carried for the same reason the drafts are: losing it on restart reads as
    the add-on forgetting an adjustment the user made deliberately.
    """
    workspace_id = str(workspace_id or "").strip()
    if not workspace_id:
        return
    try:
        previous = json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        previous = None
    workspaces = {}
    if isinstance(previous, dict) and previous.get("version") in (2, 3, _VERSION):
        stored = previous.get("workspaces")
        if isinstance(stored, dict):
            workspaces.update(stored)
    workspaces[workspace_id] = _workspace_data(
        chats,
        drafts,
        active,
        next_id,
        thread_height,
        scene_bindings,
        mention_documents,
    )
    payload = {"version": _VERSION, "workspaces": workspaces}
    path = _history_file()
    temp = None
    try:
        text = json.dumps(payload, ensure_ascii=False)
        # Atomic: the file either stays the last good save or becomes this one.
        # A plain write interrupted by Blender quitting mid-flush would leave
        # half a JSON file and load() silently dropping the whole history.
        handle, temp = tempfile.mkstemp(
            dir=str(path.parent), prefix=".scene-chats-", suffix=".json"
        )
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write(text)
        os.replace(temp, path)
        temp = None
        paths.harden(path)
    except (OSError, TypeError, ValueError):
        pass
    finally:
        if temp is not None:
            with contextlib.suppress(OSError):
                os.unlink(temp)


def _restore_workspace(raw):
    """Restore one workspace payload in any format from v1 through v4."""
    if not isinstance(raw, dict):
        return None
    chats = {}
    drafts = {}
    mention_documents = {}
    for entry in raw.get("threads") or ():
        if not isinstance(entry, dict):
            continue
        thread_id = str(entry.get("id") or "")
        if not thread_id.isdigit():
            continue
        try:
            messages = [
                _restore_message(data)
                for data in entry.get("messages") or ()
                if isinstance(data, dict)
            ]
        except (TypeError, ValueError):
            continue
        chat = conversation.Conversation()
        chat.chat_id = str(entry.get("chat") or "") or None
        chat.messages = messages
        chats[thread_id] = chat
        stored_draft = entry.get("draft")
        if isinstance(stored_draft, dict):
            drafts[thread_id] = str(stored_draft.get("text") or "")
            mention_documents[thread_id] = mentions_module.MentionDocument.from_data(
                stored_draft.get("mentions")
            )
        else:
            drafts[thread_id] = str(stored_draft or "")
            mention_documents[thread_id] = mentions_module.MentionDocument()
    if not chats:
        return None

    active = str(raw.get("active") or "")
    if active not in chats:
        active = next(reversed(chats))
    try:
        next_id = int(raw.get("next") or 0)
    except (TypeError, ValueError):
        next_id = 0
    # `next` must clear every restored id even if the saved counter is stale.
    next_id = max(next_id, 1 + max(int(thread_id) for thread_id in chats), 2)
    try:
        thread_height = float(raw["thread_height"])
    except (KeyError, TypeError, ValueError):
        thread_height = None
    bindings = {}
    for scene_id, thread_id in (raw.get("scenes") or {}).items():
        scene_id = str(scene_id or "")
        thread_id = str(thread_id or "")
        if scene_id and thread_id in chats:
            bindings[scene_id] = thread_id
    return (
        chats,
        drafts,
        active,
        next_id,
        thread_height,
        bindings,
        mention_documents,
    )


def load(workspace_id=None):
    """Conversation state plus per-scene bindings from disk, or None.

    ``thread_height`` is None when the save predates the resizable thread —
    the caller keeps its default rather than treating absence as zero.

    None covers every way the file can be absent or unreadable — first run,
    a user who deleted the state dir, a truncated write from a crash — and
    the caller keeps its in-memory defaults for all of them.
    """
    try:
        raw = json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    workspace_id = str(workspace_id or "").strip()
    version = raw.get("version")
    if version == 1:
        # The old file had no workspace provenance. Adopt it into whichever
        # workspace is active on the first post-upgrade load; the next save
        # rewrites it as a v4 workspace bucket.
        return _restore_workspace(raw) if workspace_id else None
    if version not in (2, 3, _VERSION) or not workspace_id:
        return None
    workspaces = raw.get("workspaces")
    if not isinstance(workspaces, dict):
        return None
    return _restore_workspace(workspaces.get(workspace_id))
