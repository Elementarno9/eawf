"""Unit tests for the subagent prompt renderer (B025).

Exercises :func:`eawf.dispatch.renderer.render_wave_prompt` against
hand-built :class:`State` instances. The renderer is pure — no I/O —
so each test composes a state in memory and inspects the returned
string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from eawf.dispatch import render_wave_prompt
from eawf.lifecycle.transitions import (
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    DecisionStatus,
    HypothesisStatus,
    HypothesisVerdict,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
    WorktreeStatus,
)
from eawf.state.models import (
    Audit,
    CurrentPointers,
    Decision,
    Hypothesis,
    Project,
    State,
    WorktreeRecord,
)

# ---- Builders ---------------------------------------------------------------


def _empty_state() -> State:
    """Return a minimal State with project=QR, scope_id=QR for the phase."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
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


def _seed_chain(state: State) -> None:
    """Seed P01 → P01-I01 → P01-I01-W01..W03 (linear chain)."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="First wave",
        file_scopes=["src/foo/", "tests/unit/test_foo.py"],
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="Second wave",
        file_scopes=["src/bar/"],
        deps=["P01-I01-W01"],
    )


# ---- Section coverage -------------------------------------------------------


def test_render_minimal_wave_prompt() -> None:
    """A wave with no deps/decisions/hypotheses/audits still emits all headers."""
    state = _empty_state()
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Solo wave",
        file_scopes=["src/"],
    )
    out = render_wave_prompt(state, "P01-I01-W01")
    # Required section headers.
    assert "# Wave P01-I01-W01: Solo wave" in out
    assert "## Scope" in out
    assert "## Dependencies" in out
    assert "## Decisions" in out
    assert "## Hypotheses" in out
    assert "## Recent audits" in out
    assert "## Working tree" in out
    assert "## Workflow" in out
    assert "## Out of scope" in out
    # No-dep / no-evidence sentinels.
    assert "None." in out
    # Commit-prefix uses the wave's phase + wave segments (Pxx-Wzz).
    assert "[P01-W01]" in out


def test_render_includes_dependencies_with_status() -> None:
    """Each dep is rendered as ``- <id>: <title> (status=<value>)``."""
    state = _empty_state()
    _seed_chain(state)
    out = render_wave_prompt(state, "P01-I01-W02")
    # Deps section enumerates W01 with title + status.
    assert "- P01-I01-W01: First wave (status=pending)" in out
    # W01's prompt should have None.
    out_w1 = render_wave_prompt(state, "P01-I01-W01")
    deps_block = out_w1.split("## Dependencies", 1)[1].split("##", 1)[0]
    assert "None." in deps_block


def test_render_includes_attached_decisions() -> None:
    """Decisions in the same scope appear under ``## Decisions`` with rationale."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {
        "D01": Decision(
            id="D01",
            scope_id="QR",
            summary="Cherry-pick worktrees, never merge",
            rationale="Merges break the [P-W] / [P-CORE] history audit trail.",
            alternatives=["squash"],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D02": Decision(
            id="D02",
            scope_id="QR",
            summary="State CLI is the only writer",
            rationale="Direct edits bypass the audit-side event.jsonl.",
            alternatives=[],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D99": Decision(
            id="D99",
            scope_id="OTHER",
            summary="Out-of-scope",
            rationale="Should not appear in QR wave prompts.",
            alternatives=[],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
    }
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "### D01: Cherry-pick worktrees, never merge" in out
    assert "Merges break the [P-W] / [P-CORE] history audit trail." in out
    assert "### D02: State CLI is the only writer" in out
    # Out-of-scope decision must NOT appear.
    assert "D99" not in out
    # Sort: D01 before D02.
    assert out.index("### D01") < out.index("### D02")


def test_render_includes_hypotheses_with_open_verdict() -> None:
    """A hypothesis without a verdict appears with verdict=open."""
    state = _empty_state()
    _seed_chain(state)
    state.hypotheses = {
        "H01-01": Hypothesis(
            id="H01-01",
            scope_id="QR",
            text="Workflow rendering is idempotent",
            metric="render_drift_count",
            confirm="drift == 0",
            reject="drift > 0",
            status=HypothesisStatus.PENDING,
            verdict=None,
            audit_id=None,
            source_artifact_id=None,
        ),
        "H01-02": Hypothesis(
            id="H01-02",
            scope_id="QR",
            text="Closed deps unblock children",
            metric="ready_after_close",
            confirm="next-ready surfaces child",
            reject="next-ready stays empty",
            status=HypothesisStatus.CONFIRMED,
            verdict=HypothesisVerdict.CONFIRMED,
            audit_id=None,
            source_artifact_id=None,
        ),
    }
    out = render_wave_prompt(state, "P01-I01-W01")
    block = out.split("## Hypotheses", 1)[1].split("## ", 1)[0]
    assert "metric='render_drift_count'" in block
    assert "verdict: open" in block
    assert "verdict: confirmed" in block


def test_render_recent_audits_sorted_desc_and_truncated_to_5() -> None:
    """Audits sort by ``created_at`` desc and at most five appear."""
    state = _empty_state()
    _seed_chain(state)
    base = datetime(2026, 1, 1, tzinfo=UTC)
    audits: dict[str, Audit] = {}
    for idx in range(7):
        aid = f"A{idx:02d}"
        audits[aid] = Audit(
            id=aid,
            scope_id="QR",
            kind=AuditKind.EVALUATION,
            status=AuditStatus.COMPLETE,
            report_artifact_id=None,
            check_results=[],
            integrity_results=[],
            created_at=base + timedelta(days=idx),
            verdict=AuditVerdict.PASS if idx % 2 == 0 else None,
        )
    state.audits = audits
    out = render_wave_prompt(state, "P01-I01-W01")
    block = out.split("## Recent audits", 1)[1].split("## ", 1)[0]
    # Most recent five: A06, A05, A04, A03, A02 — A00 / A01 dropped.
    assert "A06" in block
    assert "A02" in block
    assert "A01" not in block
    assert "A00" not in block
    # Desc order: A06 appears before A02 in the rendered block.
    assert block.index("A06") < block.index("A02")
    # Pending verdict surfaces as 'pending', PASS as 'pass'.
    assert "verdict=pass" in block
    assert "verdict=pending" in block


def test_render_when_worktree_record_present() -> None:
    """A wave with a worktree_id surfaces the Branch / Worktree path / Base lines."""
    state = _empty_state()
    _seed_chain(state)
    record_id = "WT-P01-I01-W01-1700000000"
    state.worktrees = {
        record_id: WorktreeRecord(
            id=record_id,
            wave_id="P01-I01-W01",
            branch="feature/eawf-v0.1-p01-w01",
            path=".claude/worktrees/p01-w01",
            base_branch="feature/eawf-v0.1",
            status=WorktreeStatus.ACTIVE,
            owner_session_id="SES-1",
            created_at=datetime.now(UTC),
            merged_commit=None,
        )
    }
    state.waves["P01-I01-W01"].worktree_id = record_id
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "Branch: feature/eawf-v0.1-p01-w01" in out
    assert "Worktree path: .claude/worktrees/p01-w01" in out
    assert "Base commit: feature/eawf-v0.1" in out


def test_render_when_no_worktree_record_omits_branch_lines() -> None:
    """No worktree_id ⇒ Branch / Base lines absent; ``Worktree path: inline`` shown."""
    state = _empty_state()
    _seed_chain(state)
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "Branch:" not in out
    assert "Base commit:" not in out
    assert "Worktree path: inline" in out


def test_render_unknown_wave_raises_key_error() -> None:
    """Missing wave id surfaces as :class:`KeyError`."""
    state = _empty_state()
    _seed_chain(state)
    with pytest.raises(KeyError, match="unknown wave"):
        render_wave_prompt(state, "P01-I01-W99")


# ---- Boundary: closed wave still renders -----------------------------------


def test_render_terminal_wave_still_emits_prompt() -> None:
    """A CLOSED wave produces a prompt for history inspection."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].status = WaveStatus.CLOSED
    state.waves["P01-I01-W01"].commit = "abc1234"
    state.waves["P01-I01-W01"].outcome = "all green"
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "# Wave P01-I01-W01: First wave" in out
