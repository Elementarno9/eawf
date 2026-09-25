"""Tests for the one-way gate-kind rewrite on a CLOSED wave.

A wave that closed on single-token grep gates, or on attested criteria
with no gate, carries a verification record that cannot fail. The
rewrite binds a real ``command_exit_zero`` gate in its place and refuses
to move anything the other way.

Coverage:

* gate-fire proof -- a grep gate becomes a command gate with an audit
  event carrying the reason, while a request that would turn a command
  gate into a grep is refused and leaves the state bytes and the event
  log untouched;
* an ungated attested criterion gains a command gate and is promoted to
  deterministic with a derived ``exits`` / ``pytest`` clause;
* boundary and error paths -- replay no-op, empty and duplicate
  requests, a non-grep kind, a moved command argv, a non-closed wave, a
  blank reason, unknown wave / criterion, a criterion mismatch, a jury
  owner, an argv the L0 policy refuses, and a tampered candidate that
  would move the criterion text;
* the RPC dry run, the non-wave scope refusal, the CLI parser and the
  CLI reason floor;
* the re-receipt outcome row refuses to record a non-zero exit as a pass.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import typer
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf import __version__
from eawf.kernel.spec.common import OracleTier
from eawf.kernel.state.enums import GateReceiptResult, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.store.kinds.gate_rereceipt import GateRereceiptOutcome
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.spec_repoint import rewrite_gate_kind
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.close import render_rereceipt
from eawf.surfaces.cli.commands.spec import parse_gate_kind_rewrite
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.cli.exit_codes import USER_ERROR
from eawf.workflow.lifecycle import gate_kind_rewrite
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.gate_kind_rewrite import (
    GateKindRewrite,
    rewrite_closed_wave_gate_kinds,
)

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 24, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P33-I02-W01"
_ARGV = ["uv", "run", "pytest", "tests/integration/runtime/daemon/test_lazy.py", "-q"]
_REASON = "the converted grep gates pass on any file naming the token"
_SCOPES = ["src/eawf/runtime/daemon/server.py"]


def _criterion(
    criterion_id: str,
    *,
    kind: str,
    evidence_kind: str,
    gate_ids: list[str],
    response: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One criterion row as a closed wave records it."""
    return {
        "id": criterion_id,
        "text": f"{criterion_id} the daemon registers its liveness verbs; pytest exits zero",
        "kind": kind,
        "acceptance_style": "binary",
        "evidence_kind": evidence_kind,
        "gate_ids": gate_ids,
        "quality_dimension": "functional_suitability",
        "measurable_signal": f"{criterion_id} pytest exits zero over the lazy registration suite",
        "response": response,
    }


def _grep_response() -> dict[str, Any]:
    """The response clause the legacy converter derives for a grep gate."""
    return {
        "observe": "file_matches",
        "object": "unregistered",
        "locus": "source",
        "gate_ref": "criterion_in_diff",
    }


def _criteria() -> list[dict[str, Any]]:
    """Criteria covering each owner shape the rewrite distinguishes."""
    return [
        _criterion(
            "CR-01",
            kind="converted",
            evidence_kind="deterministic",
            gate_ids=["GATE-01"],
            response=_grep_response(),
        ),
        _criterion("CR-02", kind="legacy", evidence_kind="attested", gate_ids=[]),
        _criterion("CR-03", kind="deterministic", evidence_kind="deterministic", gate_ids=["G-03"]),
        _criterion("CR-04", kind="legacy", evidence_kind="attested", gate_ids=["G-04"]),
        _criterion("CR-05", kind="legacy", evidence_kind="jury", gate_ids=[]),
    ]


def _gates() -> list[dict[str, Any]]:
    """A grep gate, a command gate and a structural gate."""
    return [
        {
            "id": "GATE-01",
            "criterion_id": "CR-01",
            "kind": "criterion_in_diff",
            "args": {
                "criterion": "unregistered",
                "pattern": "unregistered",
                "file_scopes": _SCOPES,
            },
            "policy": "block",
            "cadence": "every-wave",
        },
        {
            "id": "G-03",
            "criterion_id": "CR-03",
            "kind": "command_exit_zero",
            "args": {"argv": list(_ARGV)},
            "policy": "block",
            "cadence": "every-wave",
        },
        {
            "id": "G-04",
            "criterion_id": "CR-04",
            "kind": "tui_flow",
            "args": {},
            "policy": "advisory",
            "cadence": "every-wave",
        },
    ]


def _state_payload(*, wave_status: str = "closed") -> dict[str, Any]:
    """A minimal valid State with one wave carrying weak and strong gates."""
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
            "P33": {
                "id": "P33",
                "scope_id": "EAWF",
                "track_id": None,
                "title": "P33",
                "status": "active",
                "iter_ids": ["P33-I02"],
                "outcome_ids": [],
                "opened_at": _T0.isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P33-I02": {
                "id": "P33-I02",
                "phase_id": "P33",
                "title": "I02",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _T0.isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P33-I02",
                "title": "wave that closed on grep gates",
                "status": wave_status,
                "file_scopes": list(_SCOPES),
                "success_criteria": _criteria(),
                "gates": _gates(),
                "effort_bucket": "S",
                "agent_role": "executor",
                "opened_at": _T0.isoformat(),
                "closed_at": _T0.isoformat() if wave_status == "closed" else None,
                "commit": "b" * 40,
                "outcome": "closed on converted grep gates",
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


def _rewrite(gate_id: str, **kwargs: Any) -> GateKindRewrite:
    """A rewrite request carrying the default command argv."""
    return GateKindRewrite(gate_id=gate_id, argv=kwargs.pop("argv", list(_ARGV)), **kwargs)


def _seed(tmp_path: Path) -> tuple[Path, MethodContext]:
    """Write the fixture state under a tmp ``.ea/`` and build a daemon context."""
    state_path = tmp_path / "repo" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(_state().model_dump_json(), encoding="utf-8")
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-09-24T00:00:00+00:00",
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
    return state_path, ctx


def _drive(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    """Drive a daemon coroutine to completion."""
    asyncio.run(body())


def _events(state_path: Path) -> list[dict[str, Any]]:
    """Return every event row appended beside *state_path*."""
    path = store_path(state_path, StoreKind.EVENT)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ---- Gate-fire proof: strengthen accepted, weaken refused -------------------


def test_rewrite_gate_kind_rpc_strengthens_a_grep_gate_with_an_audit_row(tmp_path: Path) -> None:
    """A grep gate becomes a command gate; the event row carries the reason."""
    state_path, ctx = _seed(tmp_path)

    async def body() -> None:
        result = await rewrite_gate_kind(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "rewrites": [{"gate_id": "GATE-01", "argv": list(_ARGV)}],
                "reason": _REASON,
                "repo_root": str(state_path.parent.parent),
            },
        )
        assert result["changed_count"] == 1
        assert result["changed"][0]["before_kind"] == "criterion_in_diff"
        assert result["changed"][0]["after_kind"] == "command_exit_zero"
        assert result["before_version"] != result["after_version"]

    _drive(body)
    wave = State.model_validate_json(state_path.read_text(encoding="utf-8")).waves[_WAVE_ID]
    gate = next(gate for gate in wave.gates if gate.id == "GATE-01")
    assert gate.kind == "command_exit_zero"
    assert gate.args == {"argv": _ARGV}
    criterion = wave.success_criteria[0]
    assert criterion.response is not None
    assert criterion.response.gate_ref == "command_exit_zero"
    assert criterion.oracle_tier == OracleTier.T4_CONTRACT
    assert wave.status.value == "closed"
    assert wave.closed_at == _T0

    events = _events(state_path)
    assert len(events) == 1
    payload = events[0]["payload"]
    assert payload["event_type"] == "state.mutate.spec_rewrite_gate_kind"
    assert payload["extras"]["reason"] == _REASON
    assert payload["extras"]["gate_kind_moves"] == "GATE-01:criterion_in_diff->command_exit_zero"


def test_rewrite_gate_kind_rpc_refuses_weakening_a_command_gate_to_a_grep(tmp_path: Path) -> None:
    """A command gate never becomes a grep; nothing is written or logged."""
    state_path, ctx = _seed(tmp_path)
    before_bytes = state_path.read_bytes()

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="never weakens"):
            await rewrite_gate_kind(
                ctx,
                {
                    "wave_id": _WAVE_ID,
                    "rewrites": [
                        {"gate_id": "G-03", "kind": "criterion_in_diff", "argv": list(_ARGV)}
                    ],
                    "reason": _REASON,
                    "repo_root": str(state_path.parent.parent),
                },
            )

    _drive(body)
    assert state_path.read_bytes() == before_bytes
    assert _events(state_path) == []


# ---- The lifecycle rewrite ----------------------------------------------------


def test_rewrite_adds_a_command_gate_to_an_ungated_attested_criterion() -> None:
    """An attested criterion with no gate gains one and becomes deterministic."""
    state = _state()
    report = rewrite_closed_wave_gate_kinds(
        state,
        wave_id=_WAVE_ID,
        rewrites=[_rewrite("G-CMD-02", criterion_id="CR-02")],
        reason=_REASON,
    )

    assert [change.before_kind for change in report.changed] == [None]
    wave = state.waves[_WAVE_ID]
    assert [gate.id for gate in wave.gates] == ["GATE-01", "G-03", "G-04", "G-CMD-02"]
    criterion = wave.success_criteria[1]
    assert criterion.gate_ids == ["G-CMD-02"]
    assert criterion.evidence_kind == "deterministic"
    assert criterion.response is not None
    assert criterion.response.observe.value == "exits"
    assert criterion.response.locus.value == "pytest"
    assert criterion.text.startswith("CR-02 the daemon")
    assert criterion.kind == "legacy"


def test_rewrite_keeps_every_untouched_gate_and_criterion() -> None:
    """Only the named gate and its owner's proof fields move."""
    state = _state()
    wave = state.waves[_WAVE_ID]
    untouched_gates = [gate.model_copy(deep=True) for gate in wave.gates[1:]]
    untouched_criteria = [row.model_copy(deep=True) for row in wave.success_criteria[1:]]

    rewrite_closed_wave_gate_kinds(
        state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=_REASON
    )

    assert wave.gates[1:] == untouched_gates
    assert wave.success_criteria[1:] == untouched_criteria
    assert wave.outcome == "closed on converted grep gates"
    assert wave.commit == "b" * 40


def test_rewrite_replay_reports_the_command_gate_unchanged() -> None:
    """A second identical request is a no-op rather than a refusal."""
    state = _state()
    rewrite_closed_wave_gate_kinds(
        state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=_REASON
    )

    report = rewrite_closed_wave_gate_kinds(
        state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=_REASON
    )

    assert report.changed == []
    assert report.unchanged_gate_ids == ["GATE-01"]


def test_rewrite_refuses_moving_a_command_gate_argv() -> None:
    """Argv moves on a command gate belong to the argv repoint."""
    state = _state()
    other = ["uv", "run", "pytest", "tests/unit/test_other.py", "-q"]

    with pytest.raises(LifecycleError, match="spec repoint-gates"):
        rewrite_closed_wave_gate_kinds(
            state, wave_id=_WAVE_ID, rewrites=[_rewrite("G-03", argv=other)], reason=_REASON
        )


def test_rewrite_refuses_a_gate_that_is_not_grep_style() -> None:
    """A structural gate is not a grep and is left alone."""
    state = _state()

    with pytest.raises(LifecycleError, match="not a grep-style gate"):
        rewrite_closed_wave_gate_kinds(
            state, wave_id=_WAVE_ID, rewrites=[_rewrite("G-04")], reason=_REASON
        )


@pytest.mark.parametrize("wave_status", ["pending", "claimed", "in_progress"])
def test_rewrite_refuses_a_wave_that_is_not_closed(wave_status: str) -> None:
    """Plan-time and in-flight gates go through spec sync, not this repair."""
    state = _state(wave_status=wave_status)

    with pytest.raises(LifecycleError, match="is not closed"):
        rewrite_closed_wave_gate_kinds(
            state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=_REASON
        )


@pytest.mark.parametrize("reason", [None, "", "   "])
def test_rewrite_refuses_a_blank_reason(reason: str | None) -> None:
    """The audit row must say why the record moved."""
    state = _state()

    with pytest.raises(LifecycleError, match="non-empty reason"):
        rewrite_closed_wave_gate_kinds(
            state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=reason
        )


def test_rewrite_refuses_an_unknown_wave() -> None:
    """An unknown wave id is refused before any gate is inspected."""
    with pytest.raises(LifecycleError, match="unknown wave"):
        rewrite_closed_wave_gate_kinds(
            _state(), wave_id="P33-I02-W99", rewrites=[_rewrite("GATE-01")], reason=_REASON
        )


def test_rewrite_refuses_an_empty_request() -> None:
    """Rewriting nothing is a caller bug, not a no-op."""
    with pytest.raises(LifecycleError, match="no gate-kind rewrites"):
        rewrite_closed_wave_gate_kinds(_state(), wave_id=_WAVE_ID, rewrites=[], reason=_REASON)


def test_rewrite_refuses_a_duplicate_gate() -> None:
    """Two requests for one gate leave the applied argv ambiguous."""
    with pytest.raises(LifecycleError, match="duplicate gate-kind rewrite"):
        rewrite_closed_wave_gate_kinds(
            _state(),
            wave_id=_WAVE_ID,
            rewrites=[_rewrite("GATE-01"), _rewrite("GATE-01")],
            reason=_REASON,
        )


def test_rewrite_refuses_a_new_gate_without_a_criterion() -> None:
    """A gate the wave does not record needs the criterion it proves."""
    with pytest.raises(LifecycleError, match="needs the criterion it proves"):
        rewrite_closed_wave_gate_kinds(
            _state(), wave_id=_WAVE_ID, rewrites=[_rewrite("G-NEW")], reason=_REASON
        )


def test_rewrite_refuses_a_new_gate_for_an_unknown_criterion() -> None:
    """A rewrite never creates a criterion."""
    with pytest.raises(LifecycleError, match="records no criterion 'CR-99'"):
        rewrite_closed_wave_gate_kinds(
            _state(),
            wave_id=_WAVE_ID,
            rewrites=[_rewrite("G-NEW", criterion_id="CR-99")],
            reason=_REASON,
        )


def test_rewrite_refuses_rebinding_a_recorded_gate() -> None:
    """A recorded gate keeps the criterion it was bound to."""
    with pytest.raises(LifecycleError, match="never rebinds a gate"):
        rewrite_closed_wave_gate_kinds(
            _state(),
            wave_id=_WAVE_ID,
            rewrites=[_rewrite("GATE-01", criterion_id="CR-02")],
            reason=_REASON,
        )


def test_rewrite_refuses_a_jury_owner() -> None:
    """A judged criterion is not replaced by a command."""
    with pytest.raises(LifecycleError, match="judgement a command gate cannot replace"):
        rewrite_closed_wave_gate_kinds(
            _state(),
            wave_id=_WAVE_ID,
            rewrites=[_rewrite("G-CMD-05", criterion_id="CR-05")],
            reason=_REASON,
        )


def test_rewrite_refuses_an_argv_the_l0_policy_rejects() -> None:
    """The new argv passes the same policy every gate argv does."""
    state = _state()

    with pytest.raises(LifecycleError, match="rewrite argv rejected"):
        rewrite_closed_wave_gate_kinds(
            state,
            wave_id=_WAVE_ID,
            rewrites=[_rewrite("GATE-01", argv=["curl", "https://example.invalid"])],
            reason=_REASON,
        )
    assert state.waves[_WAVE_ID].gates[0].kind == "criterion_in_diff"


def test_rewrite_refuses_and_rolls_back_a_candidate_that_moves_criterion_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A candidate that rewrites prose under cover of a gate fix is refused."""
    state = _state()
    wave = state.waves[_WAVE_ID]
    original = gate_kind_rewrite._promote_criteria

    def tampered(*args: Any, **kwargs: Any) -> Any:
        rows = original(*args, **kwargs)
        rows[0] = rows[0].model_copy(update={"text": "smuggled prose; pytest exits zero"})
        return rows

    monkeypatch.setattr(gate_kind_rewrite, "_promote_criteria", tampered)

    with pytest.raises(LifecycleError, match="changes more than the rewritten gates"):
        rewrite_closed_wave_gate_kinds(
            state, wave_id=_WAVE_ID, rewrites=[_rewrite("GATE-01")], reason=_REASON
        )
    assert wave.gates[0].kind == "criterion_in_diff"
    assert wave.success_criteria[0].text.startswith("CR-01 the daemon")


# ---- The RPC surface ----------------------------------------------------------


def test_rewrite_gate_kind_rpc_dry_run_writes_nothing(tmp_path: Path) -> None:
    """A dry run reports the would-change set with the state bytes intact."""
    state_path, ctx = _seed(tmp_path)
    before_bytes = state_path.read_bytes()

    async def body() -> None:
        result = await rewrite_gate_kind(
            ctx,
            {
                "wave_id": _WAVE_ID,
                "rewrites": [{"gate_id": "G-CMD-02", "criterion_id": "CR-02", "argv": _ARGV}],
                "reason": _REASON,
                "dry_run": True,
                "repo_root": str(state_path.parent.parent),
            },
        )
        assert result["dry_run"] is True
        assert result["changed_count"] == 1
        assert result["envelope"] is None

    _drive(body)
    assert state_path.read_bytes() == before_bytes


def test_rewrite_gate_kind_rpc_refuses_a_non_wave_scope(tmp_path: Path) -> None:
    """A phase scope is refused: the repair is authorised one wave at a time."""
    state_path, ctx = _seed(tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="wave scope"):
            await rewrite_gate_kind(
                ctx,
                {
                    "wave_id": "P33",
                    "rewrites": [{"gate_id": "GATE-01", "argv": _ARGV}],
                    "reason": _REASON,
                    "repo_root": str(state_path.parent.parent),
                },
            )

    _drive(body)


def test_rewrite_gate_kind_rpc_refuses_an_empty_rewrite_list(tmp_path: Path) -> None:
    """The params floor requires at least one rewrite."""
    _state_path, ctx = _seed(tmp_path)

    async def body() -> None:
        with pytest.raises(ValueError, match="validation_failed"):
            await rewrite_gate_kind(ctx, {"wave_id": _WAVE_ID, "rewrites": [], "reason": _REASON})

    _drive(body)


# ---- The CLI -------------------------------------------------------------------


def test_parse_gate_kind_rewrite_reads_a_recorded_gate() -> None:
    """A bare gate id leaves the criterion to the recorded binding."""
    row = parse_gate_kind_rewrite("GATE-01=uv run pytest tests/x.py -k 'not slow' -q")

    assert row == {
        "gate_id": "GATE-01",
        "criterion_id": None,
        "argv": ["uv", "run", "pytest", "tests/x.py", "-k", "not slow", "-q"],
    }


def test_parse_gate_kind_rewrite_reads_a_new_gate_with_its_criterion() -> None:
    """``@CR-02`` names the criterion a new gate proves."""
    row = parse_gate_kind_rewrite(" G-CMD-02 @ CR-02 =uv run pytest tests/x.py")

    assert row["gate_id"] == "G-CMD-02"
    assert row["criterion_id"] == "CR-02"


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        ("uv run pytest", "expected <gate-id>"),
        ("=uv run pytest", "empty gate id"),
        ("G-01@=uv run pytest", "empty criterion id"),
        ("G-01=   ", "empty argv"),
    ],
)
def test_parse_gate_kind_rewrite_rejects_a_malformed_value(spec: str, message: str) -> None:
    """Each malformed half is named rather than defaulted."""
    with pytest.raises(typer.BadParameter, match=message):
        parse_gate_kind_rewrite(spec)


def test_rewrite_gate_kind_cli_refuses_a_missing_reason(tmp_path: Path) -> None:
    """The CLI floors the reason before any daemon contact."""
    result = CliRunner().invoke(
        app,
        [
            "-w",
            str(tmp_path),
            "spec",
            "rewrite-gate-kind",
            _WAVE_ID,
            "--gate",
            "GATE-01=uv run pytest tests/x.py -q",
        ],
    )

    assert isinstance(result.exception, UserError), result.output
    assert result.exception.exit_code == USER_ERROR
    assert "non-empty --reason" in str(result.exception)


# ---- The re-receipt row keeps a red as a finding ----------------------------


def test_rereceipt_outcome_refuses_a_nonzero_exit_recorded_as_pass() -> None:
    """A red re-run cannot be written down as a pass."""
    with pytest.raises(ValidationError, match="cannot be recorded as a pass"):
        GateRereceiptOutcome(
            gate_id="G-01", result=GateReceiptResult.PASS, exit_status=1, argv=_ARGV
        )


def test_rereceipt_outcome_records_a_red_as_a_failure_with_its_command() -> None:
    """The failing row names what ran and how it ended."""
    outcome = GateRereceiptOutcome(
        gate_id="G-01", result=GateReceiptResult.FAIL, exit_status=1, argv=_ARGV
    )

    assert outcome.result is GateReceiptResult.FAIL
    assert outcome.exit_status == 1
    assert outcome.argv == _ARGV


def test_rereceipt_outcome_accepts_a_pass_with_exit_zero() -> None:
    """The boundary: exit zero is a pass."""
    outcome = GateRereceiptOutcome(gate_id="G-01", result=GateReceiptResult.PASS, exit_status=0)

    assert outcome.result is GateReceiptResult.PASS


def test_render_rereceipt_names_each_red_gate_with_its_command() -> None:
    """A red gate is listed with its exit and argv; a green one is not."""
    text = render_rereceipt(
        {
            "wave_id": _WAVE_ID,
            "landed_sha": "c" * 40,
            "binding_id": "GRR-0123456789ab",
            "passed_count": 1,
            "failed_count": 1,
            "receipt_ids": [],
            "gates": [
                {"gate_id": "G-01", "result": "pass", "exit_status": 0, "argv": _ARGV},
                {"gate_id": "G-02", "result": "fail", "exit_status": 1, "argv": _ARGV},
            ],
        }
    )

    assert "G-02: fail exit=1 argv=uv run pytest" in text
    assert "G-01:" not in text
