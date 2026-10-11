"""A client that reads only the published schema files a report the server forwards.

Codex reduces an MCP tool's input schema before its model reads it: the
string grammar keywords (``pattern``, ``maxLength``) and every ``title``
are dropped, ``const`` becomes a one-member ``enum``, and types,
``required``, ``additionalProperties`` and descriptions survive. A
certification walk recorded ``submit_report`` calls whose digests and
body reference broke a grammar the model was never shown, and a Claude
worker that invented an all-zero contract digest. The tool now asks only
for the report's content, and eawf derives the contract and the body's
digest itself.

The cases here pin what keeps that recoverable: the published schema
admits exactly what the validator admits and states its grammar where
the reduced view still carries it, a payload built only from the
published schema is forwarded, the members the walk's workers guessed at
are no longer asked for, and a refused payload names the member that
failed rather than the whole argument object.
"""

from __future__ import annotations

import json
from typing import Any, Final

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from jsonschema import Draft202012Validator

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import (
    RETRY_CLASS_BY_CODE,
    SemanticToolErrorCode,
    SemanticToolId,
    SubmitReportInput,
    tool_input_schema,
)
from eawf.runtime.mcp.semantic_stdio import RunServerBinding, SemanticStdioServer

pytestmark = pytest.mark.integration

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN_URN: Final = f"{SLOT}/run/RUN-00000010"
TASK_URN: Final = f"{SLOT}/task/EAWF-0042"
ZERO_DIGEST: Final = f"sha256:{'0' * 64}"

#: The keywords Codex drops from a tool's input schema before its model
#: reads it, as recorded in the walk's session log.
CODEX_DROPPED: Final = frozenset(
    {
        "pattern",
        "maxLength",
        "minLength",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "minItems",
        "maxItems",
        "title",
    }
)

#: The members the walk's workers had to guess and eawf now derives.
DERIVED_MEMBERS: Final = ("report_schema_ref", "contract_digest")

#: The members the old contract asked for and the tool no longer has.
RETIRED_MEMBERS: Final = ("body_ref", "body_digest")

#: What a worker sends now: the report's content and nothing eawf knows.
CONTENT_CALL: Final[dict[str, Any]] = {
    "tool_id": "submit_report",
    "verdict": "pass",
    "summary": "every containment attempt was denied",
    "criteria": [{"criterion_id": "CR-01", "outcome": "met"}],
    "body": '{"outcomes": []}',
}

#: The argument map the Claude worker sent in the walk, invented digest and all.
CLAUDE_WALK_CALL: Final[dict[str, Any]] = {
    "tool_id": "submit_report",
    "report_schema_ref": "schema://executor-report/v1",
    "contract_digest": ZERO_DIGEST,
    "body_ref": "artifact://executor-report/wsh-94e42485c31c436f639f993d55ef8492",
    "body_digest": ZERO_DIGEST,
    "verdict": "pass",
}


class StubTransport:
    """A daemon that answers every forward with one succeeded receipt.

    Attributes:
        forwarded: The ``call`` member of every request that left the
            server, so a case asserts what was forwarded and how often.
    """

    def __init__(self) -> None:
        """Build the transport with nothing forwarded yet."""
        self.forwarded: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record one forward and answer it as accepted."""
        self.forwarded.append(params["call"])
        return {"result": {"status": "succeeded", "tool_id": params["call"]["tool_id"]}}


def build_server(*grants: str) -> tuple[SemanticStdioServer, StubTransport]:
    """Return a server granted *grants* and the transport behind it."""
    digest = f"sha256:{'a' * 64}"
    capsule = AuthorityCapsule.seal(
        {
            "run_ref": RUN_URN,
            "scope_ref": TASK_URN,
            "scope_digest": digest,
            "agent_role": "executor",
            "purpose": "implement",
            "authority": {"state": "read_only", "workspace": "scoped_write"},
            "tool_grants": grants,
            "budget": {"wall_seconds": 3600, "output_bytes": 1_048_576},
            "criteria_digest": digest,
            "policy_digest": digest,
            "compiled_spec_digest": digest,
            "report_schema_ref": "schema://executor-report/v1",
            "stop_conditions": ("budget_exhausted",),
        }
    )
    binding = RunServerBinding.model_validate(
        {"run_ref": RUN_URN, "repo_root": "repo", "capsule_path": "capsule.json"}
    )
    transport = StubTransport()
    return SemanticStdioServer(binding=binding, capsule=capsule, transport=transport), transport


def call_tool(server: SemanticStdioServer, name: str, arguments: Any) -> dict[str, Any]:
    """Return the tool answer to one ``tools/call`` frame."""
    frame = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert frame is not None
    result: dict[str, Any] = frame["result"]
    return result


def published_schema(tool: str) -> dict[str, Any]:
    """Return the input schema the server lists for *tool*."""
    server, _ = build_server(tool)
    frame = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert frame is not None
    schema: dict[str, Any] = frame["result"]["tools"][0]["inputSchema"]
    return schema


def normalized(arguments: dict[str, Any]) -> dict[str, Any]:
    """Return *arguments* as the server forwards them: validated, defaults filled."""
    payload: dict[str, Any] = SubmitReportInput.model_validate(
        {"tool_id": "submit_report", **arguments}
    ).model_dump(mode="json")
    return payload


def codex_view(node: Any) -> Any:
    """Return *node* reduced the way Codex reduces a tool input schema."""
    if isinstance(node, list):
        return [codex_view(item) for item in node]
    if not isinstance(node, dict):
        return node
    reduced = {key: codex_view(value) for key, value in node.items() if key not in CODEX_DROPPED}
    if "const" in reduced:
        reduced["enum"] = [reduced.pop("const")]
    return reduced


def strategy_for(node: dict[str, Any]) -> st.SearchStrategy[Any]:
    """Return values a client reading only *node* would produce."""
    if "const" in node:
        return st.just(node["const"])
    if "enum" in node:
        return st.sampled_from(node["enum"])
    if "anyOf" in node:
        return st.one_of([strategy_for(branch) for branch in node["anyOf"]])
    kind = node.get("type")
    if kind == "null":
        return st.none()
    if kind == "object":
        properties = node["properties"]
        required = {name: strategy_for(properties[name]) for name in node.get("required", [])}
        optional = {
            name: strategy_for(member)
            for name, member in properties.items()
            if name not in required
        }
        return st.fixed_dictionaries(required, optional=optional)
    if kind == "array":
        items = node["items"]
        # An array of objects is a set keyed by its first required member, which
        # the member's description states ("one entry per declared criterion").
        key = items.get("required", [None])[0] if items.get("type") == "object" else None
        return st.lists(
            strategy_for(items),
            max_size=3,
            unique_by=(lambda item: json.dumps(item if key is None else item[key])),
        )
    assert kind == "string", node
    limit = node.get("maxLength", 10_000)
    if "pattern" in node:
        return st.from_regex(node["pattern"], fullmatch=True).filter(
            lambda text: len(text) <= limit
        )
    words = st.text(alphabet="abcdefghij", min_size=max(1, node.get("minLength", 0)), max_size=40)
    return words.filter(lambda text: len(text) <= limit)


# ---- the published schema ------------------------------------------------------


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_the_published_schema_is_flat(tool_id: SemanticToolId) -> None:
    text = json.dumps(tool_input_schema(tool_id))
    assert "$ref" not in text
    assert "$defs" not in text


@pytest.mark.parametrize("tool_id", list(SemanticToolId), ids=lambda tool: tool.value)
def test_every_pattern_survives_the_codex_reduction_in_its_description(
    tool_id: SemanticToolId,
) -> None:
    schema = tool_input_schema(tool_id)
    reduced = codex_view(schema)
    for name, member in schema["properties"].items():
        if "pattern" in member:
            assert "pattern" not in reduced["properties"][name]
            assert member["pattern"] in reduced["properties"][name]["description"]
        if "maxLength" in member:
            assert f"at most {member['maxLength']} characters" in member["description"]


def test_an_unconstrained_property_gains_no_description() -> None:
    schema = tool_input_schema(SemanticToolId.BUDGET_STATUS)
    assert "description" not in schema["properties"]["include_children"]


def test_a_nullable_property_names_null_beside_its_grammar() -> None:
    member = tool_input_schema(SemanticToolId.REPO_SEARCH)["properties"]["path_prefix"]
    assert member["description"].endswith(" or null.")


def test_a_bounded_array_states_its_item_count() -> None:
    member = tool_input_schema(SemanticToolId.ASK_OPERATOR)["properties"]["options"]
    assert member["description"] == "Constraint: at least 2 items, at most 3 items."


def test_an_authored_description_keeps_its_grammar_beside_it() -> None:
    member = tool_input_schema(SemanticToolId.SUBMIT_REPORT)["properties"]["contract_digest"]
    reduced = codex_view(member)
    assert reduced["description"].startswith("Omit: eawf fills it from the Run")
    assert "^sha256:[0-9a-f]{64}$" in reduced["description"]


# ---- the report asks only for content ------------------------------------------


def test_the_report_requires_only_content() -> None:
    schema = tool_input_schema(SemanticToolId.SUBMIT_REPORT)
    assert set(schema["required"]) == {"tool_id", "verdict", "summary"}
    assert all(member not in schema["required"] for member in DERIVED_MEMBERS)
    assert all(member not in schema["properties"] for member in RETIRED_MEMBERS)


def test_each_derived_member_tells_the_worker_to_omit_it() -> None:
    schema = codex_view(tool_input_schema(SemanticToolId.SUBMIT_REPORT))
    for member in DERIVED_MEMBERS:
        assert schema["properties"][member]["description"].startswith("Omit:")


# ---- what a schema-faithful client sends is forwarded ---------------------------


@settings(max_examples=60, deadline=None, suppress_health_check=[HealthCheck.too_slow])
@given(data=st.data())
def test_a_payload_built_from_the_published_schema_is_forwarded(data: st.DataObject) -> None:
    schema = published_schema(SemanticToolId.SUBMIT_REPORT.value)
    arguments = data.draw(strategy_for(schema))
    Draft202012Validator(schema).validate(arguments)
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)

    answer = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, arguments)

    assert answer["isError"] is False, answer
    assert len(transport.forwarded) == 1
    assert transport.forwarded[0]["payload"] == normalized(arguments)


def test_a_content_only_payload_is_forwarded() -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)

    answer = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, CONTENT_CALL)

    assert answer["isError"] is False
    payload = transport.forwarded[0]["payload"]
    assert payload == normalized(CONTENT_CALL)
    assert payload["contract_digest"] is None
    assert payload["report_schema_ref"] is None


def test_a_payload_without_the_tool_tag_is_forwarded_tagged() -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)
    untagged = {key: value for key, value in CONTENT_CALL.items() if key != "tool_id"}

    answer = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, untagged)

    assert answer["isError"] is False
    assert transport.forwarded[0]["payload"]["tool_id"] == "submit_report"


def test_the_envelope_carries_the_capsules_contract_whatever_the_payload_says() -> None:
    """The server seals the call under the capsule's digest, never the worker's."""
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)
    guessed = {**CONTENT_CALL, "contract_digest": ZERO_DIGEST}

    call_tool(server, SemanticToolId.SUBMIT_REPORT.value, guessed)

    forwarded = transport.forwarded[0]
    assert forwarded["contract_digest"] == server._capsule.contract_digest
    assert forwarded["payload"]["contract_digest"] == ZERO_DIGEST


# ---- what the walk's workers sent is refused, and the refusal says where ---------


def test_the_recorded_walk_call_is_refused_at_a_retired_member() -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)

    answer = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, CLAUDE_WALK_CALL)

    body = answer["structuredContent"]
    assert answer["isError"] is True
    assert body["code"] == SemanticToolErrorCode.PAYLOAD_INVALID.value
    assert body["retry_class"] == RETRY_CLASS_BY_CODE[SemanticToolErrorCode.PAYLOAD_INVALID].value
    assert body["field_path"] in {
        f"/arguments/{member}" for member in (*RETIRED_MEMBERS, "summary")
    }
    assert transport.forwarded == []


def test_a_missing_member_is_named_in_the_refusal() -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)
    partial = {key: value for key, value in CONTENT_CALL.items() if key != "summary"}

    body = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, partial)["structuredContent"]

    assert body["field_path"] == "/arguments/summary"
    assert body["message"].endswith(": missing")
    assert transport.forwarded == []


def test_a_nested_member_is_named_by_its_full_pointer() -> None:
    server, _ = build_server(SemanticToolId.ASK_OPERATOR.value)
    option = {"option_id": "keep", "summary": "keep it", "effect": "no change"}
    arguments = {
        "question_kind": "design_choice",
        "subject_ref": TASK_URN,
        "options": [{**option, "summary": ""}, {**option, "option_id": "drop"}],
        "recommended_option_id": "keep",
        "reversible": True,
        "protected": False,
    }

    body = call_tool(server, SemanticToolId.ASK_OPERATOR.value, arguments)["structuredContent"]

    assert body["field_path"] == "/arguments/options/0/summary"


def test_a_bad_criterion_outcome_is_named_by_its_full_pointer() -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)
    arguments = {**CONTENT_CALL, "criteria": [{"criterion_id": "CR-01", "outcome": "done"}]}

    body = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, arguments)["structuredContent"]

    assert body["field_path"] == "/arguments/criteria/0/outcome"
    assert transport.forwarded == []


@pytest.mark.parametrize("extra", ["Body-Ref", "body ref", "ÿ"], ids=["upper", "space", "latin"])
def test_a_member_name_outside_the_pointer_grammar_is_not_echoed(extra: str) -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)

    answer = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, {**CONTENT_CALL, extra: 1})

    assert answer["structuredContent"]["field_path"] == "/arguments"
    assert answer["structuredContent"]["message"].endswith(": extra_forbidden")
    assert transport.forwarded == []


@pytest.mark.parametrize("arguments", [None, "{}", [], 7], ids=["none", "string", "list", "int"])
def test_arguments_that_are_not_an_object_are_refused_at_the_first_required_member(
    arguments: Any,
) -> None:
    server, transport = build_server(SemanticToolId.SUBMIT_REPORT.value)

    body = call_tool(server, SemanticToolId.SUBMIT_REPORT.value, arguments)["structuredContent"]

    assert body["code"] == SemanticToolErrorCode.PAYLOAD_INVALID.value
    assert body["field_path"].startswith("/arguments/")
    assert transport.forwarded == []
