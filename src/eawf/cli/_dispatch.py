"""Daemon-vs-daemonless escalation rules for CLI verbs.

This module centralises the verb-class escalation decision so every
mutating / read-only handler shares one implementation rather than
re-deriving the rule per command. The escalation table:

* **Read-only (R)** verbs MAY bypass the daemon when ``--daemonless``
  is passed or ``EAWF_DAEMONLESS=1`` is set — the carve-out for CI,
  read-only flows, and the recovery shell. Otherwise they still hop the
  daemon for cache freshness.
* **Mutating (W)** verbs ALWAYS escalate to the daemon: they refuse
  ``--daemonless`` with a ``UserError`` carrying ``data.kind=
  "InvalidInput"`` + exit-code 1, and auto-spawn the daemon when none
  is running (the on-demand spawn flow).

A separate **dev-mode gate** (``--debug`` flag or ``EAWF_DEBUG=1``)
controls whether the raw JSON-RPC passthrough verb (``state rpc``) and
the hidden ``daemon`` control verbs are reachable. The raw passthrough
is never exposed in normal operation.

The helpers here are intentionally side-effect-light:

* :func:`daemonless_requested` and :func:`dev_mode_enabled` are pure
  predicates over flags + the environment.
* :func:`reject_daemonless_on_mutating` raises the canonical
  rejection error (it never prints — the caller's ``emit_error`` owns
  rendering).
* :func:`escalate_mutation` is the one entry the mutating-verb wrappers
  call: it applies the rejection rule, ensures a daemon is up
  (auto-spawn), and hands the resolved runtime dir back so the caller
  can open its :class:`~eawf.cli._daemon_client.DaemonClient`.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags

logger = logging.getLogger(__name__)


def daemonless_requested(flags: GlobalFlags | None) -> bool:
    """Return True when the daemon-bypass carve-out is requested.

    The carve-out is requested by either the ``--daemonless`` global
    flag or the ``EAWF_DAEMONLESS=1`` environment override (CI hooks /
    recovery shell that cannot pass a flag). Mirrors the env check in
    :func:`eawf.cli._mutation._proxy_enabled` so the two surfaces never
    drift.

    Args:
        flags: Resolved global flags, or ``None`` when the caller has no
            flag context (env-only resolution).

    Returns:
        True when ``flags.daemonless`` is set OR ``EAWF_DAEMONLESS=1``.
    """
    if flags is not None and flags.daemonless:
        return True
    return os.environ.get("EAWF_DAEMONLESS", "") == "1"


def dev_mode_enabled(flags: GlobalFlags | None) -> bool:
    """Return True when dev-mode (``--debug`` / ``EAWF_DEBUG=1``) is on.

    Dev-mode gates the raw JSON-RPC passthrough verb and the hidden
    ``daemon`` control verbs. It is off in normal operation; turning it
    on does not change any default behaviour, it only un-hides the
    developer escape hatches.

    Args:
        flags: Resolved global flags, or ``None`` for env-only
            resolution.

    Returns:
        True when ``flags.debug`` is set OR ``EAWF_DEBUG=1``.
    """
    if flags is not None and flags.debug:
        return True
    return os.environ.get("EAWF_DEBUG", "") == "1"


def reject_daemonless_on_mutating(verb: str) -> None:
    """Raise the canonical ``--daemonless`` rejection for a mutating verb.

    A mutating verb cannot run daemonless — the daemon owns the WAL +
    event-append + bus-publish ordering, so a daemonless write would
    bypass the transactional guarantees. The rejection is a
    :class:`~eawf.cli.errors.UserError` (exit-code 1) carrying
    ``data.kind="InvalidInput"`` so CI scripts pivot on the specific
    failure mode without growing the exit-code surface.

    Args:
        verb: The operator-facing verb name (e.g. ``"wave claim"``),
            interpolated into the error body for the envelope.

    Raises:
        UserError: Always — the caller surfaces it via
            :func:`eawf.cli.errors.emit_error` with
            ``data={"kind": "InvalidInput", "verb": verb}``.
    """
    raise cli_errors.UserError(
        f"--daemonless rejected: {verb} is a mutating verb "
        "(requires daemon-mediated transactions per V1)"
    )


def ensure_daemon(runtime_dir: Path | None = None) -> int:
    """Ensure a daemon is running for *runtime_dir*; return its PID.

    Thin wrapper over :func:`eawf.daemon.spawn.auto_spawn_daemon` that
    resolves the default runtime dir when none is supplied. Used by the
    mutating-verb escalation path: when no daemon is up, the first
    mutating call cold-spawns one (the auto-spawn flow). The spawn is
    silent unless ``EAWF_VERBOSE=1`` is set (the spawn helper owns that
    emission).

    Args:
        runtime_dir: Daemon runtime directory. ``None`` resolves the
            per-user default via
            :func:`eawf.daemon.runtime_dir.runtime_dir`.

    Returns:
        Integer PID of the live daemon (pre-existing or freshly spawned).

    Raises:
        DaemonUnreachable: When the auto-spawn never produced a live
            socket within the spawn-poll timeout window. Mapped from the
            spawn helper's :class:`DaemonSpawnTimeoutError` so the caller
            surfaces exit-code 4.
    """
    from eawf.daemon.runtime_dir import runtime_dir as default_runtime_dir
    from eawf.daemon.spawn import DaemonSpawnTimeoutError, auto_spawn_daemon

    resolved = runtime_dir if runtime_dir is not None else default_runtime_dir()
    try:
        pid = auto_spawn_daemon(resolved)
    except DaemonSpawnTimeoutError as exc:
        raise cli_errors.DaemonUnreachable(f"daemon auto-spawn failed: {exc}") from exc
    logger.debug(f"ensure_daemon pid={pid} runtime={resolved.name!r}")
    return pid


def escalate_mutation(
    verb: str,
    *,
    flags: GlobalFlags | None,
    runtime_dir: Path | None = None,
) -> int:
    """Apply the mutating-verb escalation rule and return the daemon PID.

    The single entry point every mutating-verb wrapper calls before it
    opens a :class:`~eawf.cli._daemon_client.DaemonClient`. Procedure:

    1. When the daemon-bypass carve-out is requested (``--daemonless``
       or ``EAWF_DAEMONLESS=1``), refuse via
       :func:`reject_daemonless_on_mutating` — mutating verbs are
       daemon-only.
    2. Otherwise ensure a daemon is up (auto-spawn on first call) and
       return its PID for the caller to attach to.

    Args:
        verb: Operator-facing verb name for the rejection envelope.
        flags: Resolved global flags (``--daemonless`` source). ``None``
            falls back to env-only resolution.
        runtime_dir: Optional explicit runtime dir (tests anchor it at a
            temp dir); ``None`` resolves the per-user default.

    Returns:
        Integer PID of the live daemon.

    Raises:
        UserError: When ``--daemonless`` was requested (mutating verbs
            reject it; ``data.kind="InvalidInput"``).
        DaemonUnreachable: When the auto-spawn failed to expose a socket.
    """
    if daemonless_requested(flags):
        reject_daemonless_on_mutating(verb)
    return ensure_daemon(runtime_dir)


__all__ = [
    "daemonless_requested",
    "dev_mode_enabled",
    "ensure_daemon",
    "escalate_mutation",
    "reject_daemonless_on_mutating",
]
