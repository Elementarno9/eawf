"""Every tool publishes a JSON schema its payload validates against.

The per-Run MCP stdio server publishes one input schema per catalog tool
and carries the envelopes verbatim, so two things have to hold before any
server exists: the published schema has to admit exactly what the model
admits, and an envelope has to survive the round trip through JSON
unchanged, byte for byte.

Both are checked here against the real ``jsonschema`` validator rather
than against the model that produced the schema, because a schema that
only its own generator can read would still break the provider process
that is meant to read it.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as SchemaError
from pydantic import ValidationError

from eawf.kernel.runtime.semantic import (
    SEMANTIC_TOOL_CATALOG,
    SemanticCall,
    SemanticResult,
    SemanticToolErrorCode,
    SemanticToolId,
    decode_call,
    decode_result,
    encode_envelope,
    error_for,
    tool_input_schema,
)

pytestmark = pytest.mark.unit

RUN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
TASK = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
BATCH = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007"
EVIDENCE = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001"
HANDLE = f"wsh-{'f' * 32}"
LEASE = f"LSE-{'0' * 32}"
CALL_ID = f"call-{'1' * 16}"
RECEIPT_ID = f"receipt-{'2' * 16}"
DIGEST = f"sha256:{'a' * 64}"
REQUESTED_AT = "2026-09-17T12:00:00Z"

PAYLOADS: dict[SemanticToolId, dict[str, Any]] = {
    SemanticToolId.EAWF_STATE_QUERY: {
        "selector": "activity.runs",
        "scope_ref": TASK,
        "requested_fields": ["status", "owner"],
    },
    SemanticToolId.REPO_READ: {
        "resource_handle": HANDLE,
        "path": "src/eawf/kernel/runtime/semantic.py",
        "byte_cap": 4096,
    },
    SemanticToolId.REPO_SEARCH: {
        "resource_handle": HANDLE,
        "query": "compile_run_spec",
        "path_prefix": "src/eawf",
        "result_cap": 20,
    },
    SemanticToolId.DIFF_READ: {"resource_handle": HANDLE, "path": "src/eawf"},
    SemanticToolId.WORKSPACE_APPLY_PATCH: {
        "lease_id": LEASE,
        "expected_workspace_generation": 3,
        "patch_ref": "artifact://patch/run-10-1",
        "patch_digest": DIGEST,
    },
    SemanticToolId.RUN_SCOPED_COMMAND: {
        "command_family_id": "pytest",
        "cwd_handle": HANDLE,
        "argv": ["-q", "tests/unit/kernel/runtime"],
        "timeout_seconds": 600,
        "expected_evidence_kind": "deterministic",
    },
    SemanticToolId.REPORT_PROGRESS: {
        "milestone": "criterion_met",
        "summary": "the catalog test passes",
        "criterion_ids": ["CR-01"],
        "evidence_refs": [EVIDENCE],
    },
    SemanticToolId.ATTACH_EVIDENCE: {
        "subject_ref": TASK,
        "criterion_id": "CR-01",
        "evidence_kind": "deterministic",
        "artifact_ref": "artifact://log/pytest-run-10",
        "note": "109 tests green",
    },
    SemanticToolId.ASK_OPERATOR: {
        "question_kind": "design_choice",
        "subject_ref": TASK,
        "options": [
            {"option_id": "keep", "summary": "keep the shape", "effect": "no change"},
            {"option_id": "split", "summary": "split the module", "effect": "two modules"},
        ],
        "recommended_option_id": "keep",
        "reversible": True,
        "protected": False,
    },
    SemanticToolId.SUBMIT_PLAN: {
        "plan_scope_ref": BATCH,
        "proposal_ref": "artifact://plan/revision-4",
        "proposal_digest": DIGEST,
        "supersedes_revision": 3,
    },
    SemanticToolId.SUBMIT_COORDINATION_PROPOSAL: {
        "action": "resequence",
        "target_refs": [BATCH],
        "basis": "the integration queue is serialized",
        "estimated_cost_microusd": 1500,
    },
    SemanticToolId.SUBMIT_CANDIDATE: {
        "task_ref": TASK,
        "lease_id": LEASE,
        "submission_ref": "artifact://candidate/run-10",
        "changed_paths": ["src/eawf/kernel/runtime/semantic.py"],
        "resulting_tree_digest": DIGEST,
    },
    SemanticToolId.SUBMIT_REPORT: {
        "report_schema_ref": "schema://report/executor/v1",
        "contract_digest": DIGEST,
        "body_ref": "artifact://report/run-10",
        "body_digest": DIGEST,
        "verdict": "pass",
    },
    SemanticToolId.BUDGET_STATUS: {"include_children": True},
}


def payload_for(tool_id: SemanticToolId) -> dict[str, Any]:
    """Return the sample payload of *tool_id*, tagged with its tool."""
    return {"tool_id": tool_id.value, **PAYLOADS[tool_id]}


def call_for(tool_id: SemanticToolId, **overrides: Any) -> SemanticCall:
    """Return a sealed call of *tool_id* carrying its sample payload."""
    fields: dict[str, Any] = {
        "call_id": CALL_ID,
        "run_ref": RUN,
        "contract_digest": DIGEST,
        "idempotency_key": f"{tool_id.value}-1",
        "tool_id": tool_id.value,
        "tool_schema_version": "1.0.0",
        "payload": payload_for(tool_id),
        "requested_at": REQUESTED_AT,
    }
    fields.update(overrides)
    return SemanticCall.seal(fields)


def result_fields(**overrides: Any) -> dict[str, Any]:
    """Return a succeeded result for the repository-read call."""
    fields: dict[str, Any] = {
        "call_id": CALL_ID,
        "run_ref": RUN,
        "receipt_id": RECEIPT_ID,
        "status": "succeeded",
        "result_schema_version": "1.0.0",
        "bounded_output": {
            "tool_id": "repo_read",
            "content_ref": "artifact://content/run-10-1",
            "content_digest": DIGEST,
            "byte_count": 4096,
            "truncated": True,
        },
        "completed_at": REQUESTED_AT,
    }
    fields.update(overrides)
    return fields


# ---- published schemas ---------------------------------------------------------


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_every_tool_publishes_a_usable_json_schema(tool_id: SemanticToolId) -> None:
    schema = tool_input_schema(tool_id)
    Draft202012Validator.check_schema(schema)
    assert schema["additionalProperties"] is False
    assert schema["properties"]["tool_id"]["const"] == tool_id.value


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_the_call_payload_validates_against_the_published_schema(tool_id: SemanticToolId) -> None:
    call = call_for(tool_id)
    schema = tool_input_schema(tool_id)
    Draft202012Validator(schema).validate(call.payload.model_dump(mode="json"))


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_the_schema_refuses_what_the_model_refuses(tool_id: SemanticToolId) -> None:
    document = {**payload_for(tool_id), "ambient_flag": True}
    validator = Draft202012Validator(tool_input_schema(tool_id))
    with pytest.raises(SchemaError):
        validator.validate(document)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        SEMANTIC_TOOL_CATALOG[tool_id].input_model.model_validate(document)


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_the_schema_refuses_a_payload_of_another_tool(tool_id: SemanticToolId) -> None:
    other = next(candidate for candidate in SemanticToolId if candidate is not tool_id)
    validator = Draft202012Validator(tool_input_schema(tool_id))
    with pytest.raises(SchemaError):
        validator.validate(payload_for(other))


def test_tool_input_schema_refuses_an_uncatalogued_tool() -> None:
    with pytest.raises(KeyError, match="names no catalog tool"):
        tool_input_schema("request_worktree")


# ---- byte-stable envelopes -----------------------------------------------------


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_a_call_round_trips_byte_stable_through_its_frame(tool_id: SemanticToolId) -> None:
    frame = encode_envelope(call_for(tool_id))
    decoded = decode_call(frame)
    assert encode_envelope(decoded) == frame
    assert decoded == call_for(tool_id)


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_a_frame_is_one_canonical_line(tool_id: SemanticToolId) -> None:
    frame = encode_envelope(call_for(tool_id))
    body = json.loads(frame)
    assert "\n" not in frame
    assert ", " not in frame
    assert list(body) == sorted(body)


def test_two_equal_calls_encode_to_the_same_bytes() -> None:
    assert encode_envelope(call_for(SemanticToolId.REPO_READ)) == encode_envelope(
        call_for(SemanticToolId.REPO_READ)
    )


def test_a_reordered_frame_re_encodes_to_the_canonical_one() -> None:
    frame = encode_envelope(call_for(SemanticToolId.BUDGET_STATUS))
    shuffled = json.dumps(dict(reversed(list(json.loads(frame).items()))), separators=(", ", ": "))
    assert encode_envelope(decode_call(shuffled)) == frame


def test_a_result_round_trips_byte_stable_through_its_frame() -> None:
    frame = encode_envelope(SemanticResult.model_validate(result_fields()))
    assert encode_envelope(decode_result(frame)) == frame


def test_a_refused_result_round_trips_with_its_error() -> None:
    error = error_for(SemanticToolErrorCode.CAPABILITY_DENIED, message="tool not granted")
    document = result_fields(
        status="denied", bounded_output=None, error=error.model_dump(mode="json")
    )
    frame = encode_envelope(SemanticResult.model_validate(document))
    decoded = decode_result(frame)
    assert decoded.error is not None
    assert decoded.error.retry_class.value == "after_policy_change"
    assert encode_envelope(decoded) == frame


# ---- envelope rules ------------------------------------------------------------


def test_seal_computes_the_payload_digest() -> None:
    call = call_for(SemanticToolId.REPO_READ)
    assert call.payload_digest.startswith("sha256:")
    assert decode_call(encode_envelope(call)).payload_digest == call.payload_digest


def test_seal_refuses_a_call_with_no_payload() -> None:
    with pytest.raises(KeyError):
        SemanticCall.seal({"call_id": CALL_ID, "run_ref": RUN})


def test_a_call_refuses_a_payload_of_another_tool() -> None:
    with pytest.raises(ValidationError, match="does not match tool"):
        call_for(SemanticToolId.REPO_READ, payload=payload_for(SemanticToolId.BUDGET_STATUS))


def test_a_call_refuses_a_payload_its_digest_does_not_cover() -> None:
    document = json.loads(encode_envelope(call_for(SemanticToolId.REPO_READ)))
    document["payload"]["byte_cap"] = 8192
    with pytest.raises(ValidationError, match="payload_digest does not cover"):
        SemanticCall.model_validate(document)


@pytest.mark.parametrize(
    "call_id",
    ["", f"call-{'1' * 15}", f"call-{'1' * 17}", "call-zzzzzzzzzzzzzzzz", f"CALL-{'1' * 16}"],
    ids=["empty", "one-short", "one-long", "non-hex", "upper-case"],
)
def test_a_call_refuses_a_malformed_call_id(call_id: str) -> None:
    with pytest.raises(ValidationError):
        call_for(SemanticToolId.BUDGET_STATUS, call_id=call_id)


@pytest.mark.parametrize("frame", ["", "{", "null", "[]", '{"call_id": 1}'])
def test_decode_call_refuses_a_frame_that_is_not_a_call(frame: str) -> None:
    with pytest.raises((json.JSONDecodeError, ValidationError)):
        decode_call(frame)


def test_decode_result_refuses_a_success_with_no_output() -> None:
    document = result_fields(bounded_output=None)
    with pytest.raises(ValidationError, match="carries an output"):
        decode_result(json.dumps(document))


def test_decode_result_refuses_a_failure_with_no_error() -> None:
    document = result_fields(status="failed", bounded_output=None)
    with pytest.raises(ValidationError, match="carries an error"):
        decode_result(json.dumps(document))


def test_decode_result_refuses_a_failure_that_still_returns_output() -> None:
    error = error_for(SemanticToolErrorCode.INTERNAL_TRANSIENT, message="handler crashed")
    document = result_fields(status="failed", error=error.model_dump(mode="json"))
    with pytest.raises(ValidationError, match="no output"):
        decode_result(json.dumps(document))


def test_a_result_accepts_an_artifact_reference_as_its_output() -> None:
    document = result_fields(bounded_output=None, output_ref="artifact://content/run-10-1")
    assert SemanticResult.model_validate(document).bounded_output is None
