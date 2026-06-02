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
import re
import shutil
import sys
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.adapter import (
    ErrorClass,
    RuntimeAdapter,
    RuntimeSpawnError,
    SessionResumeFailedError,
    SpawnResult,
)
from eawf.runtime.runtimes.cache_control import inject_cache_control
from eawf.runtime.runtimes.selector import runtime_supports
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.sandbox.env_scrub import build_child_env
from eawf.runtime.sandbox.jail import jail_command, jail_supported

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


def _maybe_jail_argv(argv: list[str], *, runtime: str, cwd: str | None) -> list[str]:
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
        return argv

    jailed = jail_command(argv, runtime=runtime, cwd=cwd_path, root=root)
    logger.info(f"spawn_session jail=on wrapper={wrapper!r} runtime={runtime!r} cwd={cwd_path!s}")
    return jailed


_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|chatgpt subscription expired"
    rb"|unauthor[iz][sez]+ed)\b",
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


def _usage_int(usage: dict[str, object], key: str) -> int:
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
    value = usage.get(key, 0)
    if not value:
        return 0
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, (str, float)):
        try:
            return int(value)
        except TypeError, ValueError:
            return 0
    return 0


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
) -> tuple[str, str, dict[str, object]]:
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
        (``{}`` when no ``turn.completed`` usage was seen).

    Raises:
        RuntimeSpawnError: an ``error`` / ``turn.failed`` event was seen,
            or the stream carried no ``agent_message`` item.
    """
    session_id = ""
    text: str | None = None
    usage: dict[str, object] = {}
    for event in events:
        event_type = event.get("type")
        if event_type == "error":
            raise RuntimeSpawnError(f"codex reported an error event: {event.get('message')!r}")
        if event_type == "turn.failed":
            raise RuntimeSpawnError(f"codex turn failed: {event.get('error')!r}")
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
        snippet = stderr.decode(errors="replace").strip()[:200]
        raise RuntimeSpawnError(
            f"codex spawn exited nonzero: status={exit_status} stderr={snippet!r}"
        )
    if not stdout.decode(errors="replace").strip():
        raise RuntimeSpawnError("codex spawn produced empty stdout")

    events = _decode_codex_events(stdout)
    if not events:
        raise RuntimeSpawnError("codex output carried no parseable json event line")
    session_id, text, usage = _scan_codex_events(events)

    input_total = _usage_int(usage, "input_tokens")
    cache_read = _usage_int(usage, "cached_input_tokens")
    # Codex's input_tokens is GROSS (includes cached); split out the
    # non-cached portion so input + cache-read never double-count. Clamp at
    # 0 to respect the ge=0 SpawnResult field if a malformed envelope ever
    # reports more cached than total.
    input_non_cached = max(input_total - cache_read, 0)

    return SpawnResult(
        session_id=session_id or f"{runtime}-{subprocess_pid}",
        runtime=runtime,
        model=model,
        resolved_model=None,
        subprocess_pid=subprocess_pid,
        exit_status=exit_status,
        text=text,
        input_tokens=input_non_cached,
        output_tokens=_usage_int(usage, "output_tokens"),
        cache_creation_input_tokens=0,
        cache_creation_5m_input_tokens=0,
        cache_creation_1h_input_tokens=0,
        cache_read_input_tokens=cache_read,
        cost_usd_reported=None,
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

        The model reasoning-effort level is NOT a parameter on the
        vendor-neutral seam; a caller that needs it passes
        ``-c model_reasoning_effort=<level>`` through *extra_args* (the
        routing escape hatch) -- the confirmed dotted config key is exported
        as :data:`_REASONING_EFFORT_CONFIG_KEY`. *extra_args* are appended
        verbatim at the argv tail so that escape hatch stays last.

        **denied_tools is a per-runtime gap on codex.** ``codex exec``
        exposes a tool *allowlist* (``--allowed-tool`` / config ``[tools]``)
        but no per-call tool-DENY flag, so this adapter does NOT fabricate
        one: it logs the denied set and the (absent) mapping, and the child
        is still confined by the OS filesystem jail above. CLI-level
        tool-deny on codex is deferred to a later wave that wires the
        allowlist inversion; the *denied_tools* parameter stays on the
        signature for Protocol conformance.

        This wave builds the spawn mechanism + the parse only. Metering,
        cancellation, and the schema-forced re-ask loop are separate waves
        and are deliberately NOT wired here.

        The optional *on_spawn* callback fires with the child PID the moment
        the subprocess exists -- before output is awaited -- so a cancel
        path can register the pid and halt a still-running call mid-flight.

        Args:
            prompt: Rendered prompt passed to ``codex exec``.
            model: Model alias/id for ``-m``. No hardcoded floor -- the
                caller resolves it (the routing decision feeds this).
            cwd: Working directory for the subprocess; ``None`` inherits the
                parent's.
            extra_args: Extra CLI args appended verbatim (the routing /
                reasoning-effort / structured-output escape hatch).
            denied_tools: Per-wave sandbox deny-list (tool names). Codex has
                no per-call deny flag, so this set is logged but not mapped
                to a flag; the FS jail still confines the child. Empty (the
                default) is the common case.
            timeout: Wall-clock ceiling in seconds; ``None`` waits
                indefinitely. On expiry the child is killed and a typed
                error is raised.
            on_spawn: Optional callback invoked with the child PID right
                after spawn (before output is awaited).

        Returns:
            The validated :class:`SpawnResult` for the completed call.

        Raises:
            RuntimeSpawnError: the spawn timed out, exited non-zero, or
                returned an unparseable / error event stream.
        """

        # Codex exposes a tool allowlist but no per-call deny flag, so the
        # deny-list cannot map to a codex CLI flag this wave. Log the set so
        # the gap is observable; the OS filesystem jail still confines the
        # child regardless.
        if denied_tools:
            logger.warning(
                f"spawn_session runtime={self.id!r} denied_tools={len(denied_tools)} mapped=none "
                "reason=codex-exec-has-no-per-call-deny-flag"
            )
        argv = [
            self.cli_binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-m",
            model,
            *extra_args,
            prompt,
        ]
        # Prefix the OS filesystem jail (bubblewrap / seatbelt) when the
        # host supports it. ``start_new_session=True`` lands on the jail
        # wrapper (the group leader) so a pgid-reap still tears down the
        # whole tree; an absent wrapper runs unjailed with a warning.
        argv = _maybe_jail_argv(argv, runtime=self.id, cwd=cwd)
        started_at = datetime.now(UTC)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
            cwd=cwd,
            env=build_child_env(self.id),
        )
        pid = proc.pid
        logger.info(f"spawn_session runtime={self.id!r} pid={pid} model={model!r}")
        if on_spawn is not None:
            on_spawn(pid)
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise RuntimeSpawnError(
                f"codex spawn timed out: timeout={timeout}s pid={pid}"
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

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """Resume the session via ``codex exec resume <session-id>``.

        Raises:
            SessionResumeFailedError: ``session_id`` is empty or otherwise
                fails the daemon-side resume probe.
        """

        if not session_id:
            raise SessionResumeFailedError(f"empty session id: {session_id!r}")
        return SessionAttempt(
            attempt=1,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

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

__all__ = ["CodexAdapter"]
