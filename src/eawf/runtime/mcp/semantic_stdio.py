"""One MCP stdio server per Run, holding nothing the daemon holds.

A provider process speaks MCP, and the daemon speaks ``semantic.call``.
This module is the only thing between them: a stdio server started once
per Run that publishes the Run's granted tools and turns every
``tools/call`` into a typed :class:`~eawf.kernel.runtime.semantic.SemanticCall`
forwarded to the daemon verbatim.

It holds no state and no authority. The tool list is derived from the
sealed capsule rather than declared here, the payload schemas are the
catalog's own input models rendered as JSON Schema, and the answer a tool
call returns is the daemon's receipt unaltered. There is no branch in
this file that can admit a call: the nine pre-handler checks run on the
far side of the transport, so a compromised server can at most fail to
forward, never approve.

The capsule travels with the call for the same reason the daemon accepts
it: the daemon stores only its digest and recomputes that digest over
every field on load, so a server that widened the capsule it was handed
would produce one the Run's binding does not name and every call would be
refused. Carrying a sealed capsule is therefore not holding authority.

Three things can stop a call before it is forwarded: a tool the Run was
not granted, arguments the catalog's input model refuses, and a transport
that did not answer. Each yields a closed
:class:`~eawf.kernel.runtime.semantic.SemanticToolError` rather than an
exception string, because a provider process branching on prose cannot
build a retry ladder.
"""

from __future__ import annotations

import json
import logging
import secrets
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import IO, Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.semantic import (
    TOOL_SCHEMA_VERSION,
    SemanticCall,
    SemanticToolError,
    SemanticToolErrorCode,
    SemanticToolId,
    error_for,
    tool_contract,
    tool_input_schema,
)
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.runtime.daemon.semantic_gateway import HANDLER_NOT_BROKERED, IDENTITY_NOT_FOUND
from eawf.runtime.sandbox.policy import TOOL_UNIVERSE

logger = logging.getLogger(__name__)


#: The MCP revision this server implements. Published verbatim in the
#: ``initialize`` answer so a client that speaks another revision can
#: refuse rather than guess.
MCP_PROTOCOL_VERSION: Final = "2025-06-18"

#: The one server name a Run's provider sees. Every lane registers the
#: per-Run server under it, so the qualified tool names a provider reads
#: are identical across the three lanes.
SERVER_NAME: Final = "eawf-semantic"

#: The dotted daemon verb every forwarded call lands on.
SEMANTIC_CALL_METHOD: Final = "semantic.call"

#: How many random bytes a minted call identity carries. Matches the
#: ``call-[0-9a-f]{16}`` grammar the envelope pins.
_CALL_ENTROPY_BYTES: Final = 8

#: The JSON-RPC code for a method this server does not implement.
_METHOD_NOT_FOUND: Final = -32601

#: The JSON-RPC code for a frame that is not a request this server reads.
_INVALID_REQUEST: Final = -32600


class ForwardFailure(StrEnum):
    """Why one tool call never reached the daemon.

    Each member is a condition the server can observe on its own side.
    A call the daemon answered is not here, however it answered: a
    refusal is a receipt, not a forwarding failure.
    """

    TOOL_NOT_GRANTED = "tool_not_granted"
    PAYLOAD_INVALID = "payload_invalid"
    TRANSPORT_FAILED = "transport_failed"


#: The closed code each forwarding failure is reported as, total over the
#: enum. The mapping is a table rather than three inline constructions so
#: a failure mode added without a code fails to compile here.
FORWARD_FAILURE_CODES: Final[Mapping[ForwardFailure, SemanticToolErrorCode]] = MappingProxyType(
    {
        ForwardFailure.TOOL_NOT_GRANTED: SemanticToolErrorCode.CAPABILITY_DENIED,
        ForwardFailure.PAYLOAD_INVALID: SemanticToolErrorCode.PAYLOAD_INVALID,
        ForwardFailure.TRANSPORT_FAILED: SemanticToolErrorCode.INTERNAL_TRANSIENT,
    }
)

#: The daemon refusals that carry no receipt, mapped to the closed code
#: the provider reads. A Run this root never recorded is not running as
#: far as the caller is concerned, and a tool with no handler installed
#: is denied until the deployment that installs one.
DAEMON_REFUSAL_CODES: Final[Mapping[str, SemanticToolErrorCode]] = MappingProxyType(
    {
        IDENTITY_NOT_FOUND: SemanticToolErrorCode.RUN_NOT_ACTIVE,
        HANDLER_NOT_BROKERED: SemanticToolErrorCode.CAPABILITY_DENIED,
    }
)


class ServerTableError(RuntimeError):
    """A table in this module is not total over the vocabulary it covers."""


def _compile_failure_codes() -> Mapping[ForwardFailure, SemanticToolErrorCode]:
    """Return the failure table once it answers for every failure mode.

    Returns:
        :data:`FORWARD_FAILURE_CODES` unchanged.

    Raises:
        ServerTableError: A failure mode has no code, so a call stopped
            by it would reach the provider as an untyped exception.
    """
    missing = sorted(
        failure.value for failure in ForwardFailure if failure not in FORWARD_FAILURE_CODES
    )
    if missing:
        raise ServerTableError(f"the forwarding failure table omits {', '.join(missing)}")
    return FORWARD_FAILURE_CODES


class RunServerBinding(BaseModel):
    """Everything one per-Run server needs, and nothing it may decide.

    The binding names where to find the Run's sealed capsule rather than
    carrying its fields, so the tool list and the contract digest have
    one source. A binding that could spell its own grants would be a
    second place authority is written.

    Attributes:
        schema_version: Version of this record's shape.
        run_ref: The Run every call is made by.
        repo_root: The repository root the daemon routes the call to.
        capsule_path: Where the sealed capsule of the Run is written.
        runtime_dir: The daemon runtime directory to connect through, or
            ``None`` for the default.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["run-server/v1"] = "run-server/v1"
    run_ref: RunUrn
    repo_root: str
    capsule_path: str
    runtime_dir: str | None = None

    def capsule(self) -> AuthorityCapsule:
        """Return the sealed capsule this binding names.

        Returns:
            The validated capsule, whose digest is recomputed over every
            field on load.

        Raises:
            OSError: The capsule file cannot be read.
            pydantic.ValidationError: The file is not a sealed capsule,
                which includes a capsule edited after it was sealed.
        """
        body = json.loads(Path(self.capsule_path).read_text(encoding="utf-8"))
        return AuthorityCapsule.model_validate(body)


class RunServerConfig(BaseModel):
    """How one provider lane is told about a Run's server.

    Attributes:
        runtime_id: The lane this configuration is spelled for.
        files: Absolute path mapped to the bytes to write there, empty
            for a lane configured entirely on the command line.
        argv_flags: The flags appended to the lane's spawn argv.
        env: Environment entries the child needs to read the files.
        exposed_tools: The qualified MCP names the Run may call, which
            is what a reviewer reads to see the grant reached the lane.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    runtime_id: str
    files: Mapping[str, str] = {}
    argv_flags: tuple[str, ...] = ()
    env: Mapping[str, str] = {}
    exposed_tools: tuple[str, ...] = ()


class SemanticTransport(Protocol):
    """How a per-Run server reaches the daemon.

    One method, because the server has one thing to say. The protocol is
    an injection seam as much as an abstraction: a test drives the server
    against an in-process daemon rather than a socket, and neither the
    server nor the test needs a live provider to do it.
    """

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send one JSON-RPC request to the daemon and return its result.

        Args:
            method: The dotted verb.
            params: The request parameters.

        Returns:
            The ``result`` member of the answer.

        Raises:
            Exception: Any transport or daemon-side failure. The server
                turns it into a closed error rather than propagating it.
        """


def mcp_tool_name(tool_id: SemanticToolId) -> str:
    """Return the qualified name a provider sees one catalog tool under.

    Args:
        tool_id: A catalog tool.

    Returns:
        The ``mcp__<server>__<tool>`` spelling. Claude Code and opencode
        both address MCP tools this way, and pinning one spelling keeps a
        grant expressed in a lane's configuration readable as the same
        grant on every lane.
    """
    return f"mcp__{SERVER_NAME}__{tool_id.value}"


def ambient_tool_denial() -> tuple[str, ...]:
    """Return every ambient provider tool a Run's provider must not hold.

    Returns:
        The sorted tool universe. A Run reaches its repository through
        the brokered catalog or not at all, so the lane configuration
        denies the vendor's own file and shell tools whatever the Run was
        granted: a grant that is checked by the daemon is worth nothing
        beside an unchecked ambient shell.
    """
    return tuple(sorted(TOOL_UNIVERSE))


def _tool_descriptor(tool_id: SemanticToolId) -> dict[str, Any]:
    """Return the MCP tool entry one catalog tool is published as."""
    contract = tool_contract(tool_id)
    summary = (contract.input_model.__doc__ or "").strip().splitlines()
    return {
        "name": tool_id.value,
        "description": summary[0] if summary else tool_id.value,
        "inputSchema": tool_input_schema(tool_id),
    }


class SemanticStdioServer:
    """The per-Run MCP server: a list, a forward, and nothing else.

    Attributes:
        binding: The Run this server was started for.
        exposed: The tools it publishes, which is the capsule's resolved
            grant and never a superset of it.
    """

    def __init__(
        self,
        *,
        binding: RunServerBinding,
        capsule: AuthorityCapsule,
        transport: SemanticTransport,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        """Bind one server to one Run's capsule and one transport.

        Args:
            binding: The Run the server answers for.
            capsule: The sealed capsule every forwarded call carries.
            transport: How the daemon is reached.
            clock: What stamps each call, injected so a test pins it.
        """
        self.binding = binding
        self.exposed = capsule.semantic_tools
        self._capsule = capsule
        self._transport = transport
        self._clock = clock

    def handle(self, request: Mapping[str, Any]) -> dict[str, Any] | None:
        """Answer one JSON-RPC frame, or return ``None`` for a notification.

        Args:
            request: The decoded frame.

        Returns:
            The answer frame, or ``None`` when the frame carries no id
            and therefore expects no answer.
        """
        method = request.get("method")
        request_id = request.get("id")
        if not isinstance(method, str):
            return _rpc_error(request_id, code=_INVALID_REQUEST, message="frame names no method")
        if request_id is None:
            logger.debug(f"handle notification method={method!r}")
            return None
        if method == "initialize":
            return _rpc_result(request_id, self._initialize())
        if method == "tools/list":
            return _rpc_result(request_id, self._list_tools())
        if method == "tools/call":
            params = request.get("params")
            return _rpc_result(
                request_id, self._call_tool(params if isinstance(params, dict) else {})
            )
        return _rpc_error(
            request_id, code=_METHOD_NOT_FOUND, message=f"{method!r} is not served here"
        )

    def _initialize(self) -> dict[str, Any]:
        """Return what this server is, with no capability it does not have."""
        return {
            "protocolVersion": MCP_PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": TOOL_SCHEMA_VERSION},
        }

    def _list_tools(self) -> dict[str, Any]:
        """Return exactly the Run's granted tools, in the capsule's order."""
        return {"tools": [_tool_descriptor(tool_id) for tool_id in self.exposed]}

    def _call_tool(self, params: Mapping[str, Any]) -> dict[str, Any]:
        """Forward one tool call, or answer why it could not be forwarded."""
        name = params.get("name")
        arguments = params.get("arguments")
        granted = {tool.value: tool for tool in self.exposed}
        if not isinstance(name, str) or name not in granted:
            # The name is not echoed. It is whatever the provider process
            # sent, and the error message is a bounded field that refuses
            # shell shapes, so quoting an unknown name would turn a bad
            # tool call into a refusal this server cannot construct.
            return _tool_failure(
                ForwardFailure.TOOL_NOT_GRANTED,
                message="the tool this call names is not one this run was granted",
                field_path="/name",
            )
        try:
            call = self._sealed_call(
                granted[name], arguments if isinstance(arguments, dict) else {}
            )
        except (ValidationError, KeyError, ValueError) as error:
            logger.info(f"_call_tool refused tool={name!r} reason=payload")
            return _tool_failure(
                ForwardFailure.PAYLOAD_INVALID,
                message=f"the arguments are not a valid {name} payload",
                field_path="/arguments",
                cause=error,
            )
        try:
            receipt = self._transport.call(
                SEMANTIC_CALL_METHOD,
                {
                    "repo_root": self.binding.repo_root,
                    "call": call.model_dump(mode="json"),
                    "capsule": self._capsule.model_dump(mode="json"),
                },
            )
        except Exception as error:
            logger.info(f"_call_tool transport failed tool={name!r}")
            return _tool_failure(
                ForwardFailure.TRANSPORT_FAILED,
                message=f"the daemon did not answer the {name} call",
                cause=error,
            )
        return _receipt_answer(receipt)

    def _sealed_call(self, tool_id: SemanticToolId, arguments: Mapping[str, Any]) -> SemanticCall:
        """Return the envelope one tool call is forwarded as.

        The identity is minted here and reused as the idempotency key, so
        a retry of the same MCP call is a new request rather than a
        silent replay of one the provider has already been answered for.

        Raises:
            pydantic.ValidationError: The arguments are not a payload of
                the catalog tool they name.
            KeyError: The payload is absent, which the sealer refuses.
        """
        call_id = f"call-{secrets.token_hex(_CALL_ENTROPY_BYTES)}"
        return SemanticCall.seal(
            {
                "call_id": call_id,
                "run_ref": str(self.binding.run_ref),
                "contract_digest": self._capsule.contract_digest,
                "idempotency_key": call_id,
                "tool_id": tool_id.value,
                "tool_schema_version": TOOL_SCHEMA_VERSION,
                "payload": {"tool_id": tool_id.value, **arguments},
                "requested_at": self._clock(),
            }
        )


def _daemon_error(error: Exception) -> SemanticToolErrorCode | None:
    """Return the closed code a daemon no-receipt refusal maps to.

    A daemon that refused with one of its two no-receipt codes has
    answered, so the answer travels as that code's closed equivalent
    rather than as a transport fault the provider would retry forever.
    """
    text = str(error)
    for name, code in DAEMON_REFUSAL_CODES.items():
        if name in text:
            return code
    return None


def _tool_failure(
    failure: ForwardFailure,
    *,
    message: str,
    field_path: str | None = None,
    cause: Exception | None = None,
) -> dict[str, Any]:
    """Return the MCP answer of a call that never reached the daemon."""
    code = FORWARD_FAILURE_CODES[failure]
    if cause is not None:
        mapped = _daemon_error(cause)
        if mapped is not None:
            code = mapped
    error = error_for(code, message=message, field_path=field_path)
    return _error_answer(error)


def _error_answer(error: SemanticToolError) -> dict[str, Any]:
    """Return the MCP tool answer carrying one closed error."""
    body = error.model_dump(mode="json")
    return {
        "isError": True,
        "structuredContent": body,
        "content": [{"type": "text", "text": json.dumps(body, sort_keys=True)}],
    }


def _receipt_answer(receipt: Mapping[str, Any]) -> dict[str, Any]:
    """Return the MCP tool answer carrying one daemon receipt verbatim.

    The result envelope is passed through unaltered: the closed code, its
    retry class and the check that refused are already in it, and
    restating any of them here would be a second author of the answer.
    """
    result = receipt.get("result")
    body = result if isinstance(result, dict) else dict(receipt)
    return {
        "isError": body.get("status") != "succeeded",
        "structuredContent": body,
        "content": [{"type": "text", "text": json.dumps(body, sort_keys=True)}],
    }


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    """Return one JSON-RPC success frame."""
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, *, code: int, message: str) -> dict[str, Any]:
    """Return one JSON-RPC error frame."""
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_semantic_stdio(server: SemanticStdioServer, *, stdin: IO[str], stdout: IO[str]) -> int:
    """Read frames until the stream ends, answering each one.

    Args:
        server: The per-Run server that answers.
        stdin: The newline-framed request stream.
        stdout: Where answers are written, flushed per frame so a client
            reading synchronously is never left waiting on a buffer.

    Returns:
        How many frames were answered, which is what a caller asserts on
        rather than reaching into the stream it just drained.
    """
    answered = 0
    for line in _frames(stdin):
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            logger.info("serve_semantic_stdio dropped a frame that is not JSON")
            continue
        if not isinstance(request, dict):
            continue
        answer = server.handle(request)
        if answer is None:
            continue
        stdout.write(json.dumps(answer) + "\n")
        stdout.flush()
        answered += 1
    logger.info(f"serve_semantic_stdio drained run={server.binding.run_ref} frames={answered}")
    return answered


def _frames(stdin: IO[str]) -> Iterator[str]:
    """Yield each non-empty line of *stdin* until it ends."""
    for line in stdin:
        stripped = line.strip()
        if stripped:
            yield stripped


def run_server_config_for(
    runtime_id: str,
    *,
    binding: RunServerBinding,
    config_dir: Path,
    server_command: tuple[str, ...],
    capsule: AuthorityCapsule | None = None,
) -> RunServerConfig:
    """Return how *runtime_id* is told about this Run's server.

    The lane renderers are imported inside the branch, mirroring the
    adapter selector: each lane's module stays cheap to import from the
    lane itself, and this module keeps no edge into the three packages.

    Args:
        runtime_id: The canonical lane id.
        binding: The Run the server answers for.
        config_dir: Where the lane's configuration files are written.
        server_command: The argv that starts the per-Run stdio server.
        capsule: The Run's sealed capsule, read from *binding* when not
            supplied.

    Returns:
        The lane's configuration.

    Raises:
        ServerTableError: *runtime_id* names no lane this repo drives, so
            there is no configuration that would reach a provider.
    """
    arguments: dict[str, Any] = {
        "config_dir": config_dir,
        "server_command": server_command,
        "capsule": capsule,
    }
    if runtime_id == "claude-code":
        from eawf.runtime.runtimes.claude.mcp_config import render_run_server_config as claude

        return claude(binding, **arguments)
    if runtime_id == "codex":
        from eawf.runtime.runtimes.codex.mcp_config import render_run_server_config as codex

        return codex(binding, **arguments)
    if runtime_id == "opencode":
        from eawf.runtime.runtimes.opencode.mcp_config import render_run_server_config as opencode

        return opencode(binding, **arguments)
    raise ServerTableError(f"no per-run server configuration is rendered for {runtime_id!r}")


def materialize_run_server_config(config: RunServerConfig) -> tuple[str, ...]:
    """Write a lane's configuration files and return the paths written.

    Args:
        config: The rendered configuration.

    Returns:
        The absolute paths written, sorted.

    Raises:
        OSError: A file could not be written.
    """
    written: list[str] = []
    for path_text, body in sorted(config.files.items()):
        path = Path(path_text)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
        written.append(str(path))
    logger.info(f"materialize_run_server_config runtime={config.runtime_id} files={len(written)}")
    return tuple(written)


_compile_failure_codes()


__all__ = [
    "DAEMON_REFUSAL_CODES",
    "FORWARD_FAILURE_CODES",
    "MCP_PROTOCOL_VERSION",
    "SEMANTIC_CALL_METHOD",
    "SERVER_NAME",
    "ForwardFailure",
    "RunServerBinding",
    "RunServerConfig",
    "SemanticStdioServer",
    "SemanticTransport",
    "ServerTableError",
    "ambient_tool_denial",
    "materialize_run_server_config",
    "mcp_tool_name",
    "run_server_config_for",
    "serve_semantic_stdio",
]
