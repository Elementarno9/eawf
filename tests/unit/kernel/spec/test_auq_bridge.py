"""Tests for :mod:`eawf.kernel.spec.auq_bridge`.

Pins the cross-runtime AUQ (ask-user-question) bridge + the multi-wave
frontier drain:

1. An :class:`AUQRequest` projects to a claude ``AskUserQuestion`` shape
   (question + header + two-line options) and to the codex / opencode text
   prompts (numbered / bracket-keyed).
2. A ``needs_user`` signal -- a jury aggregate or a rung-3 outcome -- converts
   into an :class:`AUQRequest` at ``URGENT``; a resolved signal is rejected.
3. An operator selection parses to a typed :class:`AUQAnswer`; an unknown key
   is rejected.
4. The frontier drain enumerates ready waves in claim order and yields one
   AUQ per wave needing confirmation (the injected predicate is the only
   policy seam, no subprocess).
5. Boundary cases: a single-option request is rejected (min 2), a five-option
   request is rejected (max 4), duplicate option keys are rejected; an empty
   frontier drains to no steps; an all-deps-open wave is excluded; a
   lower-numbered ready sibling holds a higher one off the frontier; a
   duplicate wave id in the view is rejected.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.auq_bridge import (
    MAX_AUQ_OPTIONS,
    MIN_AUQ_OPTIONS,
    AUQAnswer,
    AUQOption,
    AUQRequest,
    ClaudeAUQProjection,
    CodexAUQProjection,
    DrainableFrontier,
    FrontierDrainStep,
    OpenCodeAUQProjection,
    WaveFrontierItem,
    compute_ready_frontier,
    drain_frontier,
    drain_frontier_from_view,
    needs_user_to_auq,
)
from eawf.kernel.state.enums import AgentReportVerdict, Urgency, WaveStatus
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.workflow.evidence.rung3 import Rung3Outcome
from eawf.workflow.evidence.rung4 import EviBoundVerdict

# ---------------------------------------------------------------------------
# Fixtures / builders
# ---------------------------------------------------------------------------


def _request(
    *options: AUQOption,
    question: str = "Proceed with the wave?",
    header: str | None = None,
    urgency: Urgency = Urgency.NORMAL,
) -> AUQRequest:
    """Build an AUQRequest, defaulting to an accept / reject pair."""
    opts = options or (
        AUQOption(key="approve", label="Approve", description="Claim and dispatch the wave."),
        AUQOption(key="skip", label="Skip", description="Leave the wave pending."),
    )
    return AUQRequest(question=question, options=opts, header=header, urgency=urgency)


def _split_jury() -> JuryAggregate:
    """Return a binary 2-ballot split with no veto -> NEEDS_USER aggregate."""
    ballots = (
        JurorBallot(juror_id="j1", acceptance_style="binary", verdict=AgentReportVerdict.PASS),
        JurorBallot(
            juror_id="j2",
            acceptance_style="binary",
            verdict=AgentReportVerdict.PASS_WITH_FOLLOWUPS,
        ),
    )
    return aggregate_jury(ballots)


def _item(
    wave_id: str,
    status: WaveStatus,
    *deps: str,
    iter_id: str = "P29-I04",
) -> WaveFrontierItem:
    """Build one WaveFrontierItem row."""
    return WaveFrontierItem(wave_id=wave_id, iter_id=iter_id, status=status, deps=tuple(deps))


# ---------------------------------------------------------------------------
# Projection: AUQRequest -> each runtime native confirm shape (criterion a)
# ---------------------------------------------------------------------------


def test_project_for_runtime_claude_renders_askuserquestion_shape() -> None:
    req = _request(header="Wave confirm")
    proj = req.project_for_runtime("claude-code")
    assert isinstance(proj, ClaudeAUQProjection)
    assert proj.question == "Proceed with the wave?"
    assert proj.header == "Wave confirm"
    assert tuple(o.label for o in proj.options) == ("Approve", "Skip")
    # The claude card renders a two-line option: every option has a non-empty
    # description (falls back to the label when the source carried none).
    assert all(o.description for o in proj.options)


def test_project_for_runtime_claude_header_defaults_when_absent() -> None:
    proj = _request().project_for_runtime("claude-code")
    assert isinstance(proj, ClaudeAUQProjection)
    assert proj.header == "Confirm"


def test_project_for_runtime_claude_option_description_falls_back_to_label() -> None:
    req = _request(
        AUQOption(key="a", label="Alpha"),
        AUQOption(key="b", label="Beta"),
    )
    proj = req.project_for_runtime("claude-code")
    assert isinstance(proj, ClaudeAUQProjection)
    # No source description -> the projected secondary line falls back to label.
    assert proj.options[0].description == "Alpha"
    assert proj.options[1].description == "Beta"


def test_project_for_runtime_codex_renders_numbered_prompt() -> None:
    req = _request()
    proj = req.project_for_runtime("codex")
    assert isinstance(proj, CodexAUQProjection)
    assert proj.prompt.startswith("Proceed with the wave?")
    # Numbered lines, one per option, carrying the key.
    assert "1) Approve" in proj.prompt
    assert "2) Skip" in proj.prompt
    assert "[approve]" in proj.prompt
    assert "Reply with the option number or its key." in proj.prompt
    assert tuple(c.number for c in proj.choices) == (1, 2)
    assert tuple(c.key for c in proj.choices) == ("approve", "skip")


def test_project_for_runtime_opencode_renders_bracket_keyed_prompt() -> None:
    req = _request()
    proj = req.project_for_runtime("opencode")
    assert isinstance(proj, OpenCodeAUQProjection)
    assert proj.prompt.startswith("Proceed with the wave?")
    assert "[approve] Approve" in proj.prompt
    assert "[skip] Skip" in proj.prompt
    assert "Reply with the bracket key." in proj.prompt
    assert proj.keys == ("approve", "skip")


def test_project_for_runtime_unknown_runtime_raises() -> None:
    with pytest.raises(ValueError, match="unknown runtime: 'cursor'"):
        _request().project_for_runtime("cursor")


def test_project_for_runtime_all_three_runtimes_distinct_types() -> None:
    req = _request()
    types = {type(req.project_for_runtime(rt)) for rt in ("claude-code", "codex", "opencode")}
    assert types == {ClaudeAUQProjection, CodexAUQProjection, OpenCodeAUQProjection}


# ---------------------------------------------------------------------------
# needs_user -> AUQRequest (criterion b)
# ---------------------------------------------------------------------------


def test_needs_user_to_auq_from_jury_aggregate_builds_urgent_request() -> None:
    agg = _split_jury()
    assert agg.outcome is JuryAggregateOutcome.NEEDS_USER
    req = needs_user_to_auq(agg)
    assert isinstance(req, AUQRequest)
    assert req.urgency is Urgency.URGENT
    assert req.option_keys == ("adjudicate", "override")
    # The split reason flows into the derived question.
    assert "no clean consensus" in req.question


def test_needs_user_to_auq_from_rung3_outcome_builds_urgent_request() -> None:
    outcome = Rung3Outcome(
        convened=True,
        verdict=EviBoundVerdict.UNRESOLVED,
        needs_user=True,
        reasons=("panel split with no veto",),
    )
    req = needs_user_to_auq(outcome)
    assert req.urgency is Urgency.URGENT
    assert "panel split with no veto" in req.question


def test_needs_user_to_auq_honors_question_override() -> None:
    req = needs_user_to_auq(_split_jury(), question="Adjudicate the jury tie?")
    assert req.question == "Adjudicate the jury tie?"


def test_needs_user_to_auq_honors_header() -> None:
    req = needs_user_to_auq(_split_jury(), header="Jury deadlock")
    assert req.header == "Jury deadlock"


def test_needs_user_to_auq_resolved_jury_raises() -> None:
    pass_ballots = (
        JurorBallot(juror_id="j1", acceptance_style="binary", verdict=AgentReportVerdict.PASS),
        JurorBallot(juror_id="j2", acceptance_style="binary", verdict=AgentReportVerdict.PASS),
    )
    resolved = aggregate_jury(pass_ballots)
    assert resolved.outcome is JuryAggregateOutcome.PASS
    with pytest.raises(ValueError, match="jury aggregate is not needs_user"):
        needs_user_to_auq(resolved)


def test_needs_user_to_auq_resolved_rung3_raises() -> None:
    resolved = Rung3Outcome(convened=True, verdict=EviBoundVerdict.SUPPORTED, needs_user=False)
    with pytest.raises(ValueError, match="rung-3 outcome does not need the operator"):
        needs_user_to_auq(resolved)


def test_needs_user_to_auq_empty_reasons_uses_generic_question() -> None:
    # A graded high-variance NEEDS_USER carries a reason; force the empty-reason
    # path through a rung-3 outcome with no reasons set.
    outcome = Rung3Outcome(convened=True, verdict=EviBoundVerdict.UNRESOLVED, needs_user=True)
    req = needs_user_to_auq(outcome)
    assert req.question == (
        "An automated check could not resolve this. How do you want to proceed?"
    )


# ---------------------------------------------------------------------------
# parse_answer -> AUQAnswer (criterion: operator selection parses typed)
# ---------------------------------------------------------------------------


def test_parse_answer_known_key_returns_typed_answer() -> None:
    req = _request()
    answer = req.parse_answer("approve")
    assert isinstance(answer, AUQAnswer)
    assert answer.selected_key == "approve"
    assert answer.selected_label == "Approve"


def test_parse_answer_unknown_key_raises() -> None:
    req = _request()
    with pytest.raises(ValueError, match="unknown option key: 'maybe'"):
        req.parse_answer("maybe")


def test_parse_answer_roundtrips_needs_user_option() -> None:
    req = needs_user_to_auq(_split_jury())
    answer = req.parse_answer("override")
    assert answer.selected_key == "override"
    assert answer.selected_label == "Override and proceed"


# ---------------------------------------------------------------------------
# AUQRequest validation boundaries (criterion: single option rejected -- min 2)
# ---------------------------------------------------------------------------


def test_auqrequest_single_option_rejected() -> None:
    with pytest.raises(ValidationError):
        AUQRequest(question="Yes?", options=(AUQOption(key="y", label="Yes"),))


def test_auqrequest_two_options_accepted_min_floor() -> None:
    assert MIN_AUQ_OPTIONS == 2
    req = AUQRequest(
        question="Yes?",
        options=(AUQOption(key="y", label="Yes"), AUQOption(key="n", label="No")),
    )
    assert len(req.options) == 2


def test_auqrequest_five_options_rejected_max_cap() -> None:
    assert MAX_AUQ_OPTIONS == 4
    five = tuple(AUQOption(key=f"k{i}", label=f"opt{i}") for i in range(5))
    with pytest.raises(ValidationError):
        AUQRequest(question="Pick", options=five)


def test_auqrequest_duplicate_option_keys_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate option keys"):
        AUQRequest(
            question="Pick",
            options=(
                AUQOption(key="same", label="One"),
                AUQOption(key="same", label="Two"),
            ),
        )


def test_auqrequest_empty_question_rejected() -> None:
    with pytest.raises(ValidationError):
        AUQRequest(
            question="",
            options=(AUQOption(key="y", label="Yes"), AUQOption(key="n", label="No")),
        )


def test_auqrequest_extra_field_forbidden() -> None:
    with pytest.raises(ValidationError):
        AUQRequest(
            question="Q",
            options=(AUQOption(key="y", label="Yes"), AUQOption(key="n", label="No")),
            bogus="x",  # type: ignore[call-arg]
        )


# ---------------------------------------------------------------------------
# Frontier compute: ready / dep-closed / sibling order (criterion c)
# ---------------------------------------------------------------------------


def test_compute_ready_frontier_includes_pending_with_closed_deps() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.CLOSED),
        _item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W01"),
    ]
    frontier = compute_ready_frontier(items)
    assert isinstance(frontier, DrainableFrontier)
    assert frontier.ready_ids == ("P29-I04-W02",)
    assert not frontier.is_empty


def test_compute_ready_frontier_excludes_wave_with_open_deps() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.IN_PROGRESS),
        _item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W01"),
    ]
    # W01 is not CLOSED, so W02's deps are open -> W02 excluded.
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ()
    assert frontier.is_empty


def test_compute_ready_frontier_excludes_wave_with_unresolved_dep() -> None:
    items = [_item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W99")]
    # The dep id is not present in the view at all -> not closed -> excluded.
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ()


def test_compute_ready_frontier_lower_sibling_holds_higher_off_frontier() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.CLOSED),
        _item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W01"),
        _item("P29-I04-W03", WaveStatus.PENDING, "P29-I04-W01"),
    ]
    # W02 and W03 are both dep-ready, but W02 is lower-numbered, so only W02 is
    # on the ready frontier (the monotonic claim order holds).
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ("P29-I04-W02",)


def test_compute_ready_frontier_independent_siblings_in_separate_iters() -> None:
    items = [
        _item("P29-I04-W02", WaveStatus.PENDING, iter_id="P29-I04"),
        _item("P29-I05-W01", WaveStatus.PENDING, iter_id="P29-I05"),
    ]
    # Different iters -> no cross-iter sibling gate; both are ready.
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ("P29-I04-W02", "P29-I05-W01")


def test_compute_ready_frontier_orders_ready_by_natural_key() -> None:
    items = [
        _item("P29-I04-W09", WaveStatus.PENDING, iter_id="P29-I04"),
        _item("P29-I05-W10", WaveStatus.PENDING, iter_id="P29-I05"),
        _item("P29-I04-W08", WaveStatus.PENDING, iter_id="P29-I06"),
    ]
    # All in distinct iters so all ready; natural-key order puts W08 < W09 < W10.
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ("P29-I04-W08", "P29-I04-W09", "P29-I05-W10")


def test_compute_ready_frontier_empty_view_is_empty() -> None:
    frontier = compute_ready_frontier([])
    assert frontier.ready_ids == ()
    assert frontier.is_empty
    assert frontier.by_id == {}


def test_compute_ready_frontier_duplicate_wave_id_rejected() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.PENDING),
        _item("P29-I04-W01", WaveStatus.CLOSED),
    ]
    with pytest.raises(ValueError, match="duplicate wave id in frontier view"):
        compute_ready_frontier(items)


def test_compute_ready_frontier_terminal_waves_never_ready() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.FAILED),
        _item("P29-I04-W02", WaveStatus.ABANDONED),
        _item("P29-I04-W03", WaveStatus.CLOSED),
    ]
    # No PENDING wave at all -> empty frontier.
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ()


# ---------------------------------------------------------------------------
# Frontier drain: one AUQ per wave needing confirmation (criterion c)
# ---------------------------------------------------------------------------


def test_drain_frontier_yields_one_step_per_wave_needing_confirmation() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.CLOSED),
        _item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W01", iter_id="P29-I04"),
        _item("P29-I05-W03", WaveStatus.PENDING, iter_id="P29-I05"),
    ]
    frontier = compute_ready_frontier(items)
    assert frontier.ready_ids == ("P29-I04-W02", "P29-I05-W03")

    seen: list[str] = []

    def _confirm(item: WaveFrontierItem) -> AUQRequest | None:
        seen.append(item.wave_id)
        return _request(question=f"Claim {item.wave_id}?")

    steps = drain_frontier(frontier, _confirm)
    # One AUQ per ready wave, in claim order; the predicate saw every ready wave.
    assert seen == ["P29-I04-W02", "P29-I05-W03"]
    assert tuple(s.wave_id for s in steps) == ("P29-I04-W02", "P29-I05-W03")
    assert all(isinstance(s, FrontierDrainStep) for s in steps)
    assert steps[0].request.question == "Claim P29-I04-W02?"


def test_drain_frontier_skips_waves_not_needing_confirmation() -> None:
    items = [
        _item("P29-I04-W02", WaveStatus.PENDING, iter_id="P29-I04"),
        _item("P29-I05-W03", WaveStatus.PENDING, iter_id="P29-I05"),
    ]
    frontier = compute_ready_frontier(items)

    def _confirm(item: WaveFrontierItem) -> AUQRequest | None:
        # Only W03 needs a pause; W02 is claim-ready without one.
        if item.wave_id == "P29-I05-W03":
            return _request(question="Confirm W03")
        return None

    steps = drain_frontier(frontier, _confirm)
    assert tuple(s.wave_id for s in steps) == ("P29-I05-W03",)


def test_drain_frontier_empty_frontier_yields_no_steps() -> None:
    frontier = compute_ready_frontier([])

    def _confirm(item: WaveFrontierItem) -> AUQRequest | None:
        raise AssertionError("predicate must not be called on an empty frontier")

    assert drain_frontier(frontier, _confirm) == ()


def test_drain_frontier_all_clear_yields_no_steps() -> None:
    items = [_item("P29-I04-W02", WaveStatus.PENDING)]
    frontier = compute_ready_frontier(items)
    steps = drain_frontier(frontier, lambda _item: None)
    assert steps == ()


def test_drain_frontier_from_view_composes_compute_and_drain() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.CLOSED),
        _item("P29-I04-W02", WaveStatus.PENDING, "P29-I04-W01"),
    ]
    frontier, steps = drain_frontier_from_view(
        items, lambda item: _request(question=f"Claim {item.wave_id}?")
    )
    assert frontier.ready_ids == ("P29-I04-W02",)
    assert tuple(s.wave_id for s in steps) == ("P29-I04-W02",)


def test_drain_frontier_from_view_propagates_duplicate_id_error() -> None:
    items = [
        _item("P29-I04-W01", WaveStatus.PENDING),
        _item("P29-I04-W01", WaveStatus.CLOSED),
    ]
    with pytest.raises(ValueError, match="duplicate wave id in frontier view"):
        drain_frontier_from_view(items, lambda _item: None)


# ---------------------------------------------------------------------------
# End-to-end: needs_user -> AUQ -> project -> parse (composition)
# ---------------------------------------------------------------------------


def test_end_to_end_needs_user_projects_and_parses() -> None:
    req = needs_user_to_auq(_split_jury())
    claude = req.project_for_runtime("claude-code")
    assert isinstance(claude, ClaudeAUQProjection)
    assert tuple(o.label for o in claude.options) == ("Adjudicate now", "Override and proceed")
    answer = req.parse_answer("adjudicate")
    assert answer.selected_key == "adjudicate"
