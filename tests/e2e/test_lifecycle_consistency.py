"""Cross-runtime lifecycle-consistency gate for the P30-I25 bug cluster.

P30-I25 fixed a headless-dispatch lifecycle bug cluster (R1-R4) that only
reproduces under a LIVE spawn -- the in-process unit suite stayed green while
the bugs shipped, because the process-spawn / claim / close / reap timing that
breaks them never executes in a ``CliRunner`` harness. The fixes landed on this
branch:

* **W02** -- ``_persist_live_session_attempt`` drops the attempt when the wave
  reached a terminal status while the (intentionally unlocked) spawn was in
  flight, so no phantom ``SessionAttempt`` / ``dispatch_cost`` lands after the
  wave closes (the R3 leak observed ~50s post-close).
* **W03** -- ``_Loop._reap_orphan_claims`` scans disk at the drained-run seam
  and drives a lane-less CLAIMED / IN_PROGRESS wave whose claiming process
  group reads dead to terminal FAILED (the R4 wedge).
* **W04** -- ``classify_floor_failure`` labels a refused deterministic
  close-gate ENVIRONMENTAL (missing repo scaffolding the executor cannot create
  in-scope), so the fleet seam closes-with-followups instead of burning the
  repair-ladder budget re-dispatching an unfixable lane.
* **W05 / W07** -- an interactive-claude / headless spawn mints a per-attempt
  ``SessionAttempt.cost_usd`` so a wave dispatched more than once surfaces the
  cost of EACH attempt, not just the single whole-wave ``runtime_latest``
  snapshot.

This module is the *discriminating experiment* for the cluster, in three parts:

* **Part A** -- pure module-level invariant assertions over a drive's resulting
  ``State`` (+ event stream). Each raises a clear ``AssertionError`` naming the
  offending wave.
* **Part B** -- DEFAULT-runnable tests with teeth. The four assertions are
  driven against SYNTHETIC in-memory ``State`` fixtures in BOTH directions: a
  healthy 2-runtime drive PASSES all four, and a regressed drive FAILS the
  matching assertion (``pytest.raises(AssertionError)``). This is what makes the
  gate real without a paid spawn: if a future change re-breaks a fix, the
  assertion's teeth catch the state shape.
* **Part C** -- the LIVE cross-runtime drive (opt-in; skipped unless
  ``EAWF_LIVE_E2E`` is set). It configures BOTH runtime adapters in an isolated
  sandbox (spawning the daemon with a long idle timeout so real minutes-long
  spawns do not trip the fixture default mid-drive), seeds a smoke frontier with
  a codex-runtime wave and a claude-runtime wave (plus a wedged orphan), drives
  the fleet to terminal
  through the real ``fleet.drive`` RPC, and applies all four Part-A assertions
  to the resulting sandbox state. This is the operator's on-demand ship-gate
  check; it does NOT run in CI.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import EffortBucket, WaveStatus
from eawf.kernel.state.models import SessionAttempt, State, Wave
from eawf.surfaces.tui.screens.overlays.detail_cost import (
    attempt_is_priced,
    wave_cost_rollup_for_wave,
)
from tests.e2e.conftest import E2EEnv

# AF_UNIX + fork-based detach are POSIX-only; the Part-C live drive speaks to
# the real UDS daemon and the whole tier mirrors the sibling e2e modules. The
# Part-B teeth tests are pure in-memory helper logic, but CI is never Windows,
# so gating the module off there costs no coverage on the platforms that run it.
pytestmark = [
    pytest.mark.e2e,
    pytest.mark.skipif(
        sys.platform.startswith("win"),
        reason="E2E tier drives the POSIX UDS daemon; Windows pipe is out of scope",
    ),
]

#: A committed state fixture carrying an active phase / iter / wave, reused as
#: the schema-valid base every synthetic drive fixture rewrites its waves onto.
_BASE_STATE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "states"
    / "valid"
    / "03-phase-iter-wave-active.json"
)

_ITER_ID = "P01-I01"

#: A fixed drive anchor so every synthetic timestamp is deterministic and
#: relative ordering (attempt-before-close vs attempt-after-close) is explicit.
_T0 = datetime(2026, 7, 7, 12, 0, tzinfo=UTC)

#: Wave statuses that mean a lane has stopped moving -- a drained run must leave
#: no wave outside this set.
_TERMINAL_STATUSES = frozenset({WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED})

#: Event types that mark a wave's terminal transition in a drive event stream.
_TERMINAL_EVENT_TYPES = frozenset({"wave_closed", "wave_failed", "wave_abandoned"})

#: Event types that represent a dispatch / attempt persist against a wave -- the
#: rows that must never carry a timestamp past the wave's terminal event.
_DISPATCH_EVENT_TYPES = frozenset(
    {"dispatch", "dispatch_cost", "session_attempt", "runtime_switched"}
)


# ==========================================================================
# Part A -- shared invariant assertions (pure helpers over post-drive state)
# ==========================================================================


@dataclass(frozen=True)
class DriveEvent:
    """One timestamped lifecycle event in a drive's per-wave event stream.

    A thin normalisation over the two real sources a drive leaves behind: the
    wave's terminal transition (from ``State``) and its dispatch / attempt rows
    (from ``State.sessions`` and the on-disk ``event.jsonl``). Normalising both
    into one ``(event_type, ts, wave_id)`` shape lets the post-close ordering
    invariant compare a dispatch timestamp against the close timestamp without
    caring which store each came from.

    Attributes:
        event_type: The transition tag (see :data:`_TERMINAL_EVENT_TYPES` and
            :data:`_DISPATCH_EVENT_TYPES`).
        ts: When the transition occurred.
        wave_id: The ``W<NN>`` wave the event belongs to.
    """

    event_type: str
    ts: datetime
    wave_id: str


def drive_events_for_wave(wave: Wave) -> list[DriveEvent]:
    """Return the lifecycle event stream a single wave's ``State`` row implies.

    Folds the wave's terminal transition (its ``closed_at`` when the status is
    terminal) and one ``session_attempt`` row per persisted
    :class:`~eawf.kernel.state.models.SessionAttempt` (at its ``started_at``)
    into a single ordered :class:`DriveEvent` list. This is the state-only view
    of the post-close ordering invariant: a phantom attempt persisted after the
    wave closed (the R3 leak W02 fixed) surfaces as a ``session_attempt`` whose
    timestamp is past the ``wave_closed`` timestamp.

    Args:
        wave: The resolved post-drive wave.

    Returns:
        The wave's ``(terminal? + per-attempt)`` drive events in append order.
    """
    events: list[DriveEvent] = []
    if wave.closed_at is not None and wave.status in _TERMINAL_STATUSES:
        terminal_type = "wave_failed" if wave.status is WaveStatus.FAILED else "wave_closed"
        events.append(DriveEvent(event_type=terminal_type, ts=wave.closed_at, wave_id=wave.id))
    for _attempt_no, attempt in sorted(wave.sessions.items()):
        events.append(
            DriveEvent(event_type="session_attempt", ts=attempt.started_at, wave_id=wave.id)
        )
    return events


def assert_one_attempt_per_execution(
    state: State, wave_id: str, *, expected_executions: int = 1
) -> None:
    """Assert *wave_id* persisted exactly one ``SessionAttempt`` per execution.

    A wave driven once and closed must carry exactly one persisted attempt; a
    phantom extra attempt (the R3 leak: a spawn that raced the close and
    persisted anyway) inflates the count. ``expected_executions`` is the number
    of genuine dispatches the wave underwent (``1`` for a clean single-pass
    drive).

    Args:
        state: The post-drive bound state.
        wave_id: The wave to check.
        expected_executions: The count of genuine executions the wave underwent.

    Raises:
        AssertionError: When the wave is unknown, or its persisted attempt count
            differs from *expected_executions*.
    """
    wave = state.waves.get(wave_id)
    assert wave is not None, f"wave {wave_id!r} is not present in state"
    persisted = len(wave.sessions)
    assert persisted == expected_executions, (
        f"wave {wave_id!r} persisted {persisted} SessionAttempt row(s) for "
        f"{expected_executions} genuine execution(s) -- a phantom attempt leaked "
        f"(attempts={sorted(wave.sessions)})"
    )


def assert_no_post_close_dispatch(events: Sequence[DriveEvent], wave_id: str) -> None:
    """Assert no dispatch event for *wave_id* is timestamped after its close.

    Finds the wave's terminal (close / fail) event in the drive stream and
    asserts no dispatch / attempt event for the same wave carries a later
    timestamp. This pins the R3 fix: after ``close_wave`` no ``SessionAttempt``
    nor ``dispatch_cost`` may persist against the now-terminal wave.

    Args:
        events: The wave's drive event stream (state-derived, optionally folded
            with on-disk ``event.jsonl`` dispatch rows).
        wave_id: The wave to check.

    Raises:
        AssertionError: When no terminal event is recorded for the wave (the
            check would otherwise be vacuous), or a dispatch event is timestamped
            strictly after the terminal event.
    """
    wave_events = [event for event in events if event.wave_id == wave_id]
    terminal = [event for event in wave_events if event.event_type in _TERMINAL_EVENT_TYPES]
    assert terminal, (
        f"wave {wave_id!r} has no terminal (close/fail) event in the drive stream; "
        f"the post-close ordering check would be vacuous"
    )
    close_ts = min(event.ts for event in terminal)
    offenders = [
        event
        for event in wave_events
        if event.event_type in _DISPATCH_EVENT_TYPES and event.ts > close_ts
    ]
    assert not offenders, (
        f"wave {wave_id!r} has {len(offenders)} dispatch event(s) timestamped after its "
        f"close at {close_ts.isoformat()}: "
        f"{[(event.event_type, event.ts.isoformat()) for event in offenders]}"
    )


def assert_orphan_claim_failed(state: State, wave_id: str) -> None:
    """Assert a wedged claimed wave was reaped to FAILED, not left stuck.

    The R4 gap left a wave CLAIMED / IN_PROGRESS forever when it wedged before
    its lane registered and its driving run then drained. W03's reaper drives
    such a wave to terminal FAILED. This asserts the wave reached FAILED (never
    still CLAIMED / IN_PROGRESS / PENDING).

    Args:
        state: The post-drive bound state.
        wave_id: The orphan wave to check.

    Raises:
        AssertionError: When the wave is unknown, or its status is not FAILED.
    """
    wave = state.waves.get(wave_id)
    assert wave is not None, f"wave {wave_id!r} is not present in state"
    assert wave.status is WaveStatus.FAILED, (
        f"wave {wave_id!r} wedged but was not reaped: status={wave.status.value!r} "
        f"(expected {WaveStatus.FAILED.value!r} -- a drained run left an orphan claim)"
    )


def assert_per_attempt_cost_present(
    state: State, wave_id: str, *, state_path: Path | None = None
) -> None:
    """Assert *wave_id* surfaces a priced per-attempt ``cost_usd``.

    Reuses the detail-cost rollup entry point
    (:func:`~eawf.surfaces.tui.screens.overlays.detail_cost.wave_cost_rollup_for_wave`),
    the same join the wave-detail ``$`` tab reads, and asserts every joined
    attempt row is priced. ``state_path`` points the join at a telemetry DB;
    the default (a path with no DB) forces the runtime-snapshot fallback that
    reads each attempt's OWN :attr:`SessionAttempt.cost_usd` -- so a wave whose
    attempts carry no per-attempt cost yields an empty / un-priced rollup and
    trips this check.

    Args:
        state: The post-drive bound state.
        wave_id: The wave to check.
        state_path: Path the telemetry DB is resolved from; defaults to a path
            with no DB so the check exercises the stored per-attempt cost.

    Raises:
        AssertionError: When the wave is unknown, surfaces no attempt rollup, or
            any joined attempt row is un-priced.
    """
    wave = state.waves.get(wave_id)
    assert wave is not None, f"wave {wave_id!r} is not present in state"
    resolved_path = state_path if state_path is not None else Path("/nonexistent/.ea/state.json")
    rollup = wave_cost_rollup_for_wave(state, wave_id, resolved_path)
    assert rollup is not None and rollup.attempts, (
        f"wave {wave_id!r} surfaces no per-attempt cost rollup -- no attempt "
        f"carried a priced cost_usd"
    )
    unpriced = [row.attempt for row in rollup.attempts if not attempt_is_priced(row)]
    assert not unpriced, (
        f"wave {wave_id!r} has un-priced attempt(s) {unpriced} -- a genuine "
        f"execution surfaced no per-attempt cost_usd"
    )


# ==========================================================================
# Part B -- DEFAULT-runnable teeth (synthetic in-memory State, no live spawn)
# ==========================================================================


def _base_state() -> State:
    """Return the committed active phase/iter/wave fixture as a bound state."""
    return State.model_validate(orjson.loads(_BASE_STATE.read_bytes()))


def _attempt(
    *,
    attempt: int,
    runtime: str,
    started_at: datetime,
    ended_at: datetime | None = None,
    cost_usd: float | None = 0.1234,
    subprocess_pid: int | None = None,
) -> SessionAttempt:
    """Build one synthetic ``SessionAttempt`` row for a drive fixture."""
    return SessionAttempt(
        attempt=attempt,
        runtime=runtime,
        session_id=f"sess-{runtime}-{attempt}",
        session_log_handle=f"urn:eawf:v1:session-log:{runtime}:sess-{attempt}",
        started_at=started_at,
        ended_at=ended_at,
        cost_usd=cost_usd,
        subprocess_pid=subprocess_pid,
    )


def _wave(
    *,
    wave_id: str,
    status: WaveStatus,
    title: str = "Lifecycle-consistency wave",
    sessions: dict[int, SessionAttempt] | None = None,
    claimed_at: datetime | None = None,
    closed_at: datetime | None = None,
    runtime_preference: list[str] | None = None,
    description: str | None = None,
) -> Wave:
    """Build one synthetic ``Wave`` row for a drive fixture."""
    return Wave(
        id=wave_id,
        iter_id=_ITER_ID,
        title=title,
        description=description,
        status=status,
        opened_at=_T0,
        claimed_at=claimed_at,
        closed_at=closed_at,
        sessions=sessions or {},
        runtime_preference=runtime_preference,
        effort_bucket=EffortBucket.S,
    )


#: A dead-simple, deterministic task each live smoke lane carries so the real
#: agent finishes fast + emits a close-ready report (the drive then closes it via
#: close-on-behalf, so the run drains and the reaper fires on the orphan).
_LIVE_SMOKE_TASK = (
    "Your ONLY task: create a file named hello.txt in the repository root "
    "containing exactly the single line: hello. Do nothing else -- no other "
    "files, no commits, no tests. Then you are done."
)


def _state_with(*waves: Wave) -> State:
    """Return the base state with its wave table replaced by *waves*."""
    return _base_state().model_copy(update={"waves": {wave.id: wave for wave in waves}})


def _healthy_codex_wave() -> Wave:
    """A codex wave with one clean priced attempt, closed after the attempt."""
    return _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        title="Codex lane -- clean single pass",
        claimed_at=_T0,
        closed_at=_T0 + timedelta(minutes=10),
        runtime_preference=["codex"],
        sessions={
            1: _attempt(
                attempt=1,
                runtime="codex",
                started_at=_T0 + timedelta(minutes=1),
                ended_at=_T0 + timedelta(minutes=9),
                cost_usd=0.1234,
            )
        },
    )


def _healthy_claude_wave() -> Wave:
    """A claude wave with one clean priced attempt, closed after the attempt."""
    return _wave(
        wave_id="P01-I01-W02",
        status=WaveStatus.CLOSED,
        title="Claude lane -- clean single pass",
        claimed_at=_T0,
        closed_at=_T0 + timedelta(minutes=12),
        runtime_preference=["claude-code"],
        sessions={
            1: _attempt(
                attempt=1,
                runtime="claude-code",
                started_at=_T0 + timedelta(minutes=1),
                ended_at=_T0 + timedelta(minutes=11),
                cost_usd=0.5678,
            )
        },
    )


def _reaped_orphan_wave() -> Wave:
    """A wedged wave the reaper drove to FAILED (its attempt never ended)."""
    return _wave(
        wave_id="P01-I01-W03",
        status=WaveStatus.FAILED,
        title="Wedged lane -- reaped to failed",
        claimed_at=_T0,
        closed_at=_T0 + timedelta(minutes=15),
        runtime_preference=["codex"],
        sessions={
            1: _attempt(
                attempt=1,
                runtime="codex",
                started_at=_T0 + timedelta(minutes=1),
                ended_at=None,
                cost_usd=0.01,
                subprocess_pid=999_999,
            )
        },
    )


def _healthy_drive_state() -> State:
    """A healthy 2-runtime drive: codex + claude clean, orphan reaped to FAILED."""
    return _state_with(_healthy_codex_wave(), _healthy_claude_wave(), _reaped_orphan_wave())


# -- healthy direction: all four assertions pass -------------------------------


def test_healthy_drive_one_attempt_per_execution_passes() -> None:
    """Each clean lane persisted exactly one attempt for its one execution."""
    state = _healthy_drive_state()
    assert_one_attempt_per_execution(state, "P01-I01-W01")
    assert_one_attempt_per_execution(state, "P01-I01-W02")


def test_healthy_drive_no_post_close_dispatch_passes() -> None:
    """No lane's attempt is timestamped after its close event."""
    state = _healthy_drive_state()
    for wave_id in ("P01-I01-W01", "P01-I01-W02"):
        events = drive_events_for_wave(state.waves[wave_id])
        assert_no_post_close_dispatch(events, wave_id)


def test_healthy_drive_orphan_claim_failed_passes() -> None:
    """The wedged lane was reaped to FAILED, not left stuck."""
    state = _healthy_drive_state()
    assert_orphan_claim_failed(state, "P01-I01-W03")


def test_healthy_drive_per_attempt_cost_present_passes() -> None:
    """Each clean lane surfaces a priced per-attempt cost."""
    state = _healthy_drive_state()
    assert_per_attempt_cost_present(state, "P01-I01-W01")
    assert_per_attempt_cost_present(state, "P01-I01-W02")


# -- regressed direction: each assertion has teeth -----------------------------


def test_regressed_extra_attempts_trip_one_attempt_per_execution() -> None:
    """A wave with 4 persisted attempts for one execution trips the check (R3)."""
    sessions = {
        n: _attempt(
            attempt=n,
            runtime="codex",
            started_at=_T0 + timedelta(minutes=n),
            ended_at=_T0 + timedelta(minutes=n, seconds=30),
        )
        for n in range(1, 5)
    }
    regressed = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        closed_at=_T0 + timedelta(minutes=10),
        sessions=sessions,
    )
    state = _state_with(regressed)
    with pytest.raises(AssertionError, match="phantom attempt leaked"):
        assert_one_attempt_per_execution(state, "P01-I01-W01")


def test_regressed_post_close_attempt_trips_no_post_close_dispatch() -> None:
    """A wave with an attempt started ~50s after close trips the check (R3)."""
    close_at = _T0 + timedelta(minutes=10)
    regressed = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        closed_at=close_at,
        sessions={
            1: _attempt(attempt=1, runtime="codex", started_at=_T0 + timedelta(minutes=1)),
            # The R3 phantom: an attempt persisted ~50s AFTER the wave closed.
            2: _attempt(attempt=2, runtime="codex", started_at=close_at + timedelta(seconds=50)),
        },
    )
    events = drive_events_for_wave(regressed)
    with pytest.raises(AssertionError, match="timestamped after its close"):
        assert_no_post_close_dispatch(events, "P01-I01-W01")


def test_regressed_stuck_claim_trips_orphan_claim_failed() -> None:
    """A wave still CLAIMED after a drained run trips the check (R4)."""
    stuck = _wave(
        wave_id="P01-I01-W03",
        status=WaveStatus.CLAIMED,
        claimed_at=_T0,
        sessions={
            1: _attempt(
                attempt=1,
                runtime="codex",
                started_at=_T0 + timedelta(minutes=1),
                subprocess_pid=999_999,
            )
        },
    )
    state = _state_with(stuck)
    with pytest.raises(AssertionError, match="was not reaped"):
        assert_orphan_claim_failed(state, "P01-I01-W03")


def test_regressed_no_cost_trips_per_attempt_cost_present() -> None:
    """A wave whose attempt carries no per-attempt cost trips the check."""
    regressed = _wave(
        wave_id="P01-I01-W01",
        status=WaveStatus.CLOSED,
        closed_at=_T0 + timedelta(minutes=10),
        sessions={
            1: _attempt(
                attempt=1,
                runtime="claude-code",
                started_at=_T0 + timedelta(minutes=1),
                ended_at=_T0 + timedelta(minutes=9),
                cost_usd=None,
            )
        },
    )
    state = _state_with(regressed)
    with pytest.raises(AssertionError, match="no per-attempt cost"):
        assert_per_attempt_cost_present(state, "P01-I01-W01")


# ==========================================================================
# Part C -- LIVE cross-runtime drive (opt-in; skipped by default)
# ==========================================================================

#: Full wave ids for the two runtime lanes the live drive seeds.
_LIVE_CODEX_WAVE = "P01-I01-W01"
_LIVE_CLAUDE_WAVE = "P01-I01-W02"
#: A wedged lane seeded pre-claimed with a dead pgid so the reaper fails it.
_LIVE_ORPHAN_WAVE = "P01-I01-W03"

#: Wall-clock budget for the daemon socket to appear, and for the whole live
#: drive to reach a terminal fleet-run state. The drive spawns real runtime
#: subprocesses, so the drain budget is generous but bounded.
_LIVE_SOCKET_TIMEOUT_S: float = 8.0
_LIVE_DRIVE_TIMEOUT_S: float = 600.0
_LIVE_POLL_INTERVAL_S: float = 0.5


def _rpc(sock_path: Path, method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Send one newline-delimited JSON-RPC frame and return the parsed reply.

    Mirrors ``tests/e2e/test_agent_dispatch_e2e._rpc`` -- ``fleet.drive`` has a
    CLI surface, but driving it raw over the socket keeps the harness in one
    process and lets the test poll the on-disk state directly.

    Args:
        sock_path: Path to the live daemon's AF_UNIX socket.
        method: JSON-RPC method name.
        params: Request params object.

    Returns:
        The parsed JSON-RPC response object.

    Raises:
        AssertionError: When the daemon closes the connection without a reply.
    """
    req = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(10.0)
        client.connect(str(sock_path))
        client.sendall(json.dumps(req).encode("utf-8") + b"\n")
        buf = b""
        while b"\n" not in buf:
            chunk = client.recv(65536)
            if not chunk:
                break
            buf += chunk
    assert buf, "daemon closed without replying"
    parsed = json.loads(buf.split(b"\n", 1)[0])
    assert isinstance(parsed, dict)
    return parsed


def _write_dual_adapter_config(repo: Path) -> None:
    """Write a ``.ea/config.yaml`` enabling BOTH the claude-code + codex adapters.

    The fleet drive resolves a wave that carries no per-wave
    ``runtime_preference`` off the project's configured ``runtime.preference``;
    seeding both adapters here makes each runtime resolvable, while each seeded
    wave still pins its OWN runtime so the cross-runtime split is deterministic.
    """
    import yaml

    config = {
        "schema_version": "1.0",
        "runtime": {
            "adapters": ["claude-code", "codex"],
            "preference": ["claude-code", "codex"],
        },
    }
    config_path = repo / ".ea" / "config.yaml"
    config_path.write_text(yaml.safe_dump(config, sort_keys=True), encoding="utf-8")


def _seed_cross_runtime_frontier(state_path: Path) -> None:
    """Rewrite the sandbox state with a codex lane, a claude lane, and an orphan.

    Both runtime lanes land PENDING (ready for the drive to claim + dispatch)
    with their runtime pinned via ``runtime_preference``. The orphan lane lands
    pre-CLAIMED with a ``SessionAttempt`` naming a dead ``subprocess_pid`` and no
    in-flight lane, so the drained-run reaper resolves its pgid, reads it dead,
    and drives it to FAILED.
    """
    from eawf.kernel.state.io import write_state_unlocked

    # Seed from a fixture that already carries phase P01 + iter P01-I01; the
    # sandbox's own base state is the empty-repo fixture (no iter to hang the
    # frontier on). The resulting state is written to the sandbox state_path.
    state = State.model_validate(orjson.loads(_BASE_STATE.read_bytes()))
    codex_wave = _wave(
        wave_id=_LIVE_CODEX_WAVE,
        status=WaveStatus.PENDING,
        title="Codex smoke lane",
        runtime_preference=["codex"],
        description=_LIVE_SMOKE_TASK,
    )
    claude_wave = _wave(
        wave_id=_LIVE_CLAUDE_WAVE,
        status=WaveStatus.PENDING,
        title="Claude smoke lane",
        runtime_preference=["claude-code"],
        description=_LIVE_SMOKE_TASK,
    )
    orphan_wave = _wave(
        wave_id=_LIVE_ORPHAN_WAVE,
        status=WaveStatus.CLAIMED,
        title="Wedged orphan lane",
        claimed_at=_T0,
        runtime_preference=["codex"],
        sessions={
            1: _attempt(
                attempt=1,
                runtime="codex",
                started_at=_T0,
                cost_usd=None,
                subprocess_pid=999_999,
            )
        },
    )
    waves = {w.id: w for w in (codex_wave, claude_wave, orphan_wave)}
    iters = dict(state.iters)
    active_iter = iters[_ITER_ID].model_copy(update={"wave_ids": sorted(waves)})
    iters[_ITER_ID] = active_iter
    current = state.current.model_copy(update={"active_wave_ids": [_LIVE_ORPHAN_WAVE]})
    reseeded = state.model_copy(update={"waves": waves, "iters": iters, "current": current})
    write_state_unlocked(state_path, reseeded.model_dump(mode="json"))


def _load_state(state_path: Path) -> State:
    """Load the bound state off disk."""
    return State.model_validate(orjson.loads(state_path.read_bytes()))


def _dispatch_events_from_store(event_path: Path) -> list[DriveEvent]:
    """Fold on-disk ``event.jsonl`` dispatch rows into drive events.

    Each ``dispatch_cost`` envelope carries the wave id in its payload and its
    persist time on the envelope ``created_at``; both feed the post-close
    ordering check alongside the state-derived ``session_attempt`` rows.
    """
    if not event_path.exists():
        return []
    events: list[DriveEvent] = []
    with event_path.open("r", encoding="utf-8") as handle:
        for raw in handle:
            raw = raw.strip()
            if not raw:
                continue
            row = json.loads(raw)
            payload = row.get("payload", {})
            event_type = payload.get("event_type")
            wave_id = payload.get("wave_id")
            if event_type in _DISPATCH_EVENT_TYPES and isinstance(wave_id, str):
                events.append(
                    DriveEvent(
                        event_type=event_type,
                        ts=datetime.fromisoformat(row["created_at"]),
                        wave_id=wave_id,
                    )
                )
    return events


def _wait_for_socket(sandbox: E2EEnv, deadline: float) -> bool:
    """Poll until the daemon socket accepts a connection or *deadline* passes."""
    while time.monotonic() < deadline:
        if sandbox.sock_file.exists():
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(0.2)
                    probe.connect(str(sandbox.sock_file))
                    return True
            except OSError:
                pass
        time.sleep(_LIVE_POLL_INTERVAL_S)
    return False


def _wait_for_terminal_drive(state_path: Path, deadline: float) -> State:
    """Poll the on-disk state until the fleet run reaches a terminal stop.

    The ``fleet.drive`` RPC backgrounds its drain and returns immediately, so
    completion is read off the persisted ``state.fleet_run`` (``done`` /
    ``halted``) or every seeded wave reaching a terminal status -- whichever
    comes first within *deadline*.

    Returns:
        The last-loaded state (terminal when the poll converged, else the final
        snapshot at timeout so the assertions surface the real stuck shape).
    """
    seeded = {_LIVE_CODEX_WAVE, _LIVE_CLAUDE_WAVE, _LIVE_ORPHAN_WAVE}
    state = _load_state(state_path)
    while time.monotonic() < deadline:
        state = _load_state(state_path)
        run = state.fleet_run
        run_done = run is not None and run.run_state.value in {"done", "halted"}
        waves_terminal = all(
            state.waves[wid].status in _TERMINAL_STATUSES for wid in seeded if wid in state.waves
        )
        if run_done or waves_terminal:
            return state
        time.sleep(_LIVE_POLL_INTERVAL_S)
    return state


@pytest.mark.skipif(
    not os.environ.get("EAWF_LIVE_E2E"),
    reason="live cross-runtime drive; set EAWF_LIVE_E2E=1",
)
def test_live_cross_runtime_drive_holds_all_invariants(e2e_env: E2EEnv) -> None:
    """A live codex + claude drive holds all four lifecycle invariants.

    The discriminating experiment the iter close is gated on: configure BOTH
    runtime adapters in the isolated sandbox, seed a codex lane + a claude lane
    (plus a wedged orphan), drive the fleet to terminal through the real
    ``fleet.drive`` RPC, then re-load the sandbox state + event store and apply
    every Part-A assertion to the resulting lanes.

    The daemon is spawned here (not via the ``running_daemon`` fixture) with a
    long idle timeout: real agent spawns take minutes, and the fixture default
    (``EAWF_DAEMON_IDLE_TIMEOUT=120``) self-exits the daemon mid-drive -- killing
    the backgrounded drain before close-on-behalf + the reaper can run.

    Not run in CI (it makes real, paid runtime spawns); the operator runs it
    on-demand as the ship-gate check with ``EAWF_LIVE_E2E=1``.
    """
    sandbox = e2e_env
    sandbox.env["EAWF_DAEMON_IDLE_TIMEOUT"] = "3600"
    sandbox.env["EAWF_DAEMON_SESSION_TTL"] = "3600"
    sandbox.spawn_daemon()
    assert _wait_for_socket(sandbox, time.monotonic() + _LIVE_SOCKET_TIMEOUT_S), (
        "sandbox daemon did not expose its socket within the ready timeout"
    )
    _write_dual_adapter_config(sandbox.repo)
    _seed_cross_runtime_frontier(sandbox.state_path)

    reply = _rpc(
        sandbox.sock_file,
        "fleet.drive",
        {"frontier": [_LIVE_CODEX_WAVE, _LIVE_CLAUDE_WAVE], "concurrency": 3},
    )
    assert "error" not in reply, reply

    deadline = time.monotonic() + _LIVE_DRIVE_TIMEOUT_S
    state = _wait_for_terminal_drive(sandbox.state_path, deadline)
    event_path = sandbox.state_path.parent / "store" / "event.jsonl"
    store_events = _dispatch_events_from_store(event_path)

    # Both runtime lanes: one clean attempt, no post-close dispatch, priced cost.
    for wave_id in (_LIVE_CODEX_WAVE, _LIVE_CLAUDE_WAVE):
        assert_one_attempt_per_execution(state, wave_id)
        wave_events = drive_events_for_wave(state.waves[wave_id])
        wave_store_events = [event for event in store_events if event.wave_id == wave_id]
        assert_no_post_close_dispatch([*wave_events, *wave_store_events], wave_id)
        assert_per_attempt_cost_present(state, wave_id, state_path=sandbox.state_path)

    # The wedged lane: reaped to FAILED at the drained-run seam.
    assert_orphan_claim_failed(state, _LIVE_ORPHAN_WAVE)
