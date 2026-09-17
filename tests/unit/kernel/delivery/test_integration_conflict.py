"""IntegrationConflict: the read-only frame a blocked attempt leaves behind."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.delivery.integration import (
    MAX_CONFLICT_SIDE_LINES,
    AgentAuthority,
    ConflictExit,
    ConflictExitKind,
    IntegrationAttempt,
    IntegrationConflict,
    PrincipalAuthority,
)
from eawf.kernel.delivery.receipts import RevisionRefKind, canonical_digest

REPOSITORY = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0001"
OTHER_BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0002"
FOREIGN_BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/batch/BAT-0001"
TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0004"
FOREIGN_TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-OTHER/task/EAWF-0004"
ACTION = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
EVIDENCE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
AT = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
LATER = datetime(2026, 9, 1, 12, 5, tzinfo=UTC)

#: One valid exit reference per exit kind.
EXIT_REFS: dict[str, str] = {
    "repair_task": TASK,
    "rebase_task": TASK,
    "operator_decision": ACTION,
}


def _side(**overrides: Any) -> dict[str, Any]:
    side: dict[str, Any] = {
        "authority": {"kind": "agent", "batch_ref": BATCH},
        "at": AT,
        "sha": "a" * 40,
        "lines": ["return parse(text)"],
    }
    return side | overrides


def _hunk(index: int) -> dict[str, Any]:
    return {
        "index": index,
        "ours": _side(),
        "theirs": _side(
            authority={"kind": "principal", "principal_key": "OPERATOR"},
            sha="b" * 40,
            lines=["return parse(text, strict=True)"],
        ),
    }


def _conflict(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": "INC-000001",
        "attempt_id": "INA-000004",
        "batch_ref": BATCH,
        "repository_ref": REPOSITORY,
        "branch": "eawf/delivery/BAT-0001",
        "ahead": 2,
        "behind": 1,
        "files": [{"path": "src/pkg/loader.py", "hunks": [_hunk(1), _hunk(2)]}],
        "exit": {"kind": "repair_task", "ref": TASK},
    }
    return payload | overrides


def _binding(generation: int) -> dict[str, Any]:
    return {
        "repository_ref": REPOSITORY,
        "ref_kind": RevisionRefKind.INTEGRATION,
        "head_sha": f"{generation}" * 40,
        "tree_sha": "c" * 40,
        "parent_sha": None,
        "batch_ref": BATCH,
        "integration_generation": generation,
        "manifest_digest": canonical_digest("manifest"),
        "criteria_digest": canonical_digest("criteria"),
        "policy_digest": canonical_digest("policy"),
        "environment_digest": None,
        "bound_at": AT,
    }


def _blocked_attempt(**overrides: Any) -> IntegrationAttempt:
    payload: dict[str, Any] = {
        "id": "INA-000004",
        "operation_attempt_id": "OPR-INT-000004",
        "batch_ref": BATCH,
        "task_ref": TASK,
        "candidate_bundle_id": "CB-00000013",
        "generation": 4,
        "source_base": _binding(3),
        "selected_batch_base": _binding(3),
        "candidate_patch_digest": canonical_digest("patch"),
        "candidate_tree_digest": canonical_digest("tree"),
        "changed_paths": ["src/pkg/loader.py", "tests/test_loader.py"],
        "idempotency_key": "BAT-0001-EAWF-0004-CB-E04",
        "status": "BLOCKED",
        "failure_kind": "conflict",
        "diagnostic_ref": EVIDENCE,
        "requested_at": AT,
        "updated_at": LATER,
        "terminal_at": LATER,
    }
    return IntegrationAttempt.model_validate(payload | overrides)


# ---- typed exit ---------------------------------------------------------------


@pytest.mark.parametrize("kind", ["repair_task", "rebase_task", "operator_decision"])
def test_integration_conflict_accepts_each_typed_exit(kind: str) -> None:
    conflict = IntegrationConflict.model_validate(
        _conflict(exit={"kind": kind, "ref": EXIT_REFS[kind]})
    )

    assert conflict.exit.kind is ConflictExitKind(kind)
    assert str(conflict.exit.ref) == EXIT_REFS[kind]


def test_conflict_exit_kind_is_exactly_the_three_exits() -> None:
    assert {kind.value for kind in ConflictExitKind} == {
        "repair_task",
        "rebase_task",
        "operator_decision",
    }


@pytest.mark.parametrize("kind", ["resolve_in_git_tool", "manual_edit", "REPAIR_TASK", "", "retry"])
def test_integration_conflict_rejects_an_undeclared_exit_kind(kind: str) -> None:
    with pytest.raises(ValidationError, match=r"exit\.kind"):
        IntegrationConflict.model_validate(_conflict(exit={"kind": kind, "ref": TASK}))


def test_integration_conflict_rejects_a_missing_exit() -> None:
    payload = _conflict()
    del payload["exit"]

    with pytest.raises(ValidationError, match="exit"):
        IntegrationConflict.model_validate(payload)


def test_integration_conflict_rejects_a_null_exit() -> None:
    with pytest.raises(ValidationError, match="exit"):
        IntegrationConflict.model_validate(_conflict(exit=None))


def test_integration_conflict_rejects_a_bare_string_exit() -> None:
    """Error path: the exit is a typed record, not a label."""
    with pytest.raises(ValidationError, match="exit"):
        IntegrationConflict.model_validate(_conflict(exit="repair_task"))


@pytest.mark.parametrize(
    ("kind", "ref"),
    [
        ("repair_task", ACTION),
        ("rebase_task", BATCH),
        ("operator_decision", TASK),
    ],
)
def test_conflict_exit_rejects_a_reference_of_the_wrong_kind(kind: str, ref: str) -> None:
    with pytest.raises(ValidationError, match=f"exit {kind} must reference"):
        ConflictExit.model_validate({"kind": kind, "ref": ref})


def test_conflict_exit_rejects_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra"):
        ConflictExit.model_validate({"kind": "repair_task", "ref": TASK, "note": "x"})


def test_integration_conflict_rejects_a_task_exit_in_another_repository() -> None:
    with pytest.raises(ValidationError, match="exit task"):
        IntegrationConflict.model_validate(
            _conflict(exit={"kind": "rebase_task", "ref": FOREIGN_TASK})
        )


# ---- conflict frame -------------------------------------------------------------


def test_integration_conflict_keeps_both_sides_of_a_two_hunk_conflict() -> None:
    conflict = IntegrationConflict.model_validate(_conflict())

    (file,) = conflict.files
    assert [hunk.index for hunk in file.hunks] == [1, 2]
    first = file.hunks[0]
    assert isinstance(first.ours.authority, AgentAuthority)
    assert isinstance(first.theirs.authority, PrincipalAuthority)
    assert first.ours.lines != first.theirs.lines
    assert conflict.cleared_at is None


def test_integration_conflict_records_the_cleared_case() -> None:
    conflict = IntegrationConflict.model_validate(_conflict(cleared_at=LATER))

    assert conflict.cleared_at == LATER


def test_integration_conflict_is_immutable() -> None:
    conflict = IntegrationConflict.model_validate(_conflict())

    with pytest.raises(ValidationError, match="frozen"):
        conflict.ahead = 5


def test_integration_conflict_rejects_hunks_out_of_order() -> None:
    files = [{"path": "src/pkg/loader.py", "hunks": [_hunk(2), _hunk(1)]}]

    with pytest.raises(ValidationError, match="hunk indexes"):
        IntegrationConflict.model_validate(_conflict(files=files))


def test_integration_conflict_rejects_a_hunk_index_gap() -> None:
    files = [{"path": "src/pkg/loader.py", "hunks": [_hunk(1), _hunk(3)]}]

    with pytest.raises(ValidationError, match="hunk indexes"):
        IntegrationConflict.model_validate(_conflict(files=files))


def test_integration_conflict_rejects_a_file_without_hunks() -> None:
    files = [{"path": "src/pkg/loader.py", "hunks": []}]

    with pytest.raises(ValidationError, match="hunks"):
        IntegrationConflict.model_validate(_conflict(files=files))


def test_integration_conflict_rejects_an_empty_file_list() -> None:
    with pytest.raises(ValidationError, match="files"):
        IntegrationConflict.model_validate(_conflict(files=[]))


def test_integration_conflict_rejects_a_repeated_path() -> None:
    files = [
        {"path": "src/pkg/loader.py", "hunks": [_hunk(1)]},
        {"path": "src/pkg/loader.py", "hunks": [_hunk(1)]},
    ]

    with pytest.raises(ValidationError, match="files repeats"):
        IntegrationConflict.model_validate(_conflict(files=files))


@pytest.mark.parametrize(
    "path", ["/src/pkg/loader.py", "../loader.py", "src//loader.py", "src\\loader.py", ""]
)
def test_integration_conflict_rejects_a_path_outside_the_repository(path: str) -> None:
    files = [{"path": path, "hunks": [_hunk(1)]}]

    with pytest.raises(ValidationError, match="path"):
        IntegrationConflict.model_validate(_conflict(files=files))


def test_integration_conflict_rejects_a_display_name_authority() -> None:
    hunk = _hunk(1)
    hunk["theirs"]["authority"] = {"kind": "principal", "principal_key": "Jane Operator"}

    with pytest.raises(ValidationError, match="principal_key"):
        IntegrationConflict.model_validate(_conflict(files=[{"path": "a.py", "hunks": [hunk]}]))


def test_integration_conflict_rejects_an_untyped_authority() -> None:
    hunk = _hunk(1)
    hunk["ours"]["authority"] = "the agent"

    with pytest.raises(ValidationError, match="authority"):
        IntegrationConflict.model_validate(_conflict(files=[{"path": "a.py", "hunks": [hunk]}]))


def test_integration_conflict_accepts_an_empty_side() -> None:
    """Boundary: a side that deleted the region carries no lines."""
    hunk = _hunk(1)
    hunk["theirs"]["lines"] = []

    conflict = IntegrationConflict.model_validate(
        _conflict(files=[{"path": "a.py", "hunks": [hunk]}])
    )

    assert conflict.files[0].hunks[0].theirs.lines == ()


def test_integration_conflict_accepts_a_side_at_the_line_bound() -> None:
    hunk = _hunk(1)
    hunk["ours"]["lines"] = ["x"] * MAX_CONFLICT_SIDE_LINES

    conflict = IntegrationConflict.model_validate(
        _conflict(files=[{"path": "a.py", "hunks": [hunk]}])
    )

    assert len(conflict.files[0].hunks[0].ours.lines) == MAX_CONFLICT_SIDE_LINES


def test_integration_conflict_rejects_a_side_past_the_line_bound() -> None:
    hunk = _hunk(1)
    hunk["ours"]["lines"] = ["x"] * (MAX_CONFLICT_SIDE_LINES + 1)

    with pytest.raises(ValidationError, match="lines"):
        IntegrationConflict.model_validate(_conflict(files=[{"path": "a.py", "hunks": [hunk]}]))


def test_integration_conflict_rejects_a_line_with_an_embedded_newline() -> None:
    hunk = _hunk(1)
    hunk["ours"]["lines"] = ["first\nsecond"]

    with pytest.raises(ValidationError, match="lines"):
        IntegrationConflict.model_validate(_conflict(files=[{"path": "a.py", "hunks": [hunk]}]))


def test_integration_conflict_accepts_zero_ahead_and_behind() -> None:
    conflict = IntegrationConflict.model_validate(_conflict(ahead=0, behind=0))

    assert (conflict.ahead, conflict.behind) == (0, 0)


def test_integration_conflict_rejects_a_negative_count() -> None:
    with pytest.raises(ValidationError, match="behind"):
        IntegrationConflict.model_validate(_conflict(behind=-1))


def test_integration_conflict_rejects_a_batch_outside_the_repository() -> None:
    with pytest.raises(ValidationError, match="is not under repository"):
        IntegrationConflict.model_validate(_conflict(batch_ref=FOREIGN_BATCH))


def test_integration_conflict_rejects_a_malformed_key() -> None:
    with pytest.raises(ValidationError, match="id"):
        IntegrationConflict.model_validate(_conflict(id="INC-1"))


# ---- binding to the blocked attempt ---------------------------------------------


def test_require_attempt_accepts_the_blocked_attempt() -> None:
    conflict = IntegrationConflict.model_validate(_conflict())

    conflict.require_attempt(_blocked_attempt())


def test_require_attempt_rejects_another_attempt() -> None:
    conflict = IntegrationConflict.model_validate(_conflict(attempt_id="INA-000005"))

    with pytest.raises(ValueError, match="does not describe attempt"):
        conflict.require_attempt(_blocked_attempt())


def test_require_attempt_rejects_another_batch() -> None:
    conflict = IntegrationConflict.model_validate(_conflict(batch_ref=OTHER_BATCH))

    with pytest.raises(ValueError, match="does not describe attempt"):
        conflict.require_attempt(_blocked_attempt())


def test_require_attempt_rejects_an_attempt_not_blocked_on_a_conflict() -> None:
    conflict = IntegrationConflict.model_validate(_conflict())
    failed = _blocked_attempt(status="FAILED", failure_kind="apply_error")

    with pytest.raises(ValueError, match="not blocked on a conflict"):
        conflict.require_attempt(failed)


def test_require_attempt_rejects_a_path_the_attempt_did_not_change() -> None:
    files = [{"path": "src/pkg/other.py", "hunks": [_hunk(1)]}]
    conflict = IntegrationConflict.model_validate(_conflict(files=files))

    with pytest.raises(ValueError, match="are not changed by"):
        conflict.require_attempt(_blocked_attempt())
