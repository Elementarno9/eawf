"""The ruled transport: one server per Run, listing its grant, forwarding it.

The suite drives the real per-Run server against the real registered
``semantic.call`` verb. The transport is injected rather than dialled: an
in-process transport hands each forwarded request straight to the daemon
dispatcher over a canary provisioned under ``tmp_path``, so what runs is
the shipped server and the shipped gateway with no daemon spawned, no
socket opened, no provider started and no repository outside the test's
own directory touched.

Three properties are what this file exists to pin.

The list is the grant. ``tools/list`` is asserted to equal the capsule's
resolved tool set exactly -- not a superset, not the catalog -- and an
empty grant lists nothing at all, so a Run that was granted no tool sees
none rather than seeing the ones the server happens to know about.

The server holds no authority. Every admitted answer here is the daemon's
receipt verbatim, and the two cases that would matter most are proved
against the daemon rather than the server: a denied call comes back as a
denied receipt with the check that refused it, and a capsule widened
between the sealing and the call is refused as a contract mismatch even
though the server itself would happily have forwarded it.

A call it cannot forward yields a closed error. Each of the three ways a
forward can stop -- a tool outside the grant, arguments the catalog
refuses, a transport that did not answer -- is asserted to produce a
:class:`SemanticToolError` with a code from the closed set and the retry
class that code implies, and the first two are asserted to have reached
the daemon zero times.
"""

from __future__ import annotations

import asyncio
import io
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import (
    RETRY_CLASS_BY_CODE,
    SemanticToolErrorCode,
    SemanticToolId,
)
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import RunPurpose
from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.run import RUN_BIND_METHOD
from eawf.runtime.daemon.methods.semantic import (
    SEMANTIC_CALL_METHOD as REGISTERED_SEMANTIC_CALL_METHOD,
)
from eawf.runtime.mcp.semantic_stdio import (
    MCP_PROTOCOL_VERSION,
    SEMANTIC_CALL_METHOD,
    SERVER_NAME,
    RunServerBinding,
    SemanticStdioServer,
    ServerTableError,
    ambient_tool_denial,
    materialize_run_server_config,
    mcp_tool_name,
    run_server_config_for,
    serve_semantic_stdio,
)
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed, seed_row

pytestmark = pytest.mark.integration


ACTOR: Final = "OP-0001"
RUN_KEY: Final = "RUN-00000010"
SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
RUN_URN: Final = f"{SLOT}/run/{RUN_KEY}"
TASK_URN: Final = f"{SLOT}/task/EAWF-0042"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
WALL_CEILING: Final = 3600
ROUTE_REVISION: Final = 3

#: The grant every capsule here carries unless a case narrows it.
GRANTS: Final = ("budget_status", "attach_evidence", "run_scoped_command")

#: A payload the catalog admits for a tool no capsule here is bound to,
#: used to prove a widened capsule is refused by the daemon rather than
#: by the payload validator on the way out.
ASK_OPERATOR_ARGUMENTS: Final[dict[str, Any]] = {
    "question_kind": "scope_clarification",
    "subject_ref": TASK_URN,
    "options": [
        {"option_id": "narrow", "summary": "narrow the task", "effect": "the write set shrinks"},
        {"option_id": "widen", "summary": "widen the task", "effect": "the write set grows"},
    ],
    "recommended_option_id": "narrow",
    "reversible": True,
    "protected": False,
}

#: A scoped-command payload the catalog admits, parameterised by argv.
COMMAND_ARGUMENTS: Final[dict[str, Any]] = {
    "command_family_id": "test-runner",
    "cwd_handle": "wsh-0123456789abcdef0123456789abcdef",
    "timeout_seconds": 60,
    "expected_evidence_kind": "deterministic",
}


def digest(char: str) -> str:
    """Return a well-formed digest whose body is one repeated character."""
    return f"sha256:{char * 64}"


def seal_capsule(**overrides: Any) -> AuthorityCapsule:
    """Return a sealed capsule, overriding whichever field is under test."""
    fields: dict[str, Any] = {
        "run_ref": RUN_URN,
        "scope_ref": TASK_URN,
        "scope_digest": digest("a"),
        "agent_role": AgentSessionRole.EXECUTOR.value,
        "purpose": RunPurpose.IMPLEMENT.value,
        "authority": {"state": "read_only", "workspace": "scoped_write"},
        "tool_grants": GRANTS,
        "budget": {"wall_seconds": WALL_CEILING, "output_bytes": 1_048_576},
        "criteria_digest": digest("b"),
        "policy_digest": digest("c"),
        "compiled_spec_digest": digest("d"),
        "report_schema_ref": "schema://executor-report/v1",
        "stop_conditions": ("budget_exhausted",),
    }
    fields.update(overrides)
    return AuthorityCapsule.seal(fields)


def make_canary(root: Path) -> CanaryProvision:
    """Provision a canary holding one RUNNING Run that started just now."""
    provisioned = provision_canary(repo_root=root, ref=canary_ref("STD"), provisioned_at=AT)
    row = seed_row("run", "RUNNING")
    row["started_at"] = (datetime.now(UTC) - timedelta(seconds=5)).isoformat()
    seed(provisioned, {"run": {RUN_KEY: row}})
    return provisioned


def method_ctx(runtime_root: Path) -> MethodContext:
    """Return a daemon context with a WAL directory of its own."""
    return MethodContext(
        started_at=AT.isoformat(),
        pid=1,
        protocol_version="1",
        version="test",
        wal_dir=runtime_root / "wal",
    )


class RecordingTransport:
    """The daemon, reached in process, with every forward recorded.

    Attributes:
        forwarded: One entry per request that reached the dispatcher, so
            a case can assert a call never left the server.
    """

    def __init__(self, ctx: MethodContext) -> None:
        """Bind the transport to a daemon context."""
        self._ctx = ctx
        self.forwarded: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Dispatch one request the way the socket listener does."""
        self.forwarded.append({"method": method, "params": params})
        return asyncio.run(methods.dispatch(method, self._ctx, params))


class DeadTransport:
    """A transport that never answers.

    Attributes:
        forwarded: Always empty; the failure happens on the way out.
    """

    def __init__(self) -> None:
        """Build the transport with nothing behind it."""
        self.forwarded: list[dict[str, Any]] = []

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Fail the way a closed socket does.

        Raises:
            ConnectionError: Always.
        """
        raise ConnectionError("the daemon socket is closed")


def write_binding(tmp_path: Path, *, canary: CanaryProvision, capsule: AuthorityCapsule) -> Path:
    """Write the capsule and its binding, and return the binding path."""
    capsule_path = tmp_path / "capsule.json"
    capsule_path.write_text(json.dumps(capsule.model_dump(mode="json")), encoding="utf-8")
    binding_path = tmp_path / "binding.json"
    binding_path.write_text(
        json.dumps(
            {
                "run_ref": RUN_URN,
                "repo_root": str(canary.root),
                "capsule_path": str(capsule_path),
            }
        ),
        encoding="utf-8",
    )
    return binding_path


def build_server(
    tmp_path: Path, *, canary: CanaryProvision, capsule: AuthorityCapsule, transport: Any
) -> SemanticStdioServer:
    """Return the per-Run server a provider process would be handed."""
    binding = RunServerBinding.model_validate(
        json.loads(write_binding(tmp_path, canary=canary, capsule=capsule).read_text("utf-8"))
    )
    return SemanticStdioServer(
        binding=binding, capsule=binding.capsule(), transport=transport, clock=lambda: AT
    )


def bind_contract(ctx: MethodContext, canary: CanaryProvision, capsule: AuthorityCapsule) -> None:
    """Record the contract this Run's calls must echo."""
    asyncio.run(
        methods.dispatch(
            RUN_BIND_METHOD,
            ctx,
            {
                "repo_root": str(canary.root),
                "urn": RUN_URN,
                "compiled_spec_digest": digest("d"),
                "authority_capsule_digest": capsule.contract_digest,
                "route_policy_revision": ROUTE_REVISION,
            },
        )
    )


def request(method: str, request_id: int = 1, **params: Any) -> dict[str, Any]:
    """Return one JSON-RPC request frame."""
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def answer_of(frame: dict[str, Any] | None) -> dict[str, Any]:
    """Return the result member of an answered frame."""
    assert frame is not None, "the frame carried an id and must be answered"
    assert "error" not in frame, f"the frame was refused: {frame.get('error')}"
    result: dict[str, Any] = frame["result"]
    return result


def assert_closed_error(
    answer: dict[str, Any], *, code: SemanticToolErrorCode, field_path: str | None = None
) -> None:
    """Assert one tool answer carries the closed error it should."""
    assert answer["isError"] is True
    body = answer["structuredContent"]
    assert body["code"] == code.value
    assert body["retry_class"] == RETRY_CLASS_BY_CODE[code].value
    if field_path is not None:
        assert body["field_path"] == field_path
    assert json.loads(answer["content"][0]["text"]) == body


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_ctx(runtime_root)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run."""
    return make_canary(tmp_path / "repo")


# ---------------------------------------------------------------------------
# The list is the grant
# ---------------------------------------------------------------------------


def test_the_server_lists_exactly_the_runs_granted_tools(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    answer = answer_of(server.handle(request("tools/list")))

    listed = [tool["name"] for tool in answer["tools"]]
    assert listed == list(GRANTS)
    assert set(listed) < {tool.value for tool in SemanticToolId}


def test_a_denied_tool_is_not_listed_even_though_it_was_granted(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A denial outranks a grant, so listing it would list a dead tool."""
    capsule = seal_capsule(tool_denials=("attach_evidence",))
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    answer = answer_of(server.handle(request("tools/list")))

    assert [tool["name"] for tool in answer["tools"]] == ["budget_status", "run_scoped_command"]


def test_an_empty_grant_lists_no_tool(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule(tool_grants=())
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    assert answer_of(server.handle(request("tools/list")))["tools"] == []


def test_each_listed_tool_publishes_the_catalog_input_schema(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """One model produces the validator and the published schema."""
    capsule = seal_capsule(tool_grants=("budget_status",))
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    published = answer_of(server.handle(request("tools/list")))["tools"][0]

    assert published["inputSchema"]["properties"]["include_children"]["type"] == "boolean"
    assert published["description"]


def test_initialize_publishes_the_protocol_and_the_one_server_name(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    answer = answer_of(server.handle(request("initialize")))

    assert answer["protocolVersion"] == MCP_PROTOCOL_VERSION
    assert answer["serverInfo"]["name"] == SERVER_NAME


# ---------------------------------------------------------------------------
# Every call is forwarded, and the answer is the daemon's
# ---------------------------------------------------------------------------


def test_a_granted_call_is_forwarded_and_answered_with_the_daemons_receipt(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    bind_contract(ctx, canary, capsule)
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(
        server.handle(
            request("tools/call", name="budget_status", arguments={"include_children": False})
        )
    )

    assert answer["isError"] is False
    assert answer["structuredContent"]["status"] == "succeeded"
    assert answer["structuredContent"]["bounded_output"]["quality"] == "measured"
    assert [entry["method"] for entry in transport.forwarded] == ["semantic.call"]
    forwarded = transport.forwarded[0]["params"]["call"]
    assert forwarded["run_ref"] == RUN_URN
    assert forwarded["tool_id"] == "budget_status"
    assert forwarded["contract_digest"] == capsule.contract_digest


def test_a_call_the_daemon_denies_comes_back_as_its_denied_receipt(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The server judges nothing: the refusal and its check are the daemon's."""
    capsule = seal_capsule()
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(
        server.handle(
            request("tools/call", name="budget_status", arguments={"include_children": False})
        )
    )

    assert answer["isError"] is True
    body = answer["structuredContent"]
    assert body["status"] == "denied"
    assert body["error"]["code"] == SemanticToolErrorCode.CONTRACT_MISMATCH.value
    assert len(transport.forwarded) == 1


def test_a_capsule_widened_after_sealing_is_refused_by_the_daemon(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """Holding a capsule is not holding authority: widening it is visible.

    The widened capsule is sealed, so the server loads it and lists the
    tool it added. The daemon then refuses every call made under it,
    because a capsule with one grant more hashes to a digest the Run's
    recorded binding does not name.
    """
    bound = seal_capsule()
    bind_contract(ctx, canary, bound)
    widened = seal_capsule(tool_grants=(*GRANTS, "ask_operator"))
    server = build_server(
        tmp_path, canary=canary, capsule=widened, transport=RecordingTransport(ctx)
    )

    listed = answer_of(server.handle(request("tools/list")))["tools"]
    answer = answer_of(
        server.handle(request("tools/call", name="ask_operator", arguments=ASK_OPERATOR_ARGUMENTS))
    )

    assert widened.contract_digest != bound.contract_digest
    assert "ask_operator" in [tool["name"] for tool in listed]
    assert answer["isError"] is True
    assert answer["structuredContent"]["status"] == "denied"
    assert (
        answer["structuredContent"]["error"]["code"]
        == SemanticToolErrorCode.CONTRACT_MISMATCH.value
    )


def test_the_daemon_names_the_check_that_refused_on_the_returned_receipt(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    bind_contract(ctx, canary, capsule)
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(
        server.handle(
            request(
                "tools/call",
                name="run_scoped_command",
                arguments={**COMMAND_ARGUMENTS, "argv": ["claude", "-p", "finish the task"]},
            )
        )
    )

    body = answer["structuredContent"]
    assert body["status"] == "denied"
    assert body["error"]["code"] == SemanticToolErrorCode.SCOPE_DENIED.value
    assert body["error"]["field_path"] == "/argv"
    assert len(transport.forwarded) == 1


# ---------------------------------------------------------------------------
# A call it cannot forward yields a closed error
# ---------------------------------------------------------------------------


def test_a_tool_outside_the_grant_never_reaches_the_daemon(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule(tool_grants=("budget_status",))
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(server.handle(request("tools/call", name="submit_candidate", arguments={})))

    assert_closed_error(answer, code=SemanticToolErrorCode.CAPABILITY_DENIED, field_path="/name")
    assert transport.forwarded == []


def test_a_tool_the_catalog_never_declared_is_refused_the_same_way(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(server.handle(request("tools/call", name="rm_rf", arguments={})))

    assert_closed_error(answer, code=SemanticToolErrorCode.CAPABILITY_DENIED)
    assert transport.forwarded == []


def test_a_tool_name_that_is_not_a_string_is_refused_without_echoing_it(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """A bounded message refuses shell shapes, so the name is never quoted."""
    capsule = seal_capsule()
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(server.handle(request("tools/call", name=None, arguments={})))

    assert_closed_error(answer, code=SemanticToolErrorCode.CAPABILITY_DENIED)
    assert "`" not in answer["structuredContent"]["message"]
    assert transport.forwarded == []


def test_a_name_carrying_a_shell_shape_is_still_answered(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The refusal is constructible for any name, which is why none is echoed."""
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    answer = answer_of(
        server.handle(request("tools/call", name="$(sudo rm -rf /) && `id`", arguments={}))
    )

    assert_closed_error(answer, code=SemanticToolErrorCode.CAPABILITY_DENIED)


@pytest.mark.parametrize(
    "arguments",
    [
        {"include_children": "yes"},
        {"include_children": False, "elevated": True},
        {"tool_id": "submit_candidate", "include_children": False},
    ],
    ids=["mistyped", "extra-field", "mislabelled-tool"],
)
def test_arguments_the_catalog_refuses_never_reach_the_daemon(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision, arguments: dict[str, Any]
) -> None:
    """Mistyped, extra and mislabelled payloads all stop at the server."""
    capsule = seal_capsule()
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(
        server.handle(request("tools/call", name="budget_status", arguments=arguments))
    )

    assert_closed_error(answer, code=SemanticToolErrorCode.PAYLOAD_INVALID, field_path="/arguments")
    assert transport.forwarded == []


def test_an_empty_argument_map_is_a_request_rather_than_a_malformed_one(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """Every field of this payload has a default, so nothing was omitted."""
    capsule = seal_capsule()
    bind_contract(ctx, canary, capsule)
    transport = RecordingTransport(ctx)
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=transport)

    answer = answer_of(server.handle(request("tools/call", name="budget_status", arguments={})))

    assert answer["structuredContent"]["status"] == "succeeded"
    assert len(transport.forwarded) == 1


def test_a_transport_that_does_not_answer_yields_a_transient_error(
    tmp_path: Path, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(tmp_path, canary=canary, capsule=capsule, transport=DeadTransport())

    answer = answer_of(
        server.handle(
            request("tools/call", name="budget_status", arguments={"include_children": False})
        )
    )

    assert_closed_error(answer, code=SemanticToolErrorCode.INTERNAL_TRANSIENT)


def test_a_tool_with_no_handler_is_reported_as_denied_rather_than_transient(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    """The daemon answered with a no-receipt refusal, so the code says so."""
    capsule = seal_capsule()
    bind_contract(ctx, canary, capsule)
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    answer = answer_of(
        server.handle(
            request(
                "tools/call",
                name="run_scoped_command",
                arguments={**COMMAND_ARGUMENTS, "argv": ["pytest", "tests/unit/eawf"]},
            )
        )
    )

    assert_closed_error(answer, code=SemanticToolErrorCode.CAPABILITY_DENIED)


# ---------------------------------------------------------------------------
# The stdio loop itself
# ---------------------------------------------------------------------------


def test_an_unknown_method_is_a_json_rpc_error_not_a_tool_error(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    frame = server.handle(request("resources/list"))

    assert frame is not None
    assert frame["error"]["code"] == -32601


def test_a_notification_is_not_answered(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_a_frame_naming_no_method_is_refused(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )

    frame = server.handle({"jsonrpc": "2.0", "id": 7})

    assert frame is not None
    assert frame["error"]["code"] == -32600


def test_the_loop_answers_every_request_and_drops_what_is_not_json(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )
    frames = "\n".join(
        [
            json.dumps(request("initialize", 1)),
            "not json at all",
            "",
            json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
            json.dumps(request("tools/list", 2)),
        ]
    )
    stdout = io.StringIO()

    answered = serve_semantic_stdio(server, stdin=io.StringIO(frames), stdout=stdout)

    written = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert answered == 2
    assert [frame["id"] for frame in written] == [1, 2]


def test_an_empty_stream_answers_nothing(
    tmp_path: Path, ctx: MethodContext, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    server = build_server(
        tmp_path, canary=canary, capsule=capsule, transport=RecordingTransport(ctx)
    )
    stdout = io.StringIO()

    assert serve_semantic_stdio(server, stdin=io.StringIO(""), stdout=stdout) == 0
    assert stdout.getvalue() == ""


# ---------------------------------------------------------------------------
# The binding and the lane configuration it feeds
# ---------------------------------------------------------------------------


def test_a_binding_naming_no_capsule_file_raises(tmp_path: Path) -> None:
    binding = RunServerBinding(
        run_ref=RUN_URN, repo_root=str(tmp_path), capsule_path=str(tmp_path / "absent.json")
    )

    with pytest.raises(OSError):
        binding.capsule()


def test_a_binding_naming_a_capsule_that_is_not_sealed_raises(tmp_path: Path) -> None:
    body = seal_capsule().model_dump(mode="json")
    body["tool_grants"] = [*body["tool_grants"], "ask_operator"]
    path = tmp_path / "tampered.json"
    path.write_text(json.dumps(body), encoding="utf-8")
    binding = RunServerBinding(run_ref=RUN_URN, repo_root=str(tmp_path), capsule_path=str(path))

    with pytest.raises(ValueError, match="contract_digest"):
        binding.capsule()


def test_an_unknown_lane_renders_no_configuration(tmp_path: Path, canary: CanaryProvision) -> None:
    capsule = seal_capsule()
    binding = RunServerBinding.model_validate(
        json.loads(write_binding(tmp_path, canary=canary, capsule=capsule).read_text("utf-8"))
    )

    with pytest.raises(ServerTableError, match="gemini"):
        run_server_config_for(
            "gemini", binding=binding, config_dir=tmp_path, server_command=("eawf",)
        )


def test_a_lane_config_with_no_files_writes_nothing(
    tmp_path: Path, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    binding = RunServerBinding.model_validate(
        json.loads(write_binding(tmp_path, canary=canary, capsule=capsule).read_text("utf-8"))
    )
    config = run_server_config_for(
        "codex", binding=binding, config_dir=tmp_path, server_command=("eawf", "mcp", "serve")
    )

    assert materialize_run_server_config(config) == ()


def test_the_qualified_tool_name_is_one_spelling() -> None:
    assert mcp_tool_name(SemanticToolId.BUDGET_STATUS) == f"mcp__{SERVER_NAME}__budget_status"
    assert ambient_tool_denial() == tuple(sorted(ambient_tool_denial()))


# ---------------------------------------------------------------------------
# The dispatcher surface that renders the configuration
# ---------------------------------------------------------------------------


def test_the_dispatcher_surface_writes_the_lane_config_and_reports_the_grant(
    tmp_path: Path, canary: CanaryProvision
) -> None:
    """``eawf mcp run-config`` is what a dispatcher runs before a spawn."""
    capsule = seal_capsule(tool_grants=("budget_status",))
    binding_path = write_binding(tmp_path, canary=canary, capsule=capsule)
    config_dir = tmp_path / "lane"

    result = CliRunner().invoke(
        app,
        [
            "--json",
            "mcp",
            "run-config",
            "--runtime",
            "claude-code",
            "--binding",
            str(binding_path),
            "--config-dir",
            str(config_dir),
        ],
    )

    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["runtime_id"] == "claude-code"
    assert payload["exposed_tools"] == [mcp_tool_name(SemanticToolId.BUDGET_STATUS)]
    assert payload["files_written"] == [str(config_dir / "mcp.json")]
    assert "--strict-mcp-config" in payload["argv_flags"]
    document = json.loads((config_dir / "mcp.json").read_text(encoding="utf-8"))
    assert document["mcpServers"][SERVER_NAME]["args"][:3] == ["mcp", "serve", "--binding"]


def test_the_dispatcher_surface_refuses_a_lane_it_cannot_configure(
    tmp_path: Path, canary: CanaryProvision
) -> None:
    capsule = seal_capsule()
    binding_path = write_binding(tmp_path, canary=canary, capsule=capsule)

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "run-config",
            "--runtime",
            "gemini",
            "--binding",
            str(binding_path),
            "--config-dir",
            str(tmp_path / "lane"),
        ],
    )

    assert result.exit_code != 0
    assert not (tmp_path / "lane").exists()


def test_the_dispatcher_surface_refuses_a_binding_that_is_not_one(tmp_path: Path) -> None:
    broken = tmp_path / "binding.json"
    broken.write_text(json.dumps({"run_ref": RUN_URN}), encoding="utf-8")

    result = CliRunner().invoke(
        app,
        [
            "mcp",
            "run-config",
            "--runtime",
            "codex",
            "--binding",
            str(broken),
            "--config-dir",
            str(tmp_path / "lane"),
        ],
    )

    assert result.exit_code != 0


def test_the_server_forwards_to_the_verb_the_daemon_registered() -> None:
    """The server spells the verb itself, so the two are pinned equal here.

    A rename on either side that reached production would send every
    forwarded call to a method nobody serves; this is the assertion that
    reds instead.
    """
    assert SEMANTIC_CALL_METHOD == REGISTERED_SEMANTIC_CALL_METHOD
