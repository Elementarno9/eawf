"""Codex CLI adapter — implements :class:`RuntimeAdapter`.

Codex CLI = ``codex exec``. There is no caller-side ``cache_control``
marker (OpenAI prompt caching is automatic at the ≥1024-token
threshold); :attr:`supports_cache_control` is therefore ``False``.

Codex session logs live under ``$CODEX_HOME`` (date-sharded JSONL); the
adapter caches ``(session_id → path)`` at dispatch time so subsequent
retries skip the tree walk. v0.3 ships the typed surface only; the live
walk lands in a later wave.

The live :meth:`CodexAdapter.spawn_session` forks ``codex exec --json``
(headless newline-delimited JSON events on stdout) and
:func:`_parse_codex_result` parses that event stream into a typed
:class:`~eawf.runtime.runtimes.adapter.SpawnResult` -- the codex juror
lane for the later cross-vendor jury. The spawn reuses the same
filesystem-jail + env-scrub floor as the claude lane; the jail helpers
are duplicated locally rather than imported from the shared adapter so
this wave touches only the codex module.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import threading
import uuid
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.enums import MeasurementQuality, MeasurementStatus
from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.adapter import (
    ErrorClass,
    RuntimeAdapter,
    RuntimeSpawnError,
    SpawnResult,
)
from eawf.runtime.runtimes.cache_control import inject_cache_control
from eawf.runtime.runtimes.selector import runtime_supports
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.sandbox.egress_proxy import (
    EnforcementSink,
    SandboxError,
    emit_enforcement,
    make_enforcement_event,
)
from eawf.runtime.sandbox.env_scrub import build_child_env, resolve_binary_dir
from eawf.runtime.sandbox.jail import jail_command, jail_supported
from eawf.runtime.sandbox.policy import invert_deny_to_allow

if TYPE_CHECKING:
    from eawf.workflow.agents.specs.models import RoleContract

logger = logging.getLogger(__name__)

#: The OS-jail wrapper binary per platform. The spawn seam jails the child
#: only when the platform supports it AND the wrapper resolves on PATH.
#: Duplicated from the claude lane because this wave edits only the codex
#: module and must not touch the shared adapter surface.
_JAIL_WRAPPER_BINARY: dict[str, str] = {
    "darwin": "sandbox-exec",
    "linux": "bwrap",
}

#: Ceiling on live agent spawns in flight at once. Mirrors the claude lane's
#: floor so the cross-vendor fleet shares one cap: a spawn past the cap fails
#: fast with :class:`ConcurrentSpawnCapError` rather than queueing. The counter
#: is codex-local (each adapter holds its own in-flight count) because this
#: wave edits only the codex module and must not touch the shared adapter; the
#: cap VALUE matches claude so the parity is honest.
_CONCURRENT_SPAWN_CAP: int = 16

#: Live in-flight spawn counter + its lock. Module-global because the cap is
#: per-process (the daemon hosts every spawn); guarded by a lock so the
#: increment / cap-check is atomic under the asyncio + worker-thread mix the
#: daemon runs spawns on.
_spawn_inflight: int = 0
_spawn_lock = threading.Lock()


class ConcurrentSpawnCapError(SandboxError):
    """Raised when a codex spawn would exceed the concurrent-spawn cap.

    Mirrors :class:`eawf.runtime.runtimes.claude.adapter.ConcurrentSpawnCapError`
    so the cross-vendor floor refuses to fork a new jailed child once
    :data:`_CONCURRENT_SPAWN_CAP` are already in flight; a runaway dispatch
    loop fails fast at the spawn boundary rather than exhausting process /
    socket resources.
    """


def _acquire_spawn_slot() -> None:
    """Reserve one in-flight spawn slot or fail fast at the cap.

    Raises:
        ConcurrentSpawnCapError: When :data:`_CONCURRENT_SPAWN_CAP` spawns
            are already in flight.
    """
    global _spawn_inflight
    with _spawn_lock:
        if _spawn_inflight >= _CONCURRENT_SPAWN_CAP:
            raise ConcurrentSpawnCapError(
                f"concurrent spawn cap reached: inflight={_spawn_inflight} "
                f"cap={_CONCURRENT_SPAWN_CAP}"
            )
        _spawn_inflight += 1


def _release_spawn_slot() -> None:
    """Release one in-flight spawn slot (never drops below zero)."""
    global _spawn_inflight
    with _spawn_lock:
        _spawn_inflight = max(_spawn_inflight - 1, 0)


def _record_env_scrub(
    child_env: dict[str, str],
    *,
    session: str,
    sink: EnforcementSink | None,
) -> None:
    """Record the env-scrub decision for a built child env.

    Mirrors the claude lane: compares the scrubbed child env against the
    parent ``os.environ`` and records an ``env-scrub`` enforcement event
    naming how many credential-bearing variables the allowlist dropped. A
    scrub that dropped nothing is an ``info`` row (the floor still ran); a
    scrub that dropped one or more vars is a ``block`` row (creds were
    withheld from the child).

    Args:
        child_env: The scrubbed child env the spawn will use.
        session: The spawning session id stamped on the event.
        sink: The enforcement sink the event is persisted through.
    """
    # Count parent keys WITHHELD from the child (present in the parent env,
    # absent from the scrubbed child). A raw length diff is wrong because the
    # child reseeds floor keys (pinned PATH / defaulted LANG) the parent may
    # lack; the withheld count is the true measure of what the allowlist drops.
    dropped = sum(1 for key in os.environ if key not in child_env)
    emit_enforcement(
        sink,
        make_enforcement_event(
            session=session,
            kind="env-scrub",
            target=f"dropped={dropped}",
            severity="block" if dropped > 0 else "info",
        ),
    )


def _jail_wrapper_binary(platform: str) -> str | None:
    """Return the jail wrapper binary for *platform*, or ``None``.

    Args:
        platform: The platform string (``sys.platform``).

    Returns:
        ``"sandbox-exec"`` on macOS, ``"bwrap"`` on Linux, ``None`` on a
        platform with no FS-jail wrapper (e.g. Windows).
    """
    if platform.startswith("linux"):
        return _JAIL_WRAPPER_BINARY["linux"]
    return _JAIL_WRAPPER_BINARY.get(platform)


def _repo_root_for(path: Path) -> Path | None:
    """Return the git repo root containing *path*, or ``None``.

    Resolved via ``git rev-parse --show-toplevel`` so the jail confines the
    child to its actual repo root. A path outside any git working tree
    yields ``None`` (the spawn then runs unjailed with a warning rather
    than confining to a bogus root).
    """
    from eawf.runtime.worktree.git import repo_root

    try:
        return repo_root(path)
    except Exception as exc:
        logger.warning(f"_repo_root_for path={path!s} reason={exc!r}")
        return None


def _maybe_jail_argv(
    argv: list[str],
    *,
    runtime: str,
    cwd: str | None,
    session: str = "",
    sink: EnforcementSink | None = None,
) -> list[str]:
    """Prefix *argv* with the OS jail when the host supports it.

    The jail is applied only when (a) the platform has an FS-jail wrapper
    (:func:`~eawf.runtime.sandbox.jail.jail_supported`) AND (b) that wrapper
    binary resolves on PATH. When the wrapper is absent (e.g. CI without
    bubblewrap) the child runs UNJAILED with a loud warning so the spawn
    keeps working on hosts without the tool and CI stays green.

    The daemon stays the sole session-setter: only the argv gains the
    prefix here; ``start_new_session`` / ``env`` / ``cwd`` on the spawn are
    untouched, so the wrapper inherits the daemon-set process group and the
    kill ladder reaps the whole tree unchanged.

    Args:
        argv: The child's own argv (``["codex", "exec", ...]``).
        runtime: The runtime adapter id selecting the own-cred carve-out.
        cwd: The spawn cwd. ``None`` defaults the jail confinement to the
            repo root containing the process cwd; when the process cwd is
            not inside a discoverable root the spawn runs unjailed with a
            warning rather than confining to a bogus path.
        session: The spawning session id stamped on a ``cwd-guard``
            enforcement event when the cwd-outside-repo fallback fires.
        sink: The enforcement sink a ``cwd-guard`` event is persisted
            through; ``None`` only logs.

    Returns:
        Either ``jail_command(argv, ...)`` (jailed) or *argv* unchanged
        (unjailed fallback).
    """
    if not jail_supported():
        logger.warning(
            f"spawn_session jail=unavailable platform={sys.platform!r} runtime={runtime!r}"
        )
        return argv

    wrapper = _jail_wrapper_binary(sys.platform)
    if wrapper is None or shutil.which(wrapper) is None:
        logger.warning(
            f"spawn_session jail=unavailable wrapper={wrapper!r} runtime={runtime!r} "
            "reason=binary-not-on-path"
        )
        return argv

    cwd_path = Path(cwd) if cwd is not None else Path.cwd()
    root = _repo_root_for(cwd_path)
    if root is None or not cwd_path.exists() or not is_path_inside(cwd_path, root=root):
        logger.warning(
            f"spawn_session jail=unavailable runtime={runtime!r} cwd={cwd_path!s} "
            "reason=cwd-outside-repo-root"
        )
        # A cwd that escapes (or cannot resolve) the repo root is a cwd-guard
        # refusal: the jail confinement is dropped, so record the
        # degraded-but-continued decision on the denial timeline.
        emit_enforcement(
            sink,
            make_enforcement_event(
                session=session,
                kind="cwd-guard",
                target=str(cwd_path),
                severity="warn",
            ),
        )
        return argv

    jailed = jail_command(argv, runtime=runtime, cwd=cwd_path, root=root)
    logger.info(f"spawn_session jail=on wrapper={wrapper!r} runtime={runtime!r} cwd={cwd_path!s}")
    return jailed


_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate[_ -]?limit)\b", re.IGNORECASE)
# An account-entitlement failure: 401/403, an expired/invalid credential, or a
# model the account is not entitled to (a ChatGPT-account codex rejects the
# API-key-only model ids with "not supported when using Codex with a ChatGPT
# account"). All route to HALT -- switching runtime or retrying cannot fix an
# account-level restriction; the operator must adjust auth or the model config.
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|chatgpt subscription expired"
    rb"|unauthor[iz][sez]+ed)\b"
    rb"|not supported when using codex",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b5\d\d\b")
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")

#: Dotted codex config key for the model reasoning-effort level, confirmed
#: live against ``codex exec -c model_reasoning_effort=<level> ...`` (codex
#: 0.134.0). Passed via ``-c`` so the value lands as a TOML override on the
#: per-call config rather than mutating the user's ``config.toml``.
_REASONING_EFFORT_CONFIG_KEY = "model_reasoning_effort"

#: Dotted codex config namespace for the per-tool grant table, confirmed live
#: against ``codex exec`` help (codex 0.138.0 exposes ``tools.<name>`` toggles,
#: e.g. ``tools.web_search``). ``codex exec`` has NO single tool-allowlist or
#: tool-deny flag; the per-tool boolean config is the only per-call grant
#: surface, so a deny-list is mapped to ``-c tools.<name>=<bool>`` overrides:
#: the inverted allowlist (universe minus denied) is granted ``true`` and the
#: denied names are pinned ``false`` so a denied tool can never reach the
#: child's effective grant even under a default-allow codex.
_TOOLS_CONFIG_NAMESPACE = "tools"


def _codex_tool_config_key(tool: str) -> str:
    """Return the dotted codex config key for a tool's grant boolean.

    Maps an eawf tool name (e.g. ``"WebSearch"``) to the codex ``tools.<name>``
    dotted config path, lowercasing the camel/Pascal name to codex's
    snake-ish convention (``tools.websearch``). Codex's ``-c`` override parses
    the value as TOML, so the boolean is emitted bare (``true`` / ``false``).

    Args:
        tool: The eawf tool name to map.

    Returns:
        The dotted config key (e.g. ``"tools.websearch"``).
    """
    return f"{_TOOLS_CONFIG_NAMESPACE}.{tool.lower()}"


def _codex_tool_grant_overrides(denied_tools: Sequence[str]) -> list[str]:
    """Build the ``-c tools.<name>=<bool>`` overrides for a deny-list.

    Codex grants tools by config, not by a deny flag, so the deny-list is
    expressed as its inverted allowlist:
    :func:`~eawf.runtime.sandbox.policy.invert_deny_to_allow` yields the
    universe-minus-denied allow set, each granted ``true``; the denied names
    are additionally pinned ``false``. The denied set is therefore absent from
    the ``true`` grant AND explicitly disabled, so a deny cannot silently pass
    through to the child's effective tool grant.

    The overrides are interleaved as ``["-c", "<key>=<bool>", ...]`` in a
    deterministic order (the allow set is sorted by the inversion helper; the
    denied set is sorted here) so the argv is reproducible.

    Args:
        denied_tools: The per-wave deny-list (already known non-empty by the
            caller; an empty list yields no overrides).

    Returns:
        The flat ``-c`` override token list to splice into the codex argv.
    """
    overrides: list[str] = []
    for allowed in invert_deny_to_allow(list(denied_tools)):
        overrides.extend(("-c", f"{_codex_tool_config_key(allowed)}=true"))
    for denied in sorted(set(denied_tools)):
        overrides.extend(("-c", f"{_codex_tool_config_key(denied)}=false"))
    return overrides


def _usage_int(
    usage: dict[str, object],
    key: str,
    *,
    missing: int | None = None,
) -> int | None:
    """Read a non-negative token count from a codex ``usage`` block.

    The ``usage`` block is decoded from JSON so each value is typed
    ``object``; this coerces *key* to ``int`` (defaulting a missing /
    falsey value to ``0``) so the caller stays type-clean. A value that is
    not int-coercible falls back to ``0`` rather than raising -- a malformed
    usage figure must not sink an otherwise-complete spawn result.

    Args:
        usage: The decoded ``turn.completed`` usage mapping.
        key: The token-count key to read (e.g. ``"input_tokens"``).

    Returns:
        The coerced integer, or ``0`` when absent / falsey / non-coercible.
    """
    if key not in usage:
        return missing
    value = usage[key]
    if value == 0:
        return 0
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, (str, float)):
        try:
            parsed = int(value)
        except TypeError, ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


#: Chunk size for the incremental stdout / stderr drain. Reads in fixed-size
#: byte chunks (rather than line-bounded ``readline``) so an arbitrarily long
#: JSONL frame -- a large ``agent_message`` body -- can never overrun the
#: StreamReader line-length limit; the line framing for ``on_chunk`` is done
#: in-helper over the accumulated buffer.
_STREAM_CHUNK_BYTES: int = 65536


async def _drain_stream(stream: asyncio.StreamReader | None) -> bytes:
    """Drain a subprocess stream to its full bytes (no per-line callback).

    Reads *stream* in :data:`_STREAM_CHUNK_BYTES` chunks until EOF and returns
    the concatenated bytes -- byte-equivalent to what ``communicate`` would
    have collected. Used for stderr (and for stdout when no ``on_chunk`` is
    wired) so the two streams drain concurrently without deadlock.

    Args:
        stream: The subprocess stream reader, or ``None`` (no PIPE) yielding
            empty bytes.

    Returns:
        The full accumulated stream bytes.
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    while True:
        chunk = await stream.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


async def _drain_stream_chunked(
    stream: asyncio.StreamReader | None,
    on_chunk: Callable[[str], Awaitable[None]],
) -> bytes:
    """Drain *stream* to full bytes while firing *on_chunk* per stdout line.

    Reads *stream* in :data:`_STREAM_CHUNK_BYTES` chunks, accumulating the raw
    bytes for the result parser AND framing complete newline-terminated lines
    out of a running buffer to fire ``on_chunk`` as each line arrives. The
    decoded line string keeps its trailing newline (matching what a
    ``readline`` loop would yield); a final partial line with no trailing
    newline is emitted at EOF. Decoding uses ``errors="replace"`` so a partial
    multi-byte sequence at a chunk boundary -- or a non-JSON stray line -- can
    never crash the live fan-out. The returned bytes are byte-equivalent to a
    full buffered read, so the existing parser sees the same input.

    Args:
        stream: The subprocess stdout reader, or ``None`` (no PIPE) yielding
            empty bytes and firing no callback.
        on_chunk: The async callback invoked once per decoded line as it
            arrives.

    Returns:
        The full accumulated stdout bytes.
    """
    if stream is None:
        return b""
    chunks: list[bytes] = []
    line_buf = b""
    while True:
        chunk = await stream.read(_STREAM_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        line_buf += chunk
        while b"\n" in line_buf:
            line, line_buf = line_buf.split(b"\n", 1)
            await on_chunk((line + b"\n").decode(errors="replace"))
    if line_buf:
        await on_chunk(line_buf.decode(errors="replace"))
    return b"".join(chunks)


async def _collect_spawn_output(
    proc: asyncio.subprocess.Process,
    *,
    on_chunk: Callable[[str], Awaitable[None]] | None,
) -> tuple[bytes, bytes]:
    """Collect a spawned child's ``(stdout, stderr)`` incrementally.

    Replaces ``proc.communicate`` so stdout can be fanned to *on_chunk* line
    by line as it arrives rather than buffered to process exit. stdout and
    stderr are drained CONCURRENTLY (a single-stream drain would deadlock when
    the child fills the other pipe's OS buffer), then the child is reaped via
    :meth:`asyncio.subprocess.Process.wait` so ``returncode`` is populated --
    exactly the post-conditions ``communicate`` guaranteed. When *on_chunk* is
    ``None`` the stdout drain takes the plain (callback-free) path, so the
    accumulated bytes are byte-equivalent to the buffered result.

    Args:
        proc: The spawned subprocess.
        on_chunk: The per-line async callback, or ``None`` for the buffered
            (byte-equivalent) path.

    Returns:
        The ``(stdout, stderr)`` bytes the result parser consumes.
    """
    if on_chunk is None:
        stdout, stderr = await asyncio.gather(
            _drain_stream(proc.stdout),
            _drain_stream(proc.stderr),
        )
    else:
        stdout, stderr = await asyncio.gather(
            _drain_stream_chunked(proc.stdout, on_chunk),
            _drain_stream(proc.stderr),
        )
    await proc.wait()
    return stdout, stderr


def _decode_codex_events(stdout: bytes) -> list[dict[str, object]]:
    """Decode a ``codex exec --json`` byte stream into event objects.

    Codex emits one JSON object per line on stdout. Blank lines are
    skipped; a stray non-JSON line is skipped rather than failing the whole
    parse (the absence of any usable event is the caller's concern). Only
    JSON objects are kept.

    Args:
        stdout: Raw subprocess stdout bytes (already known non-empty).

    Returns:
        The decoded event objects in stream order.
    """
    events: list[dict[str, object]] = []
    for line in stdout.decode(errors="replace").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            event = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _scan_codex_events(
    events: list[dict[str, object]],
) -> tuple[str, str, dict[str, object] | None]:
    """Scan decoded codex events into ``(session_id, text, usage)``.

    Walks the event stream once, pulling the session id from
    ``thread.started``, the answer text from the LAST ``item.completed``
    ``agent_message`` (so a multi-message turn resolves to the final
    assistant text), and the token usage from ``turn.completed``.

    Args:
        events: Decoded codex event objects in stream order.

    Returns:
        A tuple of the session id (``""`` when the stream carried no
        ``thread.started``), the answer text, and the usage mapping
        (``None`` when no ``turn.completed`` usage was seen).

    Raises:
        RuntimeSpawnError: an ``error`` / ``turn.failed`` event was seen,
            or the stream carried no ``agent_message`` item.
    """
    session_id = ""
    text: str | None = None
    usage: dict[str, object] | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "error":
            detail = _unwrap_codex_error_message(event.get("message"))
            raise RuntimeSpawnError(f"codex reported an error event: {detail!r}")
        if event_type == "turn.failed":
            err = event.get("error")
            raw = err.get("message") if isinstance(err, dict) else None
            detail = _unwrap_codex_error_message(raw)
            raise RuntimeSpawnError(f"codex turn failed: {detail!r}")
        if event_type == "thread.started":
            session_id = str(event.get("thread_id") or "")
        elif event_type == "item.completed":
            item = event.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                text_field = item.get("text")
                text = "" if text_field is None else str(text_field)
        elif event_type == "turn.completed":
            turn_usage = event.get("usage")
            if isinstance(turn_usage, dict):
                usage = turn_usage
    if text is None:
        raise RuntimeSpawnError("codex output carried no agent_message item")
    return session_id, text, usage


def _unwrap_codex_error_message(raw: object) -> str:
    """Return the human-readable message from a codex error payload.

    Codex wraps the upstream API failure as a JSON STRING inside the event's
    ``message`` field (e.g.
    ``{"type":"error","status":400,"error":{"message":"The 'gpt-5-mini' model
    is not supported ..."}}``). This unwraps that envelope to the innermost
    ``error.message`` (or top-level ``message``) so the surfaced detail is the
    400 reason a human reads, not the JSON envelope. A plain (non-JSON) string
    is returned verbatim; a non-string returns ``""``.

    Args:
        raw: The event's ``message`` (or nested ``error.message``) value.

    Returns:
        The innermost human message, or ``""`` when *raw* is not a string.
    """
    if not isinstance(raw, str):
        return ""
    text = raw.strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict) and isinstance(err.get("message"), str):
            return str(err["message"])
        if isinstance(parsed.get("message"), str):
            return str(parsed["message"])
    return text


def _codex_stream_error_detail(stdout: bytes) -> str:
    """Extract the failure message from a codex event stream, or ``""``.

    Codex ``--json`` reports a failed turn as an ``error`` / ``turn.failed``
    event on STDOUT (not stderr), then exits non-zero. The nonzero-exit guard
    in :func:`_parse_codex_result` runs before the stream is scanned, so
    without this the real reason (e.g. a 400 model-rejection) is lost and only
    the empty stderr surfaces. This walks the decoded events and returns the
    LAST error / turn.failed message (unwrapped via
    :func:`_unwrap_codex_error_message`) so the caller can put the actual
    reason in the raised error + feed it to the classifier.

    Args:
        stdout: Raw subprocess stdout bytes (the newline-delimited events).

    Returns:
        The unwrapped failure message, or ``""`` when the stream carried no
        error / turn.failed event (e.g. a crash with no structured output).
    """
    detail = ""
    for event in _decode_codex_events(stdout):
        event_type = event.get("type")
        if event_type == "error":
            detail = _unwrap_codex_error_message(event.get("message"))
        elif event_type == "turn.failed":
            err = event.get("error")
            if isinstance(err, dict):
                detail = _unwrap_codex_error_message(err.get("message"))
    return detail


def _parse_codex_result(
    *,
    runtime: str,
    model: str,
    stdout: bytes,
    stderr: bytes,
    exit_status: int,
    subprocess_pid: int,
    started_at: datetime,
    ended_at: datetime,
) -> SpawnResult:
    """Parse a ``codex exec --json`` event stream into a result.

    Codex ``--json`` emits newline-delimited JSON events on stdout, not a
    single envelope like claude. The events this parser consumes are:

    * ``thread.started`` -- carries ``thread_id`` (the session id).
    * ``item.completed`` with ``item.type == "agent_message"`` -- carries
      the answer ``text``. The LAST such event wins so a multi-message turn
      resolves to the final assistant message.
    * ``turn.completed`` -- carries ``usage`` with ``input_tokens`` (the
      GROSS input total INCLUDING cached), ``cached_input_tokens`` (the
      prompt-cache read count), ``output_tokens``, and
      ``reasoning_output_tokens``.
    * ``error`` / ``turn.failed`` -- a failure event; either raises.

    Codex reports no cache-CREATION tokens and no self-reported cost, so
    :attr:`SpawnResult.cache_creation_input_tokens` and
    :attr:`SpawnResult.cost_usd_reported` stay ``0`` / ``None`` -- the
    metering layer prices that honestly rather than inventing a figure.
    Because codex's ``input_tokens`` is gross, the non-cached count stamped
    onto the result is ``input_tokens - cached_input_tokens`` (clamped at
    ``0``) and the cached portion lands on ``cache_read_input_tokens`` so
    the two never double-count.

    Kept standalone (no adapter ``self``) so the parse is unit-testable
    against a fixed event stream without spawning a subprocess. The
    function is fail-fast: every malformed-output condition raises
    :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` rather than
    returning a partially-populated result, so a caller never silently
    meters a garbage spawn.

    Args:
        runtime: Adapter id stamped onto the result (e.g. ``"codex"``).
        model: Model alias/id the spawn was requested with.
        stdout: Raw subprocess stdout bytes (the newline-delimited events).
        stderr: Raw subprocess stderr bytes (surfaced in the error on a
            non-zero exit).
        exit_status: Subprocess exit code.
        subprocess_pid: PID of the spawned subprocess.
        started_at: When the subprocess started.
        ended_at: When the subprocess exited.

    Returns:
        The validated :class:`SpawnResult` for the completed call.

    Raises:
        RuntimeSpawnError: non-zero exit, empty stdout, no parseable JSON
            event line, an ``error`` / ``turn.failed`` event, or no
            ``agent_message`` item in the stream.
    """

    if exit_status != 0:
        # Codex reports the real failure (e.g. a 400 model-rejection) as an
        # error / turn.failed event on STDOUT, not stderr -- surface that so a
        # nonzero exit carries the actual reason rather than an empty stderr.
        stream_detail = _codex_stream_error_detail(stdout)
        stderr_snippet = stderr.decode(errors="replace").strip()
        diagnostic = " ".join(part for part in (stream_detail, stderr_snippet) if part)
        raise RuntimeSpawnError(
            f"codex spawn exited nonzero: status={exit_status} detail={diagnostic[:300]!r}",
            exit_status=exit_status,
            # Feed the combined stdout-event + stderr text to the classifier
            # (parse_error keys on this) so a model-rejection / auth failure is
            # classified from its real message, not blank stderr. Fall back to
            # the raw stderr bytes when the stream carried no detail.
            stderr=diagnostic.encode() if diagnostic else stderr,
        )
    if not stdout.decode(errors="replace").strip():
        raise RuntimeSpawnError("codex spawn produced empty stdout")

    events = _decode_codex_events(stdout)
    if not events:
        raise RuntimeSpawnError("codex output carried no parseable json event line")
    session_id, text, usage = _scan_codex_events(events)

    input_total = _usage_int(usage, "input_tokens") if usage is not None else None
    output_tokens = _usage_int(usage, "output_tokens") if usage is not None else None
    cache_read = _usage_int(usage, "cached_input_tokens", missing=0) if usage is not None else None
    usage_observed = (
        input_total is not None and output_tokens is not None and cache_read is not None
    )
    # Codex's input_tokens is GROSS (includes cached); split out the
    # non-cached portion so input + cache-read never double-count. Clamp at
    # 0 to respect the ge=0 SpawnResult field if a malformed envelope ever
    # reports more cached than total.
    input_non_cached = (
        max(input_total - cache_read, 0)
        if usage_observed and input_total is not None and cache_read is not None
        else None
    )

    return SpawnResult(
        session_id=session_id or f"{runtime}-{subprocess_pid}",
        runtime=runtime,
        model=model,
        resolved_model=None,
        subprocess_pid=subprocess_pid,
        exit_status=exit_status,
        text=text,
        input_tokens=input_non_cached,
        output_tokens=output_tokens if usage_observed else None,
        cache_creation_input_tokens=0 if usage_observed else None,
        cache_creation_5m_input_tokens=0 if usage_observed else None,
        cache_creation_1h_input_tokens=0 if usage_observed else None,
        cache_read_input_tokens=cache_read,
        cost_usd_reported=None,
        measurement_quality=(
            MeasurementQuality.EXACT if usage_observed else MeasurementQuality.UNAVAILABLE
        ),
        measurement_status=(
            MeasurementStatus.USAGE_OBSERVED
            if usage_observed
            else MeasurementStatus.NO_TOKEN_EVIDENCE
        ),
        measurement_reason=None if usage_observed else "codex_turn_usage_missing_or_invalid",
        started_at=started_at,
        ended_at=ended_at,
    )


class CodexAdapter:
    """Codex CLI runtime adapter (``codex exec`` subprocess primary)."""

    id: str = "codex"
    cli_binary: str = "codex"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix via
    # :func:`eawf.runtime.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table.
    accepts_continue: bool = runtime_supports("codex", "session_resume")
    supports_cache_control: bool = runtime_supports("codex", "cache_control")
    error_classes_emitted: tuple[ErrorClass, ...] = (
        "RUNTIME_RATE_LIMIT",
        "RUNTIME_SERVER_ERROR",
        "RUNTIME_TIMEOUT",
        "RUNTIME_API_ERROR",
        "RUNTIME_AUTH_ERROR",
    )

    async def open_session(
        self,
        wave: Wave,
        prompt: str,
        *,
        cache_prefix: str | None = None,
        model_hint: str | None = None,
        role_contract: RoleContract | None = None,
    ) -> SessionAttempt:
        """Construct a fresh-session :class:`SessionAttempt` row.

        ``cache_prefix`` is routed through
        :func:`~eawf.runtime.runtimes.cache_control.inject_cache_control` for
        boundary parity, but Codex is a **no-op path**: OpenAI prompt
        caching is automatic at the ≥1024-token threshold and there is
        no caller-side marker surface, so the prefix is returned
        unchanged.

        The optional *role_contract* keyword carries the typed
        :class:`~eawf.workflow.agents.specs.models.RoleContract`
        projection of the dispatched wave's role. When present the
        spawn seam reads its ``system_prompt`` / ``allowed_tools`` /
        ``model`` fields rather than a hardcoded executor preamble;
        today the adapter only debug-logs the attach so callers can
        observe the seam wire-up, and the live ``codex exec`` spawn in
        P26-SURFACES consumes the contract to materialise the
        per-session system prompt. ``None`` (the default) keeps the
        spawn byte-equivalent to the pre-W13 surface.
        """

        injected_prefix = inject_cache_control(
            runtime_id=self.id,
            cache_prefix=cache_prefix,
        )
        if injected_prefix is not None:
            logger.debug(f"open_session runtime={self.id!r} cache_control=no_op")
        if role_contract is not None:
            logger.debug(
                f"open_session runtime={self.id!r} role={role_contract.role!r} "
                f"allowed_tools={len(role_contract.allowed_tools)} "
                f"denied_tools={len(role_contract.denied_tools)} "
                f"model={role_contract.model!r}"
            )
        session_id = str(uuid.uuid4())
        attempts = sorted(wave.sessions)
        next_attempt = (max(attempts) + 1) if attempts else 1
        return SessionAttempt(
            attempt=next_attempt,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    async def spawn_session(
        self,
        prompt: str,
        *,
        model: str,
        cwd: str | None = None,
        extra_args: Sequence[str] = (),
        denied_tools: Sequence[str] = (),
        timeout: float | None = None,
        on_spawn: Callable[[int], None] | None = None,
        on_pgid: Callable[[int], None] | None = None,
        on_chunk: Callable[[str], Awaitable[None]] | None = None,
        session: str = "",
        enforcement_sink: EnforcementSink | None = None,
    ) -> SpawnResult:
        """Spawn a live ``codex exec`` subprocess and collect its result.

        Runs ``codex exec --json --skip-git-repo-check -m <model> <prompt>``
        via :func:`asyncio.create_subprocess_exec`, captures stdout (the
        newline-delimited JSON event stream) + stderr, and parses the
        outcome into a typed
        :class:`~eawf.runtime.runtimes.adapter.SpawnResult` via
        :func:`_parse_codex_result` (raw text + the token classes + pid +
        exit). Codex has no ``--output-format json`` single-envelope flag
        like claude; ``--json`` is the headless structured surface and
        ``--skip-git-repo-check`` lets the spawn run when the jailed cwd is
        not itself a git working tree. The child is started in its own
        session / process group (``start_new_session=True``) so a later
        cancel can signal the whole group by pgid. The child environment is
        SCRUBBED via :func:`~eawf.runtime.sandbox.env_scrub.build_child_env`
        -- it receives an allowlist floor plus the codex lane's own auth,
        never the full parent env. The argv is additionally prefixed with
        the OS filesystem jail (bubblewrap / seatbelt via
        :func:`~eawf.runtime.sandbox.jail.jail_command`) when the host
        supports it; a host without the wrapper binary runs unjailed with a
        warning rather than failing.

        The sandbox-enforcement seam is at parity with the claude lane: the
        env-scrub drop, the per-wave argv deny, and the cwd-guard fallback
        are each recorded to *enforcement_sink* (when wired) so a
        denial-timeline surface reads what the floor refused for *session*.
        The floor also caps the number of live spawns in flight at once
        (:data:`_CONCURRENT_SPAWN_CAP`): a spawn past the cap fails fast with
        :class:`ConcurrentSpawnCapError` before any subprocess is forked.

        The model reasoning-effort level is NOT a parameter on the
        vendor-neutral seam; a caller that needs it passes
        ``-c model_reasoning_effort=<level>`` through *extra_args* (the
        routing escape hatch) -- the confirmed dotted config key is exported
        as :data:`_REASONING_EFFORT_CONFIG_KEY`. *extra_args* are appended
        verbatim at the argv tail so that escape hatch stays last.

        **denied_tools maps to an INVERTED allowlist on codex.** ``codex
        exec`` (0.138.0) has no single tool-allowlist flag and no per-call
        tool-DENY flag -- its only per-call tool-grant surface is the
        ``-c tools.<name>=<bool>`` config override (e.g. ``tools.web_search``).
        Codex grants by allowlist, so a deny-list is expressed as its
        complement: when *denied_tools* is non-empty the argv gains
        ``-c tools.<allowed>=true`` for every tool in the universe minus the
        denied set (the inverted allowlist via
        :func:`~eawf.runtime.sandbox.policy.invert_deny_to_allow`) PLUS
        ``-c tools.<denied>=false`` for each denied name. The denied tool is
        therefore absent from the ``true`` grant AND explicitly disabled, so a
        deny can never reach the child's effective grant -- even under a
        default-allow codex. The overrides are spliced ahead of *extra_args*
        so the verbatim escape hatch stays at the argv tail, and the FS jail
        still confines the child on top. An empty deny-list adds no override
        (byte-equivalent to a deny-free spawn). A non-empty deny is recorded
        as an ``argv-deny`` enforcement event (``block`` severity) so the
        denial is auditable on the timeline -- the same shape the claude lane
        records for its ``--disallowedTools`` deny.

        The optional *on_spawn* callback fires with the child PID the moment
        the subprocess exists -- before output is awaited -- so a cancel
        path can register the pid and halt a still-running call mid-flight.
        The optional *on_pgid* callback fires with the child's process-GROUP
        id (resolved via :func:`os.getpgid` right after spawn) so the
        budget-HALT interlock can ``os.killpg`` the whole group when the wave
        runs over its hard token cap -- at parity with the claude lane.

        The optional *on_chunk* async callback fires once per stdout line AS
        IT ARRIVES: the spawn drains stdout incrementally (via
        :func:`_collect_spawn_output`) rather than buffering the whole event
        stream to process exit, so a downstream wave can surface each codex
        JSONL frame live. The full stdout is still accumulated and fed to
        :func:`_parse_codex_result` unchanged, so with ``on_chunk=None`` the
        returned :class:`SpawnResult` is byte-equivalent to the buffered path.

        Args:
            prompt: Rendered prompt passed to ``codex exec``.
            model: Model alias/id for ``-m``. No hardcoded floor -- the
                caller resolves it (the routing decision feeds this).
            cwd: Working directory for the subprocess; ``None`` inherits the
                parent's.
            extra_args: Extra CLI args appended verbatim (the routing /
                reasoning-effort / structured-output escape hatch).
            denied_tools: Per-wave sandbox deny-list (tool names). Codex has
                no per-call deny flag, so a non-empty set is mapped to its
                inverted allowlist as ``-c tools.<name>=<bool>`` overrides
                (allowed granted ``true``, denied pinned ``false``); the FS
                jail still confines the child on top. Empty (the default) adds
                no override.
            timeout: Wall-clock ceiling in seconds; ``None`` waits
                indefinitely. On expiry the child is killed and a typed
                error is raised.
            on_spawn: Optional callback invoked with the child PID right
                after spawn (before output is awaited).
            on_pgid: Optional callback invoked with the child's process-GROUP
                id right after spawn so the budget-HALT interlock can reap
                the whole group. Resolution failures (the child raced to
                exit) are swallowed -- the cancel path falls back to the pid.
            on_chunk: Optional async callback invoked once per stdout line as
                it arrives (live streaming); ``None`` (the default) leaves the
                spawn byte-equivalent to the buffered path.
            session: The spawning session id stamped on every enforcement
                event this spawn records.
            enforcement_sink: The sink each enforcement decision is persisted
                through; ``None`` only logs.

        Returns:
            The validated :class:`SpawnResult` for the completed call.

        Raises:
            ConcurrentSpawnCapError: When the concurrent-spawn cap is already
                saturated (raised before any subprocess is forked).
            RuntimeSpawnError: the spawn timed out, exited non-zero, or
                returned an unparseable / error event stream.
        """

        # Codex grants tools by allowlist config, not a deny flag, so a
        # deny-list is mapped to its inverted allowlist: every tool in the
        # universe minus the denied set is granted ``-c tools.<name>=true`` and
        # each denied name is pinned ``-c tools.<name>=false``, so a denied
        # tool is absent from the grant by construction. The overrides precede
        # the verbatim *extra_args* escape hatch. An empty deny-list emits no
        # override (byte-equivalent to a deny-free spawn).
        tool_overrides: list[str] = []
        if denied_tools:
            tool_overrides = _codex_tool_grant_overrides(denied_tools)
            logger.info(
                f"spawn_session runtime={self.id!r} denied_tools={len(denied_tools)} "
                f"mapped=inverted-allowlist allowed={len(invert_deny_to_allow(list(denied_tools)))}"
            )
            # Record the deny on the denial timeline at parity with the claude
            # lane: the target names the sorted denied tools so the timeline
            # surface reads which tools were refused for the session.
            emit_enforcement(
                enforcement_sink,
                make_enforcement_event(
                    session=session,
                    kind="argv-deny",
                    target=" ".join(sorted(denied_tools)),
                    severity="block",
                ),
            )
        argv = [
            self.cli_binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-m",
            model,
            *tool_overrides,
            *extra_args,
            prompt,
        ]
        # Prefix the OS filesystem jail (bubblewrap / seatbelt) when the
        # host supports it. ``start_new_session=True`` lands on the jail
        # wrapper (the group leader) so a pgid-reap still tears down the
        # whole tree; an absent wrapper runs unjailed with a warning. The
        # cwd-guard fallback records a degraded-but-continued decision.
        argv = _maybe_jail_argv(
            argv, runtime=self.id, cwd=cwd, session=session, sink=enforcement_sink
        )
        # Build the scrubbed child env + record the env-scrub decision (which
        # credential-bearing families were dropped) onto the denial timeline.
        child_env = build_child_env(self.id, extra_path_dir=resolve_binary_dir(self.cli_binary))
        _record_env_scrub(child_env, session=session, sink=enforcement_sink)

        # The concurrent-spawn cap is the LAST gate before the fork so the slot
        # is held only for the real subprocess lifetime; it is released in the
        # ``finally`` after the child is reaped / parsed.
        _acquire_spawn_slot()
        try:
            started_at = datetime.now(UTC)
            proc = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
                cwd=cwd,
                env=child_env,
            )
            pid = proc.pid
            logger.info(f"spawn_session runtime={self.id!r} pid={pid} model={model!r}")
            if on_spawn is not None:
                on_spawn(pid)
            if on_pgid is not None:
                # The child is its own group leader (start_new_session=True),
                # so its pgid equals its pid; resolve via getpgid so a future
                # double-fork still yields the leader. A child that already
                # exited makes getpgid raise -- the cancel path then falls back
                # to the pid, so the lookup failure is non-fatal.
                try:
                    on_pgid(os.getpgid(pid))
                except ProcessLookupError:
                    logger.warning(f"spawn_session pgid-unresolved pid={pid}")
            try:
                # Drain stdout / stderr incrementally so each codex JSONL frame
                # is fanned to on_chunk as it arrives; the full stdout is still
                # accumulated for the existing parser. wait_for preserves the
                # wall-clock ceiling + the kill-on-timeout contract.
                stdout, stderr = await asyncio.wait_for(
                    _collect_spawn_output(proc, on_chunk=on_chunk), timeout=timeout
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                # 124 is the conventional timeout exit code parse_error maps to
                # RUNTIME_TIMEOUT, so a classifier routes the V5 switch ladder.
                raise RuntimeSpawnError(
                    f"codex spawn timed out: timeout={timeout}s pid={pid}",
                    exit_status=124,
                ) from None
            ended_at = datetime.now(UTC)
            return _parse_codex_result(
                runtime=self.id,
                model=model,
                stdout=stdout or b"",
                stderr=stderr or b"",
                exit_status=proc.returncode if proc.returncode is not None else -1,
                subprocess_pid=pid,
                started_at=started_at,
                ended_at=ended_at,
            )
        finally:
            _release_spawn_slot()

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """Session resume is not implemented; the daemon must not call this.

        The ``session_resume`` capability is ``unsupported`` for codex
        (see ``capabilities.yaml``): no real ``codex exec resume`` spawn
        exists yet. Resume is deferred to P31. The signature keeps
        ``SessionAttempt`` so callers still type-check against the
        :class:`RuntimeAdapter` protocol.

        Raises:
            NotImplementedError: always — session resume is unimplemented.
        """

        raise NotImplementedError("codex session resume is not implemented")

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the session log.

        The handle abstracts over Codex's date-sharded JSONL layout
        under ``$CODEX_HOME`` (resolved via blitz brief; §5.4); the
        daemon walks the tree once + caches the result.
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class per §5.5.

        Codex follows the same regex ladder as Claude except for the
        Codex-specific "chatgpt subscription expired" auth phrasing.
        """

        if _AUTH_RE.search(stderr):
            return "RUNTIME_AUTH_ERROR"
        if _RATE_LIMIT_RE.search(stderr):
            return "RUNTIME_RATE_LIMIT"
        if _TIMEOUT_RE.search(stderr) or exit_status in (-15, 124):
            return "RUNTIME_TIMEOUT"
        if _SERVER_RE.search(stderr):
            return "RUNTIME_SERVER_ERROR"
        if _API_RE.search(stderr):
            return "RUNTIME_API_ERROR"
        return "RUNTIME_API_ERROR"

    def supports_continue(self) -> bool:
        """Codex supports ``codex exec resume <session-id>``."""

        return self.accepts_continue


_ADAPTER_CHECK: RuntimeAdapter = CodexAdapter()

__all__ = ["CodexAdapter", "ConcurrentSpawnCapError"]
