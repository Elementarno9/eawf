"""Codex CLI adapter — implements :class:`RuntimeAdapter`.

There is no caller-side ``cache_control`` marker (OpenAI prompt caching
is automatic at the ≥1024-token threshold); :attr:`supports_cache_control`
is therefore ``False``.

Codex session logs live under ``$CODEX_HOME`` (date-sharded JSONL); the
adapter caches ``(session_id → path)`` at dispatch time so subsequent
retries skip the tree walk. v0.3 ships the typed surface only; the live
walk lands in a later wave.

A native Run is driven by :class:`CodexNativeLauncher`, which drives
``codex app-server`` through the official Codex Python SDK (see
:mod:`~eawf.runtime.runtimes.codex.app_server`) and borrows only the jail
and the error taxonomy from here.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from eawf.kernel.state.models import SessionAttempt
from eawf.runtime.runtimes.adapter import (
    ConcurrentSpawnCapError,
    ErrorClass,
    NativeLaunchOutcome,
    NativeLaunchRequest,
    NativeRunLauncher,
    RuntimeAdapter,
)
from eawf.runtime.runtimes.selector import runtime_supports
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.runtime.sandbox.egress_proxy import (
    EnforcementSink,
    emit_enforcement,
    make_enforcement_event,
)
from eawf.runtime.sandbox.jail import jail_command, jail_supported

logger = logging.getLogger(__name__)

#: The OS-jail wrapper binary per platform. The spawn seam jails the child
#: only when the platform supports it AND the wrapper resolves on PATH.
#: Duplicated from the claude lane because this wave edits only the codex
#: module and must not touch the shared adapter surface.
_JAIL_WRAPPER_BINARY: dict[str, str] = {
    "darwin": "sandbox-exec",
    "linux": "bwrap",
}

#: The codex sandbox mode a seatbelt-jailed child is started with. Codex
#: wraps every model-issued command in its own ``sandbox-exec`` profile, and
#: macOS refuses to apply a seatbelt profile inside a process that already
#: runs under one (``sandbox_apply: Operation not permitted``), so a jailed
#: codex on darwin dies before it reaches the model. The eawf jail is the
#: enforced boundary on that path. ``codex app-server`` refuses
#: ``--sandbox`` after its subcommand, so the mode is a root ``-c`` override.
OUTER_SEATBELT_OVERRIDE: str = 'sandbox_mode="danger-full-access"'


def _defer_to_outer_seatbelt(argv: list[str]) -> list[str]:
    """Return a codex *argv* that leaves sandboxing to the eawf jail.

    Args:
        argv: The child's own argv, ``[<binary>, <subcommand>, ...]``.

    Returns:
        A new argv with :data:`OUTER_SEATBELT_OVERRIDE` as a root ``-c``
        ahead of the subcommand.
    """
    return [argv[0], "-c", OUTER_SEATBELT_OVERRIDE, *argv[1:]]


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
    private_home: Path | None = None,
    storage_dirs: tuple[Path, ...] = (),
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
        argv: The child's own argv (``["codex", "app-server", ...]``).
        runtime: The runtime adapter id selecting the own-cred carve-out.
        cwd: The spawn cwd. ``None`` defaults the jail confinement to the
            repo root containing the process cwd; when the process cwd is
            not inside a discoverable root the spawn runs unjailed with a
            warning rather than confining to a bogus path.
        session: The spawning session id stamped on a ``cwd-guard``
            enforcement event when the cwd-outside-repo fallback fires.
        sink: The enforcement sink a ``cwd-guard`` event is persisted
            through; ``None`` only logs.
        private_home: The Run's own ``CODEX_HOME``, made writable in place
            of the operator's (see :func:`~eawf.runtime.sandbox.jail.jail_command`).
        storage_dirs: The daemon's storage directories a native Run may
            not read.

    Returns:
        Either ``jail_command(argv, ...)`` (jailed) or *argv* unchanged
        (unjailed fallback). Under the seatbelt wrapper the jailed argv also
        turns codex's own sandbox off (:func:`_defer_to_outer_seatbelt`),
        because a nested seatbelt profile cannot be applied; an unjailed
        child keeps codex's own sandbox.
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

    if wrapper == _JAIL_WRAPPER_BINARY["darwin"]:
        argv = _defer_to_outer_seatbelt(argv)
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

#: The codex 0.159.2 settings that withhold each ambient tool codex lets a
#: configuration withhold, checked against that release's config schema.
#: Codex reads files through its shell, so denying ``Bash`` also takes the
#: ``Read``/``Grep``/``Glob`` route; ``Read`` alone can only drop the image
#: reader. Skills are read from ``$HOME/.agents`` and the repository as
#: well as ``CODEX_HOME``, so ``Skill`` also skips that host discovery.
#: ``Write`` and ``Edit`` have no setting: ``apply_patch`` follows
#: the model's catalog entry, so only the jail bounds what it writes. The
#: ``Task*`` names need none, since ``update_plan`` is off unless enabled.
_CODEX_TOOL_DENIALS: dict[str, tuple[str, ...]] = {
    "Agent": ("features.multi_agent=false",),
    "Bash": ("features.shell_tool=false",),
    "Read": ("features.view_image=false",),
    "Skill": (
        "features.skip_host_skill_discovery=true",
        "skills.bundled.enabled=false",
        "skills.include_instructions=false",
    ),
    "WebFetch": ('web_search="disabled"',),
    "WebSearch": ('web_search="disabled"',),
}


def codex_tool_denials(tools: Sequence[str]) -> list[str]:
    """Return the ``-c`` overrides that withhold *tools* from a codex app-server.

    Args:
        tools: Ambient tool names to withhold. A name codex has no setting
            for adds nothing, because codex cannot be told to drop it.

    Returns:
        Interleaved ``-c`` and ``key=value`` tokens, each setting once, in
        sorted order so the configuration is reproducible.
    """
    settings = sorted({s for tool in tools for s in _CODEX_TOOL_DENIALS.get(tool, ())})
    return [token for setting in settings for token in ("-c", setting)]


class CodexAdapter:
    """Codex CLI runtime adapter."""

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


class CodexNativeLauncher:
    """Start one native Run on Codex from its compiled spec.

    The launcher reads policy from the spec and from nothing else, and it
    answers with the worker's announcement rather than with free text, so
    the daemon judges the launch against the Run's recorded binding
    instead of against whatever the child printed. The Run is driven over
    ``codex app-server`` through the official Codex Python SDK (see
    :mod:`~eawf.runtime.runtimes.codex.app_server`):
    the launch returns once the thread is open and its turn started, and the
    turn runs on in the task the outcome carries.

    Attributes:
        provider_kind: The compiled ``provider_options`` discriminator
            this launcher answers to.
        driver_version: The version this driver implementation reports as
            its loaded runtime. It describes the eawf driver rather than
            the installed CLI, which publishes no version the spawn path
            can read without a second process.
        worker_protocol_version: The worker protocol this driver speaks.
        event_codec_version: The event codec this driver emits.
    """

    provider_kind: str = "codex"
    driver_version: str = "1.0.0"
    worker_protocol_version: str = "1.0.0"
    event_codec_version: str = "1.0.0"

    def __init__(self, adapter: CodexAdapter | None = None) -> None:
        """Bind the launcher to the adapter that owns the spawn.

        Args:
            adapter: The spawning adapter; a fresh one by default.
        """
        self._adapter = adapter if adapter is not None else CodexAdapter()

    async def launch(self, request: NativeLaunchRequest) -> NativeLaunchOutcome:
        """Start the child inside the leased workspace and take its hello.

        Args:
            request: The compiled spec, the sealed capsule, the resolved
                workspace and the announcement sequence expected back.

        Returns:
            The provider session, the child's pid, the announcement and the
            turn still running.

        Raises:
            ConcurrentSpawnCapError: Every spawn slot is already held.
            RuntimeSpawnError: The binary was not found, the app-server
                refused or failed a request before the turn started, or the
                per-Run MCP configuration could not be built or does not
                register the Run's server.
        """
        # The app-server driver imports this module for its jail and taxonomy.
        from eawf.runtime.runtimes.codex.app_server import launch_codex_run

        return await launch_codex_run(
            request,
            adapter=self._adapter,
            driver_version=self.driver_version,
            worker_protocol_version=self.worker_protocol_version,
            event_codec_version=self.event_codec_version,
        )


_ADAPTER_CHECK: RuntimeAdapter = CodexAdapter()
_LAUNCHER_CHECK: NativeRunLauncher = CodexNativeLauncher()

__all__ = ["CodexAdapter", "CodexNativeLauncher", "ConcurrentSpawnCapError", "codex_tool_denials"]
