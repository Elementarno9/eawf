"""Unit tests for :mod:`eawf.workflow.evidence.evibound` (EviBound rung-1).

Covers the two rung-1 surfaces:

* :func:`run_rung1_gate` — the criterion-certification rung. Asserts it
  reuses the verify-spine chain ``compile_gate ->
  _run_deterministic_gate -> run_checks`` (whose pass bit is
  ``returncode == 0``), CERTIFIES the criterion (evidence status
  ``"pass"``) on a passing gate, and does NOT certify on ``"fail"`` /
  ``"blocked"``.
* :func:`check_brief_promotable` — the brief-promotion gate. Asserts a
  brief is promotable iff every ``evidence_refs`` entry resolves under
  rung-1, with the empty-refs boundary and the multi-ref / single-fail
  paths.

The rung-1 reuse is exercised two ways: an end-to-end pass via a real
``command_exit_zero`` gate whose allowlisted argv exits 0
(``returncode == 0`` -> certify) and exits non-zero (-> no certify),
plus a monkeypatch guard that pins the delegation to
``_run_deterministic_gate`` so a future refactor cannot silently fork a
second copy of the gate chain. The real-subprocess argv heads stay
inside the L0 argv-policy allowlist (``pytest`` / ``mypy`` under the
``uv run`` wrapper) so the GateSpec construction-time validator accepts
them.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.common import CriterionSpec, GateSpec
from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.store.kinds.evidence import EvidenceRecord
from eawf.workflow.evidence.evibound import (
    BriefPromotionGate,
    BriefRefOutcome,
    check_brief_promotable,
    run_rung1_gate,
)

_SCOPE = "urn:eawf:v1:wave:owner/P29-I01-W08"


def _deterministic_criterion(criterion_id: str = "CR-1") -> CriterionSpec:
    """Build a deterministic CriterionSpec addressing one gate."""
    return CriterionSpec(
        id=criterion_id,
        text="deterministic floor holds",
        kind="behavioral",
        acceptance_style="binary",
        evidence_kind="deterministic",
        gate_ids=["G-1"],
    )


def _command_gate(argv: list[str], gate_id: str = "G-1") -> GateSpec:
    """Build a command_exit_zero GateSpec with an allowlisted argv."""
    return GateSpec(
        id=gate_id,
        criterion_id="CR-1",
        kind="command_exit_zero",
        args={"argv": argv, "scope": "all"},
        policy="block",
        cadence="every-wave",
    )


# --------------------------------------------------------------------------- #
# run_rung1_gate: reuses the gate chain; rc==0 certifies, non-zero does not.
# --------------------------------------------------------------------------- #
def test_run_rung1_gate_pass_certifies_criterion(tmp_path: Path) -> None:
    """A gate whose argv exits 0 (returncode == 0) CERTIFIES the criterion.

    ``uv run pytest --version`` exits 0 and stays inside the L0 argv
    allowlist (``pytest`` head under the ``uv run`` wrapper), so the
    GateSpec construction validator accepts it and the live subprocess
    returns ``returncode == 0`` -> ``passed`` -> rung-1 ``"pass"``.
    """
    criterion = _deterministic_criterion()
    gate = _command_gate(["uv", "run", "pytest", "--version"])
    record = run_rung1_gate(gate, criterion, scope_id=_SCOPE, runner_cwd=tmp_path)
    assert isinstance(record, EvidenceRecord)
    assert record.status == "pass"
    assert record.evidence_kind == "deterministic"
    assert record.produced_by == "tool"
    assert record.scope_id == _SCOPE
    assert "G-1" in record.refs
    assert "CR-1" in record.refs
    assert "certified" in record.summary


def test_run_rung1_gate_fail_does_not_certify(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failing rung-1 gate (returncode != 0 -> ``"fail"``) does NOT certify.

    The ``returncode != 0`` -> ``not passed`` mapping is owned by the
    single audit-DSL runner (``_check_command_exit_zero``); here the
    chain is patched to return ``"fail"`` deterministically so the test
    pins the *certification* contract (fail -> non-certifying record)
    without depending on a tool's exit code.
    """
    monkeypatch.setattr(
        "eawf.workflow.evidence.evibound._run_deterministic_gate",
        lambda gate, criterion, *, runner_cwd: "fail",
    )
    criterion = _deterministic_criterion()
    gate = _command_gate(["uv", "run", "pytest"])
    record = run_rung1_gate(gate, criterion, scope_id=_SCOPE, runner_cwd=tmp_path)
    assert record.status == "fail"
    assert "certified" not in record.summary
    assert "fail" in record.summary


def test_run_rung1_gate_blocked_when_argv_missing(tmp_path: Path) -> None:
    """A command gate that compiles to None (no usable argv) yields a blocked record.

    compile_gate returns None when ``command_exit_zero`` lacks a usable
    argv; ``_run_deterministic_gate`` maps that to ``"blocked"`` rather
    than raising, so the rung-1 record is non-certifying.
    """
    criterion = _deterministic_criterion()
    # Construct a gate whose argv is present (so GateSpec's L0 validator
    # passes) but then blank it on a model_copy so compile_gate sees an
    # empty list and returns None.
    gate = _command_gate(["uv", "run", "pytest"]).model_copy(update={"args": {"argv": []}})
    record = run_rung1_gate(gate, criterion, scope_id=_SCOPE, runner_cwd=tmp_path)
    assert record.status == "blocked"
    assert "certified" not in record.summary


def test_run_rung1_gate_delegates_to_run_deterministic_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rung-1 reuses ``_run_deterministic_gate`` (the compile_gate->run_checks chain).

    Pins the delegation so a refactor cannot fork a second copy of the
    verify-spine gate machinery. The patched chain returns ``"pass"`` and
    the rung-1 record's status MUST mirror it.
    """
    calls: list[tuple[str, str]] = []

    def _fake_chain(gate: GateSpec, criterion: CriterionSpec, *, runner_cwd: Path) -> str:
        calls.append((gate.id, criterion.id))
        assert runner_cwd == tmp_path
        return "pass"

    monkeypatch.setattr(
        "eawf.workflow.evidence.evibound._run_deterministic_gate",
        _fake_chain,
    )
    criterion = _deterministic_criterion()
    gate = _command_gate(["uv", "run", "pytest"])
    record = run_rung1_gate(gate, criterion, scope_id=_SCOPE, runner_cwd=tmp_path)
    assert calls == [("G-1", "CR-1")]
    assert record.status == "pass"
    assert "certified" in record.summary


# --------------------------------------------------------------------------- #
# check_brief_promotable: promotable iff every evidence_ref resolves rung-1.
# --------------------------------------------------------------------------- #
def test_check_brief_promotable_all_refs_resolve(tmp_path: Path) -> None:
    """Promotion allowed when every claim's evidence_refs pass rung-1."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "x.md").write_text("hi", encoding="utf-8")
    brief = IntentBrief(
        problem="ship the rung-1 gate",
        desired_outcome="evidence_refs enforced at promotion",
        evidence_refs=[
            "docs/x.md",
            "urn:eawf:v1:decision:owner/D17",
            "see prior work [3]",
        ],
    )
    gate = check_brief_promotable(brief, project_root=tmp_path)
    assert isinstance(gate, BriefPromotionGate)
    assert gate.promotable is True
    assert gate.reasons == ()
    assert len(gate.outcomes) == 3
    assert all(isinstance(o, BriefRefOutcome) and o.passed for o in gate.outcomes)


def test_check_brief_promotable_blocked_when_a_ref_fails_rung1(tmp_path: Path) -> None:
    """Promotion blocked when any claim's evidence_ref fails rung-1.

    The first ref exists on disk (resolves); the second points at a
    missing file (UNRESOLVED). A single failing ref blocks the brief and
    contributes one rejection line naming the offending ref.
    """
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "present.md").write_text("hi", encoding="utf-8")
    brief = IntentBrief(
        problem="ship the rung-1 gate",
        desired_outcome="evidence_refs enforced at promotion",
        evidence_refs=["docs/present.md", "docs/missing.md"],
    )
    gate = check_brief_promotable(brief, project_root=tmp_path)
    assert gate.promotable is False
    assert len(gate.reasons) == 1
    assert "docs/missing.md" in gate.reasons[0]
    assert "rung-1" in gate.reasons[0]
    # The passing ref is still recorded as a passed outcome.
    assert gate.outcomes[0].passed is True
    assert gate.outcomes[1].passed is False


def test_check_brief_promotable_blocks_non_portable_ref(tmp_path: Path) -> None:
    """An absolute-path ref fails rung-1 portability -> brief not promotable."""
    brief = IntentBrief(
        problem="p",
        desired_outcome="o",
        evidence_refs=["/etc/passwd"],
    )
    gate = check_brief_promotable(brief, project_root=tmp_path)
    assert gate.promotable is False
    assert len(gate.reasons) == 1
    assert "/etc/passwd" in gate.reasons[0]


def test_check_brief_promotable_empty_refs_is_promotable(tmp_path: Path) -> None:
    """An unsourced brief (no evidence_refs) is promotable trivially.

    The IntentBrief contract is that an unsourced brief still ingests;
    rung-1 has no claim to catch when the refs list is empty, so the
    gate returns promotable with no outcomes.
    """
    brief = IntentBrief(problem="p", desired_outcome="o", evidence_refs=[])
    gate = check_brief_promotable(brief, project_root=tmp_path)
    assert gate.promotable is True
    assert gate.outcomes == ()
    assert gate.reasons == ()


def test_check_brief_promotable_all_reasons_collected(tmp_path: Path) -> None:
    """Multiple failing refs each contribute a distinct rejection line."""
    brief = IntentBrief(
        problem="p",
        desired_outcome="o",
        evidence_refs=["docs/missing-a.md", "docs/missing-b.md"],
    )
    gate = check_brief_promotable(brief, project_root=tmp_path)
    assert gate.promotable is False
    assert len(gate.reasons) == 2
    assert any("missing-a" in r for r in gate.reasons)
    assert any("missing-b" in r for r in gate.reasons)


def test_evibound_public_exports() -> None:
    """The evidence package re-exports the rung-1 surface."""
    from eawf.workflow import evidence

    assert evidence.run_rung1_gate is run_rung1_gate
    assert evidence.check_brief_promotable is check_brief_promotable
    assert evidence.BriefPromotionGate is BriefPromotionGate
    assert evidence.BriefRefOutcome is BriefRefOutcome


def test_brief_promotion_gate_is_frozen(tmp_path: Path) -> None:
    """BriefPromotionGate is an immutable value object."""
    gate = check_brief_promotable(
        IntentBrief(problem="p", desired_outcome="o"), project_root=tmp_path
    )
    with pytest.raises((AttributeError, TypeError)):
        gate.promotable = False  # type: ignore[misc]
