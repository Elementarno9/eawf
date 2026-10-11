"""A round whose finding overruns its line is kept, its finding clipped and marked.

A model reads "one line of at most 200 characters" loosely now and then; refusing the
whole round for it paused a live Campaign over text the round had already paid for. The
parser shortens such a finding at a word boundary to end in an ellipsis within its
bound, marks it clipped, and states it in full in the round's report, the text the
round's checkpoint artifact keeps. Every other mismatch still fails the round, and a
Campaign a failed round paused resumes on its revision and dispatches the step again.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.campaign import CampaignView
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.campaign_scheduler import (
    StepAssignment,
    StepReport,
    UnreportedRoundError,
)
from eawf.runtime.daemon.methods import campaign_run
from eawf.runtime.daemon.methods.campaign import MAX_TEXT_BYTES
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD, parse_round_report
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    _drive,
    _runs,
    _settled,
    _start,
    served,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, and the operator's home out of reach."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    provisioned = provision(tmp_path / "cam", code="CAM")
    seed(provisioned, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    return provisioned


LIMIT: Final = 200
OUTCOME_LIMIT: Final = 500
ELLIPSIS: Final = "…"
#: Past its bound, with word boundaries to clip at.
LONG: Final = " ".join(f"word{n}" for n in range(60))
LONG_OUTCOME: Final = " ".join(f"word{n}" for n in range(120))


def _answer(**body: Any) -> str:
    base: dict[str, Any] = {"report": "# R", "outcome": "held", "sources": 2, "findings": []}
    base.update(body)
    return f"Notes.\n\n```json\n{json.dumps(base)}\n```\n"


# ---- the parse step ------------------------------------------------------------------


def test_a_finding_exactly_at_its_bound_is_kept_unchanged() -> None:
    line = "x" * LIMIT

    report = parse_round_report(_answer(findings=[line]), tokens=1)

    assert (report.findings, report.clipped_findings, report.report) == ((line,), (), "# R")


def test_a_finding_one_past_its_bound_is_clipped_to_it() -> None:
    line = "x" * (LIMIT + 1)

    report = parse_round_report(_answer(findings=[line]), tokens=1)

    (clipped,) = report.findings
    assert clipped == "x" * (LIMIT - 1) + ELLIPSIS
    assert report.clipped_findings == (clipped,)
    assert line in report.report


def test_a_long_finding_is_clipped_at_a_word_boundary_and_kept_in_full() -> None:
    report = parse_round_report(_answer(findings=[LONG]), tokens=1)

    (clipped,) = report.findings
    assert len(clipped) <= LIMIT
    assert clipped.endswith(ELLIPSIS)
    kept = clipped.removesuffix(ELLIPSIS)
    assert LONG.startswith(kept)
    assert LONG[len(kept)] == " "
    assert report.report.startswith("# R")
    assert LONG in report.report


def test_only_the_overlong_findings_of_a_round_are_clipped() -> None:
    findings = ["short one", LONG, "short two", "short three", LONG + " more"]

    report = parse_round_report(_answer(findings=findings), tokens=1)

    assert len(report.findings) == 5
    assert [report.findings[i] for i in (0, 2, 3)] == findings[0:1] + findings[2:4]
    assert report.clipped_findings == (report.findings[1], report.findings[4])
    assert all(len(f) <= LIMIT for f in report.findings)
    assert LONG in report.report
    assert LONG + " more" in report.report


def test_a_round_with_no_findings_keeps_its_report_unchanged() -> None:
    report = parse_round_report(_answer(findings=[]), tokens=1)

    assert (report.findings, report.clipped_findings, report.report) == ((), (), "# R")


def test_an_outcome_exactly_at_its_bound_is_kept_unchanged() -> None:
    line = "o" * OUTCOME_LIMIT

    report = parse_round_report(_answer(outcome=line), tokens=1)

    assert (report.outcome, report.outcome_clipped, report.report) == (line, False, "# R")


def test_an_outcome_one_past_its_bound_is_clipped_to_it() -> None:
    line = "o" * (OUTCOME_LIMIT + 1)

    report = parse_round_report(_answer(outcome=line), tokens=1)

    assert report.outcome == "o" * (OUTCOME_LIMIT - 1) + ELLIPSIS
    assert report.outcome_clipped is True
    assert line in report.report


def test_a_long_outcome_is_clipped_at_a_word_boundary_and_kept_in_full() -> None:
    report = parse_round_report(_answer(outcome=LONG_OUTCOME, findings=[LONG]), tokens=1)

    assert len(report.outcome) <= OUTCOME_LIMIT
    kept = report.outcome.removesuffix(ELLIPSIS)
    assert report.outcome.endswith(ELLIPSIS)
    assert LONG_OUTCOME[len(kept)] == " "
    assert report.outcome_clipped is True
    assert "## Outcome in full" in report.report
    assert LONG_OUTCOME in report.report
    assert LONG in report.report


def test_a_report_past_its_bound_still_fails_the_round() -> None:
    with pytest.raises(ValueError, match="report: String should have at most"):
        parse_round_report(_answer(report="r" * (MAX_TEXT_BYTES + 1)), tokens=1)


@pytest.mark.parametrize(
    ("body", "cause"),
    [
        ({"findings": [{"statement": LONG}]}, "findings.0: Input should be a valid string"),
        ({"findings": [f"{LONG}\nsecond line"]}, "findings.0: String should"),
        ({"findings": ["one\ntwo"]}, "findings.0: String should match pattern"),
        ({"findings": LONG}, "findings: Input should be a valid"),
        ({"confidence": "high"}, "confidence: Extra inputs are not permitted"),
        ({"outcome": 3}, "outcome: Input should be a valid string"),
    ],
    ids=["object", "long-two-lines", "two-lines", "not-a-list", "extra-key", "wrong-type"],
)
def test_a_structural_mismatch_still_fails_the_round(body: dict[str, Any], cause: str) -> None:
    with pytest.raises(ValueError, match="does not hold the asked shape") as caught:
        parse_round_report(_answer(**body), tokens=1)

    assert cause in str(caught.value)


def test_a_missing_key_still_fails_the_round() -> None:
    with pytest.raises(ValueError, match="report: Field required"):
        parse_round_report(f'```json\n{{"outcome": "held", "findings": ["{LONG}"]}}\n```', tokens=1)


def test_the_prompt_states_the_report_bound_beside_the_line_bounds() -> None:
    assignment = StepAssignment.model_validate(
        {
            "campaign_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001",
            "campaign_title": "Survey",
            "ordinal": 1,
            "title": "Survey the sources",
            "method": "survey",
            "question": "What does src hold?",
            "run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001",
            "round_number": 1,
            "actor": "OP-0001",
        }
    )

    prompt = campaign_run.round_prompt(assignment)

    assert f"at most {LIMIT} characters" in prompt
    assert "at most 262144 characters" in prompt


# ---- the drive -----------------------------------------------------------------------


class ScriptedAgent:
    """Answers each round from a script, through the parser the host agent uses."""

    def __init__(self, answers: list[str]) -> None:
        self.answers = answers
        self.assignments: list[StepAssignment] = []

    async def work(self, assignment: StepAssignment) -> StepReport:
        self.assignments.append(assignment)
        text = self.answers.pop(0)
        try:
            return parse_round_report(text, tokens=10)
        except ValueError as error:
            raise UnreportedRoundError(str(error), tokens=10) from error


def test_a_round_with_overlong_findings_lands_and_marks_them_clipped(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = ScriptedAgent(
        [_answer(), _answer(report="# Synthesis", findings=["short finding", LONG])]
    )
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: agent)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    assert view.status.value == "converged"
    assert [(f.statement == "short finding", f.clipped) for f in view.findings] == [
        (True, False),
        (False, True),
    ]
    assert view.findings[1].statement.endswith(ELLIPSIS)
    assert LONG in "\n".join(view.artifacts[1].lines)


def test_a_round_with_an_overlong_outcome_lands_and_marks_its_step(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = ScriptedAgent([_answer(outcome=LONG_OUTCOME), _answer()])
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: agent)

    async def body() -> CampaignView:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    assert view.status.value == "converged"
    first, synthesis = (s.step for s in view.steps)
    assert (first.outcome_clipped, synthesis.outcome_clipped) == (True, False)
    assert first.outcome is not None
    assert first.outcome.endswith(ELLIPSIS)
    assert agent.assignments[1].prior_outcomes == (first.outcome,)
    assert LONG_OUTCOME in "\n".join(view.artifacts[0].lines)


def test_a_paused_campaign_resumes_on_its_revision_and_redispatches_the_failed_step(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    agent = ScriptedAgent([_answer(confidence="high"), _answer(), _answer()])
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: agent)

    async def body() -> tuple[CampaignView, CampaignView]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow")
            )
            key = started["record"]["key"]
            while campaign_run.campaign_drive_in_flight(_drive(canary, key)):
                await asyncio.sleep(0.05)
            paused = CampaignView.model_validate(
                await client.call(
                    "projection.campaign.view", repo_root=str(canary.root), campaign_key=key
                )
            )
            resumed = await client.call(
                campaign_run.CAMPAIGN_RUN_METHOD,
                repo_root=str(canary.root),
                actor="OP-0001",
                campaign_key=key,
                expected_revision=paused.revision,
            )
            assert resumed["driving"] is True
            return paused, await _settled(client, canary.root, key)

    paused, view = asyncio.run(body())

    assert paused.status.value == "active"
    assert [s.step.state.value for s in paused.steps] == ["pending", "pending"]
    (failed_run,) = paused.steps[0].step.run_refs
    assert _runs(canary)[failed_run.entity_key]["status"] == "CANCELLED"
    assert view.status.value == "converged"
    assert [s.step.state.value for s in view.steps] == ["done", "done"]
    assert view.steps[0].step.run_refs[0] == failed_run
    assert len(view.steps[0].step.run_refs) == 2
    assert [a.ordinal for a in agent.assignments] == [1, 1, 2]
    assert agent.assignments[1].round_number == 2
