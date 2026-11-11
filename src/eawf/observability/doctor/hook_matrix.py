"""The hook contract matrix: contexts, events, payloads and the stdout shapes a host reads.

``eawf doctor hooks`` and the contract matrix test run the same cells, so the
matrix is defined once, here. A cell is one context, one event and one
payload. Every context gets the event's normal payloads, an empty stdin and
malformed JSON; the managed contexts also get a normal payload with the
daemon down (a socket nothing listens on) and with a stale daemon (a stub
answering ``daemon.ping`` with version ``0.0.0``).

The shapes are the JSON fields Claude Code reads from each event's stdout:
the common ``continue``, ``stopReason``, ``suppressOutput`` and
``systemMessage``, plus the event's own ``decision``/``reason`` and
``hookSpecificOutput`` fields.
"""

from __future__ import annotations

import contextlib
import json
import os
import socket
import threading
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Final, Literal

#: Where a cell runs; the first three are managed.
CONTEXTS: Final = (
    "managed-root",
    "managed-subfolder",
    "managed-worktree",
    "unmanaged-git",
    "no-git",
    "runner-missing",
)

#: The contexts a hook does its managed work in, with a runner present.
MANAGED_CONTEXTS: Final = frozenset(CONTEXTS[:3])

#: The session the matrix payloads name.
SESSION_ID: Final = "doctor-hooks"

_COMMON_KEYS: Final = frozenset({"continue", "stopReason", "suppressOutput", "systemMessage"})

#: Per host event: the top-level keys it reads beyond the common ones, and
#: the ``hookSpecificOutput`` keys beside ``hookEventName`` (``None``: none).
_SHAPES: Final[dict[str, tuple[frozenset[str], frozenset[str] | None]]] = {
    "SessionStart": (frozenset({"hookSpecificOutput"}), frozenset({"additionalContext"})),
    "UserPromptSubmit": (
        frozenset({"decision", "reason", "hookSpecificOutput"}),
        frozenset({"additionalContext"}),
    ),
    "PreToolUse": (
        frozenset({"decision", "reason", "hookSpecificOutput"}),
        frozenset(
            {"permissionDecision", "permissionDecisionReason", "updatedInput", "additionalContext"}
        ),
    ),
    "PostToolUse": (
        frozenset({"decision", "reason", "hookSpecificOutput"}),
        frozenset({"additionalContext"}),
    ),
    "PostToolUseFailure": (frozenset({"hookSpecificOutput"}), frozenset({"additionalContext"})),
    "PermissionRequest": (frozenset({"hookSpecificOutput"}), frozenset({"decision"})),
    "SubagentStart": (frozenset({"hookSpecificOutput"}), frozenset({"additionalContext"})),
    "SubagentStop": (frozenset({"decision", "reason"}), None),
    "PreCompact": (frozenset(), None),
    "SessionEnd": (frozenset(), None),
}


@dataclass(frozen=True, slots=True)
class MatrixEvent:
    """One subscribed hook event.

    Attributes:
        value: The eawf event value, the wrapper's argument.
        cc_event: The host event it is subscribed under.
    """

    value: str
    cc_event: str


@dataclass(frozen=True, slots=True)
class Payload:
    """One payload a cell feeds the wrapper.

    Attributes:
        label: The name a failing cell line prints.
        fields: The event's own payload fields; ``None`` for a raw payload.
        raw: The stdin of a raw payload: empty or malformed.
        daemon: ``none`` leaves the runtime directory empty, ``down`` puts a
            socket nothing listens on where the hook dials its daemon,
            ``stale`` a stub answering version ``0.0.0``.
        deny_when_managed: The call must be refused in a managed context, so
            empty stdout there means the guard is dead.
    """

    label: str
    fields: dict[str, Any] | None = None
    raw: str = ""
    daemon: Literal["none", "down", "stale"] = "none"
    deny_when_managed: bool = False

    def stdin(self, cwd: Path) -> str:
        """Return the stdin for a cell running in *cwd*.

        Args:
            cwd: The directory the host session stands in.

        Returns:
            The raw text, or the payload as one JSON document naming *cwd*.
        """
        if self.fields is None:
            return self.raw
        return json.dumps({"session_id": SESSION_ID, "cwd": str(cwd), **self.fields})


def matrix_events() -> tuple[MatrixEvent, ...]:
    """Return the events the plugin subscribes for every call, in manifest order.

    Returns:
        One entry per handler-backed event subscribed without a matcher.
    """
    from eawf.runtime.runtimes.claude.hook_map import handler_backed_plugin_hooks

    return tuple(
        MatrixEvent(value=spec.event_type.value, cc_event=spec.cc_event)
        for spec in handler_backed_plugin_hooks()
        if not spec.matcher
    )


def _tool(name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"tool_name": name, "tool_input": tool_input}


_BASH: Final = _tool("Bash", {"command": "git status"})
_EDIT: Final = _tool("Edit", {"file_path": "README.md", "old_string": "a", "new_string": "b"})

#: The normal payloads of each event, by eawf event value.
_NORMAL: Final[dict[str, tuple[Payload, ...]]] = {
    "session_start": (Payload("startup", {"source": "startup"}),),
    "user_prompt_submit": (
        Payload("prompt", {"prompt": "hello"}),
        Payload("research", {"prompt": "/eawf:research x"}),
        Payload("plan", {"prompt": "/eawf:plan x"}),
    ),
    "pre_tool_use": (
        Payload("bash", _BASH),
        Payload("edit", _EDIT),
        Payload("skill", _tool("Skill", {"skill": "eawf:research"})),
        Payload("enter-worktree", _tool("EnterWorktree", {"name": "x"}), deny_when_managed=True),
        Payload(
            "agent-isolation",
            _tool("Agent", {"description": "d", "prompt": "p", "isolation": "worktree"}),
            deny_when_managed=True,
        ),
    ),
    "post_tool_use": (
        Payload("bash", {**_BASH, "tool_response": {"stdout": ""}}),
        Payload("edit", {**_EDIT, "tool_response": {"filePath": "README.md"}}),
    ),
    "post_tool_use_failure": (Payload("bash", {**_BASH, "error": "exit 1"}),),
    "permission_request": (Payload("bash", _BASH),),
    "subagent_start": (Payload("agent", {"agent_id": "a1", "agent_type": "general-purpose"}),),
    "subagent_stop": (Payload("agent", {"agent_id": "a1", "stop_hook_active": False}),),
    "pre_compact": (Payload("manual", {"trigger": "manual", "custom_instructions": ""}),),
    "session_end": (Payload("exit", {"reason": "other"}),),
}


def payloads_for(event: MatrixEvent, *, managed: bool) -> tuple[Payload, ...]:
    """Return the payloads one event's cells feed the wrapper.

    Args:
        event: The event.
        managed: Whether the context is managed with a runner present, where
            the daemon variants apply.

    Returns:
        The normal payloads, then empty stdin and malformed JSON, then in a
        managed context the daemon-down and stale-daemon variants of the
        first normal payload.

    Raises:
        KeyError: *event* has no normal payload defined.
    """
    normal = _NORMAL[event.value]
    faults = (Payload("empty", raw=""), Payload("malformed", raw="{not json"))
    if not managed:
        return (*normal, *faults)
    first = normal[0]
    daemon = (
        Payload("daemon-down", first.fields, daemon="down"),
        Payload("stale-daemon", first.fields, daemon="stale"),
    )
    return (*normal, *faults, *daemon)


def _hook_specific_error(
    cc_event: str, value: object, allowed: frozenset[str] | None
) -> str | None:
    """Return why a ``hookSpecificOutput`` value is not the event's, or ``None``."""
    if allowed is None:
        return "hookSpecificOutput on an event that reads none"
    if not isinstance(value, dict) or value.get("hookEventName") != cc_event:
        return f"hookSpecificOutput not named {cc_event}"
    extra = sorted(set(value) - allowed - {"hookEventName"})
    return f"hookSpecificOutput keys {extra}" if extra else None


def allowed_shape_error(cc_event: str, stdout: str) -> str | None:
    """Return why *stdout* is not an answer the host reads for *cc_event*, or ``None``.

    Args:
        cc_event: The host event name, e.g. ``PreToolUse``.
        stdout: What the hook printed.

    Returns:
        ``None`` for empty output or one JSON object of the event's shape;
        otherwise a short description of the first mismatch.

    Raises:
        KeyError: *cc_event* is not a host event the matrix knows.
    """
    top_keys, specific = _SHAPES[cc_event]
    if not stdout.strip():
        return None
    try:
        document = json.loads(stdout)
    except json.JSONDecodeError:
        return f"not one JSON document: {stdout.strip().splitlines()[0]}"
    if not isinstance(document, dict):
        return f"a JSON {type(document).__name__}, not an object"
    extra = sorted(set(document) - top_keys - _COMMON_KEYS)
    if extra:
        return f"keys {extra}"
    if "hookSpecificOutput" in document:
        return _hook_specific_error(cc_event, document["hookSpecificOutput"], specific)
    return None


class StaleDaemon:
    """A socket where a hook dials its daemon: dead, or answering an old version.

    Used as a context manager, it binds *path*. A dead socket is closed at
    once, leaving the socket node with nothing listening, the trace of a
    daemon that died. A serving one answers every ``daemon.ping`` line with
    version ``0.0.0`` and this process's pid until the block exits.

    Args:
        path: The ``eawfd.sock`` path to bind.
        serve: Answer pings; otherwise leave a dead socket.
    """

    def __init__(self, path: Path, *, serve: bool) -> None:
        self._path = path
        self._serve = serve
        self._server: socket.socket | None = None
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Path:
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self._path))
        if not self._serve:
            server.close()
            return self._path
        server.listen()
        self._server = server
        self._thread = threading.Thread(target=self._accept, args=(server,), daemon=True)
        self._thread.start()
        return self._path

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._server is not None:
            # Closing alone leaves a blocked accept() waiting on Linux.
            with contextlib.suppress(OSError):
                self._server.shutdown(socket.SHUT_RDWR)
            self._server.close()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _accept(self, server: socket.socket) -> None:
        while True:
            try:
                connection, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(connection,), daemon=True).start()

    @staticmethod
    def _answer(connection: socket.socket) -> None:
        with connection, connection.makefile("rb") as reader:
            for line in reader:
                try:
                    request = json.loads(line)
                except json.JSONDecodeError:
                    return
                reply = {
                    "jsonrpc": "2.0",
                    "id": request.get("id") if isinstance(request, dict) else None,
                    "result": {"version": "0.0.0", "pid": os.getpid()},
                }
                try:
                    connection.sendall(json.dumps(reply).encode("utf-8") + b"\n")
                except OSError:
                    return


__all__ = [
    "CONTEXTS",
    "MANAGED_CONTEXTS",
    "SESSION_ID",
    "MatrixEvent",
    "Payload",
    "StaleDaemon",
    "allowed_shape_error",
    "matrix_events",
    "payloads_for",
]
