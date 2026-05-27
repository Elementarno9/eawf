"""Unit tests for the subagent prompt renderer (B025).

Exercises :func:`eawf.workflow.dispatch.renderer.render_wave_prompt` against
hand-built :class:`State` instances. The renderer is pure — no I/O —
so each test composes a state in memory and inspects the returned
string.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from eawf.kernel.state.enums import (
    AuditKind,
    AuditStatus,
    AuditVerdict,
    Confidence,
    DecisionStatus,
    HypothesisStatus,
    HypothesisVerdict,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
    WorktreeStatus,
)
from eawf.kernel.state.models import (
    Audit,
    CurrentPointers,
    Decision,
    EstimateSummary,
    Hypothesis,
    Project,
    State,
    WorktreeRecord,
)
from eawf.workflow.agents.specs.models import SubagentSpec
from eawf.workflow.dispatch import build_subagent_spec, render_wave_prompt
from eawf.workflow.lifecycle.transitions import (
    open_iter,
    open_phase,
    plan_wave,
)

_T0 = datetime(2026, 1, 1, tzinfo=UTC)

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
        effort_bucket="M",
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="Second wave",
        file_scopes=["src/bar/"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
    )


def _estimate(*, wave_id: str, expected_eu: float, expected_minutes: float) -> EstimateSummary:
    """Return a deterministic estimate summary for renderer tests."""
    return EstimateSummary(
        id=f"EST-{wave_id}",
        scope_id=wave_id,
        expected_eu=expected_eu,
        pessimistic_eu=expected_eu * 1.5,
        expected_minutes=expected_minutes,
        pessimistic_minutes=expected_minutes * 1.5,
        display=f"{expected_eu} EU",
        reference_class="test",
        confidence=Confidence.MEDIUM,
        current_store_record_id=f"REC-{wave_id}",
        updated_at=_T0,
    )


# ---- Typed-spec projection (P27-I03-W14) -----------------------------------


def test_build_subagent_spec_returns_typed_spec() -> None:
    """``build_subagent_spec`` projects state into a typed ``SubagentSpec``."""
    state = _empty_state()
    _seed_chain(state)
    spec = build_subagent_spec(state, "P01-I01-W02")
    assert isinstance(spec, SubagentSpec)
    assert spec.wave_id == "P01-I01-W02"
    assert spec.iter_id == "P01-I01"
    assert spec.title == "Second wave"
    assert spec.scope_id == "QR"
    assert spec.estimate.effort_bucket == "M"
    # The single closed/pending dep is projected as a typed row.
    assert len(spec.dependencies) == 1
    assert spec.dependencies[0].wave_id == "P01-I01-W01"
    assert spec.dependencies[0].status == "pending"


def test_build_subagent_spec_renders_identically_to_render_wave_prompt() -> None:
    """The spec's ``render`` output equals the public ``render_wave_prompt``."""
    state = _empty_state()
    _seed_chain(state)
    spec = build_subagent_spec(state, "P01-I01-W01")
    assert spec.render() == render_wave_prompt(state, "P01-I01-W01")


def test_build_subagent_spec_unknown_wave_raises_key_error() -> None:
    """A missing wave id surfaces as ``KeyError`` from the builder."""
    state = _empty_state()
    _seed_chain(state)
    with pytest.raises(KeyError, match="unknown wave"):
        build_subagent_spec(state, "P01-I01-W99")


def test_build_subagent_spec_projects_wave_description() -> None:
    """``build_subagent_spec`` copies ``Wave.description`` onto the spec."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].description = "Long-form purpose of the first wave."
    spec = build_subagent_spec(state, "P01-I01-W01")
    assert spec.description == "Long-form purpose of the first wave."


def test_build_subagent_spec_projects_estimate_hints() -> None:
    """``build_subagent_spec`` copies estimate state and active siblings."""
    state = _empty_state()
    _seed_chain(state)
    state.estimates = {
        "P01-I01-W01": _estimate(
            wave_id="P01-I01-W01",
            expected_eu=2.5,
            expected_minutes=75.0,
        )
    }
    state.waves["P01-I01-W01"].token_budget = 4096
    state.current.active_wave_ids = ["P01-I01-W01", "P01-I01-W02"]

    spec = build_subagent_spec(state, "P01-I01-W01")

    assert spec.estimate.effort_bucket == "M"
    assert spec.estimate.expected_eu == 2.5
    assert spec.estimate.expected_minutes == 75.0
    assert spec.estimate.token_budget == 4096
    assert spec.estimate.parallel_siblings == ["P01-I01-W02"]


def test_render_wave_prompt_surfaces_description_section() -> None:
    """A wave with a description renders a ``## Description`` section."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].description = "Why this wave exists in detail."
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "## Description" in out
    assert "Why this wave exists in detail." in out


def test_render_wave_prompt_omits_description_section_when_absent() -> None:
    """A wave without a description omits the ``## Description`` section."""
    state = _empty_state()
    _seed_chain(state)
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "## Description" not in out


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
        effort_bucket="M",
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
    assert "## Estimate" in out
    # No-dep / no-evidence sentinels.
    assert "None." in out
    # Commit-prefix uses the wave's phase + wave segments (Pxx-Wzz).
    assert "[P01-W01]" in out


def test_render_wave_prompt_estimate_after_out_of_scope() -> None:
    """The estimate section follows the out-of-scope section."""
    state = _empty_state()
    _seed_chain(state)
    out = render_wave_prompt(state, "P01-I01-W01")
    assert out.index("## Out of scope") < out.index("## Estimate")


def test_render_wave_prompt_estimate_values() -> None:
    """Estimate fields render from state as stable Markdown bullets."""
    state = _empty_state()
    _seed_chain(state)
    state.estimates = {
        "P01-I01-W01": _estimate(
            wave_id="P01-I01-W01",
            expected_eu=3.0,
            expected_minutes=90.0,
        )
    }
    state.waves["P01-I01-W01"].token_budget = 8192
    state.current.active_wave_ids = ["P01-I01-W02", "P01-I01-W01"]

    out = render_wave_prompt(state, "P01-I01-W01")

    block = out.split("## Estimate", 1)[1]
    assert "- bucket: M" in block
    assert "- expected_eu: 3.0" in block
    assert "- expected_minutes: 90.0" in block
    assert "- token_budget: 8192" in block
    assert "- parallel_siblings: P01-I01-W02" in block


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
            title="Cherry-pick worktrees, never merge",
            rationale="Merges break the [P-W] / [P-CORE] history audit trail.",
            alternatives=["squash"],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D02": Decision(
            id="D02",
            scope_id="QR",
            title="State CLI is the only writer",
            rationale="Direct edits bypass the audit-side event.jsonl.",
            alternatives=[],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D99": Decision(
            id="D99",
            scope_id="OTHER",
            title="Out-of-scope",
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
            title="Workflow rendering is idempotent",
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
            title="Closed deps unblock children",
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
    state.waves["P01-I01-W01"].outcome = "all green"
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "# Wave P01-I01-W01: First wave" in out


# ---- Spike-brief surfacing (P20-W14) ---------------------------------------


def _seed_spike_brief(repo_root: Path, *, name: str, body: str = "stub") -> Path:
    """Write a spike-brief markdown file under ``.ea/local/research/``.

    Returns the absolute path so the test can assert on the rendered
    repo-relative form.
    """
    research_dir = repo_root / ".ea" / "local" / "research"
    research_dir.mkdir(parents=True, exist_ok=True)
    target = research_dir / name
    target.write_text(body, encoding="utf-8")
    return target


def test_render_no_repo_root_omits_references_section(tmp_path: Path) -> None:
    """When ``repo_root`` is ``None`` the spike-brief scan is skipped."""
    state = _empty_state()
    _seed_chain(state)
    # Even with a brief on disk, repo_root=None must skip the scan.
    _seed_spike_brief(tmp_path, name="2026-05-15-P01-spike-foo.md")
    out = render_wave_prompt(state, "P01-I01-W01")
    assert "## References" not in out


def test_render_with_repo_root_but_no_briefs_omits_references_section(
    tmp_path: Path,
) -> None:
    """An empty ``.ea/local/`` produces no ``## References`` section."""
    state = _empty_state()
    _seed_chain(state)
    # No briefs on disk — the scan walks an empty directory.
    (tmp_path / ".ea" / "local").mkdir(parents=True, exist_ok=True)
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" not in out


def test_render_with_repo_root_missing_local_dir_omits_references_section(
    tmp_path: Path,
) -> None:
    """A repo without ``.ea/local/`` at all is treated as "no briefs"."""
    state = _empty_state()
    _seed_chain(state)
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" not in out


def test_render_surfaces_spike_brief_matching_phase_id(tmp_path: Path) -> None:
    """A brief whose filename contains the phase id appears under References."""
    state = _empty_state()
    _seed_chain(state)
    _seed_spike_brief(tmp_path, name="2026-05-15-p01-spike-naming.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" in out
    assert ".ea/local/research/2026-05-15-p01-spike-naming.md" in out
    assert "Spike briefs whose filename references this wave / iter / phase." in out


def test_render_surfaces_spike_brief_matching_wave_id(tmp_path: Path) -> None:
    """A brief whose filename contains the wave id is also surfaced."""
    state = _empty_state()
    _seed_chain(state)
    _seed_spike_brief(tmp_path, name="2026-05-15-P01-I01-W01-prep.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" in out
    assert ".ea/local/research/2026-05-15-P01-I01-W01-prep.md" in out


def test_render_surfaces_spike_brief_directly_under_local_dir(tmp_path: Path) -> None:
    """Briefs at ``.ea/local/*.md`` (not the research subdir) also match."""
    state = _empty_state()
    _seed_chain(state)
    local_dir = tmp_path / ".ea" / "local"
    local_dir.mkdir(parents=True, exist_ok=True)
    (local_dir / "2026-05-15-p01-quickspike.md").write_text("stub", encoding="utf-8")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" in out
    assert ".ea/local/2026-05-15-p01-quickspike.md" in out


def test_render_skips_unrelated_briefs(tmp_path: Path) -> None:
    """Briefs whose filename does NOT mention the wave/iter/phase are dropped."""
    state = _empty_state()
    _seed_chain(state)
    _seed_spike_brief(tmp_path, name="2026-05-15-p99-unrelated.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    # No match ⇒ no section emitted.
    assert "## References" not in out
    assert "p99-unrelated" not in out


def test_render_lists_multiple_spike_briefs_sorted(tmp_path: Path) -> None:
    """Multiple matching briefs sort lexicographically in the rendered list."""
    state = _empty_state()
    _seed_chain(state)
    _seed_spike_brief(tmp_path, name="2026-05-15-p01-b-second.md")
    _seed_spike_brief(tmp_path, name="2026-05-15-p01-a-first.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    block = out.split("## References", 1)[1].split("## ", 1)[0]
    assert "p01-a-first" in block
    assert "p01-b-second" in block
    assert block.index("p01-a-first") < block.index("p01-b-second")


def test_render_spike_brief_match_is_case_insensitive(tmp_path: Path) -> None:
    """Filename casing does not affect the substring match against ids."""
    state = _empty_state()
    _seed_chain(state)
    # Uppercase phase ref in filename; wave.id segment is "P01" (already upper).
    _seed_spike_brief(tmp_path, name="2026-05-15-P01-MixedCase-spike.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    assert "## References" in out
    assert "2026-05-15-P01-MixedCase-spike.md" in out


def test_render_spike_briefs_section_lands_before_working_tree(tmp_path: Path) -> None:
    """``## References`` is placed between ``## Recent audits`` and ``## Working tree``."""
    state = _empty_state()
    _seed_chain(state)
    _seed_spike_brief(tmp_path, name="2026-05-15-p01-spike-order.md")
    out = render_wave_prompt(state, "P01-I01-W01", repo_root=tmp_path)
    audits_idx = out.index("## Recent audits")
    refs_idx = out.index("## References")
    working_idx = out.index("## Working tree")
    assert audits_idx < refs_idx < working_idx
