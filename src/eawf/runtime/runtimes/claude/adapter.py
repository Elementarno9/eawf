"""Claude Code adapter — implements :class:`RuntimeAdapter`.

The :meth:`open_session` / :meth:`continue_session` methods return
fully-typed :class:`~eawf.kernel.state.models.SessionAttempt` rows. In
v0.3-v0.5 the live subprocess spawn lives in the daemon dispatch
router; the adapter's role here is to construct the typed row + parse
subprocess outcomes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtime.runtimes.adapter import (
    ConcurrentSpawnCapError,
    ErrorClass,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
    RuntimeAdapter,
)
from eawf.runtime.runtimes.cache_control import inject_cache_control
from eawf.runtime.runtimes.selector import runtime_supports
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.sandbox.egress_proxy import (
    EnforcementSink,
    emit_enforcement,
    make_enforcement_event,
)
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
    private_home: Path | None = None,
    storage_dirs: tuple[Path, ...] = (),
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
        argv: The child's own argv (``["claude"]`` for a native Run).
        runtime: The runtime adapter id selecting the own-cred carve-out.
        cwd: The spawn cwd. ``None`` defaults the jail confinement to the
            repo root containing the process cwd; when the process cwd is
            not inside a discoverable root the spawn runs unjailed with a
            warning rather than confining to a bogus path.
        session: The spawning session id stamped on a ``cwd-guard``
            enforcement event when the cwd-outside-repo fallback fires.
        sink: The enforcement sink a ``cwd-guard`` event is persisted
            through; ``None`` only logs.
        private_home: A native Run's configuration home, made writable in
            place of the operator's ``~/.claude`` (see
            :func:`~eawf.runtime.sandbox.jail.jail_command`).
        storage_dirs: The daemon's storage directories a native Run may
            not read.

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

    jailed = jail_command(
        argv,
        runtime=runtime,
        cwd=cwd_path,
        root=root,
        private_home=private_home,
        storage_dirs=storage_dirs,
    )
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


class ClaudeAdapter:
    """Claude Code runtime adapter.

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
        """Session resume is not implemented; the daemon must not call this.

        The ``session_resume`` capability is ``unsupported`` for
        claude-code (see ``capabilities.yaml``): no real ``claude
        --continue`` resume spawn exists yet. Resume is deferred to P31.
        The signature keeps ``SessionAttempt`` so callers still
        type-check against the :class:`RuntimeAdapter` protocol.

        Raises:
            NotImplementedError: always — session resume is unimplemented.
        """

        raise NotImplementedError("claude session resume is not implemented")

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


class ClaudeNativeLauncher:
    """Start one native Run on Claude Code from its compiled spec.

    The launcher reads policy from the spec and from nothing else: the
    model, the wall ceiling, the deny list and the working directory are
    all compiled values, so a layered document cannot reach the child
    through this seam. Nor can the operator's own configuration: the child
    is started against a clean configuration home under the Run's root
    with instruction discovery and settings files switched off (see
    :mod:`~eawf.runtime.runtimes.claude.managed_run`).

    The child is driven through the Claude Agent SDK (see
    :mod:`~eawf.runtime.runtimes.claude.sdk_launcher`): the launch returns
    once the session is announced, and the turn runs on in the task the
    outcome carries.

    Attributes:
        provider_kind: The compiled ``provider_options`` discriminator
            this launcher answers to.
        driver_version: The version this driver implementation reports as
            its loaded runtime. It describes the eawf driver rather than
            the installed CLI, because the CLI publishes no version the
            spawn path can read without a second process.
        worker_protocol_version: The worker protocol this driver speaks.
        event_codec_version: The event codec this driver emits.
    """

    provider_kind: str = "claude"
    driver_version: str = "1.0.0"
    worker_protocol_version: str = "1.0.0"
    event_codec_version: str = "1.0.0"

    def __init__(self, adapter: ClaudeAdapter | None = None) -> None:
        """Bind the launcher to the adapter that owns the spawn.

        Args:
            adapter: The spawning adapter; a fresh one by default.
        """
        self._adapter = adapter if adapter is not None else ClaudeAdapter()

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Start the child inside the leased workspace and take its hello.

        Args:
            request: The compiled spec, the sealed capsule, the resolved
                workspace and the announcement sequence expected back.

        Returns:
            The provider session, the child's pid, the announcement and
            the turn still running.

        Raises:
            RuntimeSpawnError: The CLI was not found, the child failed or
                reported an error before announcing its session, or the
                per-Run MCP configuration could not be built or does not
                register the Run's server.
        """
        # The SDK driver imports this module for its jail and taxonomy.
        from eawf.runtime.runtimes.claude.sdk_launcher import launch_claude_run

        return await launch_claude_run(
            request,
            adapter=self._adapter,
            driver_version=self.driver_version,
            worker_protocol_version=self.worker_protocol_version,
            event_codec_version=self.event_codec_version,
        )


# Module-level Protocol-conformance sanity check. The daemon's
# ``isinstance(adapter, RuntimeAdapter)`` load-time gate catches a
# Protocol-mismatch; checking at import keeps the failure mode at the
# right layer.
_ADAPTER_CHECK: RuntimeAdapter = ClaudeAdapter()
_LAUNCHER_CHECK: NativeRunLauncher = ClaudeNativeLauncher()

__all__ = ["ClaudeAdapter", "ClaudeNativeLauncher", "ConcurrentSpawnCapError"]
