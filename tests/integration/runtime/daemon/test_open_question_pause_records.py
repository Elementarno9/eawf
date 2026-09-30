"""Questions and pauses as the daemon records, projects and answers them, through its verbs.

PLAN-045 and PLAN-047 say a question and a pause render in exactly one situation of their
projection, computed from the persisted fields when they are read; PLAN-046 says an
operator answers a question by one of its options or in their own words, the answer and
its ``ANSWERED`` transition written together. D-PAUSE retires the epoch-1 ``needs_user``
pause: the Run a host question holds waits under an epoch-2 pause, and an epoch-1 pause
still open when the daemon attaches the tree is moved onto the native records once. FU-70
makes the two question stores one key space. A disposable epoch-2 canary holds one running
Run on a host session, and every case drives the verbs a hook or the console reaches.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.projection.attention import AttentionNeedKind, build_attention_view
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.state.enums import OpenQuestionStatus, StoreKind
from eawf.kernel.state.epoch2.pause import OpenPause, PauseReason, PauseStatus
from eawf.kernel.state.epoch2.question import QuestionSituation
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.budget.legacy_notices import PAUSE_EVENT_TYPE
from eawf.runtime.daemon.campaign_scheduler import plan_campaign
from eawf.runtime.daemon.main import _attach_bound_tree
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.campaign import approve_plan
from eawf.runtime.daemon.methods.host_question import (
    HOST_QUESTION_ANSWER_METHOD,
    HOST_QUESTION_RAISE_METHOD,
    question_keys,
)
from eawf.runtime.daemon.methods.pause import PAUSE_READ_METHOD, latest_pauses
from eawf.runtime.daemon.methods.question import QUESTION_ANSWER_METHOD, QUESTION_READ_METHOD
from eawf.runtime.daemon.pause_migration import MIGRATED_CHOICE, open_legacy_pauses
from eawf.workflow.skills.bodies.user_question import UserQuestion, UserQuestionOption
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    firehose_path,
    method_context,
    root_context,
    seed,
    seed_row,
    tree_root,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import TRACK
from tests.integration.runtime.daemon.test_host_question_producer import (
    QUESTIONS,
    ask,
    call,
    canary,
    ctx,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import OPERATOR, RUN_KEY

__all__ = ["canary", "ctx"]

pytestmark = pytest.mark.integration

#: The key the first question the host asks is filed under, and the pause over its Run.
FIRST: Final = "QST-0001"
SECOND: Final = "QST-0002"
PAUSE: Final = "PAU-0001"

#: When a plan is approved and a legacy pause was raised.
_NOW: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)


def _read_questions(ctx: MethodContext, canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    answer = call(QUESTION_READ_METHOD, ctx, canary, principal=OPERATOR)
    return {item["question"]["key"]: item for item in answer["questions"]}


def _read_pauses(ctx: MethodContext, canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    answer = call(PAUSE_READ_METHOD, ctx, canary)
    return {item["pause"]["key"]: item for item in answer["pauses"]}


def _answer(
    ctx: MethodContext, canary: CanaryProvision, key: str, revision: int, **answer: Any
) -> dict[str, Any]:
    urn = _read_questions(ctx, canary)[key]["question"]["urn"]
    result: dict[str, Any] = call(
        QUESTION_ANSWER_METHOD,
        ctx,
        canary,
        urn=urn,
        expected_revision=revision,
        actor=OPERATOR,
        **answer,
    )
    return result


# ---------- PLAN-047 / D-PAUSE: a host question holds its Run under a pause ----------


def test_plan_047_d_pause_a_host_question_holds_its_run_under_a_person_pause(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Each raised question opens one pause over the Run, waiting on a person answering it."""
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)  # a retried hook opens nothing more

    pauses = _read_pauses(ctx, canary)

    assert sorted(pauses) == ["PAU-0001", "PAU-0002"]
    held = OpenPause.model_validate(pauses[PAUSE]["pause"])
    assert held.reason is PauseReason.USER and held.status is PauseStatus.OPEN
    assert held.scope_ref.entity_key == RUN_KEY
    assert held.waiting_on_ref is not None and held.waiting_on_ref.entity_key == FIRST
    assert held.health_evidence_refs == (held.waiting_on_ref,)
    assert pauses[PAUSE]["situation"] == "waiting_on_person"
    assert pauses[PAUSE]["ends_when"].startswith("an eligible principal answers")
    assert call(PAUSE_READ_METHOD, ctx, canary)["run_states"] == {RUN_KEY: "RUNNING"}


def test_plan_047_the_hosts_answer_resolves_the_pause_it_ended(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """An answered question resolves its pause; an unanswered one keeps its Run waiting."""
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    ask(
        ctx,
        canary,
        HOST_QUESTION_ANSWER_METHOD,
        answers={QUESTIONS[0]["question"]: "SQLite"},
    )

    pauses = _read_pauses(ctx, canary)

    assert pauses[PAUSE]["situation"] == "resolved"
    resolved = OpenPause.model_validate(pauses[PAUSE]["pause"])
    assert resolved.resolved_at is not None and resolved.resume_predicate.last_result is True
    assert pauses["PAU-0002"]["situation"] == "waiting_on_person"


# ---------- PLAN-045: the question read projects each question for its reader ----------


def test_plan_045_the_question_read_projects_a_host_question_for_its_reader(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Waiting, it is open and blocking; answered by the host, it is answered elsewhere."""
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    waiting = _read_questions(ctx, canary)
    assert waiting[FIRST]["situation"] == QuestionSituation.OPEN_BLOCKING.value
    assert waiting[FIRST]["asked_by_run"] == RUN_KEY
    assert waiting[FIRST]["ends_when"] == "an operator or evidence answers it"

    ask(ctx, canary, HOST_QUESTION_ANSWER_METHOD, answers={QUESTIONS[0]["question"]: "SQLite"})

    answered = _read_questions(ctx, canary)
    assert answered[FIRST]["situation"] == QuestionSituation.ANSWERED_ELSEWHERE.value
    assert answered[SECOND]["situation"] == QuestionSituation.OPEN_BLOCKING.value


def test_plan_045_the_attention_register_lists_a_waiting_host_question(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """A waiting host question is an answer every principal may give, until it is answered."""
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    read = call("projection.attention.read", ctx, canary)
    view = build_attention_view(build_register_view(RouteProjection.model_validate(read)))
    items = {item.key: item for item in view.items}
    assert items[FIRST].need is AttentionNeedKind.ANSWER and items[FIRST].assignee_ref is None

    ask(ctx, canary, HOST_QUESTION_ANSWER_METHOD, answers={QUESTIONS[0]["question"]: "SQLite"})

    read = call("projection.attention.read", ctx, canary)
    view = build_attention_view(build_register_view(RouteProjection.model_validate(read)))
    assert FIRST not in {item.key for item in view.items}
    assert SECOND in {item.key for item in view.items}


# ---------- PLAN-046: an operator answers by option or in their own words ----------


def test_plan_046_an_operator_answers_by_option_and_its_pause_resolves(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    result = _answer(ctx, canary, FIRST, 1, option_key="option_1")

    assert result["question"]["status"] == OpenQuestionStatus.ANSWERED.value
    assert result["question"]["chosen_option_key"] == "option_1"
    assert result["question"]["resolution_actor"] == OPERATOR
    assert result["resolved_pauses"] == [PAUSE]
    mine = _read_questions(ctx, canary)[FIRST]
    assert mine["situation"] == QuestionSituation.ANSWERED_BY_YOU.value


def test_plan_046_a_reply_answers_a_question_with_no_options_verbatim(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    words = "Call it cache_backend, as the ADR does."

    result = _answer(ctx, canary, SECOND, 1, reply=words)

    assert result["question"]["reply"]["text"] == words
    assert result["question"]["chosen_option_key"] is None
    assert _read_pauses(ctx, canary)["PAU-0002"]["situation"] == "resolved"


@pytest.mark.parametrize(
    ("answer", "revision", "code"),
    [
        ({"reply": "x" * 2001}, 1, "check reply"),
        ({"option_key": "option_9"}, 1, "question_option_unknown"),
        ({"option_key": "option_1"}, 7, "revision_conflict"),
    ],
    ids=["reply-past-2000", "unknown-option", "stale-revision"],
)
def test_plan_046_an_answer_the_question_cannot_take_is_refused_unwritten(
    canary: CanaryProvision, ctx: MethodContext, answer: dict[str, Any], revision: int, code: str
) -> None:
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    with pytest.raises(DaemonValidationError, match=code):
        _answer(ctx, canary, FIRST, revision, **answer)

    assert _read_questions(ctx, canary)[FIRST]["question"]["revision"] == 1


def test_plan_046_a_reply_at_the_limit_lands_and_a_second_answer_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    _answer(ctx, canary, SECOND, 1, reply="y" * 2000)

    with pytest.raises(DaemonValidationError, match="question_already_resolved"):
        _answer(ctx, canary, SECOND, 2, reply="changed my mind")


# ---------- FU-70: one question key space across both stores ----------


def _plan(taken: set[str]) -> Any:
    return plan_campaign(
        taken,
        actor=OPERATOR,
        track_ref=parse_qualified_urn(TRACK),
        title="Which backend?",
        questions=["Is SQLite fast enough?"],
        depth=ResearchDepth.SHALLOW,
    )


def test_fu_70_a_campaign_plans_its_seeds_past_every_host_question(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """A Campaign's seed questions take keys after the host's, and approve without refusal."""
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)
    path = document_path(canary)
    taken = question_keys(read_document(path), read_ledger_records(_run_ledger(canary)))
    assert {FIRST, SECOND} <= taken

    plan = _plan(taken)

    assert [seed.urn.entity_key for seed in plan.seed_questions] == ["QST-0003"]
    commit, _ = approve_plan(root_context(canary, tmp_path / "runtime"), plan, now=_NOW)
    assert commit.committed
    assert {FIRST, SECOND, "QST-0003"} <= set(_read_questions(ctx, canary))


def test_fu_70_a_seed_question_under_a_host_questions_key_is_refused(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """A seed filed under a key a host question holds asks another question, so it is refused."""
    seed(canary, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    with pytest.raises(DaemonValidationError, match="already asks another question"):
        approve_plan(root_context(canary, tmp_path / "runtime"), _plan(set()), now=_NOW)


def _run_ledger(canary: CanaryProvision) -> Path:
    return ledger_path(document_path(canary), Epoch2Collection.RUN)


# ---------- D-PAUSE: an epoch-1 pause moves onto the native records at daemon start ----------

LEGACY_URN: Final = "urn:eawf:v1:event:P30-I01-W02/needs-user-abc123"


def _legacy_pause(canary: CanaryProvision) -> None:
    """Append one open epoch-1 pause, as the retired store wrote it."""
    question = UserQuestion(
        question="Apply the roadmap revision?",
        options=[UserQuestionOption(label="apply"), UserQuestionOption(label="revise")],
    )
    payload = EventPayload(
        timestamp=_NOW,
        event_type=PAUSE_EVENT_TYPE,
        actor="skill",
        command="skill pause",
        args_hash="",
        status="needs_user",
        message=question.question,
        extras={
            "pause_urn": LEGACY_URN,
            "scope_id": "P30-I01-W02",
            "session": "urn:eawf:v1:session:cli/SES-1",
            "user_question": question.model_dump_json(),
        },
    )
    append_envelope(
        firehose_path(canary),
        Envelope(
            id=f"EV-{uuid.uuid4().hex[:12]}",
            kind=StoreKind.EVENT,
            scope_id="P30-I01-W02",
            created_at=_NOW,
            updated_at=None,
            summary=f"needs_user pause {LEGACY_URN}",
            payload=payload.model_dump(mode="json"),
        ),
    )


def test_d_pause_an_open_epoch1_pause_moves_once_when_the_daemon_starts(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The start attaches the tree and moves the pause; a second start finds nothing left."""
    seed(canary, {"project": {"EAWF": {"key": "EAWF"}}})
    _legacy_pause(canary)
    state_path = tree_root(canary) / "state.json"
    assert [pause.pause_urn for pause in open_legacy_pauses(state_path)] == [LEGACY_URN]

    for run in ("first", "second"):
        started = method_context(tmp_path / run)
        started.state_path = state_path
        asyncio.run(_attach_bound_tree(started))

    assert open_legacy_pauses(state_path) == []
    rows = document_rows(read_document(document_path(canary)), Epoch2Collection.OPEN_QUESTION)
    moved = [row for row in rows.values() if row["question"] == "Apply the roadmap revision?"]
    assert len(moved) == 1 and moved[0]["status"] == OpenQuestionStatus.OPEN.value
    assert [option["label"] for option in moved[0]["options"]] == ["apply", "revise"]
    pauses = latest_pauses(read_ledger_records(_run_ledger(canary)))
    assert len(pauses) == 1
    pause = next(iter(pauses.values()))
    assert pause.reason is PauseReason.USER and pause.status is PauseStatus.OPEN
    assert pause.waiting_on_ref is not None
    assert pause.waiting_on_ref.entity_key == moved[0]["key"]
    closing = [
        line
        for line in firehose_path(canary).read_text("utf-8").splitlines()
        if MIGRATED_CHOICE in line and LEGACY_URN in line
    ]
    assert len(closing) == 1
