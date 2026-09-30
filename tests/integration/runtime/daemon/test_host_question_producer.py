"""The host question, approval and tool-error producers, through the real daemon verbs.

RUN-062 names the runtime adapters the producers of a Run's question, approval and
error lines; PLAN-045 says a question is never expired and renders what would end it;
RUN-051 says a provider permission resolves on its own record with the provider's
deadline. A disposable epoch-2 canary holds one running Run on a host session, and
every case drives the verb the host's hook reaches, then reads the Run's stream and the
run ledger back.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.identity import EntityKind, format_qualified_urn, parse_qualified_urn
from eawf.kernel.projection.transcript import block_body, build_transcript_blocks
from eawf.kernel.runtime.events import (
    ErrorPayload,
    QuestionActionPayload,
    RunEventKind,
    RunEventRecord,
)
from eawf.kernel.state.enums import OpenQuestionStatus
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_error import HOST_ERROR_OBSERVE_METHOD
from eawf.runtime.daemon.methods.host_question import (
    HOST_QUESTION_ANSWER_METHOD,
    HOST_QUESTION_RAISE_METHOD,
    HostQuestionLine,
)
from eawf.runtime.daemon.methods.permission import (
    PERMISSION_DECIDE_METHOD,
    PERMISSION_EXPIRE_METHOD,
    PERMISSION_OPEN_METHOD,
)
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.runtimes.host_transcript import WITHHELD_TEXT
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    root_context,
    seed,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    OPERATOR,
    RUN_KEY,
    RUN_URN,
    SESSION,
    _running_run,
    request,
)

pytestmark = pytest.mark.integration

#: The host's id of the asking call.
CALL: Final = "toolu_01ask"

#: A string the leak scan reads as a token, built here so no token-shaped literal is kept.
TOKEN_SHAPE: Final = "ghp_" + "a" * 36

#: What the call asks.
QUESTIONS: Final = [
    {"question": "Which storage backend should the cache use?", "options": ["SQLite", "Redis"]},
    {"question": "Name the new module.", "options": []},
]


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run on the host session."""
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: _running_run(SESSION)}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    """One daemon context shared by every call of a case."""
    return method_context(tmp_path / "runtime")


def call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> Any:
    """Dispatch one daemon verb against the canary, the way a client does."""
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def ask(ctx: MethodContext, canary: CanaryProvision, method: str, **extra: Any) -> dict[str, Any]:
    """Report the question call, as the question hook does before or after it runs."""
    answer: dict[str, Any] = call(
        method,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        tool_use_id=CALL,
        questions=QUESTIONS,
        **extra,
    )
    return answer


def stream(canary: CanaryProvision, tmp_path: Path) -> tuple[RunEventRecord, ...]:
    """Return the Run's stream lines, in ledger order."""
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    return run_events_of(records, parse_qualified_urn(RUN_URN))


def questions(canary: CanaryProvision, tmp_path: Path) -> dict[str, HostQuestionLine]:
    """Return the latest revision of every host question line, by key."""
    context = root_context(canary, tmp_path / "runtime")
    with context.session([RUN_URN]) as session:
        records = read_ledger_records(session.ledger_path(Epoch2Collection.RUN))
    latest: dict[str, HostQuestionLine] = {}
    for record in records:
        if record.payload.get("payload_kind") == "host_question":
            line = HostQuestionLine.model_validate(record.payload)
            latest[line.question.key] = line
    return latest


def kinds(lines: tuple[RunEventRecord, ...]) -> list[str]:
    return [line.event_kind.value for line in lines]


def test_plan_045_a_raised_question_is_blocking_and_waits_open(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """Each question of the call is a blocking record with no default and no deadline."""
    answer = ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    assert [ref.rsplit("/", 1)[-1] for ref in answer["question_refs"]] == ["QST-0001", "QST-0002"]
    held = questions(canary, tmp_path)
    first, second = held["QST-0001"].question, held["QST-0002"].question
    assert first.status is OpenQuestionStatus.BLOCKED and first.blocking
    assert first.default_key is None and first.override_until is None
    assert [o.label for o in first.options] == ["SQLite", "Redis"]
    assert second.options == ()
    lines = stream(canary, tmp_path)
    assert kinds(lines) == ["question_raised", "question_raised"]
    blocks, _ = build_transcript_blocks(lines)
    assert dict(blocks[0].body)["WAITS ON"] == "an answer to QST-0001 · open"
    assert all(block.in_flight for block in blocks)


def test_a_retried_raise_records_nothing_twice(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    again = ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    assert len(again["question_refs"]) == 2
    assert sorted(questions(canary, tmp_path)) == ["QST-0001", "QST-0002"]
    assert kinds(stream(canary, tmp_path)) == ["question_raised", "question_raised"]


def test_run_062_an_answer_chooses_an_option_or_records_a_reply(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """An offered label chooses its option; any other words are the operator's reply."""
    ask(ctx, canary, HOST_QUESTION_RAISE_METHOD)

    answer = ask(
        ctx,
        canary,
        HOST_QUESTION_ANSWER_METHOD,
        answers={QUESTIONS[0]["question"]: "Redis", QUESTIONS[1]["question"]: "cache_store"},
    )

    assert answer["answered"] == 2
    held = questions(canary, tmp_path)
    chosen, replied = held["QST-0001"].question, held["QST-0002"].question
    assert chosen.status is OpenQuestionStatus.ANSWERED
    assert chosen.chosen_option_key == "option_2"
    assert chosen.resolution_actor == "HARNESS-CLAUDE-CODE"
    assert replied.chosen_option_key is None
    assert replied.reply is not None and replied.reply.text == "cache_store"
    resolved = [
        line for line in stream(canary, tmp_path) if line.event_kind.value.endswith("resolved")
    ]
    assert [line.event_kind for line in resolved] == [RunEventKind.APPROVAL_RESOLVED] * 2
    payload = resolved[0].payload
    assert isinstance(payload, QuestionActionPayload)
    assert payload.choice_key == "option_2"
    assert payload.receipt_ref is not None and payload.receipt_ref.startswith("receipt-")
    assert not any(b.in_flight for b in build_transcript_blocks(stream(canary, tmp_path))[0])


def test_an_answer_whose_raise_never_arrived_raises_first(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    ask(ctx, canary, HOST_QUESTION_ANSWER_METHOD, answers={QUESTIONS[0]["question"]: "SQLite"})

    assert kinds(stream(canary, tmp_path)) == [
        "question_raised",
        "question_raised",
        "approval_resolved",
    ]
    assert questions(canary, tmp_path)["QST-0002"].question.status is OpenQuestionStatus.BLOCKED


def test_a_repeated_answer_writes_nothing_twice(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    answers = {QUESTIONS[0]["question"]: "SQLite"}
    ask(ctx, canary, HOST_QUESTION_ANSWER_METHOD, answers=answers)

    again = ask(ctx, canary, HOST_QUESTION_ANSWER_METHOD, answers=answers)

    assert again["answered"] == 0
    assert kinds(stream(canary, tmp_path)).count("approval_resolved") == 1


def test_words_carrying_a_leak_shape_are_withheld(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    call(
        HOST_QUESTION_RAISE_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=SESSION,
        tool_use_id=CALL,
        questions=[{"question": f"Use the token {TOKEN_SHAPE}?", "options": []}],
    )

    assert questions(canary, tmp_path)["QST-0001"].question.question == WITHHELD_TEXT


def test_a_question_on_no_live_run_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(
            HOST_QUESTION_RAISE_METHOD,
            ctx,
            canary,
            harness="claude-code",
            host_session_id="another-session",
            tool_use_id=CALL,
            questions=QUESTIONS,
        )


@pytest.mark.parametrize(
    "params",
    [
        {"questions": []},
        {"questions": [{"question": "", "options": []}]},
        {"questions": QUESTIONS, "stray": 1},
        {"questions": [{"question": "Too many?", "options": ["a", "b", "c", "d", "e"]}]},
    ],
    ids=["empty", "blank-question", "unknown-field", "five-options"],
)
def test_a_malformed_question_call_is_refused(
    canary: CanaryProvision, ctx: MethodContext, params: dict[str, Any]
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(
            HOST_QUESTION_RAISE_METHOD,
            ctx,
            canary,
            harness="claude-code",
            host_session_id=SESSION,
            tool_use_id=CALL,
            **params,
        )


def test_run_051_run_062_a_held_call_is_requested_then_resolved_on_the_stream(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """The permission's open and its decision each state a line naming the permission."""
    held = request(ctx, canary)["permission"]

    call(
        PERMISSION_DECIDE_METHOD,
        ctx,
        canary,
        urn=held["urn"],
        verb="deny",
        principal_class="operator",
        actor=OPERATOR,
        expected_revision=1,
    )

    lines = stream(canary, tmp_path)
    assert kinds(lines) == ["approval_requested", "approval_resolved"]
    requested, resolved = (line.payload for line in lines)
    assert isinstance(requested, QuestionActionPayload)
    assert isinstance(resolved, QuestionActionPayload)
    assert str(requested.subject_ref) == held["urn"] == str(resolved.subject_ref)
    assert resolved.choice_key == "denied" and resolved.receipt_ref is not None
    assert lines[1].actor == OPERATOR
    deadline = datetime.fromisoformat(held["deadline_at"])
    blocks, _ = build_transcript_blocks(lines, deadlines={held["urn"]: deadline})
    assert dict(blocks[0].body)["WAITS ON"] == (
        f"an answer to PERM-0001 · deadline {deadline.astimezone(UTC):%H:%M:%S} UTC"
    )
    assert dict(blocks[1].body)["ANSWERED"].startswith("denied · under receipt receipt-")


def test_run_051_a_lapse_is_stated_as_the_provider_expiring_the_call(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    deadline = datetime.now(UTC) + timedelta(seconds=1)
    call(
        PERMISSION_OPEN_METHOD,
        ctx,
        canary,
        urn=RUN_URN,
        call_ref="call-0123456789abcdef",
        tool_id="bash",
        action_class="tool",
        request_scope="Run the suite",
        deadline_at=deadline.isoformat(),
        approval_authority={"approve": ["operator"], "deny": ["operator"]},
    )
    while datetime.now(UTC) <= deadline:
        asyncio.run(asyncio.sleep(0.05))

    call(PERMISSION_EXPIRE_METHOD, ctx, canary, urn=RUN_URN)

    lines = stream(canary, tmp_path)
    assert kinds(lines) == ["approval_requested", "approval_resolved"]
    expired = lines[1].payload
    assert isinstance(expired, QuestionActionPayload) and expired.choice_key == "expired"
    assert lines[1].actor == "EAWFD"


def test_a_permission_whose_deadline_went_unread_says_so() -> None:
    """A permission block the caller read no deadline for never claims to be open."""
    permission = format_qualified_urn(
        workspace_key="WSP-MAIN",
        project_key="PRJ-EAWF",
        repository_key=None,
        kind=EntityKind.PERMISSION,
        entity_key="PERM-0001",
    )
    line = RunEventRecord.model_validate(
        {
            "event_ref": "EVT-0123456789abcdef",
            "run_ref": RUN_URN,
            "run_sequence": 1,
            "event_kind": "approval_requested",
            "provenance": "daemon_observed",
            "payload": {
                "payload_kind": "question_action",
                "subject_ref": permission,
                "phase": "requested",
            },
            "actor": "EAWFD",
            "recorded_at": "2026-09-30T12:00:00+00:00",
        }
    )
    assert dict(block_body(line))["WAITS ON"] == "an answer to PERM-0001 · deadline not read"


@pytest.mark.parametrize(
    ("interrupted", "code", "retry"),
    [
        (False, "read.failed", "after_input_change"),
        (True, "read.interrupted", "transient_same_run"),
    ],
)
def test_run_062_a_failed_host_call_is_an_error_line(
    canary: CanaryProvision,
    ctx: MethodContext,
    tmp_path: Path,
    interrupted: bool,
    code: str,
    retry: str,
) -> None:
    params = {
        "harness": "claude-code",
        "host_session_id": SESSION,
        "tool_use_id": "toolu_02",
        "tool_name": "Read",
        "error": "File does not exist.",
        "interrupted": interrupted,
    }
    first = call(HOST_ERROR_OBSERVE_METHOD, ctx, canary, **params)

    again = call(HOST_ERROR_OBSERVE_METHOD, ctx, canary, **params)

    assert again["event_ref"] == first["event_ref"]
    (line,) = stream(canary, tmp_path)
    assert line.event_kind is RunEventKind.ERROR_OBSERVED
    assert isinstance(line.payload, ErrorPayload)
    assert (line.payload.code, line.payload.retry_class) == (code, retry)
    assert line.payload.message.endswith("File does not exist.")
    assert line.payload.diagnostic_ref is not None
    assert line.payload.diagnostic_ref.startswith("artifact://content/")
