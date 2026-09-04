"""Unit tests for the subagent prompt renderer (B025).

Exercises :func:`eawf.workflow.dispatch.renderer.render_wave_prompt` against
hand-built :class:`State` instances. The renderer is pure — no I/O —
so each test composes a state in memory and inspects the returned
string.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    AgentSessionRole,
    AgentSessionStatus,
    AuditKind,
    AuditStatus,
    AuditVerdict,
    Confidence,
    DecisionStatus,
    EffortBucket,
    HypothesisStatus,
    HypothesisVerdict,
    ProjectStatus,
    ScopeKind,
    WaveStatus,
    WorktreeStatus,
)
from eawf.kernel.state.models import (
    AgentSession,
    Audit,
    CurrentPointers,
    Decision,
    EstimateSummary,
    Hypothesis,
    Project,
    State,
    WorktreeRecord,
)
from eawf.surfaces.cli.app import app as cli_app
from eawf.workflow.agents.specs.models import SubagentSpec
from eawf.workflow.dispatch import (
    build_subagent_spec,
    render_dispatch_envelope,
    render_wave_prompt,
)
from eawf.workflow.lifecycle.transitions import (
    open_iter,
    open_phase,
    plan_wave,
)
from tests._criteria_helpers import legacy_criteria
from tests.conftest import make_floor_waiver, make_intent

_T0 = datetime(2026, 1, 1, tzinfo=UTC)
# Seam-wave golden: the dispatch prompt an intent-bearing wave renders once
# the decision store is filtered down to the wave's own rows.
_SEAM_WAVE_ID = "P09-I01-W07"
_SEAM_GOLDEN = Path(__file__).resolve().parents[1] / "golden" / "dispatch" / "cc_seam_wave.txt"
_PROMPT_BUDGET_BYTES = 20 * 1024

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
        intent=make_intent(),
    )
    plan_wave(
        state,
        wave_id="P01-I01-W02",
        iter_id="P01-I01",
        title="Second wave",
        file_scopes=["src/bar/"],
        deps=["P01-I01-W01"],
        effort_bucket="M",
        intent=make_intent(),
    )


def _decision(
    *,
    decision_id: str,
    scope_id: str,
    title: str,
    rationale: str = "Recorded so the dispatch renderer has a row to project.",
    status: DecisionStatus = DecisionStatus.ACTIVE,
) -> Decision:
    """Return one decision row for the dispatch scoping tests."""
    return Decision(
        id=decision_id,
        scope_id=scope_id,
        title=title,
        rationale=rationale,
        alternatives=[],
        status=status,
        created_at=_T0,
        superseded_by=None,
    )


def _seam_wave_state() -> State:
    """Return a seam-wave state: in-scope decisions plus a project-scope flood.

    Models the shape the filter exists for — a handful of rows the wave
    actually needs (phase-scoped, iter-scoped, and one project-scoped row
    the wave cites by id) drowning in thirty project-scoped rows it does
    not, whose rationales alone outweigh the whole prompt budget.
    """
    state = _empty_state()
    open_phase(state, phase_id="P09", title="Seam repairs")
    open_iter(state, iter_id="P09-I01", phase_id="P09", title="Wire the dispatch seams")
    plan_wave(
        state,
        wave_id=_SEAM_WAVE_ID,
        iter_id="P09-I01",
        title="Render the wave intent block in dispatch",
        description=(
            "The seam waves carry an intent block in state but the dispatch renderer "
            "never emits it, and the Decisions section dumps every row in the store. "
            "Render the intent and scope the decisions, keeping D-CITED-01 intact."
        ),
        file_scopes=["src/eawf/workflow/dispatch/renderer.py", "tests/golden/dispatch"],
        success_criteria=legacy_criteria(
            "the dispatch prompt carries the wave intent",
            "the dispatch prompt carries only the wave's decisions",
        ),
        criteria_floor_waiver=make_floor_waiver(),
        agent_role=AgentSessionRole.EXECUTOR,
        effort_bucket=EffortBucket.XS,
        intent=IntentBrief(
            problem="executors never see the intent block the planner attached",
            desired_outcome="each dispatch prompt carries the intent and the wave's decisions",
            priority_rationale="thirteen seam waves are dispatchable and each would run blind",
            planned_steps=[
                "render the intent as its own section",
                "filter the decisions to the wave's phase and its citations",
            ],
            risks=["existing dispatch goldens move when the section order changes"],
            evidence_refs=["docs/rules/artifact-chassis.md"],
        ),
    )
    decisions = {
        "D-SEAM-01": _decision(
            decision_id="D-SEAM-01",
            scope_id="P09",
            title="Render the intent block in dispatch",
            rationale="The planner's intent is the wave's why; dispatch must carry it.",
        ),
        "D-SEAM-02": _decision(
            decision_id="D-SEAM-02",
            scope_id="P09-I01",
            title="Keep the memory section as it is",
            rationale="The prompt-size drop must come from the decision filter alone.",
        ),
        "D-CITED-01": _decision(
            decision_id="D-CITED-01",
            scope_id="QR",
            title="Scope the decisions to the dispatched wave",
            rationale="A project-scoped row the wave names by id stays in the prompt.",
        ),
    }
    noise = "Project-scoped rationale with nothing to say about the seam wave. " * 12
    for index in range(30):
        decision_id = f"D-NOISE-{index:02d}"
        decisions[decision_id] = _decision(
            decision_id=decision_id,
            scope_id="QR",
            title=f"Unrelated project decision {index:02d}",
            rationale=noise,
        )
    state.decisions = decisions
    return state


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


# ---- Typed-spec projection -----------------------------------


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
    """The spec render is the prompt minus the state-spliced intent section."""
    state = _empty_state()
    _seed_chain(state)
    spec = build_subagent_spec(state, "P01-I01-W01")
    prompt = render_wave_prompt(state, "P01-I01-W01")
    # The intent lives on ``Wave``, not on the spec, so the renderer splices
    # it between the wave tags and the scope; every other byte is the spec's.
    start = prompt.index("\n\n## Intent")
    end = prompt.index("\n\n## Scope")
    assert "## Intent" not in spec.render()
    assert prompt[:start] + prompt[end:] == spec.render()


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
        intent=make_intent(),
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


def test_render_wave_prompt_includes_ceremony_recommendation() -> None:
    """Operator-confirmed history renders the read-only ceremony section."""
    state = _empty_state()
    _seed_chain(state)
    state.agent_sessions["SES-operator"] = AgentSession(
        id="SES-operator",
        role=AgentSessionRole.OPERATOR,
        runtime="claude",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=_T0,
    )
    state.waves["P01-I01-W01"].status = WaveStatus.CLOSED
    state.waves["P01-I01-W01"].closed_at = _T0
    state.waves["P01-I01-W01"].claim_session_id = "SES-operator"

    out = render_wave_prompt(state, "P01-I01-W02")

    block = out.split("## Ceremony", 1)[1].split("## ", 1)[0]
    assert "- recommendation: mode B" in block
    assert "- operator_confirmed_counter: 1" in block
    assert "- latest_operator_confirmed_wave: P01-I01-W01" in block


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
    """Decisions scoped to the wave's phase appear with their rationale."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {
        "D01": Decision(
            id="D01",
            scope_id="P01",
            title="Cherry-pick worktrees, never merge",
            rationale="Merges break the [P-W] / [P-CORE] history audit trail.",
            alternatives=["squash"],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D02": Decision(
            id="D02",
            scope_id="P01-I01",
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


def test_render_filters_obsolete_and_superseded_decisions() -> None:
    """Dispatch prompts exclude stale decisions from the in-scope list."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {
        "D01": Decision(
            id="D01",
            scope_id="P01",
            title="Active decision",
            rationale="Should appear.",
            alternatives=[],
            status=DecisionStatus.ACTIVE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
        "D02": Decision(
            id="D02",
            scope_id="P01",
            title="Superseded decision",
            rationale="Should not appear.",
            alternatives=[],
            status=DecisionStatus.SUPERSEDED,
            created_at=datetime.now(UTC),
            superseded_by="D01",
        ),
        "D03": Decision(
            id="D03",
            scope_id="P01",
            title="Obsolete decision",
            rationale="Should not appear.",
            alternatives=[],
            status=DecisionStatus.OBSOLETE,
            created_at=datetime.now(UTC),
            superseded_by=None,
        ),
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "### D01: Active decision" in out
    assert "D02" not in out
    assert "D03" not in out


def test_render_decisions_drops_project_scoped_rows_the_wave_never_cites() -> None:
    """A project-scoped decision no longer rides along on every wave prompt."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {
        "D01": _decision(decision_id="D01", scope_id="P01", title="Phase decision"),
        "D02": _decision(decision_id="D02", scope_id="P01-I01", title="Iter decision"),
        "D03": _decision(decision_id="D03", scope_id="P01-I01-W01", title="Wave decision"),
        "D04": _decision(decision_id="D04", scope_id="QR", title="Project decision"),
        "D05": _decision(decision_id="D05", scope_id="P02", title="Other-phase decision"),
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "### D01: Phase decision" in out
    assert "### D02: Iter decision" in out
    assert "### D03: Wave decision" in out
    assert "D04" not in out
    assert "D05" not in out


def test_render_decisions_keep_ids_cited_by_the_wave_description() -> None:
    """An out-of-phase decision the description names by id still renders."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].description = "Implements the D04 contract end to end."
    state.decisions = {
        "D04": _decision(decision_id="D04", scope_id="QR", title="Cited project decision"),
        "D05": _decision(decision_id="D05", scope_id="QR", title="Uncited project decision"),
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "### D04: Cited project decision" in out
    assert "D05" not in out


def test_render_decisions_keep_ids_cited_by_the_wave_intent() -> None:
    """Every intent field is scanned for citations, evidence refs included."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].intent = IntentBrief(
        problem="the D06 seam is unwired",
        desired_outcome="the seam is wired",
        priority_rationale="D07 ranked it first",
        planned_steps=["wire the D08 adapter"],
        risks=["D09 may need a follow-up"],
        evidence_refs=["docs/decisions/D10.md"],
    )
    state.decisions = {
        f"D{index:02d}": _decision(
            decision_id=f"D{index:02d}", scope_id="QR", title=f"Decision {index:02d}"
        )
        for index in range(6, 12)
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    for cited in ("D06", "D07", "D08", "D09", "D10"):
        assert f"### {cited}: " in out, f"{cited} cited by the intent but dropped"
    assert "D11" not in out


def test_render_decisions_citation_match_is_boundary_anchored() -> None:
    """A citation of ``D01`` must not drag ``D012`` into the prompt."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].description = "Follows D01 exactly."
    state.decisions = {
        "D01": _decision(decision_id="D01", scope_id="QR", title="Cited short id"),
        "D012": _decision(decision_id="D012", scope_id="QR", title="Longer id prefix match"),
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "### D01: Cited short id" in out
    assert "D012" not in out


def test_render_decisions_hide_superseded_rows_even_when_cited() -> None:
    """The status filter outranks a citation — a stale decision stays hidden."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].description = "Supersedes D04 and D05 alike."
    state.decisions = {
        "D04": _decision(
            decision_id="D04",
            scope_id="QR",
            title="Superseded but cited",
            status=DecisionStatus.SUPERSEDED,
        ),
        "D05": _decision(
            decision_id="D05",
            scope_id="P01",
            title="Obsolete and in phase",
            status=DecisionStatus.OBSOLETE,
        ),
    }

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "## Decisions\n\nNone." in out
    assert "### D04" not in out
    assert "### D05" not in out


def test_render_decisions_empty_store_renders_the_none_sentinel() -> None:
    """An empty decision store keeps the section with its ``None.`` body."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {}

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "## Decisions\n\nNone." in out


def test_render_decisions_unknown_wave_raises_key_error() -> None:
    """The decision filter never runs for a wave id absent from state."""
    state = _empty_state()
    _seed_chain(state)
    state.decisions = {
        "D01": _decision(decision_id="D01", scope_id="P01", title="Phase decision"),
    }

    with pytest.raises(KeyError, match="unknown wave"):
        render_wave_prompt(state, "P01-I01-W99")


def test_seam_wave_prompt_decisions_golden_stays_under_20kb() -> None:
    """A seam wave renders its own decisions only and fits the 20 KB budget.

    Regenerate the fixture with ``EAWF_REFRESH_GOLDEN=1 uv run pytest
    tests/integration/test_dispatch_renderer.py -k decisions``.
    """
    state = _seam_wave_state()
    rendered = render_wave_prompt(state, _SEAM_WAVE_ID)

    if os.environ.get("EAWF_REFRESH_GOLDEN") == "1":
        _SEAM_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        _SEAM_GOLDEN.write_text(rendered, encoding="utf-8")

    expected = _SEAM_GOLDEN.read_text(encoding="utf-8")
    assert rendered == expected, (
        f"seam dispatch golden {_SEAM_GOLDEN.name!r} drifted. If intentional, "
        "regenerate with EAWF_REFRESH_GOLDEN=1 and commit the new bytes."
    )
    # The unfiltered store alone busts the budget, so passing the cap is
    # the decision filter's doing rather than a small fixture's.
    noise = sum(len(d.rationale.encode("utf-8")) for d in (state.decisions or {}).values())
    assert noise > _PROMPT_BUDGET_BYTES
    assert len(rendered.encode("utf-8")) < _PROMPT_BUDGET_BYTES
    assert "### D-SEAM-01: Render the intent block in dispatch" in rendered
    assert "### D-CITED-01: Scope the decisions to the dispatched wave" in rendered
    assert "D-NOISE-" not in rendered


# ---- Intent section ---------------------------------------------------------


def test_render_intent_section_emits_every_planner_field() -> None:
    """The intent renders problem, outcome, steps, risks and evidence refs."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].intent = IntentBrief(
        problem="dispatch prompts hide the planner's intent",
        desired_outcome="every dispatched agent reads the intent it implements",
        priority_rationale="thirteen seam waves are dispatchable now",
        planned_steps=["render the intent section", "scope the decisions"],
        risks=["golden fixtures move"],
        evidence_refs=["docs/rules/artifact-chassis.md", "urn:eawf:v1:decision:DX"],
    )

    out = render_wave_prompt(state, "P01-I01-W01")
    block = out.split("## Intent", 1)[1].split("\n## ", 1)[0]

    assert "- problem: dispatch prompts hide the planner's intent" in block
    assert "- desired_outcome: every dispatched agent reads the intent it implements" in block
    assert "- planned_steps:\n  - render the intent section\n  - scope the decisions" in block
    assert "- risks:\n  - golden fixtures move" in block
    assert "- evidence_refs:\n  - docs/rules/artifact-chassis.md" in block
    assert "  - urn:eawf:v1:decision:DX" in block


def test_render_intent_section_renders_none_for_empty_list_fields() -> None:
    """Empty planner lists render as ``none`` rather than vanishing."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].intent = IntentBrief(
        problem="the wave has no planned steps yet",
        desired_outcome="the empty lists still render",
    )

    out = render_wave_prompt(state, "P01-I01-W01")
    block = out.split("## Intent", 1)[1].split("\n## ", 1)[0]

    assert "- planned_steps: none" in block
    assert "- risks: none" in block
    assert "- evidence_refs: none" in block


def test_render_omits_intent_section_when_the_wave_carries_no_intent() -> None:
    """A wave without an intent renders no ``## Intent`` heading at all."""
    state = _empty_state()
    _seed_chain(state)
    state.waves["P01-I01-W01"].intent = None

    out = render_wave_prompt(state, "P01-I01-W01")

    assert "## Intent" not in out


def test_render_intent_section_lands_between_wave_tags_and_scope() -> None:
    """The intent reads before the scope so the why precedes the where."""
    state = _empty_state()
    _seed_chain(state)

    out = render_wave_prompt(state, "P01-I01-W01")

    assert out.index("## Wave tags") < out.index("## Intent") < out.index("## Scope")


def test_dispatch_envelope_prompt_carries_the_intent_section() -> None:
    """The typed dispatch envelope ships the same intent block as the prompt."""
    state = _empty_state()
    _seed_chain(state)

    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")

    assert "## Intent" in envelope.prompt
    assert "- problem: test wave lacks a typed intent" in envelope.prompt


def test_render_intent_section_unknown_wave_raises_key_error() -> None:
    """A wave id absent from state raises before any intent is rendered."""
    state = _empty_state()
    _seed_chain(state)

    with pytest.raises(KeyError, match="unknown wave"):
        render_wave_prompt(state, "P01-I01-W99")


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


# ---- Spike-brief surfacing ---------------------------------------


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


# ---- CLI call-site plumbing ----------------------------------
#
# The renderer surfaces spike briefs only when a caller threads ``repo_root``
# through. These tests drive the four interactive-prompt CLI verbs end-to-end
# so the ``## References`` section is proof that ``repo_root`` reaches each
# call site (unlike the pure-renderer tests above, they touch disk + state).

_CLI_RUNNER = CliRunner()

_SPIKE_BRIEF_NAME = "2026-06-15-P21-spike-plumb.md"
_SPIKE_BRIEF_REL = f".ea/local/research/{_SPIKE_BRIEF_NAME}"


def _cli_workspace_with_wave(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Init a CLI workspace at *tmp_path* holding a PLANNED P21-I01-W01 wave.

    Points ``EA_STATE`` inside *tmp_path* and marks it a repo root (an empty
    ``.git`` directory is enough for ``Path.exists``) so
    ``_resolve_repo_root_for_drift`` resolves the workspace itself -- the
    anchor the dispatch renderer scans for spike briefs. Returns the root.
    """
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    (tmp_path / ".git").mkdir(parents=True, exist_ok=True)
    init_res = _CLI_RUNNER.invoke(
        cli_app,
        ["project", "init", "QR", "--title", "Quant Research", "--domains", "quant"],
    )
    assert init_res.exit_code == 0, init_res.output
    propose_res = _CLI_RUNNER.invoke(
        cli_app, ["roadmap", "propose", "--phase", "P21", "--title", "X"]
    )
    assert propose_res.exit_code == 0, propose_res.output
    revise_res = _CLI_RUNNER.invoke(
        cli_app,
        [
            "roadmap",
            "revise",
            "P21",
            "--add-wave",
            "W01",
            "--title",
            "Foo handling",
            "--files",
            "src/",
            "--effort-bucket",
            "M",
            "--intent-problem",
            "auto intent problem",
            "--intent-desired-outcome",
            "auto intent outcome",
            "--intent-priority-rationale",
            "auto intent rationale",
        ],
    )
    assert revise_res.exit_code == 0, revise_res.output
    return tmp_path


def test_wave_show_dispatch_prompt_cli_surfaces_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave show --dispatch-prompt`` threads repo_root -> References renders."""
    workspace = _cli_workspace_with_wave(tmp_path, monkeypatch)
    _seed_spike_brief(workspace, name=_SPIKE_BRIEF_NAME)
    res = _CLI_RUNNER.invoke(cli_app, ["wave", "show", "P21-I01-W01", "--dispatch-prompt"])
    assert res.exit_code == 0, res.output
    assert "## References" in res.stdout
    assert _SPIKE_BRIEF_REL in res.stdout


def test_wave_show_dispatch_prompt_cli_omits_references_without_brief(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No matching brief -> the plumbed repo_root yields no References section."""
    _cli_workspace_with_wave(tmp_path, monkeypatch)
    res = _CLI_RUNNER.invoke(cli_app, ["wave", "show", "P21-I01-W01", "--dispatch-prompt"])
    assert res.exit_code == 0, res.output
    assert "## References" not in res.stdout


def test_wave_dispatch_cli_surfaces_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave dispatch`` threads repo_root through render_dispatch_envelope."""
    workspace = _cli_workspace_with_wave(tmp_path, monkeypatch)
    _seed_spike_brief(workspace, name=_SPIKE_BRIEF_NAME)
    res = _CLI_RUNNER.invoke(cli_app, ["wave", "dispatch", "P21-I01-W01"])
    assert res.exit_code == 0, res.output
    assert "## References" in res.stdout
    assert _SPIKE_BRIEF_REL in res.stdout


def test_wave_dispatch_batch_cli_surfaces_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave dispatch-batch`` threads repo_root through every rendered prompt."""
    workspace = _cli_workspace_with_wave(tmp_path, monkeypatch)
    _seed_spike_brief(workspace, name=_SPIKE_BRIEF_NAME)
    res = _CLI_RUNNER.invoke(cli_app, ["wave", "dispatch-batch", "--iter", "P21-I01"])
    assert res.exit_code == 0, res.output
    assert "## References" in res.stdout
    assert _SPIKE_BRIEF_REL in res.stdout


def test_wave_review_diff_cli_surfaces_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave review --diff`` threads repo_root into the reviewer base prompt."""
    workspace = _cli_workspace_with_wave(tmp_path, monkeypatch)
    _seed_spike_brief(workspace, name=_SPIKE_BRIEF_NAME)
    diff_path = workspace / "wave.diff"
    res = _CLI_RUNNER.invoke(cli_app, ["wave", "review", "P21-I01-W01", "--diff", str(diff_path)])
    assert res.exit_code == 0, res.output
    assert "## References" in res.stdout
    assert _SPIKE_BRIEF_REL in res.stdout
