"""The question and pause details draw the situation the daemon's projection computed.

CON-130 says a decision overlay's ``STATE`` and ``ENDS WHEN`` are facts of the projection
V07-PLAN binds, not something the console re-derives: a question the daemon projected as
unanswerable, or a pause it projected as waiting on a check whose outcome is unknown, is
drawn so even when the console's own copy of the Run's state says otherwise, and the keys
the detail offers follow that projection.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Final

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.state.epoch2.pause import (
    OpenPause,
    PauseReason,
    PauseSituation,
    PauseStatus,
    ResumePredicate,
)
from eawf.kernel.state.epoch2.question import OpenQuestion, QuestionOption, QuestionSituation
from eawf.kernel.state.epoch2.values import EntityOrigin
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusalCode
from eawf.runtime.daemon.methods import question as daemon_question
from eawf.surfaces.tui.console.decisions import PauseRecord, QuestionRecord, QuestionStatus
from eawf.surfaces.tui.console.operations import QUESTION_ANSWER_METHOD, STALE_REVISION_CODE
from eawf.surfaces.tui.console.overlays.situations import (
    answerable,
    pause_situation,
    question_situation,
    unknown_outcome,
)

AT: Final = datetime(2026, 9, 30, 12, 0, tzinfo=UTC)
RUN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"


def _question() -> OpenQuestion:
    return OpenQuestion(
        uid=uuid.uuid4(),
        key="QST-0001",
        urn=parse_qualified_urn(QUESTION),
        origin=EntityOrigin(kind="native", mapping_basis="native", confidence="exact"),
        revision=1,
        created_at=AT,
        updated_at=AT,
        scope_ref=parse_qualified_urn(RUN),
        question="Which storage backend should the cache use?",
        options=(
            QuestionOption(key="option_1", label="SQLite"),
            QuestionOption(key="option_2", label="Redis"),
        ),
        blocking=True,
        status=OpenQuestionStatus.BLOCKED,
    )


def test_con_130_con_131_a_question_draws_the_situation_the_daemon_projected() -> None:
    record = QuestionRecord.of_question(
        _question(), asked_by_run="RUN-00000010", situation=QuestionSituation.UNANSWERABLE
    )

    assert record.status is QuestionStatus.BLOCKED
    drawn = question_situation(record, principal="OP-0001", run_state="RUNNING")
    assert drawn.name == "unanswerable · recover RUN-00000010 first"
    assert drawn.ends == "the asking Run is recovered or let go"
    assert not answerable(record, "RUNNING")


def test_con_130_a_question_the_daemon_projected_open_is_answerable_by_its_options() -> None:
    record = QuestionRecord.of_question(
        _question(), asked_by_run="RUN-00000010", situation=QuestionSituation.OPEN_BLOCKING
    )

    assert question_situation(record, principal=None, run_state=None).name == "open · blocking"
    assert answerable(record, None)
    assert [option.label for option in record.options] == ["SQLite", "Redis"]


def test_con_130_con_132_a_pause_draws_the_situation_the_daemon_projected() -> None:
    pause = OpenPause(
        key="PAU-0001",
        scope_ref=parse_qualified_urn(RUN),
        reason=PauseReason.PROVIDER,
        health_evidence_refs=(parse_qualified_urn(EVIDENCE),),
        resume_predicate=ResumePredicate(
            description="a heartbeat at or after the anchor", evaluator="the lease watcher"
        ),
        status=PauseStatus.OPEN,
        opened_at=AT,
    )
    record = PauseRecord.of_pause(pause, situation=PauseSituation.CONTROL_OUTCOME_UNKNOWN)

    assert unknown_outcome(record, "RUNNING")
    drawn = pause_situation(record, "RUNNING")
    assert drawn.name == "waiting on a check · the control outcome is unknown"
    assert record.evaluator == "the lease watcher"


def test_the_console_spells_the_answer_verb_and_the_stale_code_as_the_daemon_does() -> None:
    assert daemon_question.QUESTION_ANSWER_METHOD == QUESTION_ANSWER_METHOD
    assert TransactionRefusalCode.REVISION_CONFLICT.value == STALE_REVISION_CODE
