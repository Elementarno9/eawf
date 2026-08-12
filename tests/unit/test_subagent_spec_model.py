"""Unit tests for the typed subagent-spec model.

Exercises :class:`eawf.workflow.agents.specs.models.SubagentSpec` and its nested
section models directly — the model layer is pure data + formatting, so
each test builds a spec in memory and inspects the rendered Markdown.
The :func:`eawf.workflow.dispatch.renderer.build_subagent_spec` projection from
``State`` is covered by ``test_dispatch_renderer.py`` (byte-identity
against the legacy output).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.workflow.agents.specs.models import (
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecEstimate,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)


def _minimal_spec(**overrides: object) -> SubagentSpec:
    """Return a minimal :class:`SubagentSpec` with optional field overrides."""
    base: dict[str, object] = {
        "wave_id": "P01-I01-W01",
        "iter_id": "P01-I01",
        "title": "Solo wave",
        "scope_id": "QR",
    }
    base.update(overrides)
    return SubagentSpec.model_validate(base)


# ---- SubagentSpec section coverage -----------------------------------------


def test_render_minimal_spec_emits_all_headers() -> None:
    """A spec with no deps/decisions/etc. still emits every required header."""
    out = _minimal_spec(file_scopes=["src/"]).render()
    assert "# Wave P01-I01-W01: Solo wave" in out
    assert "## Wave tags" in out
    assert "## Scope" in out
    assert "## Dependencies" in out
    assert "## Decisions" in out
    assert "## Hypotheses" in out
    assert "## Recent audits" in out
    assert "## Working tree" in out
    assert "## Workflow" in out
    assert "## Out of scope" in out
    assert "## Estimate" in out
    # Empty-section sentinels + inline worktree fallback.
    assert "None." in out
    assert "Worktree path: inline" in out
    # Commit prefix uses phase + wave segments.
    assert "[P01-W01]" in out


def test_render_ends_with_single_trailing_newline() -> None:
    """The rendered prompt ends in exactly one newline (verbatim emit)."""
    out = _minimal_spec().render()
    assert out.endswith("\n")
    assert not out.endswith("\n\n")


def test_render_unspecified_role_and_bucket() -> None:
    """A spec without role/bucket renders the ``unspecified`` sentinels."""
    out = _minimal_spec().render()
    assert "- agent_role: unspecified" in out
    assert "- effort_bucket: unspecified" in out
    assert "- success_criteria: none" in out


def test_render_role_bucket_and_criteria_present() -> None:
    """Role, bucket, and success criteria render when supplied."""
    out = _minimal_spec(
        agent_role="executor",
        effort_bucket="XL",
        success_criteria=["criterion one", "criterion two"],
    ).render()
    assert "- agent_role: executor" in out
    assert "- effort_bucket: XL" in out
    assert "- success_criteria:" in out
    assert "  - criterion one" in out
    assert "  - criterion two" in out


def test_render_estimate_section_defaults_to_unknowns() -> None:
    """A spec without estimate inputs still renders the required section."""
    out = _minimal_spec().render()
    block = out.split("## Estimate", 1)[1]
    assert "- bucket: unknown" in block
    assert "- expected_eu: unknown" in block
    assert "- expected_minutes: unknown" in block
    assert "- token_budget: unknown" in block
    assert "- parallel_siblings: none" in block


def test_render_estimate_section_with_values() -> None:
    """A populated estimate renders bucket, effort, budget, and siblings."""
    out = _minimal_spec(
        estimate=SpecEstimate(
            effort_bucket="M",
            expected_eu=2.5,
            expected_minutes=75.0,
            token_budget=4096,
            parallel_siblings=["P01-I01-W02", "P01-I01-W03"],
        )
    ).render()
    block = out.split("## Estimate", 1)[1]
    assert "- bucket: M" in block
    assert "- expected_eu: 2.5" in block
    assert "- expected_minutes: 75.0" in block
    assert "- token_budget: 4096" in block
    assert "- parallel_siblings: P01-I01-W02, P01-I01-W03" in block


def test_render_estimate_lands_after_out_of_scope() -> None:
    """``## Estimate`` sits immediately after ``## Out of scope``."""
    out = _minimal_spec().render()
    assert out.index("## Out of scope") < out.index("## Estimate")


def test_render_workflow_report_then_stop_on_both_shapes() -> None:
    """CR-01: step 5 reports and step 6 stops on BOTH shapes; no self-close.

    The interactive self-close branch is removed: no `eawf wave close`
    instruction survives in the section on either render shape, and the
    never-narrow schema-wave clause rides step 3.
    """
    for headless in (False, True):
        out = _minimal_spec().render(headless=headless)
        workflow = out.split("## Workflow", 1)[1].split("## Out of scope", 1)[0]
        assert "5. Emit the typed executor report" in workflow
        assert "6. Stop after the report" in workflow
        assert "eawf wave close" not in workflow
        # Never-narrow clause on the step-3 full-tree run.
        assert "never narrows this run" in workflow
        assert "full-tree `tests/` sweep" in workflow


def test_render_workflow_names_token_tally_owner_per_path() -> None:
    """CR-03: step 5 names who owns the token tally on each dispatch path."""
    interactive = _minimal_spec().render(headless=False)
    interactive_workflow = interactive.split("## Workflow", 1)[1].split("## Out of scope", 1)[0]
    assert "--tokens-consumed" in interactive_workflow
    assert "dispatching parent session" in interactive_workflow

    headless = _minimal_spec().render(headless=True)
    headless_workflow = headless.split("## Workflow", 1)[1].split("## Out of scope", 1)[0]
    assert "daemon converts your session's captured runtime delta" in headless_workflow
    assert "closes the wave on your behalf" in headless_workflow


def test_render_out_of_scope_bans_all_eawf_commands() -> None:
    """CR-02: the out-of-scope section bans every eawf command with the
    STOP-and-report escape hatch; the old `uv run eawf state` directive is
    gone."""
    out = _minimal_spec().render()
    section = out.split("## Out of scope", 1)[1].split("## Estimate", 1)[0]
    assert "Run NO `eawf` command inside this worktree" in section
    assert "STOP and name it in your report" in section
    assert "uv run eawf state" not in section
    assert "eawf wave close" not in section


def test_render_empty_file_scopes_shows_none_placeholder() -> None:
    """An empty ``file_scopes`` renders ``(none)`` in the scope section."""
    out = _minimal_spec(file_scopes=[]).render()
    scope_block = out.split("## Scope", 1)[1].split("##", 1)[0]
    assert "(none)" in scope_block


def test_render_scope_uses_iter_id_verbatim() -> None:
    """The scope rationale interpolates ``iter_id`` and ``scope_id`` verbatim."""
    out = _minimal_spec(iter_id="P09-I02", scope_id="ABC").render()
    assert "Scope is anchored on iter P09-I02 under scope ABC." in out


# ---- SpecDependency --------------------------------------------------------


def test_dependency_render_with_title_and_status() -> None:
    """A resolved dependency renders title + status."""
    dep = SpecDependency(wave_id="P01-I01-W01", title="First wave", status="pending")
    assert dep.render() == "- P01-I01-W01: First wave (status=pending)"


def test_dependency_render_missing_surfaces_unknown() -> None:
    """A dangling dependency (no title/status) renders ``status=unknown``."""
    dep = SpecDependency(wave_id="P01-I01-W09")
    assert dep.render() == "- P01-I01-W09: (missing from state) (status=unknown)"


def test_render_dependencies_section_lists_each_row() -> None:
    """Each dependency row appears under ``## Dependencies``."""
    out = _minimal_spec(
        dependencies=[
            SpecDependency(wave_id="P01-I01-W01", title="First", status="closed"),
            SpecDependency(wave_id="P01-I01-W02"),
        ]
    ).render()
    block = out.split("## Dependencies", 1)[1].split("##", 1)[0]
    assert "- P01-I01-W01: First (status=closed)" in block
    assert "- P01-I01-W02: (missing from state) (status=unknown)" in block


# ---- SpecDecision ----------------------------------------------------------


def test_decision_render_strips_trailing_rationale_whitespace() -> None:
    """The decision rationale renders with trailing whitespace stripped."""
    dec = SpecDecision(decision_id="D01", title="Cherry-pick", rationale="Keep history.\n\n")
    assert dec.render() == "### D01: Cherry-pick\n\nKeep history."


def test_render_decisions_sorted_order_preserved() -> None:
    """Decisions render in the order supplied (builder sorts upstream)."""
    out = _minimal_spec(
        decisions=[
            SpecDecision(decision_id="D01", title="One", rationale="r1"),
            SpecDecision(decision_id="D02", title="Two", rationale="r2"),
        ]
    ).render()
    assert "### D01: One" in out
    assert "### D02: Two" in out
    assert out.index("### D01") < out.index("### D02")


# ---- SpecHypothesis --------------------------------------------------------


def test_hypothesis_render_open_verdict_default() -> None:
    """A hypothesis without a verdict renders ``verdict: open``."""
    hyp = SpecHypothesis(
        hypothesis_id="H01-01",
        metric="drift",
        confirm="drift == 0",
        reject="drift > 0",
    )
    block = hyp.render()
    assert "- H01-01: metric='drift'" in block
    assert "    confirm: drift == 0" in block
    assert "    reject:  drift > 0" in block
    assert "    verdict: open" in block


def test_hypothesis_render_explicit_verdict() -> None:
    """A hypothesis with a verdict renders it verbatim."""
    hyp = SpecHypothesis(
        hypothesis_id="H01-02",
        metric="ready",
        confirm="c",
        reject="r",
        verdict="confirmed",
    )
    assert "    verdict: confirmed" in hyp.render()


# ---- SpecAudit -------------------------------------------------------------


def test_audit_render_pending_verdict_default() -> None:
    """An audit without a verdict renders ``verdict=pending``."""
    audit = SpecAudit(audit_id="A01", kind="evaluation")
    assert audit.render() == "- A01: evaluation verdict=pending"


def test_audit_render_explicit_verdict() -> None:
    """An audit with a verdict renders it verbatim."""
    audit = SpecAudit(audit_id="A02", kind="ship-gate", verdict="pass")
    assert audit.render() == "- A02: ship-gate verdict=pass"


# ---- SpecWorktree ----------------------------------------------------------


def test_render_worktree_present_emits_branch_lines() -> None:
    """A worktree renders Branch / Worktree path / Base commit lines."""
    out = _minimal_spec(
        worktree=SpecWorktree(
            branch="feature/eawf-v0.1-p01-w01",
            path=".ea/worktrees/p01-w01",
            base_branch="feature/eawf-v0.1",
        )
    ).render()
    assert "Branch: feature/eawf-v0.1-p01-w01" in out
    assert "Worktree path: .ea/worktrees/p01-w01" in out
    assert "Base commit: feature/eawf-v0.1" in out


def test_render_no_worktree_omits_branch_lines() -> None:
    """Without a worktree, Branch / Base commit lines are absent."""
    out = _minimal_spec().render()
    assert "Branch:" not in out
    assert "Base commit:" not in out
    assert "Worktree path: inline" in out


# ---- References ------------------------------------------------------------


def test_render_no_references_omits_section() -> None:
    """An empty ``references`` list omits the ``## References`` section."""
    out = _minimal_spec(references=[]).render()
    assert "## References" not in out


def test_render_references_section_present_when_populated() -> None:
    """A populated ``references`` list renders the section + each path."""
    out = _minimal_spec(
        references=[".ea/local/research/2026-05-15-p01-a.md", ".ea/local/2026-05-15-p01-b.md"]
    ).render()
    assert "## References" in out
    assert "- .ea/local/research/2026-05-15-p01-a.md" in out
    assert "- .ea/local/2026-05-15-p01-b.md" in out


def test_render_references_section_lands_before_working_tree() -> None:
    """``## References`` is placed between Recent audits and Working tree."""
    out = _minimal_spec(references=[".ea/local/research/2026-05-15-p01-a.md"]).render()
    audits_idx = out.index("## Recent audits")
    refs_idx = out.index("## References")
    working_idx = out.index("## Working tree")
    assert audits_idx < refs_idx < working_idx


# ---- Description -----------------------------------------------------------


def test_render_no_description_omits_section() -> None:
    """A spec without a description omits the ``## Description`` section."""
    out = _minimal_spec().render()
    assert "## Description" not in out


def test_render_description_section_present_when_set() -> None:
    """A populated ``description`` renders the ``## Description`` section."""
    out = _minimal_spec(description="The wave's long-form purpose.").render()
    assert "## Description" in out
    assert "The wave's long-form purpose." in out


def test_render_description_strips_trailing_whitespace() -> None:
    """The description body renders with trailing whitespace stripped."""
    out = _minimal_spec(description="Purpose.\n\n").render()
    assert "## Description\n\nPurpose." in out


def test_render_description_lands_between_header_and_wave_tags() -> None:
    """``## Description`` sits after the header and before ``## Wave tags``."""
    out = _minimal_spec(description="Purpose.").render()
    header_idx = out.index("# Wave P01-I01-W01: Solo wave")
    desc_idx = out.index("## Description")
    tags_idx = out.index("## Wave tags")
    assert header_idx < desc_idx < tags_idx


# ---- Boundary: malformed wave id -------------------------------------------


def test_render_malformed_wave_id_uses_fallback_commit_segments() -> None:
    """A wave id without three segments falls back to ``WXX`` in the prefix."""
    out = _minimal_spec(wave_id="WEIRD").render()
    assert "[WEIRD-WXX]" in out


# ---- Error paths: strict schema --------------------------------------------


def test_spec_rejects_unknown_field() -> None:
    """``extra="forbid"`` rejects an unexpected top-level key."""
    with pytest.raises(ValidationError):
        SubagentSpec.model_validate(
            {
                "wave_id": "P01-I01-W01",
                "iter_id": "P01-I01",
                "title": "x",
                "scope_id": "QR",
                "bogus": True,
            }
        )


def test_spec_rejects_missing_required_field() -> None:
    """A missing required field (``scope_id``) raises ``ValidationError``."""
    with pytest.raises(ValidationError):
        SubagentSpec.model_validate({"wave_id": "P01-I01-W01", "iter_id": "P01-I01", "title": "x"})


def test_dependency_rejects_unknown_field() -> None:
    """Nested section models also forbid extra keys."""
    with pytest.raises(ValidationError):
        SpecDependency.model_validate({"wave_id": "P01-I01-W01", "bogus": 1})


# ---- Headless report-output section ----------------------------------------


def test_render_headless_executor_emits_report_output_schema() -> None:
    """A headless executor render pins the ExecutorReportBody JSON schema."""
    out = _minimal_spec(agent_role="executor", file_scopes=["src/"]).render(headless=True)
    assert "## Report output" in out
    # The strict output-only-JSON instruction is present.
    assert "single JSON object" in out
    assert "no prose, no markdown, no code fences" in out
    # The schema names every required field + the exact enum string values
    # (verbatim from AgentReportVerdict + Confidence).
    assert '"role": "executor"' in out
    assert '"verdict": "pass|pass-with-followups|fail|blocked"' in out
    assert '"confidence": "high|medium|low"' in out
    assert '"outcome": "1-1000 chars"' in out
    assert '"files_changed": []' in out
    assert '"tests_run": []' in out
    assert '"commit_sha": "<7+ char commit SHA>"' in out
    assert '"evidence_refs": [{"kind": "artifact"' in out
    assert '"followups": []' in out
    # The dispatched wave id is injected into the schema, not a placeholder.
    assert '"wave_id": "P01-I01-W01"' in out
    assert "<THE-WAVE-ID>" not in out
    # The nested-shape fields are pinned EMPTY so a model cannot invent a
    # wrong entry shape (the live e2e showed codex populating evidence_refs
    # with {evidence_kind, notes}, which fails AgentReportEvidenceRef and
    # forces the slow re-ask loop). evidence_refs + followups must stay [].
    assert "`evidence_refs` is REQUIRED: exactly one object per success criterion" in out
    assert "`commit_sha` is REQUIRED" in out


def test_render_interactive_executor_omits_report_output_schema() -> None:
    """The interactive (headless=False) render carries no report-output block."""
    out = _minimal_spec(agent_role="executor", file_scopes=["src/"]).render()
    assert "## Report output" not in out
    assert '"role": "executor"' not in out


def test_render_headless_default_matches_interactive_for_executor() -> None:
    """``render()`` default is byte-equal to the interactive render."""
    spec = _minimal_spec(agent_role="executor", file_scopes=["src/"])
    assert spec.render() == spec.render(headless=False)


def test_render_headless_non_executor_emits_its_role_schema() -> None:
    """Headless specialist output is pinned to its own typed body."""
    out = _minimal_spec(agent_role="auditor", file_scopes=["src/"]).render(headless=True)
    assert "## Report output" in out
    assert '"const": "auditor"' in out
    assert '"title": "AuditorReportBody"' in out
    assert '"target_id"' in out
    assert "typed auditor report" in out


def test_render_headless_operator_pins_parent_phase_not_wave_id() -> None:
    """Operator output instructions name the actual parent phase."""
    out = _minimal_spec(
        agent_role="operator",
        phase_id="P01",
        file_scopes=["src/"],
    ).render(headless=True)
    assert '"title": "OperatorReportBody"' in out
    assert "Set `phase_id` to the parent phase `P01` exactly" in out
    assert "phase `P01-I01-W01`" not in out


def test_render_headless_unspecified_role_omits_report_output() -> None:
    """A wave with no agent_role never gets the report-output section."""
    out = _minimal_spec(file_scopes=["src/"]).render(headless=True)
    assert "## Report output" not in out
