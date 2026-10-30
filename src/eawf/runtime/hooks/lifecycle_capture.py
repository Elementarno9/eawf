"""Lifecycle hooks: session end and subagent adoption.

A host session's lifecycle events reach eawf only through its hooks. The exit hook answers
without writing, and a harness-spawned subagent is adopted as a Run. A
Run's runtime counters are not forwarded from here: the measurement producer reads them
through the Run's vendor session. Neither hook blocks the host: every failure degrades to a
non-blocking :class:`~eawf.runtime.hooks.runner.HookResult` that names why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from eawf.runtime.hooks import runner
from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent, HookEventType
from eawf.runtime.hooks.runner import DaemonClientFactory, HookResult


def answer_session_end(event: HookEvent) -> HookResult:
    """Answer a SESSION_END or AGENT_END event without writing anything.

    No native record is stamped on exit, so the answer is the no-op a tree
    without a live session always got.

    Args:
        event: The SESSION_END or AGENT_END hook event; the runner's handler
            contract passes it, and nothing in it changes the answer.

    Returns:
        A non-blocking :class:`HookResult` saying no session row was stamped.
    """
    return HookResult(
        name="session.end_stamp",
        block=False,
        output="session.end_stamp skipped: no live session resolved",
    )


#: The adoption verb each subagent event calls.
HOST_SUBAGENT_METHODS: dict[HookEventType, str] = {
    HookEventType.SUBAGENT_START: "runtime.host.subagent.start",
    HookEventType.SUBAGENT_STOP: "runtime.host.subagent.stop",
}


def adopt_host_subagent(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Adopt a harness-spawned subagent as a Run, or name why it was not.

    The start event admits and starts the Run; the stop event bridges the
    subagent's own transcript into it and completes it. The daemon writes the
    Run, so every outcome is either that durable row or the daemon's typed
    refusal -- a tree still in epoch 1 answers ``native_authority_required``
    -- and the hook never blocks the host over it.

    Args:
        event: The SUBAGENT_START or SUBAGENT_STOP event.
        daemon_client_factory: Opens the daemon client; the default one when
            ``None``.
        repo_root: The repository the harness runs in; the process working
            directory when ``None``.

    Returns:
        A non-blocking :class:`HookResult` naming the adopted Run, or the
        reason no Run was written.
    """
    name = "runtime.host_subagent"
    harness = HOST_HARNESSES.get(event.runtime)
    if harness is None:
        return HookResult(name=name, output=f"{name} skipped: {event.runtime} spawns no subagent")
    payload = runner._session_end_payload(event)
    agent_id = payload.get("agent_id")
    if not isinstance(agent_id, str) or not agent_id:
        return HookResult(name=name, output=f"{name} skipped: missing agent_id")
    params: dict[str, Any] = {
        "harness": harness,
        "agent_id": agent_id,
        "repo_root": str(repo_root if repo_root is not None else Path.cwd()),
    }
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["host_session_id"] = session_id
    transcript = payload.get("agent_transcript_path")
    if (
        event.event_type is HookEventType.SUBAGENT_STOP
        and isinstance(transcript, str)
        and transcript
    ):
        params["transcript_path"] = transcript
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            answer = client.call(HOST_SUBAGENT_METHODS[event.event_type], params)
    except Exception as exc:
        return HookResult(name=name, output=repr(exc))
    return HookResult(name=name, output=f"{name} ok run={answer.get('run_ref')}")


__all__ = [
    "HOST_SUBAGENT_METHODS",
    "adopt_host_subagent",
    "answer_session_end",
]
