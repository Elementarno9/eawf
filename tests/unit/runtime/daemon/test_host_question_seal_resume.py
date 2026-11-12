"""A seal interrupted by a document lock timeout is resumed by the next answer, once.

``runtime.host.question.answer`` records the operator's answers in one session and
seals the marked pending action in later ones. A lock timeout in the seal leaves the
answers recorded and the action waiting; the hook's next report of the same call
finds its questions already answered, and still seals the action, exactly once.
Driven through the real verbs against a provisioned canary; the timeout is raised
where the seal first takes the document lock, or where it commits.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import MethodContext, harness_seal
from eawf.runtime.daemon.methods.host_question import (
    HOST_QUESTION_ANSWER_METHOD,
    HOST_QUESTION_RAISE_METHOD,
)
from eawf.runtime.lock.portalock import LockTimeout
from tests.integration.runtime.daemon.methods.test_harness_seal import (
    APPROVE_LABEL,
    DECLINE_LABEL,
    action_row,
    canary,
    claim,
    ctx,
    dispatch,
    evidence_rows,
    host_questions,
    open_plan_decision,
    reported,
    sealed_events,
    tool_input,
    transcript_file,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import SESSION

__all__ = ["canary", "claim", "ctx"]

#: Where the seal takes the document lock: filing the evidence, then committing the seal.
SEAL_STEPS = ("record_evidence", "_commit_seal")


def _time_out_once(monkeypatch: pytest.MonkeyPatch, step: str) -> list[int]:
    """Make the seal's *step* raise LockTimeout on its first call only; return its call log."""
    real: Callable[..., Any] = getattr(harness_seal, step)
    calls: list[int] = []

    def timing_out(*args: Any, **kwargs: Any) -> Any:
        calls.append(1)
        if len(calls) == 1:
            raise LockTimeout("document.lock not acquired within 5.0s")
        return real(*args, **kwargs)

    monkeypatch.setattr(harness_seal, step, timing_out)
    return calls


def _interrupted(
    ctx: MethodContext, canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch, step: str
) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    """Raise a marked call, and answer it with the seal timing out at *step*.

    Returns:
        The opened decision, the reported params and the answers.
    """
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    answers = {tool["questions"][0]["question"]: APPROVE_LABEL}
    params = reported(tool)
    dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)
    _time_out_once(monkeypatch, step)
    with pytest.raises(LockTimeout):
        dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)
    return opened, params, answers


@pytest.mark.parametrize("step", SEAL_STEPS)
def test_a_seal_interrupted_by_a_lock_timeout_is_resumed_once(
    canary: CanaryProvision,
    ctx: MethodContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    step: str,
) -> None:
    opened, params, answers = _interrupted(ctx, canary, monkeypatch, step)
    interrupted = action_row(canary)
    statuses = {line.question.status for line in host_questions(canary, tmp_path).values()}
    assert (interrupted["status"], interrupted["revision"]) == ("WAITING", 1)
    assert statuses == {OpenQuestionStatus.ANSWERED}
    assert sealed_events(canary) == []

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    row = action_row(canary)
    assert again["answered"] == 0
    assert again["sealed_action_refs"] == [opened["action_ref"]]
    assert again["seal_refusals"] == []
    assert (row["status"], row["revision"]) == ("SEALED", 2)
    assert row["selected_option_id"] == "approve"
    assert row["harness_attestation"]["tool_use_id"] == params["tool_use_id"]
    assert row["harness_attestation"]["question_ref"] == again["question_refs"][0]
    assert len(sealed_events(canary)) == 1
    filed = {
        key
        for key, item in evidence_rows(canary, tmp_path).items()
        if "ACT-0001" in item["summary"]
    }
    assert filed == {row["receipt_ref"].rsplit("/", 1)[-1]}


def test_a_resumed_seal_is_never_sealed_again(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A further answer after the resumed seal, retried or choosing otherwise, seals nothing."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "_commit_seal")
    dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)
    sealed = action_row(canary)
    declined = dict.fromkeys(answers, DECLINE_LABEL)

    for said in (answers, declined):
        further = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=said)
        assert (further["answered"], further["sealed_action_refs"], further["seal_refusals"]) == (
            0,
            [],
            [],
        )

    assert action_row(canary) == sealed
    assert sealed["revision"] == 2
    assert len(sealed_events(canary)) == 1


def test_an_answer_for_an_action_sealed_elsewhere_seals_nothing(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: the interrupted call's action is sealed by another call before the retry."""
    opened, params, answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")
    other = {**params, "tool_use_id": "toolu_01other"}
    dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **other)
    first = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **other, answers=answers)
    assert first["sealed_action_refs"] == [opened["action_ref"]]
    sealed = action_row(canary)

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    assert (again["sealed_action_refs"], again["seal_refusals"]) == ([], [])
    assert action_row(canary) == sealed
    assert len(sealed_events(canary)) == 1


def test_a_retry_without_the_answer_seals_nothing(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boundary: a retried report that carries no answer for the question resumes nothing."""
    _opened, params, _answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers={})

    assert (again["answered"], again["sealed_action_refs"], again["seal_refusals"]) == (0, [], [])
    assert action_row(canary)["status"] == "WAITING"
    assert sealed_events(canary) == []


def test_a_resumed_seal_still_runs_every_check(
    canary: CanaryProvision, ctx: MethodContext, claim: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: the resume seals through the same checks, so a failed one still refuses."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")
    claim.unlink()

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    assert [item["code"] for item in again["seal_refusals"]] == ["harness_operator_unclaimed"]
    assert again["sealed_action_refs"] == []
    assert action_row(canary)["status"] == "WAITING"


def test_the_attestation_names_the_reporting_session(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resumed attestation is the call's own, its session kept out as a digest."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "_commit_seal")

    dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    attestation = action_row(canary)["harness_attestation"]
    assert attestation["host_session_digest"].startswith("sha256:")
    assert SESSION not in json.dumps(attestation)
    assert attestation["raise_gap_ms"] >= 0


@pytest.mark.parametrize(
    ("retried", "sealed"),
    [(APPROVE_LABEL, "approve"), (DECLINE_LABEL, None)],
    ids=["same-answer", "other-answer"],
)
def test_a_resumed_seal_seals_only_the_recorded_answer(
    canary: CanaryProvision,
    ctx: MethodContext,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    retried: str,
    sealed: str | None,
) -> None:
    """The recorded answer is final: a retry naming another option seals nothing."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")
    said = dict.fromkeys(answers, retried)

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=said)

    row = action_row(canary)
    [line] = host_questions(canary, tmp_path).values()
    assert line.question.chosen_option_key == "option_1"
    if sealed is None:
        assert again["sealed_action_refs"] == []
        assert [item["code"] for item in again["seal_refusals"]] == ["harness_answer_changed"]
        assert (row["status"], row["revision"]) == ("WAITING", 1)
        assert sealed_events(canary) == []
    else:
        assert again["seal_refusals"] == []
        assert (row["status"], row["selected_option_id"]) == ("SEALED", sealed)
        assert len(sealed_events(canary)) == 1


def test_a_retry_asking_in_other_words_seals_nothing(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a retry whose question differs from the one recorded, even by its spacing."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")
    [question] = answers
    marker, body = question.split("\n", 1)
    rewrapped = f"{marker}\n{body.replace(' ', '  ')}"
    retry = {
        **params,
        "questions": [{**params["questions"][0], "question": rewrapped}],
        "tool_input_questions": [{**params["tool_input_questions"][0], "question": rewrapped}],
    }

    again = dispatch(
        ctx, canary, HOST_QUESTION_ANSWER_METHOD, **retry, answers={rewrapped: APPROVE_LABEL}
    )

    assert again["sealed_action_refs"] == []
    assert [item["code"] for item in again["seal_refusals"]] == ["harness_question_mismatch"]
    row = action_row(canary)
    assert (row["status"], row["revision"]) == ("WAITING", 1)
    assert sealed_events(canary) == []


def test_seal_resume_repro_changed_answer(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A retry answering another option than the recorded one never seals it."""
    _opened, params, answers = _interrupted(ctx, canary, monkeypatch, "record_evidence")
    recorded = [line.question for line in host_questions(canary, tmp_path).values()]
    assert [question.chosen_option_key for question in recorded] == ["option_1"]
    declined = dict.fromkeys(answers, DECLINE_LABEL)

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=declined)

    assert again["sealed_action_refs"] == []
    assert action_row(canary)["status"] == "WAITING"


@pytest.mark.xfail(
    strict=True,
    reason="a refused seal leaves no durable record, so its retry cannot be held to the "
    "evidence the refused report carried",
)
def test_a_retry_without_the_transcript_keeps_a_transcript_refusal(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """A retry must not seal on weaker evidence than the report that was refused."""
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    [question] = tool["questions"]
    answers = {question["question"]: APPROVE_LABEL}
    transcript = transcript_file(tmp_path, tool, {question["question"]: DECLINE_LABEL})
    params = reported(tool, transcript=transcript)
    dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)
    refused = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)
    assert [item["code"] for item in refused["seal_refusals"]] == ["harness_transcript_mismatch"]
    del params["transcript_path"]

    dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    assert action_row(canary)["status"] == "WAITING"
