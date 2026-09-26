"""The semantic tool catalog is closed and typed, and its errors are total.

The runtime agreement states the tool contract as thirteen rows, and its
repository read row names three tools, so the catalog is fifteen ids
under one contract each. Every id carries a typed input model and a typed
output model, both frozen and closed, because the same models produce the
gateway's validator and the schema the per-Run MCP server publishes.

:class:`SemanticToolError` is pinned from both sides: the code set is
closed at fourteen members, the retry-class set at five, and the table
between them is total and single-valued, so a handler cannot label a
policy denial transient and invite a retry loop.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.semantic import (
    RETRY_CLASS_BY_CODE,
    SEMANTIC_TOOL_CATALOG,
    AskOperatorInput,
    AskOperatorOutput,
    RepoReadInput,
    RetryClass,
    RunScopedCommandOutput,
    SemanticToolError,
    SemanticToolErrorCode,
    SemanticToolId,
    SemanticToolInputBase,
    SemanticToolOutputBase,
    SubmitEvidenceOutput,
    SubmitReportOutput,
    error_for,
    tool_contract,
)

pytestmark = pytest.mark.unit

CATALOG_TOOL_IDS = (
    "eawf_state_query",
    "repo_read",
    "repo_search",
    "diff_read",
    "workspace_apply_patch",
    "run_scoped_command",
    "report_progress",
    "attach_evidence",
    "ask_operator",
    "submit_plan",
    "submit_coordination_proposal",
    "submit_candidate",
    "submit_report",
    "submit_evidence",
    "budget_status",
)
ERROR_CODES = (
    "RUN_NOT_ACTIVE",
    "CONTRACT_MISMATCH",
    "CAPABILITY_DENIED",
    "SCOPE_DENIED",
    "LEASE_NOT_ACTIVE",
    "STALE_WORKSPACE_GENERATION",
    "BUDGET_EXHAUSTED",
    "POLICY_REVOKED",
    "CERTIFICATION_REVOKED",
    "PAYLOAD_INVALID",
    "IDEMPOTENCY_PAYLOAD_MISMATCH",
    "PROTECTED_ACTION_REQUIRED",
    "PROVIDER_CONTINUITY_UNKNOWN",
    "INTERNAL_TRANSIENT",
)
RETRY_CLASSES = (
    "never",
    "after_input_change",
    "after_policy_change",
    "transient_same_run",
    "new_linked_run",
)
#: The two tools that write, and so bind to a mutating task-scoped Run.
WRITING_TOOLS = ("workspace_apply_patch", "submit_candidate")
HANDLE = f"wsh-{'f' * 32}"
DIGEST = f"sha256:{'a' * 64}"
ARTIFACT = "artifact://log/run-10"
SUBJECT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"


def _repo_read(**overrides: Any) -> dict[str, Any]:
    """Return a valid repository-read payload with *overrides* applied."""
    return {"tool_id": "repo_read", "resource_handle": HANDLE, "path": "src/eawf/x.py", **overrides}


def _option(option_id: str) -> dict[str, str]:
    """Return one operator option keyed on *option_id*."""
    return {"option_id": option_id, "summary": "do the thing", "effect": "the thing is done"}


def _ask_operator(**overrides: Any) -> dict[str, Any]:
    """Return a valid ask-operator payload with *overrides* applied."""
    return {
        "tool_id": "ask_operator",
        "question_kind": "design_choice",
        "subject_ref": SUBJECT,
        "options": [_option("keep"), _option("replace")],
        "recommended_option_id": "keep",
        "reversible": True,
        "protected": False,
        **overrides,
    }


# ---- the catalog --------------------------------------------------------------


def test_catalog_is_the_closed_tool_set() -> None:
    assert tuple(SEMANTIC_TOOL_CATALOG) == tuple(SemanticToolId)
    assert tuple(tool.value for tool in SEMANTIC_TOOL_CATALOG) == CATALOG_TOOL_IDS


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_every_catalog_tool_carries_typed_input_and_output_models(tool_id: SemanticToolId) -> None:
    contract = SEMANTIC_TOOL_CATALOG[tool_id]
    assert issubclass(contract.input_model, SemanticToolInputBase)
    assert issubclass(contract.output_model, SemanticToolOutputBase)
    for model in (contract.input_model, contract.output_model):
        assert model.model_config["extra"] == "forbid"
        assert model.model_config["frozen"] is True
        assert model.model_fields["tool_id"].annotation is not None
    assert contract.schema_version == "1.0.0"


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_each_model_answers_only_its_own_tool(tool_id: SemanticToolId) -> None:
    contract = SEMANTIC_TOOL_CATALOG[tool_id]
    for model in (contract.input_model, contract.output_model):
        assert model.model_json_schema()["properties"]["tool_id"]["const"] == tool_id.value
        with pytest.raises(ValidationError):
            model.model_validate({"tool_id": "not_a_tool"})


def test_only_the_writing_tools_require_a_mutating_task() -> None:
    writing = tuple(
        tool.value for tool, row in SEMANTIC_TOOL_CATALOG.items() if row.requires_mutating_task
    )
    assert writing == WRITING_TOOLS


def test_tool_contract_accepts_a_plain_string_id() -> None:
    assert tool_contract("repo_read").tool_id is SemanticToolId.REPO_READ


@pytest.mark.parametrize("tool_id", ["", "repo", "request_integration", "REPO_READ"])
def test_tool_contract_refuses_an_uncatalogued_tool(tool_id: str) -> None:
    with pytest.raises(KeyError, match="names no catalog tool"):
        tool_contract(tool_id)


# ---- the error set ------------------------------------------------------------


def test_error_code_and_retry_class_sets_are_closed() -> None:
    assert tuple(code.value for code in SemanticToolErrorCode) == ERROR_CODES
    assert tuple(retry.value for retry in RetryClass) == RETRY_CLASSES


def test_retry_class_table_is_total_and_uses_every_class() -> None:
    assert tuple(RETRY_CLASS_BY_CODE) == tuple(SemanticToolErrorCode)
    assert set(RETRY_CLASS_BY_CODE.values()) == set(RetryClass)


@pytest.mark.parametrize("code", list(SemanticToolErrorCode), ids=lambda code: code.value)
def test_error_for_carries_the_retry_class_the_code_implies(code: SemanticToolErrorCode) -> None:
    error = error_for(code, message="refused")
    assert error.retry_class is RETRY_CLASS_BY_CODE[code]
    assert error.field_path is None


@pytest.mark.parametrize("code", ["", "nope", "run_not_active", "PAYLOAD_TOO_BIG"])
def test_semantic_tool_error_refuses_an_uncatalogued_code(code: str) -> None:
    with pytest.raises(ValidationError):
        SemanticToolError(code=code, retry_class=RetryClass.NEVER, message="refused")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("code", "retry_class"),
    [
        (SemanticToolErrorCode.CAPABILITY_DENIED, RetryClass.TRANSIENT_SAME_RUN),
        (SemanticToolErrorCode.SCOPE_DENIED, RetryClass.AFTER_INPUT_CHANGE),
        (SemanticToolErrorCode.INTERNAL_TRANSIENT, RetryClass.NEVER),
    ],
)
def test_semantic_tool_error_refuses_a_retry_class_its_code_denies(
    code: SemanticToolErrorCode, retry_class: RetryClass
) -> None:
    with pytest.raises(ValidationError, match="implies retry_class"):
        SemanticToolError(code=code, retry_class=retry_class, message="refused")


@pytest.mark.parametrize(
    "message",
    ["run `git status`", "fix with $(pwd)", "retry && push", "a || b", "run sudo make install"],
)
def test_semantic_tool_error_refuses_a_shell_remedy(message: str) -> None:
    with pytest.raises(ValidationError, match="shell remedy"):
        error_for(SemanticToolErrorCode.PAYLOAD_INVALID, message=message)


@pytest.mark.parametrize(
    ("message", "admitted"),
    [("", False), ("x", True), ("y" * 500, True), ("z" * 501, False)],
    ids=["empty", "single", "max-length", "over-length"],
)
def test_semantic_tool_error_message_is_bounded(message: str, admitted: bool) -> None:
    if admitted:
        assert error_for(SemanticToolErrorCode.PAYLOAD_INVALID, message=message).message == message
        return
    with pytest.raises(ValidationError):
        error_for(SemanticToolErrorCode.PAYLOAD_INVALID, message=message)


@pytest.mark.parametrize("field_path", ["payload", "/Payload", "/payload/", ""])
def test_semantic_tool_error_refuses_a_malformed_field_path(field_path: str) -> None:
    with pytest.raises(ValidationError):
        error_for(SemanticToolErrorCode.PAYLOAD_INVALID, message="bad", field_path=field_path)


def test_semantic_tool_error_is_frozen() -> None:
    error = error_for(SemanticToolErrorCode.RUN_NOT_ACTIVE, message="the Run ended")
    with pytest.raises(ValidationError, match="frozen"):
        error.retry_class = RetryClass.TRANSIENT_SAME_RUN
    assert error.retry_class is RetryClass.NEVER


def test_semantic_tool_error_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SemanticToolError.model_validate(
            {
                "code": "PAYLOAD_INVALID",
                "retry_class": "after_input_change",
                "message": "bad",
                "remedy": "rerun",
            }
        )


# ---- typed payload rules ------------------------------------------------------


@pytest.mark.parametrize(
    ("byte_cap", "admitted"),
    [(0, False), (1, True), (1_048_576, True), (1_048_577, False)],
    ids=["zero", "single", "max", "over-max"],
)
def test_repo_read_byte_cap_is_bounded(byte_cap: int, admitted: bool) -> None:
    document = _repo_read(byte_cap=byte_cap)
    if admitted:
        assert RepoReadInput.model_validate(document).byte_cap == byte_cap
        return
    with pytest.raises(ValidationError):
        RepoReadInput.model_validate(document)


@pytest.mark.parametrize(
    ("path", "admitted"),
    [
        ("", False),
        ("a", True),
        ("../outside.py", False),
        ("src/../../outside.py", False),
        ("/etc/passwd", False),
        ("b" * 500, True),
        ("b" * 501, False),
    ],
    ids=["empty", "single", "escaping", "escaping-inner", "absolute", "max-length", "over-length"],
)
def test_repo_read_path_stays_inside_the_repository(path: str, admitted: bool) -> None:
    document = _repo_read(path=path)
    if admitted:
        assert RepoReadInput.model_validate(document).path == path
        return
    with pytest.raises(ValidationError):
        RepoReadInput.model_validate(document)


@pytest.mark.parametrize(
    "resource_handle",
    ["", "/srv/checkout/eawf", "wsh-zzzz", f"wsh-{'f' * 31}", f"wsh-{'f' * 33}"],
)
def test_repo_read_refuses_a_handle_that_is_not_opaque(resource_handle: str) -> None:
    with pytest.raises(ValidationError):
        RepoReadInput.model_validate(_repo_read(resource_handle=resource_handle))


def test_repo_read_refuses_a_non_integer_byte_cap() -> None:
    with pytest.raises(ValidationError, match="int_type"):
        RepoReadInput.model_validate(_repo_read(byte_cap="65536"))


def test_repo_read_refuses_an_unknown_field() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        RepoReadInput.model_validate(_repo_read(follow_symlinks=True))


def test_repo_read_refuses_a_missing_required_field() -> None:
    with pytest.raises(ValidationError, match="missing"):
        RepoReadInput.model_validate({"tool_id": "repo_read", "resource_handle": HANDLE})


@pytest.mark.parametrize("count", [1, 4], ids=["single", "over-max"])
def test_ask_operator_refuses_an_option_count_outside_two_or_three(count: int) -> None:
    options = [_option(f"opt{index}") for index in range(count)]
    with pytest.raises(ValidationError):
        AskOperatorInput.model_validate(_ask_operator(options=options))


@pytest.mark.parametrize("count", [2, 3])
def test_ask_operator_accepts_two_or_three_options(count: int) -> None:
    options = [_option(f"opt{index}") for index in range(count)]
    document = _ask_operator(options=options, recommended_option_id="opt0")
    assert len(AskOperatorInput.model_validate(document).options) == count


def test_ask_operator_refuses_a_repeated_option() -> None:
    with pytest.raises(ValidationError, match="appears more than once"):
        AskOperatorInput.model_validate(_ask_operator(options=[_option("keep"), _option("keep")]))


def test_ask_operator_refuses_a_recommendation_it_does_not_offer() -> None:
    with pytest.raises(ValidationError, match="is not offered"):
        AskOperatorInput.model_validate(_ask_operator(recommended_option_id="third"))


def test_ask_operator_output_suspension_names_what_clears_it() -> None:
    document = {
        "tool_id": "ask_operator",
        "question_ref": "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0007",
        "suspended": True,
    }
    with pytest.raises(ValidationError, match="suspension_reason"):
        AskOperatorOutput.model_validate(document)
    answered = AskOperatorOutput.model_validate(
        {**document, "suspension_reason": "AWAITING_OPERATOR_INPUT"}
    )
    assert answered.suspended is True


def test_ask_operator_output_refuses_a_reason_without_a_suspension() -> None:
    with pytest.raises(ValidationError, match="suspension_reason"):
        AskOperatorOutput.model_validate(
            {
                "tool_id": "ask_operator",
                "question_ref": "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0007",
                "suspended": False,
                "suspension_reason": "AWAITING_OPERATOR_INPUT",
            }
        )


def _command_output(**overrides: Any) -> dict[str, Any]:
    """Return a valid scoped-command output with *overrides* applied."""
    return {
        "tool_id": "run_scoped_command",
        "outcome": "completed",
        "exit_status": 0,
        "log_ref": ARTIFACT,
        "command_digest": DIGEST,
        "environment_digest": DIGEST,
        **overrides,
    }


@pytest.mark.parametrize(
    "overrides",
    [
        {"outcome": "completed", "exit_status": None},
        {"outcome": "timed_out"},
        {"outcome": "denied", "exit_status": 1},
    ],
    ids=["completed-without-status", "timed-out-with-status", "denied-with-status"],
)
def test_scoped_command_output_binds_its_status_to_its_outcome(overrides: dict[str, Any]) -> None:
    with pytest.raises(ValidationError, match="disagrees with exit_status"):
        RunScopedCommandOutput.model_validate(_command_output(**overrides))


@pytest.mark.parametrize("exit_status", [0, 255])
def test_scoped_command_output_accepts_a_bounded_exit_status(exit_status: int) -> None:
    output = RunScopedCommandOutput.model_validate(_command_output(exit_status=exit_status))
    assert output.exit_status == exit_status
    assert output.stdout_excerpt == ""


def test_scoped_command_output_refuses_an_out_of_range_exit_status() -> None:
    with pytest.raises(ValidationError):
        RunScopedCommandOutput.model_validate(_command_output(exit_status=256))


def test_submit_report_output_refuses_an_acceptance_with_no_reference() -> None:
    with pytest.raises(ValidationError, match="carries a reference"):
        SubmitReportOutput.model_validate({"tool_id": "submit_report", "accepted": True})


def test_submit_report_output_refuses_a_refusal_with_no_finding() -> None:
    with pytest.raises(ValidationError, match="at least one finding"):
        SubmitReportOutput.model_validate({"tool_id": "submit_report", "accepted": False})


def test_submit_report_output_accepts_a_refusal_that_names_its_finding() -> None:
    output = SubmitReportOutput.model_validate(
        {
            "tool_id": "submit_report",
            "accepted": False,
            "findings": [{"code": "body_invalid", "message": "verdict missing"}],
        }
    )
    assert output.findings[0].code == "body_invalid"


def test_submit_evidence_output_accepts_an_empty_contract_refs_on_acceptance() -> None:
    """Unlike submit_report, acceptance needs no reference: a verified spike
    that probed no external surface legitimately promotes nothing."""
    output = SubmitEvidenceOutput.model_validate({"tool_id": "submit_evidence", "accepted": True})

    assert output.contract_refs == ()


def test_submit_evidence_output_refuses_an_acceptance_with_findings() -> None:
    with pytest.raises(ValidationError, match="carries no findings"):
        SubmitEvidenceOutput.model_validate(
            {
                "tool_id": "submit_evidence",
                "accepted": True,
                "findings": [{"code": "spike_report_unverified", "message": "not verified"}],
            }
        )


def test_submit_evidence_output_refuses_a_refusal_with_no_finding() -> None:
    with pytest.raises(ValidationError, match="at least one finding"):
        SubmitEvidenceOutput.model_validate({"tool_id": "submit_evidence", "accepted": False})
