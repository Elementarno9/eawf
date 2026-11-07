"""A marked pending action is sealed from the host operator's answer to its question.

Driven through the real ``runtime.host.question.raise`` and ``.answer`` verbs against a
provisioned canary, with the operator claim written to a user layer under
``tmp_path``. The question is the one ``runtime.question.open_decision`` or
``runtime.delivery.open_acceptance_approval`` presents, reported the way the
question hook reports an AskUserQuestion call. Every refusal leaves the action
waiting and the question answered.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
import tempfile
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.config import layered
from eawf.kernel.delivery.acceptance import MilestoneAcceptanceBundle
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record, read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.delivery_approval import (
    APPROVAL_SEALED_EVENT,
    DELIVERY_OPEN_APPROVAL_METHOD,
)
from eawf.runtime.daemon.methods.harness_seal import HARNESS_SEAL_ACTOR
from eawf.runtime.daemon.methods.host_question import (
    HOST_QUESTION_ANSWER_METHOD,
    HOST_QUESTION_RAISE_METHOD,
    HostQuestionLine,
)
from eawf.runtime.daemon.methods.pause import latest_pauses
from eawf.runtime.daemon.methods.pending_action_disposition import ACTION_ASSIGN_METHOD
from eawf.runtime.daemon.methods.question_decision import QUESTION_OPEN_DECISION_METHOD
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.host_calls import record_host_question
from eawf.workflow.delivery.acceptance import sealed_approval
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    firehose_path,
    method_context,
    provision,
    root_context,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_URN,
    SESSION,
    _running_run,
)

pytestmark = pytest.mark.integration

#: The operator the user layer claims, and the one an action may be addressed to instead.
OPERATOR_ID: Final = "OP-0001"
OTHER_OPERATOR: Final = "OP-0002"
HUMAN: Final[dict[str, str]] = {"principal_kind": "human", "principal_id": OPERATOR_ID}

#: The host's id of the asking call, and of a session that is on no Run.
CALL: Final = "toolu_01seal"
NO_RUN_SESSION: Final = "operator-session-0001"

APPROVE_LABEL: Final = "Apply the plan"
DECLINE_LABEL: Final = "Do not apply it"


def plan_options() -> list[dict[str, Any]]:
    """Return the options of a plan-approval decision, approve first."""
    return [
        {
            "option_id": "approve",
            "label": APPROVE_LABEL,
            "effect": "approve",
            "consequence": "The Milestone, its Batches and its Tasks are created as written.",
            "cost": "Work starts on the plan as it stands.",
            "preview": "plan -> Milestone -> Tasks",
        },
        {
            "option_id": "decline",
            "label": DECLINE_LABEL,
            "effect": "decline",
            "consequence": "Nothing is created and the plan waits for a revision.",
            "cost": "The work waits until a revised plan is approved.",
            "preview": "plan -> revise",
        },
    ]


@pytest.fixture(autouse=True)
def claim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep canary runtimes under tmp and point the user layer at a claim of OP-0001."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    path = tmp_path / "home" / ".config" / "eawf" / "config.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(f"operator:\n  principal: {OPERATOR_ID}\n", encoding="utf-8")
    monkeypatch.setattr(layered, "global_config_path", lambda: path)
    return path


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run on the host session."""
    provisioned = provision(tmp_path / "repo", code="SEAL")
    seed(provisioned, {"run": {"RUN-00000010": _running_run(SESSION)}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """One daemon context shared by every call of a case."""
    return method_context(tmp_path / "runtime")


def dispatch(ctx: MethodContext, canary: CanaryProvision, method: str, **params: Any) -> Any:
    """Dispatch one daemon verb against the canary, the way a client does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def open_plan_decision(ctx: MethodContext, canary: CanaryProvision, **extra: Any) -> dict[str, Any]:
    """File the waiting plan-approval decision and return what the verb answered."""
    opened: dict[str, Any] = dispatch(
        ctx,
        canary,
        QUESTION_OPEN_DECISION_METHOD,
        urn=MILESTONE_URN,
        idempotency_key="ask-plan-seal",
        actor=OPERATOR_ID,
        requested_by=dict(HUMAN),
        question="Should this plan be applied as written?",
        options=plan_options(),
        recommended_option_id="approve",
        recommendation_rationale="The plan passed every check it was submitted against.",
        **extra,
    )
    return opened


def tool_input(host_question: dict[str, Any]) -> dict[str, Any]:
    """Return the AskUserQuestion input an agent passes for *host_question*, verbatim."""
    return {
        "questions": [
            {
                "question": host_question["question"],
                "header": "Approval",
                "multiSelect": False,
                "options": [
                    {"label": option["label"], "description": option["description"]}
                    for option in host_question["options"]
                ],
            }
        ]
    }


def reported(tool: dict[str, Any], *, transcript: Path | None = None) -> dict[str, Any]:
    """Return the verb params the question hook sends for the call *tool* asks."""
    questions = [
        {
            "question": item["question"],
            "options": [option["label"] for option in item["options"]],
            **({"multi_select": True} if item.get("multiSelect") else {}),
        }
        for item in tool["questions"]
    ]
    params: dict[str, Any] = {
        "harness": "claude-code",
        "host_session_id": SESSION,
        "tool_use_id": CALL,
        "questions": questions,
        "tool_input_questions": tool["questions"],
    }
    if "answers" in tool:
        params["preanswered"] = True
    if transcript is not None:
        params["transcript_path"] = str(transcript)
    return params


def raise_and_answer(
    ctx: MethodContext,
    canary: CanaryProvision,
    params: dict[str, Any],
    answers: dict[str, str],
    *,
    raised: bool = True,
) -> dict[str, Any]:
    """Report the call before it runs, unless *raised* is off, then its answers."""
    if raised:
        dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)
    answer: dict[str, Any] = dispatch(
        ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers
    )
    return answer


def action_row(canary: CanaryProvision, key: str = "ACT-0001") -> dict[str, Any]:
    """Return one pending-action row the tree holds."""
    row: dict[str, Any] = read_document(document_path(canary))["pending_action"][key]
    return row


def run_ledger(canary: CanaryProvision, tmp_path: Path) -> tuple[LedgerRecord, ...]:
    """Return every line of the canary's run ledger."""
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        return read_ledger_records(session.ledger_path(Epoch2Collection.RUN))


def host_questions(canary: CanaryProvision, tmp_path: Path) -> dict[str, HostQuestionLine]:
    """Return the latest revision of every host question line, by key."""
    latest: dict[str, HostQuestionLine] = {}
    for record in run_ledger(canary, tmp_path):
        if record.payload.get("payload_kind") == "host_question":
            line = HostQuestionLine.model_validate(record.payload)
            latest[line.question.key] = line
    return latest


def evidence_rows(canary: CanaryProvision, tmp_path: Path) -> dict[str, dict[str, Any]]:
    """Return every evidence row the canary holds, by key."""
    context = root_context(canary, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.EVIDENCE))
    return {record.record_key: record.payload for record in records}


def sealed_events(canary: CanaryProvision) -> list[dict[str, Any]]:
    """Return the payload of every seal the firehose holds."""
    path = firehose_path(canary)
    lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    payloads = [json.loads(line)["payload"] for line in lines]
    return [item for item in payloads if item.get("name") == APPROVAL_SEALED_EVENT]


def transcript_file(tmp_path: Path, tool: dict[str, Any], answers: dict[str, str] | None) -> Path:
    """Write a Claude Code transcript holding the call *tool* asks and, if set, its answers."""
    lines: list[dict[str, Any]] = [
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": CALL, "name": "AskUserQuestion", "input": tool}
                ],
            },
        }
    ]
    if answers is not None:
        lines.append(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": CALL, "content": "ok"}],
                },
                "toolUseResult": {"questions": tool["questions"], "answers": answers},
            }
        )
    path = tmp_path / "transcript.jsonl"
    path.write_text("".join(f"{json.dumps(line)}\n" for line in lines), encoding="utf-8")
    return path


# ---------- CR-777: a marked answer seals its action ----------


def test_a_marked_answer_seals_its_action(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    marked = tool["questions"][0]["question"]
    answers = {marked: APPROVE_LABEL}
    transcript = transcript_file(tmp_path, tool, answers)

    answer = raise_and_answer(ctx, canary, reported(tool, transcript=transcript), answers)

    row = action_row(canary)
    assert answer["sealed_action_refs"] == [opened["action_ref"]]
    assert answer["seal_refusals"] == []
    assert answer["run_ref"] == RUN_URN
    assert (row["status"], row["revision"]) == ("SEALED", 2)
    assert row["resolution_actor"] == HUMAN
    assert row["selected_option_id"] == "approve"
    evidence_key = row["receipt_ref"].rsplit("/", 1)[-1]
    evidence = evidence_rows(canary, tmp_path)[evidence_key]
    assert evidence["kind"] == "store_record"
    for word in ("ACT-0001", "QST-0001", "approve", "transcript_check=matched"):
        assert word in evidence["summary"]
    attestation = row["harness_attestation"]
    assert attestation["harness"] == "claude-code"
    assert attestation["tool_use_id"] == CALL
    assert attestation["question_ref"] == answer["question_refs"][0]
    assert attestation["run_ref"] == RUN_URN
    assert attestation["transcript_check"] == "matched"
    assert attestation["raise_gap_ms"] >= 0
    assert attestation["host_session_digest"].startswith("sha256:")
    assert SESSION not in json.dumps(attestation)
    assert attestation["payload_digest"].startswith("sha256:")
    [event] = sealed_events(canary)
    assert event["actor_ref"] == HARNESS_SEAL_ACTOR
    assert host_questions(canary, tmp_path)["QST-0001"].question.chosen_option_key == "option_1"


def test_a_retried_answer_seals_nothing_twice(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    answers = {tool["questions"][0]["question"]: APPROVE_LABEL}
    params = reported(tool)
    raise_and_answer(ctx, canary, params, answers)

    again = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    assert (again["sealed_action_refs"], again["seal_refusals"], again["answered"]) == ([], [], 0)
    assert action_row(canary)["revision"] == 2
    assert len(sealed_events(canary)) == 1


@pytest.mark.parametrize(
    ("answers", "check"),
    [(None, "tool_use_only"), ("no-file", "absent"), ("no-path", "absent")],
    ids=["answer-not-flushed", "file-missing", "path-not-sent"],
)
def test_a_transcript_that_cannot_confirm_is_recorded_not_refused(
    canary: CanaryProvision,
    ctx: MethodContext,
    tmp_path: Path,
    answers: str | None,
    check: str,
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    said = {tool["questions"][0]["question"]: APPROVE_LABEL}
    transcript = transcript_file(tmp_path, tool, None)
    if answers == "no-file":
        transcript.unlink()
    params = reported(tool, transcript=None if answers == "no-path" else transcript)

    answer = raise_and_answer(ctx, canary, params, said)

    row = action_row(canary)
    assert answer["sealed_action_refs"] == [opened["action_ref"]]
    assert row["harness_attestation"]["transcript_check"] == check
    assert f"transcript_check={check}" in str(evidence_rows(canary, tmp_path))


def test_a_transcript_that_is_no_regular_file_is_never_opened(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """A pipe named as the transcript would block the reader until something wrote to it."""
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    said = {tool["questions"][0]["question"]: APPROVE_LABEL}
    pipe = tmp_path / "transcript.jsonl"
    os.mkfifo(pipe)

    answer = raise_and_answer(ctx, canary, reported(tool, transcript=pipe), said)

    assert answer["sealed_action_refs"] == [opened["action_ref"]]
    assert action_row(canary)["harness_attestation"]["transcript_check"] == "absent"


@pytest.mark.parametrize(
    "rewrap",
    [False, True],
    ids=["verbatim", "whitespace-rewrapped"],
)
def test_the_presented_question_seals_as_presented(
    canary: CanaryProvision, ctx: MethodContext, rewrap: bool
) -> None:
    """Boundary: the words the action presents seal, and only their whitespace may move."""
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    question = tool["questions"][0]
    if rewrap:
        marker, body = question["question"].split("\n", 1)
        question["question"] = f"{marker}\n  {body.replace(' ', '  ')}\n"
        for option in question["options"]:
            option["description"] = f" {option['description']}\n"
    question["options"][0]["preview"] = opened["host_question"]["options"][0]["preview"]

    answer = raise_and_answer(ctx, canary, reported(tool), {question["question"]: APPROVE_LABEL})

    assert answer["seal_refusals"] == []
    assert answer["sealed_action_refs"] == [opened["action_ref"]]


def test_a_declining_answer_seals_the_declining_option(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Any offered label seals; what it means is the consuming verb's to read."""
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])

    raise_and_answer(ctx, canary, reported(tool), {tool["questions"][0]["question"]: DECLINE_LABEL})

    assert action_row(canary)["selected_option_id"] == "decline"


def test_an_unmarked_question_seals_nothing(canary: CanaryProvision, ctx: MethodContext) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    unmarked = tool["questions"][0]["question"].split("\n", 1)[1]
    tool["questions"][0]["question"] = unmarked

    answer = raise_and_answer(ctx, canary, reported(tool), {unmarked: APPROVE_LABEL})

    assert (answer["answered"], answer["sealed_action_refs"], answer["seal_refusals"]) == (
        1,
        [],
        [],
    )
    assert action_row(canary)["status"] == "WAITING"


# ---------- CR-778: every failed check refuses with its code ----------


Forged = tuple[dict[str, Any], dict[str, str], bool]


def _reword_body(question: dict[str, Any]) -> None:
    """Keep the marker line and ask something harmless-sounding under it."""
    marker = question["question"].split("\n", 1)[0]
    question["question"] = f"{marker}\nMay I tidy whitespace in README?"


def _reword_descriptions(question: dict[str, Any]) -> None:
    """Keep every label and describe every option as harmless."""
    for option in question["options"]:
        option["description"] = "Harmless"


def _reword_preview(question: dict[str, Any]) -> None:
    """Show a preview of something the action does not produce."""
    question["options"][0]["preview"] = "README -> tidied"


#: How each paraphrase of the presented question is made, by the case that names it.
PARAPHRASES: Final[dict[str, Callable[[dict[str, Any]], None]]] = {
    "harness_question_mismatch:body": _reword_body,
    "harness_question_mismatch:description": _reword_descriptions,
    "harness_question_mismatch:preview": _reword_preview,
}


def _forged(code: str) -> Callable[[MethodContext, CanaryProvision, Path, Path], Forged]:
    """Return the setup of the one forged call *code* refuses: params, answers, raised."""

    def build(ctx: MethodContext, canary: CanaryProvision, tmp_path: Path, claim: Path) -> Forged:
        opening = {"assignee": OTHER_OPERATOR} if code == "harness_assignee_mismatch" else {}
        tool = tool_input(open_plan_decision(ctx, canary, **opening)["host_question"])
        question = tool["questions"][0]
        answer = APPROVE_LABEL
        transcript: Path | None = None
        raised = code != "harness_raise_missing"
        if code == "harness_preanswered":
            tool["answers"] = {question["question"]: APPROVE_LABEL}
        elif code == "harness_multi_select":
            question["multiSelect"] = True
        elif code == "harness_options_mismatch":
            question["options"][0]["label"] = "Apply the whole plan"
            answer = "Apply the whole plan"
        elif code == "harness_answer_unoffered":
            answer = "Apply it, but only the first Batch"
        elif code == "harness_marker_duplicate":
            twin = copy.deepcopy(question)
            twin["question"] = f"{question['question']}\nAsked again."
            tool["questions"].append(twin)
        elif code == "harness_action_unheld":
            question["question"] = question["question"].replace("ACT-0001", "ACT-0099", 1)
        elif code == "harness_operator_unclaimed":
            claim.unlink()
        elif code == "harness_transcript_mismatch":
            transcript = transcript_file(tmp_path, tool, {question["question"]: DECLINE_LABEL})
        answers = {item["question"]: answer for item in tool["questions"]}
        return reported(tool, transcript=transcript), answers, raised

    return build


def _paraphrased(case: str) -> Callable[[MethodContext, CanaryProvision, Path, Path], Forged]:
    """Return the setup of a call asking *case*'s other words, or not reporting its words."""

    def build(ctx: MethodContext, canary: CanaryProvision, _tmp: Path, _claim: Path) -> Forged:
        tool = tool_input(open_plan_decision(ctx, canary)["host_question"])
        question = tool["questions"][0]
        if case in PARAPHRASES:
            PARAPHRASES[case](question)
        params = reported(tool)
        if case not in PARAPHRASES:
            del params["tool_input_questions"]
        return params, {question["question"]: APPROVE_LABEL}, True

    return build


CODES: Final = (
    "harness_raise_missing",
    "harness_preanswered",
    "harness_multi_select",
    "harness_options_mismatch",
    "harness_answer_unoffered",
    "harness_marker_duplicate",
    "harness_action_unheld",
    "harness_action_stale",
    "harness_operator_unclaimed",
    "harness_assignee_mismatch",
    "harness_transcript_mismatch",
    *PARAPHRASES,
    "harness_question_mismatch:unforwarded",
)


@pytest.mark.parametrize("case", CODES)
def test_a_failed_check_refuses_the_seal_with_its_code(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path, claim: Path, case: str
) -> None:
    code = case.split(":", 1)[0]
    setup = _forged(code) if code == case else _paraphrased(case)
    params, answers, raised = setup(ctx, canary, tmp_path, claim)
    if raised:
        dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)
    if code == "harness_action_stale":
        dispatch(
            ctx,
            canary,
            ACTION_ASSIGN_METHOD,
            urn=f"{MILESTONE_URN.rsplit('/', 2)[0]}/pending-action/ACT-0001",
            expected_revision=1,
            idempotency_key="assign-stale",
            actor=OPERATOR_ID,
            assignee=OPERATOR_ID,
        )
    before = action_row(canary)

    answer = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    row = action_row(canary)
    assert row == before
    assert row["status"] == "WAITING"
    refused = {
        (item["action_ref"].rsplit("/", 1)[-1], item["code"]) for item in answer["seal_refusals"]
    }
    expected = "ACT-0099" if code == "harness_action_unheld" else "ACT-0001"
    assert refused == {(expected, code)}
    assert answer["sealed_action_refs"] == []
    assert answer["answered"] == len(params["questions"])
    statuses = {line.question.status for line in host_questions(canary, tmp_path).values()}
    assert statuses == {OpenQuestionStatus.ANSWERED}
    assert sealed_events(canary) == []


@pytest.mark.parametrize(
    "body",
    ["operator:\n  principal: EAWF-PLANNER\n", "operator: [not, a, block]\n"],
    ids=["agent-key", "unreadable"],
)
def test_a_claim_that_is_no_operator_refuses_the_seal(
    canary: CanaryProvision, ctx: MethodContext, claim: Path, body: str
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    claim.write_text(body, encoding="utf-8")

    answer = raise_and_answer(
        ctx, canary, reported(tool), {tool["questions"][0]["question"]: APPROVE_LABEL}
    )

    assert [item["code"] for item in answer["seal_refusals"]] == ["harness_operator_unclaimed"]
    assert action_row(canary)["status"] == "WAITING"


def test_an_action_addressed_to_the_claimed_operator_seals(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    opened = open_plan_decision(ctx, canary, assignee=OPERATOR_ID)
    tool = tool_input(opened["host_question"])

    answer = raise_and_answer(
        ctx, canary, reported(tool), {tool["questions"][0]["question"]: APPROVE_LABEL}
    )

    assert answer["sealed_action_refs"] == [opened["action_ref"]]


# ---------- CR-779: an operator session on no Run ----------


def test_a_marked_question_seals_without_a_run(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    params = {**reported(tool), "host_session_id": NO_RUN_SESSION}
    answers = {tool["questions"][0]["question"]: APPROVE_LABEL}

    raised = dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)
    answer = dispatch(ctx, canary, HOST_QUESTION_ANSWER_METHOD, **params, answers=answers)

    assert raised["run_ref"] is None and answer["run_ref"] is None
    line = host_questions(canary, tmp_path)["QST-0001"]
    assert str(line.question.scope_ref) == opened["action_ref"]
    assert line.question.status is OpenQuestionStatus.ANSWERED
    records = run_ledger(canary, tmp_path)
    assert latest_pauses(records) == {}
    assert run_events_of(records, parse_qualified_urn(RUN_URN)) == ()
    row = action_row(canary)
    assert answer["sealed_action_refs"] == [opened["action_ref"]]
    assert (row["status"], row["resolution_actor"]) == ("SEALED", HUMAN)
    assert row["harness_attestation"]["run_ref"] is None


def test_an_unmarked_question_on_no_run_is_still_the_quiet_skip(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    tool["questions"][0]["question"] = tool["questions"][0]["question"].split("\n", 1)[1]
    params = {**reported(tool), "host_session_id": NO_RUN_SESSION}

    for method in (HOST_QUESTION_RAISE_METHOD, HOST_QUESTION_ANSWER_METHOD):
        with pytest.raises(DaemonValidationError, match="0 live Runs are on the host session"):
            dispatch(ctx, canary, method, **params)

    assert host_questions(canary, tmp_path) == {}


def test_a_marker_naming_no_held_action_on_no_run_is_the_quiet_skip(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """With nothing held to scope it to, the call has nowhere to land."""
    opened = open_plan_decision(ctx, canary)
    tool = tool_input(opened["host_question"])
    question = tool["questions"][0]
    question["question"] = question["question"].replace("ACT-0001", "ACT-0099", 1)
    params = {**reported(tool), "host_session_id": NO_RUN_SESSION}

    with pytest.raises(DaemonValidationError, match="0 live Runs are on the host session"):
        dispatch(ctx, canary, HOST_QUESTION_RAISE_METHOD, **params)


# ---------- CR-781: a protected acceptance approval through the hook path ----------


class HookClient(AbstractContextManager["HookClient"]):
    """A daemon client that dispatches each call to the real verbs in process."""

    def __init__(self, ctx: MethodContext) -> None:
        self._ctx = ctx

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> Any:
        return asyncio.run(methods.dispatch(method, self._ctx, params))


def hook_event(
    event_type: HookEventType, tool: dict[str, Any], *, answers: dict[str, str] | None = None
) -> HookEvent:
    """Return the question hook's event for the call *tool* asks, as Claude Code sends it."""
    payload: dict[str, Any] = {
        "session_id": SESSION,
        "tool_name": "AskUserQuestion",
        "tool_use_id": CALL,
        "tool_input": tool,
    }
    if answers is not None:
        payload["tool_response"] = {"questions": tool["questions"], "answers": answers}
    return HookEvent(
        event_type=event_type,
        runtime="claude",
        occurred_at=AT,
        payloads={event_type.value: payload},
    )


def ask_through_the_hook(
    ctx: MethodContext, canary: CanaryProvision, tool: dict[str, Any], answers: dict[str, str]
) -> Iterator[str]:
    """Report the call before and after it runs through the question hook; yield its output."""
    for event_type, said in (
        (HookEventType.PRE_TOOL_USE, None),
        (HookEventType.POST_TOOL_USE, answers),
    ):
        result = record_host_question(
            hook_event(event_type, tool, answers=said),
            daemon_client_factory=lambda: HookClient(ctx),
            repo_root=canary.root,
        )
        assert not result.block
        yield result.output


@pytest.fixture
def review(tmp_path: Path) -> CanaryProvision:
    """A canary holding MLS-0030 in review, one verified Batch, its evidence and the Run."""
    provisioned = provision(tmp_path / "review", code="REVIEW")
    seed(
        provisioned,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            "batch": {"BAT-0007": seed_row("batch", "READY_TO_MERGE")},
            "run": {"RUN-00000010": _running_run(SESSION)},
        },
    )
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        for key, kind in (("EVD-0001", "decision"), ("EVD-0002", "artifact")):
            append_ledger_record(
                session.ledger_path(Epoch2Collection.EVIDENCE),
                LedgerRecord(
                    collection=Epoch2Collection.EVIDENCE,
                    record_key=key,
                    status="recorded",
                    recorded_at=AT,
                    payload={
                        "id": key,
                        "kind": kind,
                        "summary": f"{kind} observation",
                        "recorded_at": AT.isoformat(),
                    },
                ),
            )
    return provisioned


def test_a_harness_sealed_acceptance_approval_is_accepted(
    review: CanaryProvision, ctx: MethodContext
) -> None:
    opened = dispatch(
        ctx,
        review,
        DELIVERY_OPEN_APPROVAL_METHOD,
        urn=MILESTONE_URN,
        actor="SKILL-VERIFY",
        requested_by=dict(HUMAN),
        steps=[
            {
                "step_id": "AS-01",
                "passed": True,
                "observation": "the install completed and reported the version",
                "evidence_kinds": ["artifact"],
                "evidence_refs": ["eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"],
            }
        ],
        accepted_binding=seed_row("milestone", "COMPLETED")["accepted_binding"],
    )
    question = opened["host_question"]
    approving = next(o["label"] for o in question["options"] if o["option_id"] == "approve")
    tool = tool_input(question)

    outputs = list(ask_through_the_hook(ctx, review, tool, {question["question"]: approving}))

    assert outputs[1].endswith(f"sealed={opened['action_ref'].rsplit('/', 1)[-1]}")
    action = PendingAction.model_validate(action_row(review))
    approval = sealed_approval(
        action,
        bundle=MilestoneAcceptanceBundle.model_validate(opened["acceptance_bundle"]),
        milestone_ref=parse_qualified_urn(MILESTONE_URN),
    )
    assert approval.resolved_by.principal_id == OPERATOR_ID
    assert action.harness_attestation is not None
    assert action.harness_attestation.run_ref is not None
