"""Lifecycle hooks: exit stamp and subagent adoption.

A host session's lifecycle events reach eawf only through its hooks. The exit hook stamps
the ending session's ``ended_at``, and a harness-spawned subagent is adopted as a Run. A
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


def stamp_session_end_on_exit(
    event: HookEvent,
    *,
    repo_root: Path | None = None,
) -> HookResult:
    """Stamp the exiting session's ``ended_at`` from a SESSION_END / AGENT_END event.

    The exit hook is the only place that knows the true end instant. Before
    this seam ``ended_at`` was written by the daemon-boot orphan reconcile, so a
    session that ended at 14:02 was recorded as ending whenever the daemon next
    restarted and every derived duration was fiction. Stamping here also means
    the reconcile finds nothing left to flip on a clean exit.

    Never blocks the source runtime: an unresolvable repo root, an absent
    ``state.json``, or an ambiguous session all degrade to a non-blocking
    no-op result, matching how the sibling ``runtime.host_subagent`` hook fails.

    Args:
        event: The SESSION_END or AGENT_END hook event.
        repo_root: Repo root whose ``.ea/state.json`` owns the session rows;
            defaults to the process working directory.

    Returns:
        A non-blocking :class:`HookResult` naming the stamped session id, or
        the reason no row was stamped.
    """
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.paths import store_path
    from eawf.runtime.session.store import stamp_session_end_at_exit

    root = repo_root if repo_root is not None else Path.cwd()
    state_path = root / ".ea" / "state.json"
    payload = runner._session_end_payload(event)
    raw_session_id = payload.get("session_id")
    runtime_session_id = raw_session_id if isinstance(raw_session_id, str) else None
    try:
        stamped = stamp_session_end_at_exit(
            state_path,
            store_path(state_path, StoreKind.EVENT),
            runtime_session_id=runtime_session_id or None,
            scope_id=event.scope_id or None,
            summary=f"stamped at process exit ({event.event_type.value})",
            now=event.occurred_at,
        )
    except Exception as exc:
        return HookResult(name="session.end_stamp", block=False, output=repr(exc))
    if stamped is None:
        return HookResult(
            name="session.end_stamp",
            block=False,
            output="session.end_stamp skipped: no live session resolved",
        )
    return HookResult(
        name="session.end_stamp",
        block=False,
        output=f"session.end_stamp ok id={stamped}",
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
    "stamp_session_end_on_exit",
]
