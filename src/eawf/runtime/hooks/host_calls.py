"""Host-call hooks: permissions, tool calls, file edits, questions and tool errors.

What the host does inside a tool call reaches eawf only through its hooks. A call the
host holds for its operator is recorded as a provider permission and its decision carried
back (:data:`HOST_PERMISSION_HOOK`); every tool call is observed on the Run the host's
session runs as (:data:`HOST_TOOL_HOOK`); an edit tool's file is recorded before and after
it runs; a question the host asks its operator is raised and answered; and a failed tool
call is recorded as an error. None of them blocks the host over a daemon failure.
"""

from __future__ import annotations

import hashlib
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any, Final

import orjson

from eawf.runtime.hooks import runner
from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent, HookEventType
from eawf.runtime.hooks.runner import DaemonClientFactory, HookResult, HookRunner

#: The verb a held host call is recorded through.
_HOST_PERMISSION_METHOD: Final = "runtime.host.permission.request"

#: The verb the recorded call's decision is read back through.
_PERMISSION_READ_METHOD: Final = "runtime.permission.read"

#: How often a waiting hook reads the record again. Each read takes the Run's
#: lock, so the wait sleeps between reads rather than asking back to back.
_DECISION_POLL_S: Final = 0.5

#: The host behaviour each principal decision is answered with. An expiry is
#: absent: the provider decided it, so there is nothing to hand back.
HOST_PERMISSION_BEHAVIOR: Final[dict[str, str]] = {"approved": "allow", "denied": "deny"}

#: The hook whose output a host permission decision is read from.
HOST_PERMISSION_HOOK: Final = "runtime.host_permission"


def _await_decision(
    client: Any, permission: dict[str, Any], *, repo_root: str, wait_s: float
) -> str | None:
    """Return the principal decision recorded on *permission* within *wait_s*.

    Args:
        client: The open daemon client the call was recorded through.
        permission: The record as the daemon answered it.
        repo_root: The repository the harness runs in.
        wait_s: How long to keep reading before giving up.

    Returns:
        ``approved`` or ``denied`` when a principal decided in time; ``None``
        when nobody did, or the record ended any other way.
    """
    deadline = time.monotonic() + wait_s
    while (resolution := permission.get("resolution")) is None:
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(_DECISION_POLL_S, left))
        answer = client.call(
            _PERMISSION_READ_METHOD, {"urn": permission["run_ref"], "repo_root": repo_root}
        )
        permission = next(
            item["permission"]
            for item in answer["permissions"]
            if item["permission"]["key"] == permission["key"]
        )
    decision = resolution["decision"]
    return decision if decision in HOST_PERMISSION_BEHAVIOR else None


def host_permission_decision(results: Iterable[HookResult]) -> str | None:
    """Return the decision the permission hook carried back, if it carried one.

    Args:
        results: The results one PERMISSION_REQUEST dispatch returned.

    Returns:
        ``approved`` or ``denied`` when :func:`record_host_permission` read a
        principal's decision in time; ``None`` otherwise, which leaves the
        call to the host's own prompt.
    """
    for result in results:
        words = result.output.split()
        if result.name == HOST_PERMISSION_HOOK and words[:1] == [HOST_PERMISSION_HOOK]:
            return next((word for word in words[1:2] if word in HOST_PERMISSION_BEHAVIOR), None)
    return None


def record_host_permission(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Record a call the host is holding as a provider permission, and await its decision.

    The host fires this while it asks its own operator whether a tool call may
    run. The hook records the held call with the daemon, bound to the Run on
    the host's session, then reads the record back for up to
    ``runtime.claude.permission_wait_s`` seconds. A principal's decision
    recorded in that window is carried back so the host applies it; past the
    window, or on any daemon error, the hook carries nothing and the host's
    own prompt decides. It never denies by timing out. Inside a subagent the
    host names the subagent, whose own Run the call belongs to.

    Args:
        event: The PERMISSION_REQUEST event.
        daemon_client_factory: Opens the daemon client; the default one when
            ``None``.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the recorded permission and,
        when one arrived in time, the decision -- the form
        :func:`host_permission_decision` reads -- or the reason none was
        recorded.
    """
    from eawf.kernel.config.layered import resolve_permission_wait_seconds

    name = HOST_PERMISSION_HOOK
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} holds no call")
    payload = runner._session_end_payload(event)
    session = next(
        (
            value
            for value in (payload.get("agent_id"), payload.get("session_id"))
            if isinstance(value, str) and value
        ),
        None,
    )
    tool_name = payload.get("tool_name")
    if session is None or not isinstance(tool_name, str) or not tool_name:
        return HookResult(name=name, output=f"{name} skipped: missing session_id or tool_name")
    tool_input = payload.get("tool_input")
    root = repo_root if repo_root is not None else Path.cwd()
    params: dict[str, Any] = {
        "harness": harness,
        "host_session_id": session,
        "tool_name": tool_name,
        "tool_input": tool_input if isinstance(tool_input, dict) else {},
        "repo_root": str(root),
    }
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        wait_s = resolve_permission_wait_seconds(root)
        with factory() as client:
            permission = client.call(_HOST_PERMISSION_METHOD, params)["permission"]
            decision = _await_decision(client, permission, repo_root=str(root), wait_s=wait_s)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    return HookResult(name=name, output=f"{name} {decision or 'ok'} permission={permission['key']}")


#: The verb a host tool call is accounted for through.
_HOST_TOOL_METHOD: Final = "runtime.host.tool.observe"

#: The hook that accounts for host tool calls.
HOST_TOOL_HOOK: Final = "runtime.host_tool"

#: The most output characters a tool hook carries; the daemon refuses a longer one.
_HOST_TOOL_OUTPUT_CHARS: Final = 65_536

#: The phase each tool event reports, and whether the call it reports failed.
_HOST_TOOL_PHASES: Final[dict[HookEventType, tuple[str, bool]]] = {
    HookEventType.PRE_TOOL_USE: ("requested", False),
    HookEventType.POST_TOOL_USE: ("result", False),
    HookEventType.POST_TOOL_USE_FAILURE: ("result", True),
}


def _tool_output_text(response: Any) -> str:
    """Return the text a host tool's response carries, in the shape a reader expects.

    A file read answers with the file's content, a shell command with its output
    streams, and any other tool with its text blocks or, failing those, its response
    as indented JSON.
    """
    if isinstance(response, str):
        return response
    if isinstance(response, dict):
        read = response.get("file")
        if isinstance(read, dict) and isinstance(read.get("content"), str):
            return str(read["content"])
        streams = [response.get(key) for key in ("stdout", "stderr")]
        if any(isinstance(text, str) and text for text in streams):
            return "\n".join(text for text in streams if isinstance(text, str) and text)
        content = response.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = [block.get("text") for block in content if isinstance(block, dict)]
            if any(isinstance(text, str) for text in texts):
                return "\n".join(text for text in texts if isinstance(text, str))
    return orjson.dumps(response, option=orjson.OPT_INDENT_2, default=str).decode()


def _tool_response_failed(response: Any) -> bool:
    """Return whether a tool response that reached the success hook reports a failure."""
    if not isinstance(response, dict):
        return False
    code = next(
        (response[key] for key in ("exit_code", "exitCode", "returncode") if key in response),
        0,
    )
    return response.get("is_error") is True or (isinstance(code, int) and code != 0)


def _host_call_key(payload: dict[str, Any], tool_name: str) -> str:
    """Return the host's id of a tool call, or a digest of what the call asked."""
    tool_use_id = payload.get("tool_use_id")
    if isinstance(tool_use_id, str) and tool_use_id:
        return tool_use_id[:256]
    body = orjson.dumps([tool_name, payload.get("tool_input")], option=orjson.OPT_SORT_KEYS)
    return f"input-{hashlib.sha256(body).hexdigest()[:32]}"


def observe_host_tool(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Account for one host tool call on the Run its session runs as.

    Before the call runs the host reports it requested; after it has run, the hook
    carries its output, cut to what the daemon accepts, so the daemon files it through
    the gateway's host-call door. The hook never blocks the host: a tree with no Run on
    the session, or no daemon, is named in the result and the call goes ahead.

    Args:
        event: A PRE_TOOL_USE, POST_TOOL_USE or POST_TOOL_USE_FAILURE event.
        daemon_client_factory: Opens the daemon client; the default one when ``None``.
        repo_root: The repository the harness runs in; the process working directory
            when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the call and its Run, or why the
        call was not accounted for.
    """
    name = HOST_TOOL_HOOK
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} reports no tool")
    payload = runner._session_end_payload(event)
    session = next(
        (
            value
            for value in (payload.get("agent_id"), payload.get("session_id"))
            if isinstance(value, str) and value
        ),
        None,
    )
    tool_name = payload.get("tool_name")
    if session is None or not isinstance(tool_name, str) or not tool_name:
        return HookResult(name=name, output=f"{name} skipped: missing session_id or tool_name")
    phase, failed = _HOST_TOOL_PHASES[event.event_type]
    response = payload.get("error") if failed else payload.get("tool_response")
    params: dict[str, Any] = {
        "harness": harness,
        "host_session_id": session[:256],
        "tool_name": tool_name[:256],
        "host_call_key": _host_call_key(payload, tool_name),
        "phase": phase,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    if phase == "result":
        params["output"] = _tool_output_text(response)[:_HOST_TOOL_OUTPUT_CHARS]
        params["failed"] = failed or _tool_response_failed(response)
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_TOOL_METHOD, params)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    return HookResult(
        name=name, output=f"{name} ok run={answer.get('run_ref')} call={answer.get('call_id')}"
    )


def register_host_tool_hooks(
    runner: HookRunner,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> None:
    """Register the hook that states every host tool call on its Run's transcript."""

    def _host_tool_hook(event: HookEvent) -> HookResult:
        return observe_host_tool(
            event, daemon_client_factory=daemon_client_factory, repo_root=repo_root
        )

    for tool_event_type in _HOST_TOOL_PHASES:
        runner.register(tool_event_type, _host_tool_hook, name=HOST_TOOL_HOOK)


#: The host tools whose calls edit one file, which the edit hooks bracket.
_HOST_EDIT_TOOLS: Final = frozenset({"Edit", "Write", "MultiEdit"})

#: The verb each tool-use event reports an edit through.
_HOST_FILE_EDIT_METHODS: Final[dict[HookEventType, str]] = {
    HookEventType.PRE_TOOL_USE: "runtime.host.file_edit.before",
    HookEventType.POST_TOOL_USE: "runtime.host.file_edit.after",
    HookEventType.POST_TOOL_USE_FAILURE: "runtime.host.file_edit.after",
}


def record_host_file_edit(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Bracket one host edit tool call so its change lands on the Run's stream.

    Before the tool runs the daemon keeps the repository's tree; after it runs
    the daemon records what the tool changed, bound to the Run on the host's
    session -- the subagent's own Run inside a subagent. Any other tool is
    skipped, and the hook never blocks the host.

    Args:
        event: The PRE_TOOL_USE event, or the POST_TOOL_USE or
            POST_TOOL_USE_FAILURE event -- a failed edit can still have
            changed the file, and either way its kept tree is released.
        daemon_client_factory: Opens the daemon client; the default one when
            ``None``.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the Run and the recorded
        line, or why nothing was recorded.
    """
    name = "runtime.host_file_edit"
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} edits no host file")
    payload = runner._session_end_payload(event)
    tool_name = payload.get("tool_name")
    if tool_name not in _HOST_EDIT_TOOLS:
        return HookResult(name=name, output=f"{name} skipped: {tool_name} edits no file")
    session = next(
        (
            value
            for value in (payload.get("agent_id"), payload.get("session_id"))
            if isinstance(value, str) and value
        ),
        None,
    )
    tool_use_id = payload.get("tool_use_id")
    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    if session is None or not all(
        isinstance(value, str) and value for value in (tool_use_id, file_path)
    ):
        return HookResult(
            name=name, output=f"{name} skipped: missing session_id, tool_use_id or file_path"
        )
    params: dict[str, Any] = {
        "harness": harness,
        "host_session_id": session,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "file_path": file_path,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_FILE_EDIT_METHODS[event.event_type], params)
    except Exception as exc:
        # A tool hook's output reaches the host's log without its name, so it leads.
        return HookResult(name=name, output=f"{name} failed: {exc!r}")
    return HookResult(
        name=name,
        output=f"{name} ok run={answer.get('run_ref')} event={answer.get('event_ref')}",
    )


def register_host_file_edit_hooks(
    runner: HookRunner,
    *,
    daemon_client_factory: DaemonClientFactory | None,
    repo_root: Path | None,
) -> None:
    """Register the host edit bracket on both tool-use events."""

    def _hook(event: HookEvent) -> HookResult:
        return record_host_file_edit(
            event, daemon_client_factory=daemon_client_factory, repo_root=repo_root
        )

    for event_type in _HOST_FILE_EDIT_METHODS:
        runner.register(event_type, _hook, name="runtime.host_file_edit")


#: The host tool that puts a multiple-choice question to the host's operator.
HOST_QUESTION_TOOL: Final = "AskUserQuestion"

#: The verb each phase of a host question is recorded through.
_HOST_QUESTION_METHODS: Final[dict[HookEventType, str]] = {
    HookEventType.PRE_TOOL_USE: "runtime.host.question.raise",
    HookEventType.POST_TOOL_USE: "runtime.host.question.answer",
}

#: The verb a failed host tool call is stated through.
_HOST_ERROR_METHOD: Final = "runtime.host.error.observe"


def _host_session_of(payload: dict[str, Any]) -> str | None:
    """Return the host's id of the calling subagent, else of the calling session."""
    return next(
        (
            value
            for value in (payload.get("agent_id"), payload.get("session_id"))
            if isinstance(value, str) and value
        ),
        None,
    )


def _asked_questions(tool_input: object) -> list[dict[str, Any]]:
    """Return the questions a question call asks, each its words and option labels."""
    raw = tool_input.get("questions") if isinstance(tool_input, dict) else None
    asked: list[dict[str, Any]] = []
    for item in raw if isinstance(raw, list) else []:
        words = item.get("question") if isinstance(item, dict) else None
        if not isinstance(words, str) or not words.strip():
            continue
        options = item.get("options")
        labels = [
            option["label"]
            for option in (options if isinstance(options, list) else [])
            if isinstance(option, dict) and isinstance(option.get("label"), str) and option["label"]
        ]
        asked.append({"question": words, "options": labels})
    return asked


def _host_answers(tool_response: object) -> dict[str, str]:
    """Return the operator's answers a returned question call carries, by question."""
    raw = tool_response.get("answers") if isinstance(tool_response, dict) else None
    if not isinstance(raw, dict):
        return {}
    return {
        question: answer
        for question, answer in raw.items()
        if isinstance(question, str) and isinstance(answer, str) and question and answer
    }


def record_host_question(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Record a question the host puts to its operator, or the answer it got.

    Before the host shows the question the hook records it, bound to the Run on the
    host's session, so the Run is seen waiting and on what; once the call returns the
    hook records the operator's answer. The hook never blocks the host and never
    answers for it: the host's own prompt is where the operator answers.

    Args:
        event: The PRE_TOOL_USE or POST_TOOL_USE event.
        daemon_client_factory: Opens the daemon client; the default one when ``None``.
        repo_root: The repository the harness runs in; the process working directory
            when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the recorded questions, or why
        none was recorded.
    """
    name = "runtime.host_question"
    harness = HOST_HARNESSES.get(event.runtime)
    payload = runner._session_end_payload(event)
    if harness is None or payload.get("tool_name") != HOST_QUESTION_TOOL:
        return HookResult(name=name, output=f"{name} skipped: not a host question")
    session = _host_session_of(payload)
    tool_use_id = payload.get("tool_use_id")
    questions = _asked_questions(payload.get("tool_input"))
    if session is None or not isinstance(tool_use_id, str) or not tool_use_id or not questions:
        return HookResult(name=name, output=f"{name} skipped: missing session, call or question")
    params: dict[str, Any] = {
        "harness": harness,
        "host_session_id": session,
        "tool_use_id": tool_use_id,
        "questions": questions,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    if event.event_type is HookEventType.POST_TOOL_USE:
        params["answers"] = _host_answers(payload.get("tool_response"))
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_QUESTION_METHODS[event.event_type], params)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    refs = " ".join(ref.rsplit("/", 1)[-1] for ref in answer.get("question_refs", ()))
    return HookResult(name=name, output=f"{name} ok questions={refs}")


def record_host_tool_error(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """State a failed host tool call on the Run on the host's session.

    The host reports a failed call on its own event, never on the one a call that
    returned fires, carrying what went wrong and whether its operator interrupted it.

    Args:
        event: The POST_TOOL_USE_FAILURE event.
        daemon_client_factory: Opens the daemon client; the default one when ``None``.
        repo_root: The repository the harness runs in; the process working directory
            when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the stated error, or why none was.
    """
    name = "runtime.host_tool_error"
    harness = HOST_HARNESSES.get(event.runtime)
    payload = runner._session_end_payload(event)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} reports no call")
    session = _host_session_of(payload)
    tool_use_id = payload.get("tool_use_id")
    tool_name = payload.get("tool_name")
    error = payload.get("error")
    if (
        session is None
        or not isinstance(tool_use_id, str)
        or not tool_use_id
        or not isinstance(tool_name, str)
        or not tool_name
        or not isinstance(error, str)
        or not error.strip()
    ):
        return HookResult(name=name, output=f"{name} skipped: missing session, call or error")
    params = {
        "harness": harness,
        "host_session_id": session,
        "tool_use_id": tool_use_id,
        "tool_name": tool_name,
        "error": error[:8000],
        "interrupted": payload.get("is_interrupt") is True,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(_HOST_ERROR_METHOD, params)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    return HookResult(name=name, output=f"{name} ok event={answer.get('event_ref')}")


def register_host_question_hooks(
    runner: HookRunner,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> None:
    """Register the hooks that record host questions and failed host calls on *runner*."""

    def _question_hook(event: HookEvent) -> HookResult:
        return record_host_question(
            event, daemon_client_factory=daemon_client_factory, repo_root=repo_root
        )

    for event_type in _HOST_QUESTION_METHODS:
        runner.register(event_type, _question_hook, name="runtime.host_question")

    def _error_hook(event: HookEvent) -> HookResult:
        return record_host_tool_error(
            event, daemon_client_factory=daemon_client_factory, repo_root=repo_root
        )

    runner.register(
        HookEventType.POST_TOOL_USE_FAILURE, _error_hook, name="runtime.host_tool_error"
    )


__all__ = [
    "HOST_PERMISSION_BEHAVIOR",
    "HOST_PERMISSION_HOOK",
    "HOST_QUESTION_TOOL",
    "HOST_TOOL_HOOK",
    "host_permission_decision",
    "observe_host_tool",
    "record_host_file_edit",
    "record_host_permission",
    "record_host_question",
    "record_host_tool_error",
    "register_host_file_edit_hooks",
    "register_host_question_hooks",
    "register_host_tool_hooks",
]
