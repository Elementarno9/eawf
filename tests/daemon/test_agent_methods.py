"""Tests for the ``agent.*`` JSON-RPC handlers (P24-W07).

Covers the fresh-dispatch path of :func:`eawf.runtime.daemon.methods.agent.dispatch`
plus the inspection helpers :func:`agent.session` and the placeholder
:func:`agent.kill`. W07 ships only the fresh path; the suite asserts that
``session_policy="continue"`` is rejected with ``-32602 invalid params``
(via :class:`ValueError`, which the server maps to that code).

The handlers are driven directly through the module-level coroutines
— the JSON-RPC framing is exercised in :mod:`tests.daemon.test_scaffolding`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest import mock

import orjson
import pytest
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.state.enums import DispatchNote
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.kernel.store.envelope import Envelope
from eawf.observability.telemetry.pricing import PRICING_VERSION
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.agent import (
    _runtime_triple,
    dispatch,
    kill,
    pause,
    resume,
    session,
)

pytestmark = pytest.mark.unit


def _now() -> datetime:
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _build_ctx(
    *,
    state_path: Path | None = None,
    event_path: Path | None = None,
    bus: EventBus | None = None,
) -> MethodContext:
    return MethodContext(
        started_at="2026-05-19T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        state_path=state_path,
        event_path=event_path,
        bus=bus,
    )


def _read_envelopes(event_path: Path) -> list[Envelope]:
    """Return every envelope row from *event_path* in append order."""
    rows: list[Envelope] = []
    with event_path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                rows.append(Envelope.model_validate_json(line))
    return rows


def _read_event_payloads(event_path: Path) -> list[dict[str, Any]]:
    """Return the ``payload`` dict of every envelope row in append order."""
    return [env.payload for env in _read_envelopes(event_path)]


def _run(body: Callable[[], Awaitable[None]]) -> None:
    """Run an async test body without ``pytest-asyncio``."""
    asyncio.run(body())


def _build_state_payload(
    *,
    wave_id: str,
    sessions: dict[int, SessionAttempt] | None = None,
    runtime_preference: list[str] | None = None,
) -> dict[str, object]:
    """Construct a minimal valid State payload for fixtures."""
    sessions = sessions or {}
    wave = Wave.model_validate(
        {
            "id": wave_id,
            "iter_id": "P24-I01",
            "title": "agent-dispatch-test",
            "status": "in_progress",
            "opened_at": _now().isoformat(),
            "sessions": {str(k): v.model_dump(mode="json") for k, v in sessions.items()},
            "runtime_preference": runtime_preference,
        }
    )
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {wave_id: wave.model_dump(mode="json")},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _write_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(orjson.dumps(payload))


def test_dispatch_fresh_path_returns_attempt_one_with_uuid_session(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
        )
        assert result["attempt"] == 1
        assert result["runtime"] == "claude-code"
        # session_id is a UUID4 — round-trip parse.
        uuid.UUID(result["session_id"])
        assert result["annotation"]["note"] == DispatchNote.FRESH_DISPATCH.value
        assert result["annotation"]["attempt"] == 1
        assert result["session_attempt"]["attempt"] == 1
        assert result["session_attempt"]["runtime"] == "claude-code"
        assert result["session_attempt"]["session_log_handle"].startswith(
            "urn:eawf:v1:session-log:claude-code:"
        )

    _run(body)


def test_dispatch_uses_runtime_override_over_preference(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "runtime": "codex", "session_policy": "fresh"},
        )
        assert result["runtime"] == "codex"

    _run(body)


def test_dispatch_increments_attempt_when_wave_has_prior_sessions(
    tmp_path: Path,
) -> None:
    """A wave with one prior attempt yields attempt=2 on the next dispatch."""
    state_path = tmp_path / "state.json"
    prior = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="prior-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: prior},
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
        )
        assert result["attempt"] == 2
        # Same runtime as the prior attempt → no swap, fresh-dispatch note.
        assert result["annotation"]["note"] == DispatchNote.FRESH_DISPATCH.value
        assert result["annotation"]["runtime_from"] is None

    _run(body)


def test_dispatch_manual_runtime_override_emits_switch_manual(tmp_path: Path) -> None:
    """A runtime override that differs from the prior attempt is a manual swap.

    The fresh-dispatch path resolves the new runtime from an operator override
    (or preference) with no error involved, so the annotation note is
    ``SWITCH_MANUAL`` — ``SWITCH_ON_ERROR`` is reserved for the error-driven
    V5 reactive switch in :mod:`eawf.runtime.runtimes.fallback`.
    """
    state_path = tmp_path / "state.json"
    prior = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="prior-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: prior},
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "runtime": "codex", "session_policy": "fresh"},
        )
        assert result["attempt"] == 2
        assert result["runtime"] == "codex"
        annotation = result["annotation"]
        assert annotation["note"] == DispatchNote.SWITCH_MANUAL.value
        assert annotation["note"] != DispatchNote.SWITCH_ON_ERROR.value
        assert annotation["runtime_from"] == "claude-code"
        assert annotation["runtime_to"] == "codex"

    _run(body)


def test_dispatch_rejects_session_policy_continue(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="not implemented in W07"):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "continue"},
            )

    _run(body)


def test_dispatch_rejects_unknown_wave(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            runtime_preference=["claude-code"],
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await dispatch(
                ctx,
                {"wave_id": "P99-I01-W99", "session_policy": "fresh"},
            )

    _run(body)


def test_dispatch_rejects_extra_params(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "rogue": True},
            )

    _run(body)


def test_dispatch_fails_without_runtime_when_preference_missing(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=None),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="no runtime resolved"):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
            )

    _run(body)


def test_session_returns_typed_sessions_table(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    attempt_1 = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="abc-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    attempt_2 = SessionAttempt(
        attempt=2,
        runtime="codex",
        session_id="def-uuid",
        session_log_handle="urn:eawf:v1:session-log:codex:def",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: attempt_1, 2: attempt_2},
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07"})
        sessions_dict = result["sessions"]
        keys = sorted(int(k) for k in sessions_dict)
        assert keys == [1, 2]

    _run(body)


def test_session_returns_single_attempt_when_attempt_supplied(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "state.json"
    attempt_1 = SessionAttempt(
        attempt=1,
        runtime="claude-code",
        session_id="abc-uuid",
        session_log_handle="urn:eawf:v1:session-log:claude-code:abc",
        started_at=_now(),
    )
    attempt_2 = SessionAttempt(
        attempt=2,
        runtime="codex",
        session_id="def-uuid",
        session_log_handle="urn:eawf:v1:session-log:codex:def",
        started_at=_now(),
    )
    _write_state(
        state_path,
        _build_state_payload(
            wave_id="P24-I01-W07",
            sessions={1: attempt_1, 2: attempt_2},
        ),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07", "attempt": 2})
        keys = [int(k) for k in result["sessions"]]
        assert keys == [2]

    _run(body)


def test_session_returns_empty_when_attempt_missing(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", sessions={}),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await session(ctx, {"wave_id": "P24-I01-W07", "attempt": 99})
        assert result["sessions"] == {}

    _run(body)


def test_session_rejects_unknown_wave(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07"),
    )
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="unknown wave"):
            await session(ctx, {"wave_id": "P99-I01-W99"})

    _run(body)


def test_session_raises_runtime_error_without_state_path() -> None:
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await session(ctx, {"wave_id": "P24-I01-W07"})

    _run(body)


def _write_state_with_lane(
    path: Path,
    *,
    wave_id: str,
    attempt: int,
    pgid: int | None,
) -> None:
    """Write a State carrying a DRAINING FleetRun with one in-flight lane.

    The lane registers ``(wave_id, attempt) -> pgid`` so :func:`agent.kill` can
    resolve it; a ``None`` pgid records an unkillable lane (no addressable
    process group).
    """
    payload = _build_state_payload(wave_id=wave_id)
    payload["fleet_run"] = {
        "run_state": "draining",
        "concurrency": 1,
        "frontier": [],
        "lanes": {
            wave_id: {
                "wave_id": wave_id,
                "attempt": attempt,
                "session_id": "sess-1",
                "pgid": pgid,
                "dispatched_at": _now().isoformat(),
            }
        },
        "counters": {
            "claimed": 1,
            "dispatched": 1,
            "closed": 0,
            "forked": 0,
            "rounds": 0,
            "clean_rounds": 0,
        },
        "convergence": "drain",
        "kclean_k": 2,
        "terminal_reason": None,
        "armed_at": _now().isoformat(),
    }
    _write_state(path, payload)


def _lane_present(state_path: Path, *, wave_id: str) -> bool:
    """Return whether *wave_id* still has a registered lane in the FleetRun."""
    from eawf.workflow.evidence._io import load_state

    run = load_state(state_path).fleet_run
    return run is not None and wave_id in run.lanes


def _make_cancel(killpg: Callable[[int, int], None]) -> Callable[..., Any]:
    """Build a ``cancel_process_group`` stand-in over an injectable ``killpg``.

    Mirrors the real one-shot primitive's signal selection (SIGKILL on
    ``hard=True``, else SIGTERM) and its ``ProcessLookupError -> delivered=False``
    already-dead handling, but routes the syscall to *killpg* so the test
    records the call without delivering a real signal. Returns the typed
    :class:`~eawf.runtime.runtimes.cancel.CancelResult` the kill path reads.
    """
    from eawf.runtime.runtimes.cancel import CancelResult

    def _cancel(pgid: int, *, hard: bool = False) -> CancelResult:
        sig = signal.SIGKILL if hard else signal.SIGTERM
        try:
            killpg(pgid, sig)
        except ProcessLookupError:
            return CancelResult(pgid=pgid, signal_sent=sig, delivered=False)
        return CancelResult(pgid=pgid, signal_sent=sig, delivered=True)

    return _cancel


def test_kill_resolves_lane_and_sends_sigkill_for_signal_kill(tmp_path: Path) -> None:
    """C1: ``signal=kill`` resolves the lane + delivers SIGKILL via the registry."""
    import signal as _signal

    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=4242)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    def _fake_killpg(pgid: int, sig: int) -> None:
        sent.append((pgid, sig))

    async def body() -> None:
        with mock.patch.object(_fleet, "cancel_process_group", _make_cancel(_fake_killpg)):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
            )
        assert result == {"killed": True, "signal": "kill", "reason": None}
        assert sent == [(4242, _signal.SIGKILL)]
        # The killed lane deregisters from the registry.
        assert not _lane_present(state_path, wave_id="P24-I01-W07")

    _run(body)


def test_kill_resolves_lane_and_sends_sigterm_for_signal_halt(tmp_path: Path) -> None:
    """C1: ``signal=halt`` resolves the lane + delivers a graceful SIGTERM."""
    import signal as _signal

    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=4242)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "halt"}
            )
        assert result == {"killed": True, "signal": "halt", "reason": None}
        assert sent == [(4242, _signal.SIGTERM)]
        assert not _lane_present(state_path, wave_id="P24-I01-W07")

    _run(body)


def test_kill_defaults_signal_to_halt(tmp_path: Path) -> None:
    """C1: the default signal is the graceful ``halt`` (SIGTERM)."""
    import signal as _signal

    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=99)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(ctx, {"wave_id": "P24-I01-W07", "attempt": 1})
        assert result["killed"] is True
        assert result["signal"] == "halt"
        assert sent == [(99, _signal.SIGTERM)]

    _run(body)


def test_kill_legacy_term_alias_sends_sigterm(tmp_path: Path) -> None:
    """C1: the legacy ``term`` alias (autopilot's halt signal) still sends SIGTERM."""
    import signal as _signal

    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=7)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "term"}
            )
        assert result["killed"] is True
        assert sent == [(7, _signal.SIGTERM)]

    _run(body)


def test_kill_no_fleet_run_returns_not_found(tmp_path: Path) -> None:
    """C2: no fleet run armed -> typed not-found, never a faked kill."""
    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
            )
        assert result == {"killed": False, "signal": "kill", "reason": "no-fleet-run"}
        assert sent == []  # no signal on the not-found path

    _run(body)


def test_kill_attempt_mismatch_returns_not_found(tmp_path: Path) -> None:
    """C2: a stale attempt number resolves no lane -> typed not-found."""
    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=2, pgid=5)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
            )
        assert result == {"killed": False, "signal": "kill", "reason": "no-lane"}
        assert sent == []

    _run(body)


def test_kill_unkillable_lane_returns_not_found(tmp_path: Path) -> None:
    """C2: a lane with no addressable pgid returns not-found, never a fake kill."""
    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=None)
    ctx = _build_ctx(state_path=state_path)
    sent: list[tuple[int, int]] = []

    async def body() -> None:
        with mock.patch.object(
            _fleet, "cancel_process_group", _make_cancel(lambda p, s: sent.append((p, s)))
        ):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
            )
        assert result == {"killed": False, "signal": "kill", "reason": "unkillable-lane"}
        assert sent == []  # an unkillable lane is never signalled

    _run(body)


def test_kill_already_dead_group_degrades_to_killed_true(tmp_path: Path) -> None:
    """C2: a ProcessLookupError on the pgid degrades to killed=true already-dead."""
    from eawf.runtime.daemon.methods import fleet as _fleet

    state_path = tmp_path / "state.json"
    _write_state_with_lane(state_path, wave_id="P24-I01-W07", attempt=1, pgid=4242)
    ctx = _build_ctx(state_path=state_path)

    def _already_dead(pgid: int, sig: int) -> None:
        raise ProcessLookupError(f"no such process group: {pgid}")

    async def body() -> None:
        with mock.patch.object(_fleet, "cancel_process_group", _make_cancel(_already_dead)):
            result: dict[str, Any] = await kill(
                ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "kill"}
            )
        # Already-dead is not a crash: the group is gone, so the kill succeeds
        # and the lane deregisters.
        assert result == {"killed": True, "signal": "kill", "reason": None}
        assert not _lane_present(state_path, wave_id="P24-I01-W07")

    _run(body)


def test_kill_rejects_unknown_signal(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await kill(ctx, {"wave_id": "P24-I01-W07", "attempt": 1, "signal": "hup"})

    _run(body)


# --------------------------------------------------------------------------- #
# Dispatch-runner wiring: agent.dispatch is the production caller for
# eawf.runtime.daemon.dispatch_runner.run_dispatch. When a DispatchOutcome is
# supplied + the context carries an event_path, the C09 ``runtime_switched``
# (on a V5 fallback) + ``dispatch_cost`` events land in the live event log.
# --------------------------------------------------------------------------- #


def _outcome(
    *,
    primary_error: str | None = None,
    fallback_runtime: str | None = None,
) -> dict[str, Any]:
    """Construct a minimal valid ``DispatchOutcome`` param payload."""
    payload: dict[str, Any] = {
        "model": "claude-opus-4-7",
        "input_tokens": 1200,
        "output_tokens": 340,
        "cache_creation_input_tokens": 8000,
        "cache_read_input_tokens": 64000,
        "cost_usd": "0.123456",
    }
    if primary_error is not None:
        payload["primary_error"] = primary_error
    if fallback_runtime is not None:
        payload["fallback_runtime"] = fallback_runtime
    return payload


@pytest.mark.parametrize(
    ("runtime_id", "triple"),
    [("claude-code", "claude"), ("codex", "codex"), ("opencode", "opencode")],
)
def test_runtime_triple_maps_plugin_id_to_event_spelling(runtime_id: str, triple: str) -> None:
    assert _runtime_triple(runtime_id) == triple


def test_runtime_triple_rejects_unknown_runtime() -> None:
    with pytest.raises(ValueError, match="unknown runtime: 'goose'"):
        _runtime_triple("goose")


def test_dispatch_with_outcome_no_error_emits_only_dispatch_cost(tmp_path: Path) -> None:
    """An outcome with no primary_error emits a single ``dispatch_cost`` row."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _build_ctx(state_path=state_path, event_path=event_path, bus=EventBus())

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh", "outcome": _outcome()},
        )
        assert len(result["event_ids"]) == 1
        payloads = _read_event_payloads(event_path)
        assert [p["event_type"] for p in payloads] == ["dispatch_cost"]
        cost = payloads[0]
        assert cost["wave_id"] == "P24-I01-W07"
        assert cost["runtime"] == "claude"
        assert cost["cost_usd"] == "0.123456"
        assert cost["pricing_version"] == PRICING_VERSION

    _run(body)


def test_dispatch_with_outcome_primary_error_emits_switch_then_cost(tmp_path: Path) -> None:
    """A V5 fallback emits ``runtime_switched`` then ``dispatch_cost`` to the log."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["codex"]),
    )
    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _build_ctx(state_path=state_path, event_path=event_path, bus=EventBus())

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {
                "wave_id": "P24-I01-W07",
                "session_policy": "fresh",
                "outcome": _outcome(
                    primary_error="RUNTIME_RATE_LIMIT",
                    fallback_runtime="claude-code",
                ),
            },
        )
        assert len(result["event_ids"]) == 2
        envelopes = _read_envelopes(event_path)
        assert [e.payload["event_type"] for e in envelopes] == [
            "runtime_switched",
            "dispatch_cost",
        ]
        switched, cost = (e.payload for e in envelopes)
        assert switched["wave_id"] == "P24-I01-W07"
        assert switched["runtime_from"] == "codex"
        assert switched["runtime_to"] == "claude"
        assert switched["cause"] == "RUNTIME_RATE_LIMIT"
        # The serving (fallback) attempt is the one the cost is billed against.
        assert cost["runtime"] == "claude"
        assert switched["attempt_id_to"] == cost["attempt_id"]
        # The emitted-envelope ids ride back on the plan in append order.
        assert list(result["event_ids"]) == [e.id for e in envelopes]

    _run(body)


def test_dispatch_with_outcome_but_no_event_path_stays_plan_only(tmp_path: Path) -> None:
    """Without an event_path the dispatch cannot emit; plan-only, no event_ids."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path, event_path=None)

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh", "outcome": _outcome()},
        )
        assert result["event_ids"] == []

    _run(body)


def test_dispatch_without_outcome_emits_nothing(tmp_path: Path) -> None:
    """No outcome → no run_dispatch call even when an event_path is wired."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _build_ctx(state_path=state_path, event_path=event_path, bus=EventBus())

    async def body() -> None:
        result: dict[str, Any] = await dispatch(
            ctx,
            {"wave_id": "P24-I01-W07", "session_policy": "fresh"},
        )
        assert result["event_ids"] == []
        assert not event_path.exists()

    _run(body)


def test_dispatch_outcome_primary_error_without_fallback_raises(tmp_path: Path) -> None:
    """A primary_error with no fallback_runtime fails fast at -32602."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    event_path = tmp_path / "store" / "event.jsonl"
    ctx = _build_ctx(state_path=state_path, event_path=event_path, bus=EventBus())

    async def body() -> None:
        with pytest.raises(ValueError, match="fallback_runtime required"):
            await dispatch(
                ctx,
                {
                    "wave_id": "P24-I01-W07",
                    "session_policy": "fresh",
                    "outcome": _outcome(primary_error="RUNTIME_TIMEOUT"),
                },
            )

    _run(body)


def test_dispatch_outcome_rejects_extra_fields(tmp_path: Path) -> None:
    """The ``DispatchOutcome`` model forbids unknown keys (extra='forbid')."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path, event_path=tmp_path / "store" / "event.jsonl")

    async def body() -> None:
        bad = _outcome()
        bad["rogue"] = True
        with pytest.raises(ValidationError):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "fresh", "outcome": bad},
            )

    _run(body)


def test_dispatch_outcome_rejects_negative_tokens(tmp_path: Path) -> None:
    """Negative token counts violate the ge=0 bound on ``DispatchOutcome``."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path, event_path=tmp_path / "store" / "event.jsonl")

    async def body() -> None:
        bad = _outcome()
        bad["input_tokens"] = -1
        with pytest.raises(ValidationError):
            await dispatch(
                ctx,
                {"wave_id": "P24-I01-W07", "session_policy": "fresh", "outcome": bad},
            )

    _run(body)


def test_dispatch_outcome_rejects_unknown_fallback_runtime(tmp_path: Path) -> None:
    """An off-roster fallback runtime is rejected by the RuntimeId literal."""
    state_path = tmp_path / "state.json"
    _write_state(
        state_path,
        _build_state_payload(wave_id="P24-I01-W07", runtime_preference=["claude-code"]),
    )
    ctx = _build_ctx(state_path=state_path, event_path=tmp_path / "store" / "event.jsonl")

    async def body() -> None:
        with pytest.raises(ValidationError):
            await dispatch(
                ctx,
                {
                    "wave_id": "P24-I01-W07",
                    "session_policy": "fresh",
                    "outcome": _outcome(
                        primary_error="RUNTIME_TIMEOUT",
                        fallback_runtime="goose",
                    ),
                },
            )

    _run(body)


# --------------------------------------------------------------------------
# agent.pause / agent.resume -- the cooperative dispatch-gate flag (P29-I09-W05)
# --------------------------------------------------------------------------


def _read_dispatch_paused(state_path: Path) -> bool:
    """Return the persisted ``dispatch_paused`` flag off the on-disk state.

    Defaults to ``False`` when the key is absent: a pre-bump fixture payload
    legitimately omits it (the model defaults it on load), so absence reads as
    the not-paused default rather than a missing-key error.
    """
    payload = orjson.loads(state_path.read_bytes())
    return bool(payload.get("dispatch_paused", False))


def test_pause_sets_dispatch_paused_true_and_persists(tmp_path: Path) -> None:
    """``agent.pause`` returns ``paused=True`` and persists the flag to disk."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    assert _read_dispatch_paused(state_path) is False
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await pause(ctx, {})
        assert result == {"paused": True}
        assert _read_dispatch_paused(state_path) is True

    _run(body)


def test_resume_sets_dispatch_paused_false_and_persists(tmp_path: Path) -> None:
    """``agent.resume`` returns ``paused=False`` and clears the persisted flag."""
    state_path = tmp_path / "state.json"
    payload = _build_state_payload(wave_id="P24-I01-W07")
    payload["dispatch_paused"] = True
    _write_state(state_path, payload)
    assert _read_dispatch_paused(state_path) is True
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        result: dict[str, Any] = await resume(ctx, {})
        assert result == {"paused": False}
        assert _read_dispatch_paused(state_path) is False

    _run(body)


def test_pause_is_idempotent(tmp_path: Path) -> None:
    """Pausing an already-paused state re-asserts the flag (no toggle)."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        first: dict[str, Any] = await pause(ctx, {})
        second: dict[str, Any] = await pause(ctx, {})
        assert first == second == {"paused": True}
        assert _read_dispatch_paused(state_path) is True

    _run(body)


def test_resume_is_idempotent(tmp_path: Path) -> None:
    """Resuming an already-running state re-asserts the cleared flag."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        first: dict[str, Any] = await resume(ctx, {})
        second: dict[str, Any] = await resume(ctx, {})
        assert first == second == {"paused": False}
        assert _read_dispatch_paused(state_path) is False

    _run(body)


def test_pause_resume_round_trip_persists_each_step(tmp_path: Path) -> None:
    """A pause then resume leaves the flag cleared, persisting each transition."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        await pause(ctx, {})
        assert _read_dispatch_paused(state_path) is True
        await resume(ctx, {})
        assert _read_dispatch_paused(state_path) is False

    _run(body)


def test_pause_missing_state_path_raises(tmp_path: Path) -> None:
    """``agent.pause`` raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await pause(ctx, {})

    _run(body)


def test_resume_missing_state_path_raises(tmp_path: Path) -> None:
    """``agent.resume`` raises when the daemon context has no state path."""
    ctx = _build_ctx(state_path=None)

    async def body() -> None:
        with pytest.raises(RuntimeError, match="state_path not configured"):
            await resume(ctx, {})

    _run(body)


def test_pause_rejects_extra_params(tmp_path: Path) -> None:
    """``agent.pause`` rejects a stray param (``extra='forbid'`` on PauseParams)."""
    state_path = tmp_path / "state.json"
    _write_state(state_path, _build_state_payload(wave_id="P24-I01-W07"))
    ctx = _build_ctx(state_path=state_path)

    async def body() -> None:
        with pytest.raises(ValidationError):
            await pause(ctx, {"unexpected": "value"})

    _run(body)
