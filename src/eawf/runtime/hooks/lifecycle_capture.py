"""Lifecycle hooks: runtime counters at session end, Codex lifecycle, exit stamp, subagents.

A host session's lifecycle events reach eawf only through its hooks. The session-end hook
forwards the session's runtime counters to ``runtime.capture``; a Codex lifecycle event is
forwarded as the provider states it; the exit hook stamps the ending session's
``ended_at``; and a harness-spawned subagent is adopted as a Run. None of them blocks the
host: every failure degrades to a non-blocking :class:`~eawf.runtime.hooks.runner.HookResult`
that names why.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eawf.runtime.hooks import runner
from eawf.runtime.hooks.event import HOST_HARNESSES, HookEvent, HookEventType
from eawf.runtime.hooks.runner import DaemonClientFactory, HookResult

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters


def _coerce_cost_usd_string(raw: Any) -> Decimal | None:
    """Return a string ``cost_usd`` (e.g. ``"0.82"``) as a non-negative Decimal.

    Claude Code's Stop / SessionEnd / SubagentStop hook stdin carries **no** cost
    block at all -- the counters live in the session transcript the payload
    points at (see :func:`_transcript_counters`). This coercion exists for the
    statusline-shaped payloads a wrapper may forward instead, where ``cost_usd``
    can arrive as a JSON string while
    :func:`~eawf.runtime.runtimes.claude.runtime_counters.parse_runtime_counters`
    accepts only a numeric value. Returning a :class:`~decimal.Decimal` keeps the
    value exact through the parser without touching the statusline contract. A
    non-string, malformed, non-finite, or negative value yields ``None`` so the
    field is dropped rather than crashing the fail-open hook.
    """
    if not isinstance(raw, str):
        return None
    try:
        value = Decimal(raw)
    except InvalidOperation:
        return None
    if not value.is_finite() or value < 0:
        return None
    return value


def _normalise_claude_hook_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Adapt a counter-carrying hook stdin payload to the statusline parser shape.

    This is the *fallback* counter path. A real Claude Code Stop / SessionEnd /
    SubagentStop payload carries neither ``cost`` nor ``usage`` -- its counters
    are read from the transcript instead (:func:`_transcript_counters`) -- but a
    forwarding wrapper may hand the hook a payload that does carry a flat
    ``usage`` block or a string ``cost_usd`` inside ``cost``, while
    :func:`~eawf.runtime.runtimes.claude.runtime_counters.parse_runtime_counters`
    was built against the statusline shape (tokens under
    ``context_window.current_usage``, a numeric ``cost_usd``). This adapter
    bridges the two so the shared parser stays the single counter authority:

    - a flat ``usage`` mapping is lifted to ``context_window.current_usage``
      (only when the payload does not already carry a ``context_window``, so a
      genuine statusline payload passes through untouched); and
    - a string ``cost_usd`` inside ``cost`` is coerced to a numeric value.

    The function is a no-op for a payload already in the statusline shape, so
    existing statusline callers keep their behaviour.
    """
    normalised = dict(payload)

    usage = payload.get("usage")
    if isinstance(usage, dict) and not isinstance(payload.get("context_window"), dict):
        normalised["context_window"] = {"current_usage": usage}

    cost = payload.get("cost")
    if isinstance(cost, dict):
        cost_copy = dict(cost)
        if isinstance(cost_copy.get("cost_usd"), str):
            coerced = _coerce_cost_usd_string(cost_copy["cost_usd"])
            if coerced is not None:
                cost_copy["cost_usd"] = coerced
            else:
                cost_copy.pop("cost_usd", None)
        normalised["cost"] = cost_copy

    return normalised


def _transcript_counters(payload: dict[str, Any]) -> RuntimeCounters | None:
    """Aggregate the session transcript *payload* points at, when it has one.

    The Stop / SessionEnd payload's ``transcript_path`` is where Claude Code's
    runtime facts actually live (token usage, turn durations, the billed model
    id). Reading it is therefore the primary counter source; a payload without a
    usable ``transcript_path`` yields ``None`` and the caller falls back to the
    statusline-shaped parse.
    """
    from eawf.runtime.runtimes.claude.transcript_counters import aggregate_transcript_counters

    raw = payload.get("transcript_path")
    if not isinstance(raw, str) or not raw:
        return None
    return aggregate_transcript_counters(raw)


def _codex_lifecycle_params(
    event: HookEvent,
    payload: dict[str, Any],
    *,
    repo_root: Path | None,
) -> tuple[dict[str, Any] | None, RuntimeCounters | None]:
    """Build strict Codex lifecycle params plus exact rollout counters."""
    provider_session_id = payload.get("session_id")
    if not isinstance(provider_session_id, str) or not provider_session_id:
        return None, None

    params: dict[str, Any] = {
        "event_type": event.event_type.value,
        "provider_session_id": provider_session_id,
        "occurred_at": event.occurred_at.isoformat(),
    }
    if repo_root is not None:
        params["repo_root"] = str(repo_root)
    agent_id = payload.get("agent_id")
    if isinstance(agent_id, str) and agent_id:
        params["agent_id"] = agent_id

    counters: RuntimeCounters | None = None
    if event.event_type in {HookEventType.SUBAGENT_STOP, HookEventType.SESSION_END}:
        from eawf.runtime.runtimes.codex.rollout_counters import (
            read_codex_rollout_counters,
        )

        transcript_key = (
            "agent_transcript_path"
            if event.event_type == HookEventType.SUBAGENT_STOP
            else "transcript_path"
        )
        raw_path = payload.get(transcript_key)
        if isinstance(raw_path, str) and raw_path:
            expected_session_id = (
                agent_id
                if event.event_type == HookEventType.SUBAGENT_STOP
                and isinstance(agent_id, str)
                and agent_id
                else provider_session_id
            )
            capture = read_codex_rollout_counters(
                Path(raw_path),
                expected_session_id=expected_session_id,
            )
            counters = capture.counters
            params["agent_transcript_path"] = raw_path
            params["measurement_quality"] = capture.measurement_quality.value
            params["measurement_status"] = capture.measurement_status.value
            params["measurement_reason"] = capture.measurement_reason
        else:
            params["measurement_quality"] = "unavailable"
            params["measurement_status"] = "usage_unavailable"
            params["measurement_reason"] = "missing_transcript_path"
    if counters is not None:
        params["counters"] = counters.model_dump(mode="json")
    return params, counters


def capture_codex_lifecycle(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> tuple[HookResult, dict[str, Any] | None, RuntimeCounters | None]:
    """Forward one provider-native Codex lifecycle event to the daemon."""
    if event.runtime != "codex":
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output="runtime.codex_lifecycle skipped: non-codex runtime",
            ),
            None,
            None,
        )
    payload = runner._session_end_payload(event)
    params, counters = _codex_lifecycle_params(event, payload, repo_root=repo_root)
    if params is None:
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output="runtime.codex_lifecycle skipped: missing session_id",
            ),
            None,
            None,
        )
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            response = client.call("runtime.codex_lifecycle", params)
    except Exception as exc:
        return (
            HookResult(
                name="runtime.codex_lifecycle",
                block=False,
                output=repr(exc),
            ),
            None,
            counters,
        )
    correlated = response.get("correlated") is True
    reason = response.get("reason")
    output = (
        "runtime.codex_lifecycle ok"
        if correlated
        else f"runtime.codex_lifecycle unavailable: {reason or 'uncorrelated'}"
    )
    return (
        HookResult(
            name="runtime.codex_lifecycle",
            block=False,
            output=output,
        ),
        response,
        counters,
    )


def _capture_codex_session_end(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None,
    repo_root: Path | None,
) -> HookResult:
    lifecycle_result, response, counters = capture_codex_lifecycle(
        event,
        daemon_client_factory=daemon_client_factory,
        repo_root=repo_root,
    )
    if response is None or response.get("correlated") is not True:
        return lifecycle_result
    if counters is None:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=(f"{lifecycle_result.output}; runtime.capture skipped: usage unavailable"),
        )
    params = counters.model_dump(mode="json")
    payload = runner._session_end_payload(event)
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["session_id"] = session_id
    wave_id = response.get("wave_id")
    if isinstance(wave_id, str) and wave_id:
        params["wave_id"] = wave_id
    params["captured_at"] = event.occurred_at.isoformat()
    if repo_root is not None:
        params["repo_root"] = str(repo_root)
    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            client.call("runtime.capture", params)
    except Exception as exc:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=repr(exc),
        )
    return HookResult(
        name="runtime.capture",
        block=False,
        output=f"{lifecycle_result.output}; runtime.capture ok",
    )


def capture_runtime_on_session_end(
    event: HookEvent,
    *,
    daemon_client_factory: DaemonClientFactory | None = None,
    repo_root: Path | None = None,
) -> HookResult:
    """Forward parsed SESSION_END runtime counters to ``runtime.capture``.

    Counters come from the session transcript the payload points at, falling
    back to a statusline-shaped parse of the payload itself. The hook never
    blocks the source runtime: a payload with no usable counter (no readable
    transcript and no statusline block) is a clean no-op, and daemon failures
    are surfaced in a non-blocking result so Claude's Stop hook degrades like the
    statusline path.
    """
    from eawf.runtime.runtimes.claude.runtime_counters import parse_runtime_counters

    if event.runtime == "codex":
        return _capture_codex_session_end(
            event,
            daemon_client_factory=daemon_client_factory,
            repo_root=repo_root,
        )

    payload = runner._session_end_payload(event)
    counters = _transcript_counters(payload) or parse_runtime_counters(
        _normalise_claude_hook_payload(payload)
    )
    if counters is None:
        return HookResult(
            name="runtime.capture",
            block=False,
            output="runtime.capture skipped: no usable counters",
        )

    params = counters.model_dump(mode="json")
    session_id = payload.get("session_id")
    if isinstance(session_id, str) and session_id:
        params["session_id"] = session_id
    params["captured_at"] = event.occurred_at.isoformat()
    if repo_root is not None:
        params["repo_root"] = str(repo_root)

    factory = daemon_client_factory or runner._default_daemon_client_factory
    try:
        with factory() as client:
            client.call("runtime.capture", params)
    except Exception as exc:
        return HookResult(
            name="runtime.capture",
            block=False,
            output=repr(exc),
        )
    return HookResult(
        name="runtime.capture",
        block=False,
        output="runtime.capture ok",
    )


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
    no-op result, matching how the sibling ``runtime.capture`` hook fails.

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
    "capture_codex_lifecycle",
    "capture_runtime_on_session_end",
    "stamp_session_end_on_exit",
]
