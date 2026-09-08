"""Tests for the bounded gate-argv repoint on a CLOSED wave.

A closed wave's recorded gates are its verification record. When a later
wave moves the test tree beneath them the recorded argv names a path that
no longer resolves, so replaying the record exits ``4`` (pytest's usage
error) even though the verification itself was sound. There is no
wave-level reopen and ``spec sync`` refuses a non-PENDING wave, so the
repoint verb is the only repair path.

Coverage:

* CR-01 -- the mutation rewrites a closed wave's gate argv while the
  criteria, the recorded verdict (status + outcome) and ``closed_at``
  compare equal before and after, and a non-argv edit is refused;
* CR-02 -- the argv as originally recorded exits ``4`` and the repointed
  argv exits ``0`` for the same closed wave;
* boundary + error paths -- empty / duplicate / unknown / non-argv gate
  requests, a non-CLOSED wave, a wave with no gates, an argv the L0
  policy rejects, and a no-op replay;
* the daemon transaction that persists the repoint, plus its dry-run and
  refusal arms, driven through the module-level coroutine so the tests
  need no live UDS transport;
* the CLI ``--gate GATE_ID=<argv>`` parser.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from pydantic import ValidationError

from eawf import __version__
from eawf.kernel.spec.common import GateSpec
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec_repoint import repoint_gates
from eawf.surfaces.cli.commands.spec import parse_gate_repoint
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.gate_repoint import (
    GateArgvRepoint,
    build_argv_repoint,
    frozen_wave_fingerprint,
    repoint_closed_wave_gates,
)

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 7, 2, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P31-I01-W07"

#: The path shape the P31 test-tree collapse retired.
_RETIRED_ARGV = ["pytest", "tests/retired/test_gone.py", "-q"]

#: Where the same subject lives after the collapse.
_LIVE_RELATIVE = "tests/live/test_repointed_gate.py"


def _gate(gate_id: str, argv: list[str], *, criterion_id: str = "CR-01") -> dict[str, Any]:
    """A ``command_exit_zero`` gate row as recorded on a closed wave."""
    return {
        "id": gate_id,
        "criterion_id": criterion_id,
        "kind": "command_exit_zero",
        "args": {"argv": argv},
        "policy": "block",
        "cadence": "every-wave",
    }


def _criterion() -> dict[str, Any]:
    """A typed criterion bound to ``G-01``, as a closed wave records it."""
    return {
        "id": "CR-01",
        "text": "the repointed gate argv re-runs against the live test path",
        "kind": "deterministic",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "gate_ids": ["G-01"],
        "quality_dimension": "functional_suitability",
        "measurable_signal": "the repointed gate argv exits zero under pytest",
    }


def _state_payload(
    *,
    gates: list[dict[str, Any]] | None = None,
    wave_status: str = "closed",
) -> dict[str, Any]:
    """A minimal valid State with one closed P31 wave carrying *gates*."""
    closed_at = _T0.isoformat() if wave_status == "closed" else None
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": _T0.isoformat(),
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {"project_code": "EAWF"},
        "workspace": None,
        "phases": {
            "P31": {
                "id": "P31",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P31",
                "status": "active",
                "iter_ids": ["P31-I01"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P31-I01": {
                "id": "P31-I01",
                "phase_id": "P31",
                "title": "I01",
                "status": "closed",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat(),
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P31-I01",
                "title": "historical closed wave with recorded gates",
                "status": wave_status,
                "file_scopes": ["src/eawf/workflow/lifecycle/gate_repoint.py"],
                "success_criteria": [_criterion()],
                "gates": gates if gates is not None else [_gate("G-01", list(_RETIRED_ARGV))],
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": closed_at,
                "outcome": "closed green on the pre-collapse test tree",
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _state(**kwargs: Any) -> State:
    """Build a validated :class:`State` from :func:`_state_payload`."""
    return State.model_validate(_state_payload(**kwargs))


def _write_state(state_path: Path, state: State) -> None:
    """Persist *state* under a tmp ``.ea/`` root."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    """A daemon method context wired to the tmp state / event / WAL paths."""
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive a daemon coroutine to completion."""
    asyncio.run(body())


def _repoint(gate_id: str, argv: list[str]) -> GateArgvRepoint:
    """One repoint request row."""
    return GateArgvRepoint(gate_id=gate_id, argv=argv)


# ---- CR-01: the bounded mutation ------------------------------------------


def test_repoint_closed_wave_gate_argv() -> None:
    """CR-01: argv moves; criteria / verdict / closed_at do not; non-argv edits raise."""
    state = _state()
    wave = state.waves[_WAVE_ID]
    live_argv = ["pytest", _LIVE_RELATIVE, "-q"]

    before_criteria = [row.model_copy(deep=True) for row in wave.success_criteria]
    before_status = wave.status
    before_outcome = wave.outcome
    before_closed_at = wave.closed_at
    before_fingerprint = frozen_wave_fingerprint(wave)

    candidates = build_argv_repoint(wave, [_repoint("G-01", live_argv)])
    report = repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=candidates)

    assert [change.gate_id for change in report.changed] == ["G-01"]
    assert report.changed[0].before_argv == _RETIRED_ARGV
    assert report.changed[0].after_argv == live_argv
    assert wave.gates[0].args["argv"] == live_argv

    # The record around the argv is frozen: criteria, the recorded verdict
    # (status + outcome) and closed_at all compare equal across the mutation.
    assert wave.success_criteria == before_criteria
    assert wave.status == before_status
    assert wave.outcome == before_outcome
    assert wave.closed_at == before_closed_at
    assert frozen_wave_fingerprint(wave) == before_fingerprint

    # A non-argv edit smuggled in through the same replacement list is refused.
    tampered = wave.gates[0].model_copy(update={"policy": "warn"})
    with pytest.raises(LifecycleError, match="changes more than gate argv"):
        repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=[tampered])
    assert wave.gates[0].policy == "block"
    assert frozen_wave_fingerprint(wave) == before_fingerprint


def test_repoint_closed_wave_gates_refuses_criterion_rebinding() -> None:
    """A gate re-bound to another criterion is refused and rolled back."""
    state = _state()
    wave = state.waves[_WAVE_ID]
    rebound = wave.gates[0].model_copy(update={"criterion_id": "CR-99"})

    with pytest.raises(LifecycleError, match="changes more than gate argv"):
        repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=[rebound])
    assert wave.gates[0].criterion_id == "CR-01"


def test_repoint_closed_wave_gates_refuses_gate_removal() -> None:
    """Dropping a recorded gate is not a repoint, so the guard refuses it."""
    state = _state(
        gates=[
            _gate("G-01", list(_RETIRED_ARGV)),
            _gate("G-02", ["pytest", "tests/retired/test_other.py"]),
        ]
    )
    wave = state.waves[_WAVE_ID]

    with pytest.raises(LifecycleError, match="changes more than gate argv"):
        repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=[wave.gates[0]])
    assert [gate.id for gate in wave.gates] == ["G-01", "G-02"]


def test_repoint_closed_wave_gates_refuses_pending_wave() -> None:
    """A PENDING wave is plan-time scope and belongs to ``spec sync``."""
    state = _state(wave_status="pending")

    with pytest.raises(LifecycleError, match="is not closed"):
        repoint_closed_wave_gates(
            state,
            wave_id=_WAVE_ID,
            gates=[GateSpec.model_validate(_gate("G-01", ["pytest", _LIVE_RELATIVE]))],
        )


def test_repoint_closed_wave_gates_refuses_unknown_wave() -> None:
    """An unknown wave id is refused before anything is inspected."""
    state = _state()

    with pytest.raises(LifecycleError, match="unknown wave"):
        repoint_closed_wave_gates(state, wave_id="P31-I01-W99", gates=[])


def test_repoint_closed_wave_gates_refuses_wave_without_gates() -> None:
    """A wave that records no gates has no verification argv to repair."""
    state = _state(gates=[])

    with pytest.raises(LifecycleError, match="records no gates"):
        repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=[])


def test_repoint_closed_wave_gates_reports_noop_when_argv_matches() -> None:
    """Replaying an already-applied repoint reports zero changes, not a rewrite."""
    state = _state()
    wave = state.waves[_WAVE_ID]

    report = repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=list(wave.gates))

    assert report.changed == []
    assert report.unchanged_gate_ids == ["G-01"]


def test_build_argv_repoint_refuses_empty_repoints() -> None:
    """A repoint that repoints nothing is a caller bug, not a no-op."""
    state = _state()

    with pytest.raises(LifecycleError, match="no gate repoints supplied"):
        build_argv_repoint(state.waves[_WAVE_ID], [])


def test_build_argv_repoint_repoints_single_gate_of_many() -> None:
    """Off-by-one boundary: only the named gate of a multi-gate wave moves."""
    state = _state(
        gates=[
            _gate("G-01", list(_RETIRED_ARGV)),
            _gate("G-02", ["pytest", "tests/retired/test_other.py"], criterion_id="CR-01"),
        ]
    )
    wave = state.waves[_WAVE_ID]

    candidates = build_argv_repoint(wave, [_repoint("G-02", ["pytest", _LIVE_RELATIVE])])
    report = repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=candidates)

    assert [change.gate_id for change in report.changed] == ["G-02"]
    assert report.unchanged_gate_ids == ["G-01"]
    assert wave.gates[0].args["argv"] == _RETIRED_ARGV


def test_build_argv_repoint_refuses_unknown_gate_id() -> None:
    """A repoint never creates a gate, so an unrecorded id is refused."""
    state = _state()

    with pytest.raises(LifecycleError, match="records no gate"):
        build_argv_repoint(state.waves[_WAVE_ID], [_repoint("G-42", ["pytest", _LIVE_RELATIVE])])


def test_build_argv_repoint_refuses_duplicate_gate_id() -> None:
    """Two requests for one gate leave the applied argv ambiguous."""
    state = _state()

    with pytest.raises(LifecycleError, match="duplicate gate repoint"):
        build_argv_repoint(
            state.waves[_WAVE_ID],
            [_repoint("G-01", ["pytest", "a.py"]), _repoint("G-01", ["pytest", "b.py"])],
        )


def test_build_argv_repoint_refuses_non_argv_gate_kind() -> None:
    """A gate kind that carries no argv has nothing to repoint."""
    payload = _state_payload()
    payload["waves"][_WAVE_ID]["gates"] = [
        {
            "id": "G-01",
            "criterion_id": "CR-01",
            "kind": "regex_match",
            "args": {"pattern": "ok"},
            "policy": "block",
            "cadence": "every-wave",
        }
    ]
    state = State.model_validate(payload)

    with pytest.raises(LifecycleError, match="carries no argv"):
        build_argv_repoint(state.waves[_WAVE_ID], [_repoint("G-01", ["pytest", _LIVE_RELATIVE])])


def test_build_argv_repoint_refuses_shell_metachar_argv() -> None:
    """The L0 argv policy still fires on the replacement vector."""
    state = _state()

    with pytest.raises(LifecycleError, match="rejected"):
        build_argv_repoint(
            state.waves[_WAVE_ID],
            [_repoint("G-01", ["pytest", "tests/x.py; rm -rf /"])],
        )


def test_build_argv_repoint_refuses_disallowed_argv_head() -> None:
    """An argv head outside the allowlist cannot enter a closed wave's record."""
    state = _state()

    with pytest.raises(LifecycleError, match="rejected"):
        build_argv_repoint(state.waves[_WAVE_ID], [_repoint("G-01", ["curl", "http://x"])])


def test_gate_argv_repoint_rejects_empty_argv() -> None:
    """An empty argv vector fails the params model, not the mutation."""
    with pytest.raises(ValidationError):
        GateArgvRepoint(gate_id="G-01", argv=[])


# ---- CR-02: the repointed gate actually re-runs ----------------------------


def _pytest_argv_head() -> list[str]:
    """Resolve the ``pytest`` argv head the way a PATH lookup would.

    Returns:
        The concrete command prefix that stands in for the recorded
        ``pytest`` head: the resolved executable when one is on PATH,
        else the running interpreter's ``-m pytest`` form. The argv TAIL
        -- the repointed path, which is what is under test -- is always
        replayed exactly as recorded.
    """
    resolved = shutil.which("pytest")
    return [resolved] if resolved is not None else [sys.executable, "-m", "pytest"]


def _replay(argv: list[str], *, cwd: Path) -> int:
    """Replay a recorded gate argv and return its exit status.

    Args:
        argv: The gate's recorded argv vector.
        cwd: Working directory the relative test path resolves under.

    Returns:
        The child process exit status.
    """
    env = {key: value for key, value in os.environ.items() if key != "PYTEST_ADDOPTS"}
    completed = subprocess.run(
        [*_pytest_argv_head(), *argv[1:]],
        cwd=cwd,
        capture_output=True,
        env=env,
        check=False,
    )
    return completed.returncode


def test_repointed_gate_reruns_green(tmp_path: Path) -> None:
    """CR-02: the recorded argv exits 4; the repointed argv exits 0."""
    repo_root = tmp_path / "repo"
    live = repo_root / _LIVE_RELATIVE
    live.parent.mkdir(parents=True)
    live.write_text("def test_gate_subject() -> None:\n    assert True\n", encoding="utf-8")

    state = _state()
    wave = state.waves[_WAVE_ID]
    recorded_argv = list(wave.gates[0].args["argv"])
    assert _replay(recorded_argv, cwd=repo_root) == 4

    live_argv = ["pytest", _LIVE_RELATIVE, "-q"]
    candidates = build_argv_repoint(wave, [_repoint("G-01", live_argv)])
    repoint_closed_wave_gates(state, wave_id=_WAVE_ID, gates=candidates)

    repointed_argv = list(wave.gates[0].args["argv"])
    assert repointed_argv != recorded_argv
    assert _replay(repointed_argv, cwd=repo_root) == 0


# ---- The daemon transaction that persists the repoint ----------------------


def test_repoint_gates_rpc_persists_repoint(tmp_path: Path) -> None:
    """The daemon mutator writes the repointed argv and an audit envelope."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state())
    ctx = _build_ctx(tmp_path, state_path)
    live_argv = ["pytest", _LIVE_RELATIVE, "-q"]

    async def body() -> None:
        result = await repoint_gates(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "repoints": [{"gate_id": "G-01", "argv": live_argv}],
                "repo_root": str(repo_root),
            },
        )
        assert result["changed_count"] == 1
        assert result["changed"][0]["after_argv"] == live_argv
        assert result["envelope"]["payload"]["event_type"] == "state.mutate.spec_repoint_gates"
        assert result["envelope"]["payload"]["extras"]["changed_gate_ids"] == "G-01"
        assert result["before_version"] != result["after_version"]

    _run(body)
    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    assert wave.gates[0].args["argv"] == live_argv
    assert wave.status.value == "closed"
    assert wave.closed_at == _T0
    assert wave.outcome == "closed green on the pre-collapse test tree"
    assert wave.success_criteria[0].id == "CR-01"


def test_repoint_gates_rpc_dry_run_writes_nothing(tmp_path: Path) -> None:
    """``--dry-run`` reports the would-change set with the state bytes intact."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state())
    before_bytes = state_path.read_bytes()
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        result = await repoint_gates(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "repoints": [{"gate_id": "G-01", "argv": ["pytest", _LIVE_RELATIVE]}],
                "dry_run": True,
                "repo_root": str(repo_root),
            },
        )
        assert result["dry_run"] is True
        assert result["changed_count"] == 1
        assert result["envelope"] is None
        assert result["before_version"] is None

    _run(body)
    assert state_path.read_bytes() == before_bytes


def test_repoint_gates_rpc_refuses_pending_wave(tmp_path: Path) -> None:
    """The daemon maps the non-CLOSED refusal onto a validation error."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state(wave_status="pending"))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="is not closed"):
            await repoint_gates(
                ctx,
                {
                    "wave_id": _WAVE_ID,
                    "repoints": [{"gate_id": "G-01", "argv": ["pytest", _LIVE_RELATIVE]}],
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


def test_repoint_gates_rpc_refuses_non_wave_scope(tmp_path: Path) -> None:
    """A phase scope is refused: a repoint is authorised one wave at a time."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state())
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="wave scope"):
            await repoint_gates(
                ctx,
                {
                    "wave_id": "P31",
                    "repoints": [{"gate_id": "G-01", "argv": ["pytest", _LIVE_RELATIVE]}],
                    "repo_root": str(repo_root),
                },
            )

    _run(body)


def test_repoint_gates_rpc_refuses_empty_repoints(tmp_path: Path) -> None:
    """The params model floors the request at one repoint row."""
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    state_path = repo_root / ".ea" / "state.json"
    _write_state(state_path, _state())
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await repoint_gates(
                ctx,
                {"wave_id": _WAVE_ID, "repoints": [], "repo_root": str(repo_root)},
            )

    _run(body)


# ---- The CLI option parser -------------------------------------------------


def test_parse_gate_repoint_splits_quoted_argv() -> None:
    """A quoted argument survives as one argv element."""
    row = parse_gate_repoint("G-01=uv run pytest tests/x.py -k 'not slow'")

    assert row == {
        "gate_id": "G-01",
        "argv": ["uv", "run", "pytest", "tests/x.py", "-k", "not slow"],
    }


def test_parse_gate_repoint_rejects_missing_separator() -> None:
    """A value with no ``=`` names no gate."""
    with pytest.raises(typer.BadParameter, match="expected <gate-id>=<argv>"):
        parse_gate_repoint("uv run pytest tests/x.py")


def test_parse_gate_repoint_rejects_empty_gate_id() -> None:
    """An empty left-hand side is refused rather than defaulted."""
    with pytest.raises(typer.BadParameter, match="empty gate id"):
        parse_gate_repoint("=uv run pytest tests/x.py")


def test_parse_gate_repoint_rejects_empty_argv() -> None:
    """An empty right-hand side would clear the gate's argv."""
    with pytest.raises(typer.BadParameter, match="empty argv"):
        parse_gate_repoint("G-01=   ")
