"""Claude Code adapter — implements :class:`RuntimeAdapter`.

The :meth:`open_session` / :meth:`continue_session` methods return
fully-typed :class:`~eawf.kernel.state.models.SessionAttempt` rows. In
v0.3-v0.5 the live subprocess spawn lives in the daemon dispatch
router; the adapter's role here is to construct the typed row + parse
subprocess outcomes.
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
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
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
from eawf.runtime.sandbox.egress_proxy import (
    EnforcementSink,
    SandboxError,
    emit_enforcement,
    make_enforcement_event,
)
from eawf.runtime.sandbox.env_scrub import build_child_env
from eawf.runtime.sandbox.jail import jail_command, jail_supported

if TYPE_CHECKING:
    from eawf.workflow.agents.specs.models import RoleContract

logger = logging.getLogger(__name__)

#: The OS-jail wrapper binary per platform. The spawn seam jails the child
#: only when the platform supports it AND the wrapper resolves on PATH.
_JAIL_WRAPPER_BINARY: dict[str, str] = {
    "darwin": "sandbox-exec",
    "linux": "bwrap",
}

#: Ceiling on live agent spawns in flight at once. The floor caps the spawn
#: fan-out so a runaway dispatcher cannot fork an unbounded fleet of jailed
#: children (each holds an egress socket + a process group); a spawn past
#: the cap fails fast with :class:`ConcurrentSpawnCapError` rather than
#: queueing. Chosen to comfortably cover the parallel-wave fleet while still
#: bounding the blast radius of a dispatch loop bug.
_CONCURRENT_SPAWN_CAP: int = 16

#: Live in-flight spawn counter + its lock. Module-global because the cap is
#: per-process (the daemon hosts every spawn); guarded by a lock so the
#: increment / cap-check is atomic under the asyncio + worker-thread mix the
#: daemon runs spawns on.
_spawn_inflight: int = 0
_spawn_lock = threading.Lock()


class ConcurrentSpawnCapError(SandboxError):
    """Raised when a spawn would exceed the concurrent-spawn cap.

    The floor refuses to fork a new jailed child once
    :data:`_CONCURRENT_SPAWN_CAP` are already in flight, so a runaway
    dispatch loop fails fast at the spawn boundary rather than exhausting
    process / socket resources.
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
    bubblewrap) the child runs UNJAILED with a loud warning -- this keeps
    the read-only clarify spawn working on hosts without the tool and keeps
    CI green. Refusing a mutating spawn when the jail is unavailable is an
    I04 concern; this seam only gates on the predicate I04 will key off.

    The daemon stays the sole session-setter: only the argv gains the
    prefix here; ``start_new_session`` / ``env`` / ``cwd`` on the spawn are
    untouched, so the wrapper inherits the daemon-set process group and the
    kill ladder reaps the whole tree unchanged.

    Args:
        argv: The child's own argv (``["claude", "-p", ...]``).
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
        # A cwd that escapes (or cannot resolve) the repo root is a
        # cwd-guard refusal: the jail confinement is dropped, so record the
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


_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate_limit_error|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|unauthor[iz][sez]+ed)\b",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b(?:5\d\d|internal_server_error|overloaded_error)\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")


def _parse_claude_result(
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
    """Parse a ``claude -p --output-format json`` envelope into a result.

    The single-result JSON envelope carries ``result`` (the answer text),
    ``session_id``, ``usage`` (the token classes), an optional
    ``modelUsage`` map naming the billed model, and an optional
    ``total_cost_usd``. Kept standalone (no adapter ``self``) so the parse
    is unit-testable against a fixed envelope without spawning a
    subprocess.

    The function is fail-fast: every malformed-output condition raises
    :class:`~eawf.runtime.runtimes.adapter.RuntimeSpawnError` rather than
    returning a partially-populated result, so a caller never silently
    meters a garbage spawn.

    Args:
        runtime: Adapter id stamped onto the result (e.g. ``"claude-code"``).
        model: Model alias/id the spawn was requested with.
        stdout: Raw subprocess stdout bytes (the JSON envelope).
        stderr: Raw subprocess stderr bytes (surfaced in the error on a
            non-zero exit).
        exit_status: Subprocess exit code.
        subprocess_pid: PID of the spawned subprocess.
        started_at: When the subprocess started.
        ended_at: When the subprocess exited.

    Returns:
        The validated :class:`SpawnResult` for the completed call.

    Raises:
        RuntimeSpawnError: non-zero exit, empty stdout, unparseable JSON,
            a non-object envelope, a non-object usage block, or an
            ``is_error`` result.
    """

    if exit_status != 0:
        snippet = stderr.decode(errors="replace").strip()[:200]
        raise RuntimeSpawnError(
            f"claude spawn exited nonzero: status={exit_status} stderr={snippet!r}",
            exit_status=exit_status,
            stderr=stderr,
        )
    raw = stdout.decode(errors="replace").strip()
    if not raw:
        raise RuntimeSpawnError("claude spawn produced empty stdout")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeSpawnError(f"claude output is not valid json: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeSpawnError(f"claude output is not a json object: {type(data).__name__}")
    if data.get("is_error"):
        detail = data.get("subtype") or data.get("result")
        raise RuntimeSpawnError(f"claude reported an error result: {detail!r}")
    usage = data.get("usage") or {}
    if not isinstance(usage, dict):
        raise RuntimeSpawnError("claude usage block is not a json object")

    text_field = data.get("result")
    text = "" if text_field is None else str(text_field)

    cache_write_total = int(usage.get("cache_creation_input_tokens", 0) or 0)
    ttl_split = usage.get("cache_creation")
    if isinstance(ttl_split, dict):
        cache_write_5m = int(ttl_split.get("ephemeral_5m_input_tokens", 0) or 0)
        cache_write_1h = int(ttl_split.get("ephemeral_1h_input_tokens", 0) or 0)
    else:
        cache_write_5m = 0
        cache_write_1h = 0
    if cache_write_5m == 0 and cache_write_1h == 0 and cache_write_total > 0:
        # Envelope disclosed no TTL split: attribute the whole write to the
        # 5-minute tier (the conservative prior) rather than dropping it.
        cache_write_5m = cache_write_total

    model_usage = data.get("modelUsage")
    resolved_model = next(iter(model_usage), None) if isinstance(model_usage, dict) else None

    cost_reported = data.get("total_cost_usd")
    session_field = str(data.get("session_id") or "")

    return SpawnResult(
        session_id=session_field or f"{runtime}-{subprocess_pid}",
        runtime=runtime,
        model=model,
        resolved_model=resolved_model,
        subprocess_pid=subprocess_pid,
        exit_status=exit_status,
        text=text,
        input_tokens=int(usage.get("input_tokens", 0) or 0),
        output_tokens=int(usage.get("output_tokens", 0) or 0),
        cache_creation_input_tokens=cache_write_total or (cache_write_5m + cache_write_1h),
        cache_creation_5m_input_tokens=cache_write_5m,
        cache_creation_1h_input_tokens=cache_write_1h,
        cache_read_input_tokens=int(usage.get("cache_read_input_tokens", 0) or 0),
        cost_usd_reported=(Decimal(str(cost_reported)) if cost_reported is not None else None),
        started_at=started_at,
        ended_at=ended_at,
    )


class ClaudeAdapter:
    """Claude Code runtime adapter (``claude -p`` subprocess primary).

    Implements :class:`~eawf.runtime.runtimes.adapter.RuntimeAdapter`. ``id``
    + ``cli_binary`` follow the canonical naming per
    :class:`~eawf.kernel.state.models.SessionAttempt.runtime`.
    """

    id: str = "claude-code"
    cli_binary: str = "claude"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix via
    # :func:`eawf.runtime.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table.
    accepts_continue: bool = runtime_supports("claude-code", "session_resume")
    supports_cache_control: bool = runtime_supports("claude-code", "cache_control")
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

        Live subprocess spawn lands in P26-SURFACES; the v0.3 wave
        ships the typed contract. The returned row carries an
        adapter-allocated ``session_id`` UUID and the opaque
        ``session_log_handle`` per rule 16.

        The caller-side ``cache_prefix`` is routed through
        :func:`~eawf.runtime.runtimes.cache_control.inject_cache_control`, which
        appends the Claude ``<cache_control type="ephemeral" />``
        breakpoint (``claude-code`` is the only runtime that accepts a
        caller-side marker). The injected prefix feeds the live
        subprocess spawn.

        The optional *role_contract* keyword carries the typed
        :class:`~eawf.workflow.agents.specs.models.RoleContract`
        projection of the dispatched wave's role. When present its
        ``system_prompt`` / ``allowed_tools`` / ``denied_tools`` /
        ``model`` fields feed the spawn seam directly rather than the
        renderer-embedded copy; today the adapter only debug-logs the
        attach so callers can observe the seam wire-up, and the live
        ``claude -p`` spawn in P26-SURFACES consumes the contract to
        materialise the per-session ``--system-prompt`` /
        ``--allowed-tools`` flags. ``None`` (the default) keeps the
        spawn byte-equivalent to the pre-W13 surface.
        """

        injected_prefix = inject_cache_control(
            runtime_id=self.id,
            cache_prefix=cache_prefix,
        )
        if injected_prefix is not None:
            logger.debug(f"open_session runtime={self.id!r} cache_control=injected")
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
        session: str = "",
        enforcement_sink: EnforcementSink | None = None,
    ) -> SpawnResult:
        """Spawn a live ``claude -p`` subprocess and collect its result.

        Runs ``claude -p <prompt> --output-format json --model <model>``
        via :func:`asyncio.create_subprocess_exec`, captures stdout (the
        usage JSON envelope) + stderr, and parses the outcome into a typed
        :class:`~eawf.runtime.runtimes.adapter.SpawnResult` (raw text +
        the token classes + pid + exit + the optional self-reported cost).
        The child is started in its own session / process group
        (``start_new_session=True``) so a later cancel can signal the whole
        group by pgid without this wave building the cancel path. The child
        environment is SCRUBBED via
        :func:`~eawf.runtime.sandbox.env_scrub.build_child_env` -- it
        receives an allowlist floor (pinned ``PATH`` + ``HOME`` / locale /
        ``TERM``) plus the claude lane's own auth, never the full parent
        env (which would carry ``AWS_*`` / ``GH_*`` / ``SSH_*`` creds into
        the child). The argv is additionally prefixed with the OS
        filesystem jail (bubblewrap / seatbelt via
        :func:`~eawf.runtime.sandbox.jail.jail_command`) when the host
        supports it; the jail wrapper inherits the daemon-set session so
        the pgid-reap is preserved, and a host without the wrapper binary
        runs unjailed with a warning rather than failing.

        This wave builds the spawn mechanism + the parse only. Metering,
        cancellation, and the schema-forced re-ask loop are separate waves
        and are deliberately NOT wired here.

        When *denied_tools* is non-empty the argv gains
        ``--disallowedTools <space-joined-sorted>`` so the spawned child is
        launched with those tools disabled per the wave's sandbox policy.
        The deny flag is built BEFORE the jail wrapper is applied, so the
        jail confines the full child argv including the deny flag; the tool
        names are sorted for a deterministic argv. The deny flag is inserted
        ahead of *extra_args* so the verbatim escape hatch stays at the argv
        tail; an empty deny-list adds no flag (byte-equivalent to a deny-free
        spawn).

        The optional *on_spawn* callback fires with the child PID the
        moment the subprocess exists — before output is awaited — so a
        later cancel path can register the pid and halt a still-running
        call mid-flight. The optional *on_pgid* callback fires with the
        child's process-GROUP id (resolved via :func:`os.getpgid` right
        after spawn) so the budget-HALT interlock can ``os.killpg`` the
        whole group when the wave runs over its hard token cap.

        The floor caps the number of live spawns in flight at once
        (:data:`_CONCURRENT_SPAWN_CAP`): a spawn past the cap fails fast
        with :class:`ConcurrentSpawnCapError` before any subprocess is
        forked. Each enforcement decision the spawn makes -- the env-scrub
        drop, the per-wave argv deny, the cwd-guard fallback -- is recorded
        to *enforcement_sink* (when wired) so a denial-timeline surface can
        read what the floor refused for *session*.

        Args:
            prompt: Rendered prompt passed to ``claude -p``.
            model: Model alias/id for ``--model``. No hardcoded floor —
                the caller resolves it (the routing decision feeds this).
            cwd: Working directory for the subprocess; ``None`` inherits
                the parent's.
            extra_args: Extra CLI args appended verbatim (the routing /
                structured-output escape hatch).
            denied_tools: Per-wave sandbox deny-list (tool names). When
                non-empty the argv gains ``--disallowedTools`` with the
                sorted names space-joined into one token; empty (the
                default) adds no flag.
            timeout: Wall-clock ceiling in seconds; ``None`` waits
                indefinitely. On expiry the child is killed and a typed
                error is raised.
            on_spawn: Optional callback invoked with the child PID right
                after spawn (before output is awaited).
            on_pgid: Optional callback invoked with the child's process-GROUP
                id right after spawn so the budget-HALT interlock can reap
                the whole group. Resolution failures (the child raced to
                exit) are swallowed -- the cancel path falls back to the pid.
            session: The spawning session id stamped on every enforcement
                event this spawn records.
            enforcement_sink: The sink each enforcement decision is
                persisted through; ``None`` only logs.

        Returns:
            The validated :class:`SpawnResult` for the completed call.

        Raises:
            ConcurrentSpawnCapError: When the concurrent-spawn cap is
                already saturated (raised before any subprocess is forked).
            RuntimeSpawnError: the spawn timed out, exited non-zero, or
                returned an unparseable / error result envelope.
        """

        deny_flag: list[str] = []
        if denied_tools:
            # Map the per-wave deny-list to the claude deny flag. Sorted +
            # space-joined into one token so the argv is deterministic and
            # the vendor flag spelling stays inside the adapter.
            deny_flag = ["--disallowedTools", " ".join(sorted(denied_tools))]
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
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            model,
            *deny_flag,
            *extra_args,
        ]
        # Prefix the OS filesystem jail (bubblewrap / seatbelt) when the
        # host supports it. The daemon stays the sole session-setter:
        # ``start_new_session=True`` lands on the jail wrapper, which is the
        # group leader, so ``cancel_process_group(os.getpgid(pid))`` reaps
        # the whole tree unchanged. When the wrapper binary is absent the
        # child runs unjailed with a warning (CI / hosts without the tool).
        argv = _maybe_jail_argv(
            argv, runtime=self.id, cwd=cwd, session=session, sink=enforcement_sink
        )
        # Build the scrubbed child env + record the env-scrub decision (which
        # credential-bearing families were dropped) onto the denial timeline.
        child_env = build_child_env(self.id)
        self._record_env_scrub(child_env, session=session, sink=enforcement_sink)

        # The concurrent-spawn cap is the LAST gate before the fork so the
        # slot is held only for the real subprocess lifetime; it is released
        # in the ``finally`` after the child is reaped / parsed.
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
                # exited makes getpgid raise -- the cancel path then falls
                # back to the pid, so the lookup failure is non-fatal.
                try:
                    on_pgid(os.getpgid(pid))
                except ProcessLookupError:
                    logger.warning(f"spawn_session pgid-unresolved pid={pid}")
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except TimeoutError:
                proc.kill()
                await proc.wait()
                # 124 is the conventional timeout exit code parse_error maps
                # to RUNTIME_TIMEOUT, so a classifier routes the V5 switch.
                raise RuntimeSpawnError(
                    f"claude spawn timed out: timeout={timeout}s pid={pid}",
                    exit_status=124,
                ) from None
            ended_at = datetime.now(UTC)
            return _parse_claude_result(
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

    @staticmethod
    def _record_env_scrub(
        child_env: dict[str, str],
        *,
        session: str,
        sink: EnforcementSink | None,
    ) -> None:
        """Record the env-scrub decision for a built child env.

        Compares the scrubbed child env against the parent ``os.environ``
        and records an ``env-scrub`` enforcement event naming how many
        credential-bearing variables the allowlist dropped. A scrub that
        dropped nothing is an ``info`` row (the floor still ran); a scrub
        that dropped one or more vars is a ``block`` row (creds were
        withheld from the child).

        Args:
            child_env: The scrubbed child env the spawn will use.
            session: The spawning session id stamped on the event.
            sink: The enforcement sink the event is persisted through.
        """
        # Count parent keys WITHHELD from the child (present in the parent
        # env, absent from the scrubbed child). This is the true measure of
        # what the allowlist dropped -- a raw length diff is wrong because
        # the child reseeds floor keys (pinned PATH / defaulted LANG) the
        # parent may lack.
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

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """Resume the session at ``session_id`` via ``claude --continue``.

        Raises:
            SessionResumeFailedError: ``session_id`` is empty or otherwise
                fails the daemon-internal resume probe; daemon falls
                back to :meth:`open_session` per §5.8.
        """

        if not session_id:
            raise SessionResumeFailedError(f"empty session id: {session_id!r}")
        # P26 wires the actual ``claude --continue <session_id>``
        # subprocess spawn + JSONL log lookup. v0.3 surfaces the
        # typed row only; the resume probe lives in the daemon.
        return SessionAttempt(
            attempt=1,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the session log.

        Per §5.4 the real on-disk path lives in the daemon's
        in-process map (rule 16). The handle is a URN-shaped string
        the daemon resolves via
        :func:`eawf.runtime.daemon.session.resolve_session_log` (lands in
        P26).
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class.

        Per §5.5 rules: auth and rate-limit on exit 2; server-error
        and api-error on exit 1; timeout on SIGTERM (-15) or the
        ``timeout`` / ``deadline_exceeded`` keyword. Order matters —
        auth + rate-limit checks run before the generic
        ``5xx`` / ``4xx`` HTTP fallbacks so an "auth 401 over a 500
        response" classifies as auth (the more actionable verdict).
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
        """Claude Code supports ``--continue <session-id>``."""

        return self.accepts_continue


# Module-level Protocol-conformance sanity check. The daemon's
# ``isinstance(adapter, RuntimeAdapter)`` load-time gate catches a
# Protocol-mismatch; checking at import keeps the failure mode at the
# right layer.
_ADAPTER_CHECK: RuntimeAdapter = ClaudeAdapter()

__all__ = ["ClaudeAdapter", "ConcurrentSpawnCapError"]
