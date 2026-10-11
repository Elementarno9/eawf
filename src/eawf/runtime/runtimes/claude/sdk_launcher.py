"""Drive one native Claude Run through the Claude Agent SDK.

The SDK starts the provider process itself and merges the daemon's whole
environment into it, so none of the containment the daemon owes a Run can
be expressed through the SDK's own options. The SDK is therefore pointed
at a per-Run wrapper under the Run's managed isolation directory rather
than at ``claude``: the wrapper starts a new session, records its process
group in :data:`PGID_FILENAME`, keeps only the environment the scrub
admitted, and replaces itself with the jail-wrapped ``claude``. Everything
the SDK appends to its command line reaches ``claude`` after the jail
prefix, so the jail confines the child exactly as it confined the headless
spawn.

The Run's tools are an allow-list, not a deny-list. Claude Code ships
built-in tools faster than any deny-list can name them, and one it does
not name (``Monitor`` runs shell commands) is an unjailed shell beside the
brokered catalog. So the child is started with no built-in tool at all,
the per-Run MCP server is its only tool source, and the tools the child
reports in its session announcement are checked against that grant: a
child that reports any other tool is reaped and the launch refused.

The launch is split from the turn. :func:`launch_claude_run` returns as
soon as the provider reports its session, and the rest of the turn runs in
a task the daemon awaits; that task meters usage as it arrives, reaps the
process group when the budget says so, and resolves to a
:class:`~eawf.runtime.runtimes.adapter.TurnOutcome`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shlex
import shutil
import sys
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    CLIConnectionError,
    CLIJSONDecodeError,
    CLINotFoundError,
    Message,
    ProcessError,
    ResultError,
    ResultMessage,
    SystemMessage,
)
from claude_agent_sdk.types import McpServerConfig, SettingSource, SystemPromptPreset
from pydantic import ValidationError

from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.platform.process_liveness import hold_process_handle, release_process_handle
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.native_launch import (
    MCP_ARTIFACT_DIRNAME,
    RUN_TMP_ENV_KEYS,
    native_run_server_config,
    prepare_run_tmpdir,
)
from eawf.runtime.runtimes import cancel
from eawf.runtime.runtimes.adapter import (
    ErrorClass,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    RuntimeSpawnError,
    TurnOutcome,
    acquire_spawn_slot,
    ambient_denied_tools,
    compose_worker_hello,
    hello_sdk_version,
    observed_runtime_tuple,
    release_spawn_slot,
)
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.managed_run import prepare_managed_isolation
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.runtimes.transcript_items import ClaudeTranscriptItems
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir
from eawf.runtime.session.host_session import claude_code_provider

logger = logging.getLogger(__name__)

#: The wrapper the SDK starts in place of ``claude``.
WRAPPER_FILENAME: Final = "claude-wrapper"

#: What the wrapper reads: the jailed argv, the admitted keys, the pgid path.
WRAPPER_CONFIG_FILENAME: Final = "claude-wrapper.json"

#: Where the wrapper records the process group it started.
PGID_FILENAME: Final = "worker.pgid"

#: The variable naming the directory Claude Code keeps its own scratch in.
CLAUDE_TMPDIR_ENV: Final = "CLAUDE_CODE_TMPDIR"

#: The provider's announcement of its session.
_INIT_SUBTYPE: Final = "init"

#: How long an interrupt may take before the kill ladder runs regardless.
_INTERRUPT_SECONDS: Final = 5.0

#: How long a failed child's last stderr lines are given to arrive. The
#: SDK reads stderr on a task of its own, which can trail the exit it reports.
_STDERR_SETTLE_SECONDS: Final = 0.05

#: The terminal reasons the provider reports for a turn it was told to abort.
_ABORTED_REASONS: Final = frozenset({"aborted_streaming", "aborted_tools"})

#: The wrapper body, run as ``python -I -c``. It holds no secret: the values
#: of the admitted keys reach it through the SDK's own environment merge, and
#: only their names are written to disk.
_WRAPPER_BODY: Final = """\
import json, os, sys
with open(sys.argv[1], encoding="utf-8") as handle:
    config = json.load(handle)
env = {key: os.environ[key] for key in config["env_keys"] if key in os.environ}
os.setsid()
target = config["pgid_path"]
with open(target + ".tmp", "w", encoding="utf-8") as handle:
    handle.write(str(os.getpgid(0)))
os.replace(target + ".tmp", target)
argv = config["argv"] + sys.argv[2:]
try:
    os.execve(argv[0], argv, env)
except OSError as error:
    sys.stderr.write(f"claude wrapper: cannot start {argv[0]}: {error}\\n")
    sys.exit(127)
"""


@dataclass(frozen=True, slots=True)
class _LaneOptions:
    """The SDK options one lane's argv flags translate to."""

    mcp_servers: dict[str, McpServerConfig]
    strict_mcp_config: bool
    allowed_tools: list[str]
    lane_denied_tools: str | None
    setting_sources: list[SettingSource] | None


def _lane_options(flags: Sequence[str], files: Mapping[str, str]) -> _LaneOptions:
    """Translate the lane's and the isolation's argv flags into SDK options.

    A flag with no translation is refused rather than dropped, because each
    of these flags withholds something from the child.

    Args:
        flags: The per-Run MCP flags followed by the isolation flags.
        files: The per-Run configuration files, by path.

    Returns:
        The options the flags spell.

    Raises:
        RuntimeSpawnError: A flag has no SDK option, a value is missing, or
            the MCP document named is not one the lane rendered, or a
            settings source is named.
    """
    servers: dict[str, McpServerConfig] = {}
    strict = False
    allowed: list[str] = []
    lane_denied: str | None = None
    setting_sources: list[SettingSource] | None = None
    index = 0
    while index < len(flags):
        flag = flags[index]
        if flag == "--strict-mcp-config":
            strict = True
            index += 1
            continue
        if index + 1 >= len(flags):
            raise RuntimeSpawnError(f"lane flag {flag!r} carries no value")
        value = flags[index + 1]
        index += 2
        if flag == "--mcp-config":
            if value not in files:
                raise RuntimeSpawnError(f"lane MCP document {value!r} was not rendered")
            servers = json.loads(files[value])["mcpServers"]
        elif flag == "--allowedTools":
            allowed = value.split()
        elif flag == "--disallowedTools":
            lane_denied = value
        elif flag == "--setting-sources":
            # Only the empty source list is spelled here; a named source
            # would load a settings file the isolation exists to keep out.
            if value:
                raise RuntimeSpawnError(f"lane setting sources {value!r} are not empty")
            setting_sources = []
        else:
            raise RuntimeSpawnError(f"lane flag {flag!r} has no Agent SDK option")
    return _LaneOptions(
        mcp_servers=servers,
        strict_mcp_config=strict,
        allowed_tools=allowed,
        lane_denied_tools=lane_denied,
        setting_sources=setting_sources,
    )


def _resolve_executable(name: str) -> str:
    """Return *name* as an absolute path the wrapper can ``execve``.

    Raises:
        RuntimeSpawnError: *name* names no executable, on PATH or as a path.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeSpawnError(f"executable not found: {name!r}", exit_status=127)
    return os.path.abspath(resolved)


def _write_wrapper(
    run_dir: Path, *, argv: Sequence[str], env_keys: Sequence[str], python: str
) -> Path:
    """Write the per-Run wrapper the SDK starts in place of ``claude``.

    Args:
        run_dir: The Run's managed isolation directory.
        argv: The jail prefix and the ``claude`` binary, absolute first.
        env_keys: The only variable names the child may receive.
        python: The interpreter the wrapper body runs on.

    Returns:
        The executable wrapper.

    Raises:
        OSError: The wrapper or its configuration could not be written.
    """
    config_path = run_dir / WRAPPER_CONFIG_FILENAME
    config = {
        "argv": list(argv),
        "env_keys": sorted(env_keys),
        "pgid_path": str(run_dir / PGID_FILENAME),
    }
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    wrapper = run_dir / WRAPPER_FILENAME
    command = shlex.join([python, "-I", "-c", _WRAPPER_BODY, str(config_path)])
    wrapper.write_text(f'#!/bin/sh\nexec {command} "$@"\n', encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


class _UsageFold:
    """Fold the provider's per-call usage into the running total the meter expects.

    An assistant message's usage is what one API call billed, and the
    provider repeats one message once per content block, so readings are
    deduplicated by message and summed. The result message's usage is the
    turn's own total; it is adopted only where it exceeds the sum.
    """

    def __init__(self) -> None:
        """Start the fold at zero, with no call yet seen."""
        self._seen: set[str] = set()
        self._input_tokens = 0
        self._output_tokens = 0
        self._cache_read_input_tokens = 0

    def observe(self, message: AssistantMessage | ResultMessage) -> UsageSample | None:
        """Fold *message*'s usage in and return the new running total.

        Args:
            message: An assistant or result message of the turn.

        Returns:
            The running total after *message*, or ``None`` when it adds
            nothing: no usage, a malformed usage block, a repeated call, or
            a turn total no larger than the sum already relayed.
        """
        usage = message.usage
        if not isinstance(usage, dict):
            return None
        try:
            reading = UsageSample(
                input_tokens=usage.get("input_tokens") or 0,
                output_tokens=usage.get("output_tokens") or 0,
                cache_read_input_tokens=usage.get("cache_read_input_tokens") or 0,
            )
        except ValidationError, TypeError:
            return None
        if isinstance(message, ResultMessage):
            totals = (
                max(self._input_tokens, reading.input_tokens),
                max(self._output_tokens, reading.output_tokens),
                max(self._cache_read_input_tokens, reading.cache_read_input_tokens),
            )
            if totals == (self._input_tokens, self._output_tokens, self._cache_read_input_tokens):
                return None
            self._input_tokens, self._output_tokens, self._cache_read_input_tokens = totals
        else:
            key = message.message_id or message.uuid
            if key is not None:
                if key in self._seen:
                    return None
                self._seen.add(key)
            self._input_tokens += reading.input_tokens
            self._output_tokens += reading.output_tokens
            self._cache_read_input_tokens += reading.cache_read_input_tokens
        return UsageSample(
            input_tokens=self._input_tokens,
            output_tokens=self._output_tokens,
            cache_read_input_tokens=self._cache_read_input_tokens,
        )


def _result_text(message: ResultMessage) -> str:
    """Return the classifiable text of an error result."""
    parts = [message.subtype, str(message.api_error_status or ""), message.result or ""]
    parts.extend(message.errors or ())
    return " ".join(part for part in parts if part)


@dataclass(slots=True)
class _Turn:
    """One SDK session from its launch until the turn ends.

    Attributes:
        client: The connected SDK client.
        stream: The client's message stream, shared by the launch and the turn.
        adapter: The adapter whose error taxonomy classifies a failure.
        request: The launch request the turn serves.
        deadline: The event-loop time the Run's wall ceiling falls at.
        stderr_lines: What the child wrote to stderr, as it wrote it.
        pending: Messages that arrived before the session was announced.
        pgid: The worker's process group, once recorded.
        runtime: The runtime as the init message reported it, once announced.
        terminated: Whether the budget reaped the worker.
        fold: The running usage total the sink is handed.
        transcript: The reading of the turn's messages as transcript items.
        slot_held: Whether the turn holds one of the process-wide spawn slots.
        granted_tools: The only tools the announced session may report.
        provider: Who serves the model, as the child's environment routes it; the
            init message names no provider, so this is read before the launch.
    """

    client: ClaudeSDKClient
    stream: AsyncIterator[Message]
    adapter: claude_adapter.ClaudeAdapter
    request: NativeLaunchRequest
    deadline: float
    stderr_lines: list[str]
    pending: list[Message] = field(default_factory=list)
    pgid: int | None = None
    runtime: RunRuntimeTuple | None = None
    terminated: bool = False
    fold: _UsageFold = field(default_factory=_UsageFold)
    transcript: ClaudeTranscriptItems = field(default_factory=ClaudeTranscriptItems)
    slot_held: bool = False
    granted_tools: frozenset[str] = frozenset()
    provider: str | None = None

    def stderr(self) -> bytes:
        """Return the child's stderr so far."""
        return "\n".join(self.stderr_lines).encode()

    async def announced_session(self, pgid_path: Path) -> tuple[str, int]:
        """Read messages until the provider announces its session.

        Args:
            pgid_path: Where the wrapper recorded the worker's group.

        Returns:
            The provider's session id and the worker's process group.

        Raises:
            RuntimeSpawnError: The stream ended, failed or reported a
                result before any session was announced, or the session
                holds a tool outside the Run's grant.
        """
        async for message in self.stream:
            if isinstance(message, SystemMessage) and message.subtype == _INIT_SUBTYPE:
                session = message.data.get("session_id")
                if not isinstance(session, str) or not session:
                    raise RuntimeSpawnError("claude announced a session with no session_id")
                self.pgid = int(pgid_path.read_text(encoding="utf-8"))
                self._check_tools(message.data.get("tools"))
                self.runtime = observed_runtime_tuple(
                    self.adapter.id,
                    harness_version=message.data.get("claude_code_version"),
                    provider=self.provider,
                    model=message.data.get("model"),
                )
                return session, self.pgid
            if isinstance(message, ResultMessage):
                raise RuntimeSpawnError(
                    f"claude reported a result before announcing its session: "
                    f"{_result_text(message)!r}",
                    exit_status=1,
                    stderr=_result_text(message).encode(),
                )
            self.pending.append(message)
        raise RuntimeSpawnError(
            "claude exited before announcing its session",
            exit_status=None,
            stderr=self.stderr(),
        )

    def _check_tools(self, reported: object) -> None:
        """Refuse a session whose tools are not a subset of the grant.

        The tool restriction is enforced by the child, so the child's own
        report is the only evidence it took effect; a report that is absent
        or malformed is treated as an unrestricted session.

        Raises:
            RuntimeSpawnError: The report is not a list of names, or names a
                tool the Run was not granted.
        """
        if not isinstance(reported, list) or not all(isinstance(name, str) for name in reported):
            raise RuntimeSpawnError(
                f"tool_allowlist_unverified: claude reported no tool list; "
                f"run={self.request.spec.run_ref}"
            )
        stray = sorted(set(reported) - self.granted_tools)
        if stray:
            raise RuntimeSpawnError(
                f"tool_allowlist_unenforced: claude holds ungranted tools {stray}; "
                f"run={self.request.spec.run_ref}"
            )

    async def run(self) -> TurnOutcome:
        """Drive the turn to its end and say how it ended, on the runtime it ran."""
        outcome = await self._outcome()
        return outcome.model_copy(update={"runtime_tuple": self.runtime})

    async def _outcome(self) -> TurnOutcome:
        """Drive the turn to its end and say how it ended."""
        try:
            async with asyncio.timeout_at(self.deadline):
                return await self._consume()
        except TimeoutError:
            await self._reap()
            return TurnOutcome(
                exit_kind="failed",
                error_text=f"claude turn passed its wall ceiling of "
                f"{self.request.spec.limits.wall_seconds}s",
                error_class="RUNTIME_TIMEOUT",
            )
        except Exception as error:
            if self.terminated:
                return TurnOutcome(exit_kind="budget_terminated")
            return await self._failed(error)
        finally:
            await self.close()

    async def _consume(self) -> TurnOutcome:
        """Relay every message until the result, or until the budget reaps the worker."""
        for message in self.pending:
            outcome = await self._handle(message)
            if outcome is not None:
                return outcome
        self.pending.clear()
        async for message in self.stream:
            outcome = await self._handle(message)
            if outcome is not None:
                return outcome
        if self.terminated:
            return TurnOutcome(exit_kind="budget_terminated")
        return TurnOutcome(
            exit_kind="failed",
            error_text="claude ended the turn without a result",
            error_class=self.adapter.parse_error(1, self.stderr()),
        )

    async def _handle(self, message: Message) -> TurnOutcome | None:
        """Relay *message* and answer the turn's outcome once it has one."""
        if isinstance(message, AssistantMessage | ResultMessage):
            await self._relay_usage(message)
            if self.terminated:
                await self._reap()
                return TurnOutcome(exit_kind="budget_terminated")
        if self.request.message_sink is not None:
            for item in self.transcript.items(message):
                await self.request.message_sink(item)
        if not isinstance(message, ResultMessage):
            return None
        if message.is_error:
            text = _result_text(message)
            return TurnOutcome(
                exit_kind="failed",
                error_text=text,
                result_is_error=True,
                error_class=self.adapter.parse_error(1, text.encode()),
            )
        if message.terminal_reason in _ABORTED_REASONS:
            return TurnOutcome(exit_kind="interrupted")
        return TurnOutcome(exit_kind="completed", answer_text=message.result)

    async def _relay_usage(self, message: AssistantMessage | ResultMessage) -> None:
        """Hand the running total to the usage sink and note a reap."""
        if self.request.usage_sink is None or self.terminated:
            return
        sample = self.fold.observe(message)
        if sample is None:
            return
        self.terminated = await self.request.usage_sink(sample, self.pgid)

    async def _reap(self) -> None:
        """Interrupt the turn, then signal the group through the grace-then-kill ladder."""
        with suppress(Exception):
            await asyncio.wait_for(self.client.interrupt(), timeout=_INTERRUPT_SECONDS)
        if self.pgid is not None:
            await asyncio.to_thread(cancel.cancel_with_grace, self.pgid)

    async def _failed(self, error: Exception) -> TurnOutcome:
        """Return the failed outcome *error* classifies to."""
        spawn_error = await _spawn_error(error, self)
        error_class: ErrorClass = self.adapter.parse_error(
            spawn_error.exit_status if spawn_error.exit_status is not None else 1,
            spawn_error.stderr,
        )
        return TurnOutcome(
            exit_kind="failed",
            error_text=str(spawn_error),
            result_is_error=isinstance(error, ResultError),
            error_class=error_class,
        )

    async def close(self) -> None:
        """Disconnect the client, drop the worker's held handle and free its slot."""
        with suppress(Exception):
            await self.client.disconnect()
        if self.pgid is not None:
            release_process_handle(self.pgid)
        if self.slot_held:
            release_spawn_slot()
            self.slot_held = False


async def _spawn_error(error: BaseException, turn: _Turn) -> RuntimeSpawnError:
    """Map an SDK failure onto the spawn error the taxonomy classifies.

    Args:
        error: What the SDK or the launcher raised.
        turn: The turn whose stderr the error is classified with.

    Returns:
        The equivalent :class:`RuntimeSpawnError`.
    """
    if isinstance(error, RuntimeSpawnError):
        return error
    if isinstance(error, TimeoutError):
        return RuntimeSpawnError(
            f"claude did not announce its session within {turn.request.spec.limits.wall_seconds}s",
            exit_status=124,
        )
    if isinstance(error, CLINotFoundError):
        return RuntimeSpawnError(f"claude cli not found: {error}", exit_status=127)
    if isinstance(error, ResultError):
        text = " ".join(
            part
            for part in (error.subtype, str(error.api_error_status or ""), error.result or "")
            if part
        )
        return RuntimeSpawnError(
            f"claude reported an error result: {text!r}",
            exit_status=error.exit_code,
            stderr=text.encode(),
        )
    if isinstance(error, ProcessError):
        await asyncio.sleep(_STDERR_SETTLE_SECONDS)
        stderr = turn.stderr()
        snippet = stderr.decode(errors="replace").strip()[:200]
        return RuntimeSpawnError(
            f"claude exited nonzero: status={error.exit_code} stderr={snippet!r}",
            exit_status=error.exit_code,
            stderr=stderr,
        )
    if isinstance(error, CLIJSONDecodeError):
        return RuntimeSpawnError(f"claude output is not valid json: {error}")
    if isinstance(error, CLIConnectionError):
        return RuntimeSpawnError(f"claude could not be started: {error}", stderr=turn.stderr())
    return RuntimeSpawnError(f"claude session failed: {error}", stderr=turn.stderr())


def _sdk_options(
    request: NativeLaunchRequest,
    *,
    lane: _LaneOptions,
    wrapper: Path,
    child_env: Mapping[str, str],
    stderr_lines: list[str],
) -> ClaudeAgentOptions:
    """Return the SDK options one native Run is started with."""
    spec = request.spec
    preset: SystemPromptPreset = {"type": "preset", "preset": "claude_code"}
    return ClaudeAgentOptions(
        cli_path=wrapper,
        cwd=request.workspace,
        model=spec.model_id,
        system_prompt=preset,
        # Headless turns have no one to answer a permission prompt; the
        # boundary is the jail plus the tool allow-list below.
        permission_mode="bypassPermissions",
        mcp_servers=lane.mcp_servers,
        strict_mcp_config=lane.strict_mcp_config,
        # ``tools`` is the CLI's ``--tools``: the built-ins the child holds
        # at all, so a built-in no deny-list names is absent too. A compiled
        # spec pins ``ambient_provider_tools`` false, so the set is empty and
        # the Run's own MCP server is its only tool source. ``allowed_tools``
        # only pre-approves, which bypass mode makes moot.
        tools=[],
        allowed_tools=lane.allowed_tools,
        # The denials stay as a second layer behind the allow-list.
        disallowed_tools=ambient_denied_tools(spec),
        # The lane's own ambient denial rides as a second deny flag, exactly
        # as the headless spawn passed it beside the spec's denials.
        extra_args=({"disallowedTools": lane.lane_denied_tools} if lane.lane_denied_tools else {}),
        setting_sources=lane.setting_sources,
        env=dict(child_env),
        stderr=stderr_lines.append,
    )


async def launch_claude_run(
    request: NativeLaunchRequest,
    *,
    adapter: claude_adapter.ClaudeAdapter,
    driver_version: str,
    worker_protocol_version: str,
    event_codec_version: str,
) -> NativeLaunchOutcome:
    """Start one native Run's Claude session and return once it is announced.

    Args:
        request: The compiled spec, the sealed capsule, the resolved
            workspace and the announcement sequence expected back.
        adapter: The adapter naming the ``claude`` binary and classifying
            failures.
        driver_version: The driver version the hello reports when claude
            reports none of its own.
        worker_protocol_version: The worker protocol the hello reports.
        event_codec_version: The event codec the hello reports.

    Returns:
        The session, the worker's pid, its hello and the turn still running.

    Raises:
        RuntimeSpawnError: The host cannot run the wrapper, the CLI is not
            found, the per-Run MCP configuration is refused, or the child
            failed or reported an error before announcing its session.
    """
    if sys.platform == "win32":
        raise RuntimeSpawnError(
            "native claude runs need the POSIX session wrapper, which win32 cannot execute"
        )
    spec = request.spec
    deadline = asyncio.get_running_loop().time() + float(spec.limits.wall_seconds)
    server_config = native_run_server_config(request, runtime_id=adapter.id)
    run_dir = request.workspace / MCP_ARTIFACT_DIRNAME
    isolation = prepare_managed_isolation(run_dir, operator_env=os.environ)
    lane = _lane_options((*server_config.argv_flags, *isolation.argv_flags), server_config.files)
    claude = _resolve_executable(adapter.cli_binary)
    jailed = claude_adapter._maybe_jail_argv(
        [claude],
        runtime=adapter.id,
        cwd=str(request.workspace),
        private_home=isolation.config_dir,
        # The log, not the runtime dir: the Run's server dials the socket beside it.
        storage_dirs=(request.repo_root / ".ea", runtime_dir() / "wal"),
    )
    child_env = build_child_env(adapter.id, extra_path_dir=resolve_binary_dir(adapter.cli_binary))
    adapter._record_env_scrub(child_env, session="", sink=None)
    child_env.update(isolation.env)
    # The jail opens no shared temp area to a native Run, so its temp
    # files go to its own directory. Claude Code keeps its own scratch
    # under /tmp/claude-<uid> whatever TMPDIR says, unless told otherwise.
    run_tmp = str(prepare_run_tmpdir(run_dir))
    child_env.update(dict.fromkeys((*RUN_TMP_ENV_KEYS, CLAUDE_TMPDIR_ENV), run_tmp))
    wrapper = _write_wrapper(
        run_dir,
        argv=[_resolve_executable(jailed[0]), *jailed[1:]],
        env_keys=list(child_env),
        python=sys.executable,
    )
    pgid_path = run_dir / PGID_FILENAME
    pgid_path.unlink(missing_ok=True)
    stderr_lines: list[str] = []
    client = ClaudeSDKClient(
        _sdk_options(
            request,
            lane=lane,
            wrapper=wrapper,
            child_env=child_env,
            stderr_lines=stderr_lines,
        )
    )
    turn = _Turn(
        client=client,
        stream=client.receive_messages(),
        adapter=adapter,
        request=request,
        deadline=deadline,
        stderr_lines=stderr_lines,
        granted_tools=frozenset(lane.allowed_tools),
        provider=claude_code_provider(child_env),
    )
    # The slot is the last gate before the fork and is held for the whole
    # turn, as it was for the headless spawn.
    acquire_spawn_slot()
    turn.slot_held = True
    try:
        async with asyncio.timeout_at(deadline):
            await client.connect()
            await client.query(request.prompt)
            session, pgid = await turn.announced_session(pgid_path)
    except Exception as error:
        if turn.pgid is not None:
            # The child announced itself, so it is running with the prompt.
            await turn._reap()
        await turn.close()
        raise await _spawn_error(error, turn) from error
    # The wrapper replaced itself with the child after setsid, so the
    # child's pid is the group it recorded.
    hold_process_handle(pgid)
    assert turn.runtime is not None
    logger.info(f"launch_claude_run session announced pgid={pgid} model={spec.model_id!r}")
    return NativeLaunchOutcome(
        provider_session_ref=session,
        subprocess_pid=pgid,
        hello=compose_worker_hello(
            spec=spec,
            capsule=request.capsule,
            provider_session_ref=session,
            sdk_version=hello_sdk_version(turn.runtime, driver_version=driver_version),
            worker_protocol_version=worker_protocol_version,
            event_codec_version=event_codec_version,
            hello_sequence=request.hello_sequence,
        ),
        turn=asyncio.create_task(turn.run(), name=f"claude-turn-{spec.run_ref}"),
        runtime_tuple=turn.runtime,
    )


__all__ = [
    "CLAUDE_TMPDIR_ENV",
    "PGID_FILENAME",
    "WRAPPER_CONFIG_FILENAME",
    "WRAPPER_FILENAME",
    "launch_claude_run",
]
