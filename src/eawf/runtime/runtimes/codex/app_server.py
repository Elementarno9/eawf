"""Drive one native Codex Run through the official Codex Python SDK.

The SDK's :class:`~openai_codex.CodexClient` speaks the ``codex app-server``
JSON-RPC protocol and types every message. Its process launch cannot carry
the containment the daemon owes a Run: it merges the daemon's whole
environment into the child and starts it in the daemon's own session. The
SDK is therefore launched on a per-Run wrapper under the Run's MCP artifact
directory rather than on ``codex``: the wrapper starts a new session,
records its process group in :data:`PGID_FILENAME`, sends its stderr to
:data:`STDERR_FILENAME`, keeps only the environment the scrub admitted, and
replaces itself with the jail-wrapped ``codex app-server``. The binary it
starts is the operator's certified ``codex``, never the copy the SDK bundles.

Codex loads its whole setup -- plugins, marketplaces, MCP servers, hooks,
project trust -- from ``CODEX_HOME``, and writes back into it. Each Run
therefore gets a private home beside the wrapper, holding only a
``config.toml`` eawf generates and a link to the operator's credential
file; the jail makes that home writable and the operator's home not.

The launch is split from the turn. :func:`launch_codex_run` returns as soon
as the thread is open and its turn has started, and the rest of the turn
runs in a task the daemon awaits; that task relays usage, agent messages,
the thinking marker and each tool item as they arrive, interrupts and reaps
the worker when the budget says so, and resolves to a
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
import tomllib
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Final

from openai_codex import CodexConfig, CodexError, JsonRpcError
from openai_codex.client import CodexClient
from openai_codex.generated.v2_all import (
    AgentMessageThreadItem,
    AskForApproval,
    AskForApprovalValue,
    ErrorNotification,
    ItemCompletedNotification,
    ItemStartedNotification,
    MessagePhase,
    SandboxMode,
    ThreadStartParams,
    ThreadTokenUsageUpdatedNotification,
    TurnCompletedNotification,
    TurnStatus,
)
from openai_codex.models import JsonObject, Notification
from pydantic import ValidationError

from eawf.kernel.runtime.events import MessageSummaryPayload
from eawf.kernel.runtime.provider import SandboxMode as CompiledSandboxMode
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.runtime.daemon.runtime_dir import runtime_dir
from eawf.runtime.mcp.native_launch import (
    MCP_ARTIFACT_DIRNAME,
    RUN_TMP_ENV_KEYS,
    native_run_server_config,
    prepare_run_tmpdir,
)
from eawf.runtime.runtimes import cancel
from eawf.runtime.runtimes.adapter import (
    CONCURRENT_SPAWN_CAP,
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
from eawf.runtime.runtimes.codex import adapter as codex_adapter
from eawf.runtime.runtimes.host_transcript import (
    SUMMARY_LIMIT,
    WITHHELD_TEXT,
    SpokenMessage,
    scrubbed_words,
)
from eawf.runtime.runtimes.metering import UsageSample
from eawf.runtime.runtimes.transcript_items import CodexTranscriptItems
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir

logger = logging.getLogger(__name__)

#: The wrapper the SDK starts in place of ``codex``.
WRAPPER_FILENAME: Final = "codex-wrapper"

#: What the wrapper reads: the jailed argv, the admitted keys, its output paths.
WRAPPER_CONFIG_FILENAME: Final = "codex-wrapper.json"

#: Where the wrapper records the worker's process group.
PGID_FILENAME: Final = "worker.pgid"

#: Where the worker's stderr goes, so a failure can be classified from it.
STDERR_FILENAME: Final = "worker.stderr"

#: The name every turn's notification reader thread starts with.
TURN_READER_PREFIX: Final = "codex-turn"

#: The threads a turn's notification reads block on, one per spawn slot. A read
#: blocks for as long as the worker is silent, so a turn holds its reader for most of
#: its life; on the default executor, which the daemon's own ``to_thread`` work
#: shares, a full set of turns would starve every other blocking call.
_TURN_READERS: Final = ThreadPoolExecutor(
    max_workers=CONCURRENT_SPAWN_CAP, thread_name_prefix=TURN_READER_PREFIX
)

#: The app-server subcommand and its transport, after the binary.
APP_SERVER_ARGS: Final = ("app-server", "--listen", "stdio://")

#: The Run's private ``CODEX_HOME``, under its MCP artifact directory.
CODEX_HOME_DIRNAME: Final = "codex-home"

#: The configuration codex reads from a home.
CODEX_CONFIG_FILENAME: Final = "config.toml"

#: The credential file a home holds; the only operator file a private home links.
CODEX_AUTH_FILENAME: Final = "auth.json"

#: Features that reach past the Run -- installed plugins and apps, the
#: browser and computer-use tools, hooks, tool suggestions -- each a stable
#: codex 0.159.2 feature key, all off whatever the Run was granted.
_ISOLATION_OVERRIDES: Final = tuple(
    token
    for feature in (
        "apps",
        "browser_use",
        "computer_use",
        "hooks",
        "image_generation",
        "in_app_browser",
        "plugins",
        "remote_plugin",
        "skill_mcp_dependency_install",
        "tool_suggest",
    )
    for token in ("-c", f"features.{feature}=false")
)

#: The thread sandbox each compiled sandbox mode asks codex for.
SANDBOX_MODES: Final[dict[CompiledSandboxMode, SandboxMode]] = {
    "read_only": SandboxMode.read_only,
    "workspace_write": SandboxMode.workspace_write,
}

#: The thread sandbox under the eawf seatbelt, which a nested profile cannot join.
_OUTER_SEATBELT_SANDBOX: Final = SandboxMode.danger_full_access

#: Headless turns have no one to answer an approval; the boundary is the
#: jail plus the sandbox, as it was for ``codex exec``.
_APPROVAL_POLICY: Final = AskForApproval(root=AskForApprovalValue.never)

#: What eawf answers any request the server sends. The SDK answers only
#: with a result, never an error, and its default accepts approvals.
_SERVER_REQUEST_ANSWER: Final[JsonObject] = {"decision": "decline"}

#: How long an interrupt may take before the kill ladder runs regardless.
_INTERRUPT_SECONDS: Final = 5.0

#: The most of the worker's stderr a failure is classified with.
_STDERR_TAIL_BYTES: Final = 64 * 1024

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
stderr = os.open(config["stderr_path"], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
os.dup2(stderr, 2)
os.close(stderr)
argv = config["argv"]
try:
    os.execve(argv[0], argv, env)
except OSError as error:
    sys.stderr.write(f"codex wrapper: cannot start {argv[0]}: {error}\\n")
    sys.exit(127)
"""


def codex_home_config(flags: Sequence[str]) -> dict[str, Any]:
    """Translate ``-c key=value`` flags into the table a home's ``config.toml`` holds.

    Args:
        flags: Interleaved ``-c`` and ``key=<toml value>`` tokens; a later
            flag for the same key wins.

    Returns:
        The nested table, one level per dotted key segment.

    Raises:
        RuntimeSpawnError: A token is not a ``-c`` pair, a value is not
            TOML, or a key sets both a value and a table beneath it.
    """
    config: dict[str, Any] = {}
    if len(flags) % 2:
        raise RuntimeSpawnError(f"lane flags {list(flags)!r} are not -c pairs")
    for flag, assignment in zip(flags[::2], flags[1::2], strict=True):
        key, separator, raw = assignment.partition("=")
        if flag != "-c" or not separator:
            raise RuntimeSpawnError(f"lane flag {flag!r} {assignment!r} is not a -c override")
        try:
            value = tomllib.loads(f"value = {raw}")["value"]
        except tomllib.TOMLDecodeError as error:
            raise RuntimeSpawnError(f"lane override {key!r} is not TOML: {error}") from error
        *tables, leaf = key.split(".")
        table = config
        for name in tables:
            table = table.setdefault(name, {})
            if not isinstance(table, dict):
                raise RuntimeSpawnError(f"lane override {key!r} nests under a value")
        table[leaf] = value
    return config


def _toml_value(value: Any) -> str:
    """Return *value* as a TOML inline value; a JSON string is a TOML basic string."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, int | float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _toml_key(name: str) -> str:
    """Return *name* bare when TOML allows it, else quoted."""
    bare = name and all(c.isascii() and (c.isalnum() or c in "_-") for c in name)
    return name if bare else json.dumps(name, ensure_ascii=False)


def _toml_document(table: dict[str, Any], path: tuple[str, ...] = ()) -> str:
    """Render *table* as a TOML document, values before their sub-tables."""
    nested = {name: value for name, value in table.items() if isinstance(value, dict)}
    # A table holding only sub-tables needs no header of its own.
    titled = path and (len(nested) < len(table) or not table)
    lines = [f"[{'.'.join(_toml_key(name) for name in path)}]"] if titled else []
    lines += [
        f"{_toml_key(name)} = {_toml_value(value)}"
        for name, value in table.items()
        if name not in nested
    ]
    text = "".join(f"{line}\n" for line in lines)
    for name, value in nested.items():
        text += ("\n" if text else "") + _toml_document(value, (*path, name))
    return text


def _operator_auth_file() -> Path:
    """Return the credential file the operator's own codex reads."""
    configured = os.environ.get("CODEX_HOME")
    home = Path(configured) if configured else Path.home() / ".codex"
    return home / CODEX_AUTH_FILENAME


def _prepare_codex_home(home: Path) -> None:
    """Create the Run's private ``CODEX_HOME`` holding only the operator's credential link.

    The credential is linked rather than copied, so a refresh codex writes
    through the link keeps the operator's stored token current. An operator
    without the file gets no link, and codex then authenticates from the
    environment or refuses on its own.

    Args:
        home: The private home, under the Run's MCP artifact directory.

    Raises:
        OSError: The home or the link could not be written.
    """
    home.mkdir(mode=0o700, exist_ok=True)
    link = home / CODEX_AUTH_FILENAME
    link.unlink(missing_ok=True)
    operator_auth = _operator_auth_file()
    if operator_auth.is_file():
        link.symlink_to(operator_auth.resolve())


def _message_payload(item: AgentMessageThreadItem) -> MessageSummaryPayload | None:
    """Return *item*'s words as a transcript block holds them, or ``None``."""
    words = scrubbed_words(item.text)
    if words is None:
        return None
    whole = item.text.strip()
    truncated = words == WITHHELD_TEXT or len(whole) > SUMMARY_LIMIT
    return SpokenMessage(message_role="assistant", summary=words, truncated=truncated, text=whole)


def _user_agent_version(user_agent: str | None) -> str | None:
    """Return the version an ``initialize`` user agent names, or ``None``.

    Codex spells its user agent ``<originator>/<version> (<os>; <arch>) ...``,
    so the version is the first token after the first slash.
    """
    _, separator, rest = (user_agent or "").strip().partition("/")
    tokens = rest.split()
    return tokens[0] if separator and tokens else None


def _usage_sample(update: ThreadTokenUsageUpdatedNotification) -> UsageSample:
    """Return the thread's running total, split the way the meter counts it.

    Codex counts cached input inside ``inputTokens``, so the cached part is
    moved to the cache-read class and never counted twice.
    """
    total = update.token_usage.total
    return UsageSample(
        input_tokens=max(total.input_tokens - total.cached_input_tokens, 0),
        output_tokens=total.output_tokens,
        cache_read_input_tokens=total.cached_input_tokens,
    )


def _decline_server_request(method: str, params: JsonObject | None) -> JsonObject:
    """Answer a server request with a refusal, since a headless Run has no one to ask."""
    logger.info(f"_decline_server_request method={method!r} answer=decline")
    return _SERVER_REQUEST_ANSWER


def _resolve_executable(name: str) -> str:
    """Return *name* as an absolute path the wrapper can ``execve``.

    Raises:
        RuntimeSpawnError: *name* names no executable, on PATH or as a path.
    """
    resolved = shutil.which(name)
    if resolved is None:
        raise RuntimeSpawnError(f"codex not found: {name!r}", exit_status=127)
    return os.path.abspath(resolved)


def _write_wrapper(
    run_dir: Path, *, argv: Sequence[str], env_keys: Sequence[str], python: str
) -> Path:
    """Write the per-Run wrapper the SDK starts in place of ``codex``.

    Args:
        run_dir: The Run's MCP artifact directory.
        argv: The jail prefix, the ``codex`` binary and the app-server
            arguments, absolute executable first.
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
        "stderr_path": str(run_dir / STDERR_FILENAME),
    }
    config_path.write_text(json.dumps(config, sort_keys=True), encoding="utf-8")
    wrapper = run_dir / WRAPPER_FILENAME
    command = shlex.join([python, "-I", "-c", _WRAPPER_BODY, str(config_path)])
    wrapper.write_text(f"#!/bin/sh\nexec {command}\n", encoding="utf-8")
    wrapper.chmod(0o700)
    return wrapper


async def _call[T](method: str, call: Callable[[], T]) -> T:
    """Run one blocking SDK request off the event loop, naming *method* if refused.

    Raises:
        RuntimeSpawnError: The server answered *method* with a JSON-RPC error.
    """
    try:
        return await asyncio.to_thread(call)
    except JsonRpcError as error:
        raise RuntimeSpawnError(
            f"codex app-server refused {method}: {error.message}",
            exit_status=1,
            stderr=error.message.encode(),
        ) from error


@dataclass(slots=True)
class _Turn:
    """One app-server child from its launch until its turn ends.

    Attributes:
        client: The SDK client holding the child.
        adapter: The adapter whose error taxonomy classifies a failure.
        request: The launch request the turn serves.
        deadline: The event-loop time the Run's wall ceiling falls at.
        run_dir: The Run's MCP artifact directory, holding the wrapper's output.
        pgid: The worker's process group, once recorded.
        thread_id: The thread the turn runs on, once open.
        turn_id: The turn, once started.
        terminated: Whether the budget reaped the worker.
        error_text: The last error the server reported without retrying.
        answer_text: The last agent message that was not commentary, whole.
        runtime: The runtime as the server reported it, once the thread is open.
        last_sample: The last usage total handed to the sink.
        transcript: The reading of the turn's item notifications as transcript items.
        slot_held: Whether the turn holds one of the process-wide spawn slots.
    """

    client: CodexClient
    adapter: codex_adapter.CodexAdapter
    request: NativeLaunchRequest
    deadline: float
    run_dir: Path
    pgid: int | None = None
    thread_id: str = ""
    turn_id: str = ""
    terminated: bool = False
    error_text: str | None = None
    answer_text: str | None = None
    runtime: RunRuntimeTuple | None = None
    last_sample: UsageSample | None = None
    transcript: CodexTranscriptItems = field(default_factory=CodexTranscriptItems)
    slot_held: bool = False

    def stderr(self) -> bytes:
        """Return the tail of what the worker wrote to stderr."""
        try:
            data = (self.run_dir / STDERR_FILENAME).read_bytes()
        except FileNotFoundError:
            return b""
        return data[-_STDERR_TAIL_BYTES:]

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
                error_text=f"codex turn passed its wall ceiling of "
                f"{self.request.spec.limits.wall_seconds}s",
                error_class="RUNTIME_TIMEOUT",
            )
        except CodexError as error:
            # The SDK fails the turn's stream when the child's output ends
            # or stops being JSON-RPC.
            if self.terminated:
                return TurnOutcome(exit_kind="budget_terminated")
            return self._failed(self.error_text or str(error), result_is_error=False)
        finally:
            await self.close()

    async def _consume(self) -> TurnOutcome:
        """Relay every notification until the turn completes or the budget reaps it."""
        loop = asyncio.get_running_loop()
        while True:
            note = await loop.run_in_executor(
                _TURN_READERS, self.client.next_turn_notification, self.turn_id
            )
            outcome = await self._handle(note)
            if outcome is not None:
                return outcome

    async def _handle(self, note: Notification) -> TurnOutcome | None:
        """Relay *note* and answer the turn's outcome once it has one."""
        payload = note.payload
        if isinstance(payload, ThreadTokenUsageUpdatedNotification):
            await self._relay_usage(payload)
            if self.terminated:
                await self._reap()
                return TurnOutcome(exit_kind="budget_terminated")
        elif isinstance(payload, ItemCompletedNotification) and isinstance(
            payload.item.root, AgentMessageThreadItem
        ):
            await self._relay_message(payload.item.root)
        elif isinstance(payload, ItemStartedNotification | ItemCompletedNotification):
            await self._relay_items(payload)
        elif isinstance(payload, ErrorNotification):
            if not payload.will_retry:
                self.error_text = payload.error.message
        elif isinstance(payload, TurnCompletedNotification):
            return self._completed(payload)
        elif note.method == "turn/completed":
            return self._failed(
                f"codex app-server ended the turn out of protocol: {payload!r}",
                result_is_error=False,
            )
        return None

    def _completed(self, done: TurnCompletedNotification) -> TurnOutcome:
        """Return the outcome the server's own turn status names."""
        status = done.turn.status
        if status is TurnStatus.completed:
            return TurnOutcome(exit_kind="completed", answer_text=self.answer_text)
        if status is TurnStatus.interrupted:
            return TurnOutcome(exit_kind="budget_terminated" if self.terminated else "interrupted")
        text = done.turn.error.message if done.turn.error is not None else self.error_text
        return self._failed(text or f"codex turn ended {status.value}", result_is_error=True)

    async def _relay_message(self, item: AgentMessageThreadItem) -> None:
        """Keep *item* whole as the answer unless it is commentary, and relay its summary."""
        if item.phase is not MessagePhase.commentary:
            self.answer_text = item.text
        if self.request.message_sink is not None:
            message = _message_payload(item)
            if message is not None:
                await self.request.message_sink(message)

    async def _relay_items(self, note: ItemStartedNotification | ItemCompletedNotification) -> None:
        """Relay the thinking marker or the tool phase *note* states."""
        if self.request.message_sink is None:
            return
        for item in self.transcript.items(note):
            await self.request.message_sink(item)

    async def _relay_usage(self, update: ThreadTokenUsageUpdatedNotification) -> None:
        """Hand a changed running total to the usage sink and note a reap."""
        sample = _usage_sample(update)
        if self.request.usage_sink is None or self.terminated or sample == self.last_sample:
            return
        self.last_sample = sample
        self.terminated = await self.request.usage_sink(sample, self.pgid)

    async def _reap(self) -> None:
        """Interrupt the turn, then signal the group through the grace-then-kill ladder.

        The SDK starts the child with ``subprocess.Popen`` and reaps it only
        in its own close, so the close runs beside the ladder: a leader left
        unreaped would keep the ladder polling a live-looking group.
        """
        if self.turn_id:
            with suppress(Exception):
                await asyncio.wait_for(
                    asyncio.to_thread(self.client.turn_interrupt, self.thread_id, self.turn_id),
                    timeout=_INTERRUPT_SECONDS,
                )
        if self.pgid is not None:
            await asyncio.gather(
                asyncio.to_thread(cancel.cancel_with_grace, self.pgid),
                self._close_client(),
            )

    def _failed(self, text: str, *, result_is_error: bool) -> TurnOutcome:
        """Return the failed outcome *text* and the worker's stderr classify to."""
        return TurnOutcome(
            exit_kind="failed",
            error_text=text,
            result_is_error=result_is_error,
            error_class=self.adapter.parse_error(1, b"\n".join((text.encode(), self.stderr()))),
        )

    async def abandon(self) -> None:
        """End a child whose turn never started: signal its group, then close it.

        Closing the SDK client ends only the process it started; a launch that
        failed after the wrapper recorded the group would otherwise leave the
        rest of the group running.
        """
        if self.pgid is None:
            with suppress(FileNotFoundError, ValueError):
                self.pgid = int((self.run_dir / PGID_FILENAME).read_text(encoding="utf-8"))
        await self._reap()
        await self.close()

    async def close(self) -> None:
        """End the child through the SDK and free the turn's slot."""
        if self.turn_id:
            self.client.unregister_turn_notifications(self.turn_id)
        await self._close_client()
        if self.slot_held:
            release_spawn_slot()
            self.slot_held = False

    async def _close_client(self) -> None:
        """Close the SDK client, tolerating a child that already exited.

        A request written after the child exited stays buffered on its
        stdin, so the SDK's close raises ``BrokenPipeError`` flushing it.
        """
        with suppress(Exception):
            await asyncio.to_thread(self.client.close)


def _spawn_error(error: BaseException, turn: _Turn) -> RuntimeSpawnError:
    """Map a failure before the turn started onto the spawn error the taxonomy classifies.

    Args:
        error: What the SDK or the launcher raised.
        turn: The turn whose stderr the error is classified with.

    Returns:
        The equivalent :class:`RuntimeSpawnError`.
    """
    if isinstance(error, RuntimeSpawnError):
        return error
    stderr = turn.stderr()
    if isinstance(error, TimeoutError):
        return RuntimeSpawnError(
            f"codex did not start its turn within {turn.request.spec.limits.wall_seconds}s",
            exit_status=124,
            stderr=stderr,
        )
    if isinstance(error, ValidationError):
        return RuntimeSpawnError(f"codex app-server answered out of protocol: {error}")
    snippet = stderr.decode(errors="replace").strip()[:200]
    return RuntimeSpawnError(f"{error} stderr={snippet!r}", exit_status=1, stderr=stderr)


async def _open_turn(turn: _Turn, *, sandbox: SandboxMode) -> None:
    """Initialize the server, open the Run's thread and start its one turn.

    Raises:
        RuntimeSpawnError: The wrapper could not be started, the server
            refused a request, or the wrapper recorded no process group.
    """
    request = turn.request
    client = turn.client
    try:
        await asyncio.to_thread(client.start)
    except OSError as error:
        raise RuntimeSpawnError(f"codex could not be started: {error}", exit_status=126) from error
    initialized = await _call("initialize", client.initialize)
    try:
        turn.pgid = int((turn.run_dir / PGID_FILENAME).read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError) as error:
        raise RuntimeSpawnError(f"codex wrapper recorded no process group: {error}") from error
    params = ThreadStartParams(
        cwd=str(request.workspace),
        model=request.spec.model_id,
        sandbox=sandbox,
        approval_policy=_APPROVAL_POLICY,
    )
    opened = await _call("thread/start", lambda: client.thread_start(params))
    turn.thread_id = opened.thread.id
    turn.runtime = observed_runtime_tuple(
        turn.adapter.id,
        harness_version=_user_agent_version(initialized.userAgent),
        provider=opened.model_provider,
        model=opened.model,
    )
    started = await _call("turn/start", lambda: client.turn_start(turn.thread_id, request.prompt))
    turn.turn_id = started.turn.id


async def launch_codex_run(
    request: NativeLaunchRequest,
    *,
    adapter: codex_adapter.CodexAdapter,
    driver_version: str,
    worker_protocol_version: str,
    event_codec_version: str,
) -> NativeLaunchOutcome:
    """Start one native Run's app-server and return once its turn has started.

    Args:
        request: The compiled spec, the sealed capsule, the resolved
            workspace and the announcement sequence expected back.
        adapter: The adapter naming the ``codex`` binary and classifying
            failures.
        driver_version: The driver version the hello reports when codex
            reports none of its own.
        worker_protocol_version: The worker protocol the hello reports.
        event_codec_version: The event codec the hello reports.

    Returns:
        The thread, the worker's process group, its hello and the turn
        still running.

    Raises:
        ConcurrentSpawnCapError: Every spawn slot is already held.
        RuntimeSpawnError: The host cannot run the wrapper, the binary was
            not found, the per-Run MCP configuration is refused, or the
            server refused, failed or timed out a request before the turn
            started.
    """
    if sys.platform == "win32":
        raise RuntimeSpawnError(
            "native codex runs need the POSIX session wrapper, which win32 cannot execute"
        )
    spec = request.spec
    deadline = asyncio.get_running_loop().time() + float(spec.limits.wall_seconds)
    server_config = native_run_server_config(request, runtime_id=adapter.id)
    denials = codex_adapter.codex_tool_denials(ambient_denied_tools(spec))
    codex = _resolve_executable(adapter.cli_binary)
    run_dir = request.workspace / MCP_ARTIFACT_DIRNAME
    # The home and its link exist before the jail is built, which grants
    # write to the home and to the credential the link resolves to only.
    home = run_dir / CODEX_HOME_DIRNAME
    _prepare_codex_home(home)
    jailed = codex_adapter._maybe_jail_argv(
        [codex, *APP_SERVER_ARGS],
        runtime=adapter.id,
        cwd=str(request.workspace),
        private_home=home,
        # The log, not the runtime dir: the Run's server dials the socket beside it.
        storage_dirs=(request.repo_root / ".ea", runtime_dir() / "wal"),
    )
    deferred = codex_adapter.OUTER_SEATBELT_OVERRIDE in jailed
    sandbox = _OUTER_SEATBELT_SANDBOX if deferred else SANDBOX_MODES[spec.sandbox.mode]
    config = codex_home_config(
        (
            "-c",
            f"model={json.dumps(spec.model_id)}",
            "-c",
            f"sandbox_mode={json.dumps(sandbox.value)}",
            "-c",
            f"approval_policy={json.dumps(AskForApprovalValue.never.value)}",
            *_ISOLATION_OVERRIDES,
            *server_config.argv_flags,
            *denials,
        )
    )
    (home / CODEX_CONFIG_FILENAME).write_text(_toml_document(config), encoding="utf-8")
    child_env = build_child_env(adapter.id, extra_path_dir=resolve_binary_dir(adapter.cli_binary))
    child_env["CODEX_HOME"] = str(home)
    # The jail opens no shared temp area to a native Run, so its temp
    # files go to its own directory.
    child_env.update(dict.fromkeys(RUN_TMP_ENV_KEYS, str(prepare_run_tmpdir(run_dir))))
    codex_adapter._record_env_scrub(child_env, session="", sink=None)
    wrapper = _write_wrapper(
        run_dir,
        argv=[_resolve_executable(jailed[0]), *jailed[1:]],
        env_keys=list(child_env),
        python=sys.executable,
    )
    (run_dir / PGID_FILENAME).unlink(missing_ok=True)
    client = CodexClient(
        CodexConfig(
            launch_args_override=(str(wrapper),),
            cwd=str(request.workspace),
            env=child_env,
            client_name="eawf",
            client_title="eawf",
            client_version=driver_version,
        ),
        approval_handler=_decline_server_request,
    )
    turn = _Turn(
        client=client, adapter=adapter, request=request, deadline=deadline, run_dir=run_dir
    )
    # The slot is the last gate before the fork and is held for the whole
    # turn, because one app-server child hosts the turn from start to end.
    acquire_spawn_slot()
    turn.slot_held = True
    try:
        async with asyncio.timeout_at(deadline):
            await _open_turn(turn, sandbox=sandbox)
    except Exception as error:
        await turn.abandon()
        raise _spawn_error(error, turn) from error
    except BaseException:
        # A cancelled launch still ends its child and frees its slot.
        await asyncio.shield(turn.abandon())
        raise
    assert turn.pgid is not None
    assert turn.runtime is not None
    logger.info(f"launch_codex_run turn started pgid={turn.pgid} model={spec.model_id!r}")
    return NativeLaunchOutcome(
        provider_session_ref=turn.thread_id,
        # The wrapper replaced itself with the child after setsid, so the
        # child's pid is the group it recorded.
        subprocess_pid=turn.pgid,
        hello=compose_worker_hello(
            spec=spec,
            capsule=request.capsule,
            provider_session_ref=turn.thread_id,
            sdk_version=hello_sdk_version(turn.runtime, driver_version=driver_version),
            worker_protocol_version=worker_protocol_version,
            event_codec_version=event_codec_version,
            hello_sequence=request.hello_sequence,
        ),
        turn=asyncio.create_task(turn.run(), name=f"codex-turn-{spec.run_ref}"),
        runtime_tuple=turn.runtime,
    )


__all__ = [
    "APP_SERVER_ARGS",
    "CODEX_AUTH_FILENAME",
    "CODEX_CONFIG_FILENAME",
    "CODEX_HOME_DIRNAME",
    "PGID_FILENAME",
    "SANDBOX_MODES",
    "STDERR_FILENAME",
    "TURN_READER_PREFIX",
    "WRAPPER_CONFIG_FILENAME",
    "WRAPPER_FILENAME",
    "codex_home_config",
    "launch_codex_run",
]
