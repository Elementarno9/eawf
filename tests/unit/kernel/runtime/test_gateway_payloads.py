"""The tool, file, question and error payloads a transcript block is typed by.

RUN-062 and the packet's ``DiscriminatedEventPayload`` table: each of the four is frozen,
forbids extras, and carries exactly the fields its phase makes facts. A line of one of
these kinds now appends, so the producers that come next -- the gateway's tool calls, the
diff capture, the question and permission flow -- write lines the console already draws.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.events import (
    SUPPORTED_EVENT_KINDS,
    ErrorPayload,
    FileChangePayload,
    QuestionActionPayload,
    RunEventKind,
    RunEventRecord,
    ToolPayload,
)

CALL: Final = "call-0123456789abcdef"
RECEIPT: Final = "receipt-0123456789abcdef"
REPOSITORY: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
ACTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
BEFORE: Final = "sha256:" + "a" * 64
AFTER: Final = "sha256:" + "b" * 64
DIFF: Final = "artifact://runs/diff-0001"


def _file(**overrides: Any) -> FileChangePayload:
    fields = {
        "repository_ref": REPOSITORY,
        "changed_paths": ("src/replay.py",),
        "before_tree_digest": BEFORE,
        "after_tree_digest": AFTER,
        "diff_ref": DIFF,
    }
    return FileChangePayload.model_validate(fields | overrides)


def _record(kind: RunEventKind, payload: Any) -> RunEventRecord:
    return RunEventRecord.model_validate(
        {
            "event_ref": "EVT-00000001",
            "run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010",
            "run_sequence": 1,
            "event_kind": kind,
            "provenance": "provider_native",
            "payload": payload,
            "actor": "OP-0001",
            "recorded_at": datetime(2026, 9, 30, 12, 0, tzinfo=UTC),
        }
    )


def test_run_062_the_four_kinds_now_append() -> None:
    assert {
        RunEventKind.TOOL_REQUESTED,
        RunEventKind.TOOL_ACCEPTED,
        RunEventKind.TOOL_RESULT,
        RunEventKind.FILE_CHANGED,
        RunEventKind.DIFF_SUMMARIZED,
        RunEventKind.QUESTION_RAISED,
        RunEventKind.APPROVAL_REQUESTED,
        RunEventKind.APPROVAL_RESOLVED,
        RunEventKind.ERROR_OBSERVED,
    } <= SUPPORTED_EVENT_KINDS


# ---------- tool ----------


@pytest.mark.parametrize(
    ("outcome", "valid"),
    [
        ({"result_ref": RECEIPT}, True),
        ({"error_code": "SCOPE_DENIED"}, True),
        ({}, False),
        ({"result_ref": RECEIPT, "error_code": "SCOPE_DENIED"}, False),
    ],
    ids=["receipt", "error", "neither", "both"],
)
def test_run_062_a_tool_result_names_exactly_one_outcome(
    outcome: dict[str, Any], valid: bool
) -> None:
    fields = {"call_ref": CALL, "tool_id": "repo_read", "phase": "result", **outcome}
    if valid:
        assert ToolPayload.model_validate(fields).phase == "result"
    else:
        with pytest.raises(ValidationError, match="exactly one"):
            ToolPayload.model_validate(fields)


@pytest.mark.parametrize("phase", ["requested", "accepted"])
def test_run_062_an_earlier_tool_phase_names_no_outcome(phase: str) -> None:
    with pytest.raises(ValidationError, match="no receipt or error yet"):
        ToolPayload(call_ref=CALL, tool_id="repo_read", phase=phase, result_ref=RECEIPT)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "fields",
    [
        {"call_ref": "call-short", "tool_id": "repo_read", "phase": "requested"},
        {"call_ref": CALL, "tool_id": "Repo Read", "phase": "requested"},
        {"call_ref": CALL, "tool_id": "repo_read", "phase": "done"},
        {"call_ref": CALL, "tool_id": "repo_read", "phase": "requested", "extra": 1},
        {"tool_id": "repo_read", "phase": "requested"},
    ],
    ids=["bad-call-ref", "bad-tool-id", "bad-phase", "extra-key", "missing-call"],
)
def test_run_062_a_malformed_tool_payload_is_refused(fields: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        ToolPayload.model_validate(fields)


def test_run_062_a_tool_line_is_pinned_to_its_kinds_phase() -> None:
    accepted = ToolPayload(call_ref=CALL, tool_id="repo_read", phase="accepted")
    assert _record(RunEventKind.TOOL_ACCEPTED, accepted).payload == accepted
    with pytest.raises(ValidationError, match="'requested' phase"):
        _record(RunEventKind.TOOL_REQUESTED, accepted)


# ---------- file ----------


def test_run_062_a_file_change_needs_one_path() -> None:
    assert _file().changed_paths == ("src/replay.py",)
    with pytest.raises(ValidationError):
        _file(changed_paths=())


@pytest.mark.parametrize("path", ["/etc/passwd", "../outside.py", "src/../../x.py"])
def test_run_062_a_file_change_path_stays_inside_the_repository(path: str) -> None:
    with pytest.raises(ValidationError):
        _file(changed_paths=(path,))


@pytest.mark.parametrize(
    "overrides",
    [
        {"before_tree_digest": "a" * 64},
        {"diff_ref": "diff-0001"},
        {"repository_ref": "REP-EAWF"},
        {"summary_only": "yes"},
    ],
    ids=["bare-digest", "diff-not-an-artifact", "repository-not-a-urn", "summary-not-a-bool"],
)
def test_run_062_a_malformed_file_change_is_refused(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError):
        _file(**overrides)


def test_run_062_a_file_line_carries_the_file_change_payload() -> None:
    assert _record(RunEventKind.DIFF_SUMMARIZED, _file()).event_kind is RunEventKind.DIFF_SUMMARIZED


# ---------- question ----------


@pytest.mark.parametrize("subject", [QUESTION, ACTION])
def test_run_062_a_question_is_about_an_open_question_or_a_pending_action(subject: str) -> None:
    raised = QuestionActionPayload.model_validate({"subject_ref": subject, "phase": "raised"})
    assert str(raised.subject_ref) == subject


def test_run_062_a_question_about_another_entity_is_refused() -> None:
    with pytest.raises(ValidationError):
        QuestionActionPayload.model_validate(
            {"subject_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010", "phase": "raised"}
        )


def test_run_062_only_a_resolution_names_its_receipt() -> None:
    resolved = QuestionActionPayload.model_validate(
        {"subject_ref": QUESTION, "phase": "resolved", "choice_key": "keep", "receipt_ref": RECEIPT}
    )
    assert resolved.receipt_ref == RECEIPT
    with pytest.raises(ValidationError, match="names the receipt"):
        QuestionActionPayload.model_validate({"subject_ref": QUESTION, "phase": "resolved"})
    with pytest.raises(ValidationError, match="no receipt yet"):
        QuestionActionPayload.model_validate(
            {"subject_ref": QUESTION, "phase": "raised", "receipt_ref": RECEIPT}
        )


def test_run_062_a_question_line_is_pinned_to_its_kinds_phase() -> None:
    raised = QuestionActionPayload.model_validate({"subject_ref": QUESTION, "phase": "raised"})
    with pytest.raises(ValidationError, match="'requested' phase"):
        _record(RunEventKind.APPROVAL_REQUESTED, raised)


# ---------- error ----------


def test_run_062_an_error_states_its_retry_class() -> None:
    error = ErrorPayload(code="test_failed", retry_class="after_input_change", message="2 failed")
    assert _record(RunEventKind.ERROR_OBSERVED, error).payload == error


@pytest.mark.parametrize(
    "overrides",
    [
        {"retry_class": "sometimes"},
        {"code": "/etc/passwd"},
        {"code": ""},
        {"message": "x" * 5000},
        {"diagnostic_ref": "trace.log"},
    ],
    ids=["unknown-retry-class", "path-as-code", "empty-code", "unbounded-message", "bad-trace"],
)
def test_run_062_a_malformed_error_is_refused(overrides: dict[str, Any]) -> None:
    fields = {"code": "test_failed", "retry_class": "never", "message": "2 failed"}
    with pytest.raises(ValidationError):
        ErrorPayload.model_validate(fields | overrides)
