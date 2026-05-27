"""Tests for :mod:`eawf.workflow.lifecycle.waivers` (P28-I01-W11).

The wave's success criteria pinned by these tests:

* ``apply_waiver`` rejects an agent session (operator-only contract).
* ``apply_waiver`` rejects when no operator session is attached.
* Mode B requires ``--reason``; missing reason raises.
* Mode C requires a ``--decision`` or ``--audit`` ref; missing both raises.
* Mode A accepts a bare ``--waive`` (no reason, no ref) and stamps the
  ``WAIVER_NO_REASON_SUMMARY`` fallback on the record.
* The composed :class:`EvidenceRecord` carries ``produced_by="human"``,
  ``status="waived"``, ``evidence_kind="attested"``, the gate id in
  ``refs``, and ``metrics["wave_sha"]`` when a SHA is derivable.
* :func:`resolve_waiver_mode` returns :data:`DEFAULT_WAIVER_MODE`
  (``"B"``) when ``verify.waiver_mode`` is absent.
* :func:`resolve_waiver_mode` returns the configured value when
  ``verify.waiver_mode`` is one of ``A`` / ``B`` / ``C``.
* :func:`resolve_waiver_mode` falls back to default on an unknown
  string.
* The direct-write env gate persists the row to ``evidence.jsonl``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
    AgentSession,
    CurrentPointers,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.lifecycle.waivers import (
    DEFAULT_WAIVER_MODE,
    EVIDENCE_DIRECT_WRITE_ENV,
    WAIVER_NO_REASON_SUMMARY,
    WaiverInput,
    apply_waiver,
    resolve_waiver_mode,
)

WAVE_ID = "P01-I01-W01"
OPERATOR_SESSION_ID = "SES-operator-1"
EXECUTOR_SESSION_ID = "SES-executor-1"


# ---- Fixtures ---------------------------------------------------------------


def _empty_state() -> State:
    """Build a minimal valid State with no phases / waves seeded."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:WAI",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="WAI",
                slug="wai",
                title="WAI",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:WAI",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="WAI").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_wave_with_operator(state: State) -> None:
    """Seed P01 / P01-I01 / WAVE_ID + attach an OPERATOR session."""
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=[],
        effort_bucket="M",
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")

    operator = AgentSession(
        id=OPERATOR_SESSION_ID,
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id=WAVE_ID,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[OPERATOR_SESSION_ID] = operator
    state.current.active_session_ids = [OPERATOR_SESSION_ID]


def _seed_wave_with_executor(state: State) -> None:
    """Seed P01 / P01-I01 / WAVE_ID + attach an EXECUTOR (non-operator) session."""
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=[],
        effort_bucket="M",
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")

    executor = AgentSession(
        id=EXECUTOR_SESSION_ID,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude",
        scope_id=WAVE_ID,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[EXECUTOR_SESSION_ID] = executor
    state.current.active_session_ids = [EXECUTOR_SESSION_ID]


# ---- resolve_waiver_mode ----------------------------------------------------


def test_resolve_waiver_mode_returns_default_when_missing() -> None:
    """Absent ``verify.waiver_mode`` falls back to :data:`DEFAULT_WAIVER_MODE`."""
    assert resolve_waiver_mode({}) == DEFAULT_WAIVER_MODE
    assert resolve_waiver_mode(None) == DEFAULT_WAIVER_MODE
    assert resolve_waiver_mode({"verify": {}}) == DEFAULT_WAIVER_MODE
    assert resolve_waiver_mode({"verify": {"other_leaf": "x"}}) == DEFAULT_WAIVER_MODE
    # The default IS mode B per the W11 land — pin it so a future
    # default-flip is loud at test time.
    assert DEFAULT_WAIVER_MODE == "B"


def test_resolve_waiver_mode_honours_a_b_c() -> None:
    """Each closed-literal value is returned verbatim."""
    assert resolve_waiver_mode({"verify": {"waiver_mode": "A"}}) == "A"
    assert resolve_waiver_mode({"verify": {"waiver_mode": "B"}}) == "B"
    assert resolve_waiver_mode({"verify": {"waiver_mode": "C"}}) == "C"


def test_resolve_waiver_mode_unknown_falls_back_to_default(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A garbage value warns + falls back rather than crashing."""
    with caplog.at_level("WARNING"):
        result = resolve_waiver_mode({"verify": {"waiver_mode": "D"}})
    assert result == DEFAULT_WAIVER_MODE
    assert any("unknown-value" in rec.message for rec in caplog.records)


def test_resolve_waiver_mode_non_dict_verify_falls_back() -> None:
    """A non-dict ``verify`` leaf is ignored (defensive)."""
    assert resolve_waiver_mode({"verify": "not-a-dict"}) == DEFAULT_WAIVER_MODE
    assert resolve_waiver_mode({"verify": ["list"]}) == DEFAULT_WAIVER_MODE


def test_resolve_waiver_mode_reads_typed_verify_block() -> None:
    """P28-I01-W10: the helper accepts a typed :class:`VerifyBlock` directly."""
    from eawf.platform.profiles.models import VerifyBlock

    assert resolve_waiver_mode(VerifyBlock(waiver_mode="A")) == "A"
    assert resolve_waiver_mode(VerifyBlock(waiver_mode="B")) == "B"
    assert resolve_waiver_mode(VerifyBlock(waiver_mode="C")) == "C"
    # Default-constructed VerifyBlock IS mode B.
    assert resolve_waiver_mode(VerifyBlock()) == "B"


# ---- operator-only contract -------------------------------------------------


def test_apply_waiver_rejects_executor_session(tmp_path: Path) -> None:
    """Agent (EXECUTOR) session cannot waive — operator-only contract."""
    state = _empty_state()
    _seed_wave_with_executor(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    waiver = WaiverInput(gate_id="GATE-x", reason="please")
    with pytest.raises(cli_errors.ValidationError, match="agent sessions cannot waive"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=waiver,
            operator_identity=EXECUTOR_SESSION_ID,
            mode="B",
            state_path=state_path,
            repo_root=tmp_path,
        )


def test_apply_waiver_rejects_missing_session(tmp_path: Path) -> None:
    """``operator_identity=None`` is rejected (no operator attached)."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    waiver = WaiverInput(gate_id="GATE-x", reason="please")
    with pytest.raises(cli_errors.ValidationError, match="no active session attached"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=waiver,
            operator_identity=None,
            mode="B",
            state_path=state_path,
            repo_root=tmp_path,
        )


def test_apply_waiver_rejects_unknown_session(tmp_path: Path) -> None:
    """An ``operator_identity`` not in ``state.agent_sessions`` is rejected."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    waiver = WaiverInput(gate_id="GATE-x", reason="please")
    with pytest.raises(cli_errors.ValidationError, match="unknown session"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=waiver,
            operator_identity="SES-ghost",
            mode="B",
            state_path=state_path,
            repo_root=tmp_path,
        )


# ---- mode-gated linkage policy ----------------------------------------------


def test_mode_b_requires_reason(tmp_path: Path) -> None:
    """Mode B rejects a waiver missing ``--reason``."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    waiver = WaiverInput(gate_id="GATE-no-reason", reason=None)
    with pytest.raises(cli_errors.ValidationError, match="requires --reason in mode B"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=waiver,
            operator_identity=OPERATOR_SESSION_ID,
            mode="B",
            state_path=state_path,
            repo_root=tmp_path,
        )


def test_mode_c_requires_reason_and_ref(tmp_path: Path) -> None:
    """Mode C rejects when reason is missing OR when both decision and audit are missing."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    # Missing reason.
    with pytest.raises(cli_errors.ValidationError, match="requires --reason in mode C"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=WaiverInput(gate_id="GATE-c1", reason=None),
            operator_identity=OPERATOR_SESSION_ID,
            mode="C",
            state_path=state_path,
            repo_root=tmp_path,
        )

    # Reason present but neither decision nor audit ref.
    with pytest.raises(
        cli_errors.ValidationError, match="requires --decision or --audit in mode C"
    ):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=WaiverInput(gate_id="GATE-c2", reason="fine"),
            operator_identity=OPERATOR_SESSION_ID,
            mode="C",
            state_path=state_path,
            repo_root=tmp_path,
        )


def test_mode_c_accepts_with_decision_ref(tmp_path: Path) -> None:
    """Mode C accepts a waiver with reason + decision ref."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(
            gate_id="GATE-c-ok",
            reason="approved by ops",
            decision_ref="urn:eawf:v1:decision:OWNER/D-01",
        ),
        operator_identity=OPERATOR_SESSION_ID,
        mode="C",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert record.produced_by == "human"
    assert record.status == "waived"
    assert "GATE-c-ok" in record.refs
    assert "urn:eawf:v1:decision:OWNER/D-01" in record.refs


def test_mode_c_accepts_with_audit_ref(tmp_path: Path) -> None:
    """Mode C accepts a waiver with reason + audit ref (no decision)."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(
            gate_id="GATE-c-audit",
            reason="cleared by audit",
            audit_ref="A40-P28-w11-test",
        ),
        operator_identity=OPERATOR_SESSION_ID,
        mode="C",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert "A40-P28-w11-test" in record.refs


def test_mode_a_allows_no_reason_no_ref(tmp_path: Path) -> None:
    """Mode A accepts a bare waiver — the record falls back to the no-reason summary."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-a-bare"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="A",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert record.summary == WAIVER_NO_REASON_SUMMARY
    assert record.refs == ["GATE-a-bare"]
    assert record.status == "waived"
    assert record.produced_by == "human"


# ---- record composition -----------------------------------------------------


def test_record_carries_operator_session_metric(tmp_path: Path) -> None:
    """The composed record carries ``metrics['operator_session']`` for trace-back."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-trace", reason="why"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert record.metrics is not None
    assert record.metrics["operator_session"] == OPERATOR_SESSION_ID


def test_record_shape_matches_spec(tmp_path: Path) -> None:
    """Composed record carries the expected closed-literal field values."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-shape", reason="ok"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )

    assert record.scope_id == WAVE_ID
    assert record.produced_by == "human"
    assert record.evidence_kind == "attested"
    assert record.status == "waived"
    assert record.summary == "ok"
    assert "GATE-shape" in record.refs
    assert record.id.startswith("EV-")


# ---- direct-write persistence -----------------------------------------------


def test_direct_write_appends_to_evidence_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``EAWF_EVIDENCE_DIRECT_WRITE=1`` persists the row to evidence.jsonl."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    monkeypatch.setenv(EVIDENCE_DIRECT_WRITE_ENV, "1")
    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-direct", reason="cli-recovery"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    assert evidence_path.exists()
    line = evidence_path.read_text(encoding="utf-8").strip().splitlines()[0]
    envelope = Envelope.model_validate_json(line)
    assert envelope.kind == StoreKind.EVIDENCE
    assert envelope.id == record.id
    persisted = EvidenceRecord.model_validate(envelope.payload)
    assert persisted.status == "waived"
    assert persisted.produced_by == "human"
    assert "GATE-direct" in persisted.refs


def test_no_direct_write_does_not_touch_evidence_jsonl(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without the env gate, apply_waiver only builds; the CLI proxies the write."""
    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    monkeypatch.delenv(EVIDENCE_DIRECT_WRITE_ENV, raising=False)
    apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-noop", reason="x"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )
    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    assert not evidence_path.exists()


def test_direct_write_without_state_path_raises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct-write fallback requires ``state_path`` — None raises RuntimeError."""
    state = _empty_state()
    _seed_wave_with_operator(state)

    monkeypatch.setenv(EVIDENCE_DIRECT_WRITE_ENV, "1")
    with pytest.raises(RuntimeError, match="requires state_path"):
        apply_waiver(
            state,
            wave_id=WAVE_ID,
            waiver=WaiverInput(gate_id="GATE-x", reason="y"),
            operator_identity=OPERATOR_SESSION_ID,
            mode="B",
            state_path=None,
            repo_root=tmp_path,
        )


# ---- SHA-bound freshness on the record itself -------------------------------


def test_record_stamps_wave_sha_when_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ``derive_wave_sha`` returns a value, it's stamped into ``metrics``."""
    import eawf.workflow.lifecycle.waivers as waivers_mod

    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    monkeypatch.setattr(
        waivers_mod,
        "derive_wave_sha",
        lambda wave_id, repo_root=None: "deadbeefcafe",
    )
    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-sha", reason="ok"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert record.metrics is not None
    assert record.metrics["wave_sha"] == "deadbeefcafe"


def test_record_omits_wave_sha_when_derive_returns_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A None SHA (uncommitted wave) leaves ``metrics['wave_sha']`` absent."""
    import eawf.workflow.lifecycle.waivers as waivers_mod

    state = _empty_state()
    _seed_wave_with_operator(state)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    monkeypatch.setattr(
        waivers_mod,
        "derive_wave_sha",
        lambda wave_id, repo_root=None: None,
    )
    record = apply_waiver(
        state,
        wave_id=WAVE_ID,
        waiver=WaiverInput(gate_id="GATE-nosha", reason="ok"),
        operator_identity=OPERATOR_SESSION_ID,
        mode="B",
        state_path=state_path,
        repo_root=tmp_path,
    )
    assert record.metrics is not None
    assert "wave_sha" not in record.metrics


# ---- WaiverInput strictness -------------------------------------------------


def test_waiver_input_rejects_extra_fields() -> None:
    """WaiverInput is strict — extra fields raise ValidationError."""
    from pydantic import ValidationError as PydValidationError

    with pytest.raises(PydValidationError):
        WaiverInput.model_validate(
            {
                "gate_id": "GATE-x",
                "reason": "x",
                "phantom_field": True,
            }
        )


# ---- CLI: wave close --waive ------------------------------------------------


def _bootstrap_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[object, Path]:
    """Init a project + open the I01 wave + return (runner, state_path).

    Mirrors :func:`tests.workflow.verify.test_seams._make_repo` setup
    but skips the worktree dance — the CLI tests in this module only
    drive ``wave close``, no commit / cherry-pick.
    """
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    runner = CliRunner()
    assert (
        runner.invoke(app, ["project", "init", "WV", "--title", "W", "--domains", "x"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                WAVE_ID,
                "--title",
                "wave",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    return runner, state_path


def _seed_operator_session_into_disk_state(state_path: Path, *, session_id: str) -> None:
    """Read state.json, attach an OPERATOR session + claim wave, persist."""
    raw = orjson.loads(state_path.read_bytes())
    state = State.model_validate(raw)
    operator = AgentSession(
        id=session_id,
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id=WAVE_ID,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[session_id] = operator
    state.current.active_session_ids = [session_id]
    # Claim the wave through the operator session so close is legal.
    claim_wave(state, wave_id=WAVE_ID, session_id=session_id)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))


def test_cli_wave_close_bare_waive_without_reason_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode B default: bare ``--waive`` with no ``--reason`` is rejected (sc #2)."""
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)
    _seed_operator_session_into_disk_state(state_path, session_id=OPERATOR_SESSION_ID)

    from eawf.surfaces.cli.app import app

    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-zzz",
        ],
    )
    # Exit code != 0 with a message mentioning --reason in mode B.
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "requires --reason" in combined or "reason" in combined.lower()


def test_cli_wave_close_with_waive_and_reason_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sc #1: ``--waive GATE_ID --reason "text"`` persists the EvidenceRecord."""
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)
    _seed_operator_session_into_disk_state(state_path, session_id=OPERATOR_SESSION_ID)

    from eawf.surfaces.cli.app import app

    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-good",
            "--reason",
            "code reviewed",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    assert evidence_path.exists()
    lines = [ln for ln in evidence_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    assert len(lines) >= 1
    # Find the waiver row (status=waived).
    waiver_rows: list[EvidenceRecord] = []
    for line in lines:
        env = Envelope.model_validate_json(line)
        if env.kind != StoreKind.EVIDENCE:
            continue
        rec = EvidenceRecord.model_validate(env.payload)
        if rec.status == "waived":
            waiver_rows.append(rec)
    assert len(waiver_rows) == 1
    waiver = waiver_rows[0]
    assert waiver.produced_by == "human"
    assert waiver.evidence_kind == "attested"
    assert "GATE-good" in waiver.refs
    assert waiver.summary == "code reviewed"


def test_cli_wave_close_mode_c_missing_ref_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sc #3: Mode C requires a decision OR audit ref; missing both is rejected.

    Mode C is wired via a workspace-layer ``config.yaml`` so the
    merged config resolves :func:`resolve_waiver_mode` to ``C``.
    """
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)
    _seed_operator_session_into_disk_state(state_path, session_id=OPERATOR_SESSION_ID)

    # Drop a workspace-layer config that sets verify.waiver_mode=C.
    workspace_cfg = tmp_path / ".ea" / "config.yaml"
    workspace_cfg.write_text("verify:\n  waiver_mode: C\n", encoding="utf-8")

    from eawf.surfaces.cli.app import app

    # Reason present but no decision/audit ref.
    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-c",
            "--reason",
            "needed",
        ],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "decision" in combined.lower() or "audit" in combined.lower()


def test_cli_wave_close_mode_a_accepts_bare_waive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sc #4: Mode A accepts ``--waive`` with no reason or ref."""
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)
    _seed_operator_session_into_disk_state(state_path, session_id=OPERATOR_SESSION_ID)

    workspace_cfg = tmp_path / ".ea" / "config.yaml"
    workspace_cfg.write_text("verify:\n  waiver_mode: A\n", encoding="utf-8")

    from eawf.surfaces.cli.app import app

    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-a-cli",
        ],
    )
    assert result.exit_code == 0, (result.stdout, result.stderr)

    evidence_path = store_path(state_path, StoreKind.EVIDENCE)
    waivers = []
    for line in evidence_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        rec = EvidenceRecord.model_validate(env.payload)
        if rec.status == "waived":
            waivers.append(rec)
    assert len(waivers) == 1
    assert waivers[0].summary == WAIVER_NO_REASON_SUMMARY


def test_cli_wave_close_rejects_agent_session_waiver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """sc #7: An agent-claimed wave cannot waive — operator-only contract."""
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)

    # Inject an EXECUTOR session instead of OPERATOR.
    raw = orjson.loads(state_path.read_bytes())
    state = State.model_validate(raw)
    executor = AgentSession(
        id=EXECUTOR_SESSION_ID,
        role=AgentSessionRole.EXECUTOR,
        runtime="claude",
        scope_id=WAVE_ID,
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime.now(UTC),
    )
    state.agent_sessions[EXECUTOR_SESSION_ID] = executor
    state.current.active_session_ids = [EXECUTOR_SESSION_ID]
    claim_wave(state, wave_id=WAVE_ID, session_id=EXECUTOR_SESSION_ID)
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    from eawf.surfaces.cli.app import app

    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-agent",
            "--reason",
            "should not work",
        ],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "agent sessions cannot waive" in combined or "operator session" in combined


def test_cli_wave_close_parallel_list_length_mismatch_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Reject when ``--reason`` count disagrees with ``--waive`` count."""
    runner, state_path = _bootstrap_cli(tmp_path, monkeypatch)
    _seed_operator_session_into_disk_state(state_path, session_id=OPERATOR_SESSION_ID)

    from eawf.surfaces.cli.app import app

    # Two waivers + one reason -> length mismatch.
    result = runner.invoke(
        app,
        [
            "wave",
            "close",
            WAVE_ID,
            "--outcome",
            "ok",
            "--waive",
            "GATE-1",
            "--waive",
            "GATE-2",
            "--reason",
            "only one reason",
        ],
    )
    assert result.exit_code != 0
    combined = result.stdout + (result.stderr or "")
    assert "list length" in combined or "must equal" in combined
