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
* :func:`_mutate_via_daemon` builds on :func:`escalate_mutation` to
  provide the single generic daemon-proxy entry: it escalates, marshals
  one typed :class:`~eawf.kernel.state.mutations.Mutation` across
  ``state.mutate``, and falls back to a caller-supplied in-process
  callable when the daemon predates the kind or the transport drops
  (the V1 carve-out).
"""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags

if TYPE_CHECKING:
    from collections.abc import Callable

    from eawf.kernel.state.mutations import MutationKind

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


def _mutate_via_daemon[FallbackT](
    kind: MutationKind,
    params: dict[str, Any],
    flags: GlobalFlags | None,
    *,
    scope_id: str,
    verb: str,
    fallback: Callable[[], FallbackT],
    idempotency_key: str | None = None,
    runtime_dir: Path | None = None,
) -> dict[str, Any] | FallbackT:
    """Proxy one typed mutation through the daemon, or run *fallback*.

    The single generic daemon-proxy entry every mutating-verb wrapper
    routes through (replacing the per-verb bespoke proxies). Procedure:

    1. :func:`escalate_mutation` applies the mutating-verb rule —
       refuse ``--daemonless`` / ``EAWF_DAEMONLESS=1`` and auto-spawn a
       daemon when none is up. A daemonless mutating call raises here
       before any wire traffic.
    2. Build a typed :class:`~eawf.kernel.state.mutations.Mutation` (fresh
       ``mutation_id``) and dispatch it across the daemon's
       ``state.mutate`` RPC.
    3. On success, return the daemon's result dict verbatim.
    4. When the daemon predates the kind (``-32601`` method-not-found,
       or a ``NotImplementedError`` carried in the RPC message) OR the
       transport drops in the CONNECT phase (``__enter__`` raises
       ``OSError`` / ``NotImplementedError`` — the request was never
       sent, so the mutation provably never applied), invoke *fallback*
       and return its value — the V1 carve-out lets the in-process
       writer carry the mutation against a pre-wire / unreachable daemon.
       A POST-SEND transport drop (``state_mutate`` raises
       ``RuntimeError`` / ``TimeoutError`` / recv-side ``OSError`` after
       the request was already on the wire) is INDETERMINATE — the
       daemon may have applied the mutation before dropping — so it
       raises :class:`~eawf.cli.errors.DaemonMutationIndeterminate`
       rather than blindly re-running *fallback* (which would
       double-apply a non-idempotent kind such as ``EVENT_APPEND``).
    5. A ``-32002 validation_failed`` rejection maps to
       :class:`~eawf.cli.errors.ValidationError`; any other RPC error
       (``-32001`` lock conflict, ``-32003`` not found, ``-32005``
       runtime unavailable, ...) maps onto its specific typed
       :class:`~eawf.cli.errors.CliError` via
       :func:`~eawf.cli.errors.cli_error_for_rpc` so the verb handler's
       ``except CliError`` surfaces a proper error envelope rather than
       leaking an uncaught ``DaemonRpcError``.

    Args:
        kind: :class:`MutationKind` discriminator the daemon dispatches
            on.
        params: Kind-specific parameter dict carried in
            :attr:`Mutation.params`.
        flags: Resolved global flags (``--daemonless`` source). ``None``
            falls back to env-only resolution.
        scope_id: Canonical scope id the mutation targets (wave / phase
            / iter id), carried into the event envelope.
        verb: Operator-facing verb name for the ``--daemonless``
            rejection envelope (e.g. ``"wave close"``).
        fallback: Zero-arg callable run when the daemon cannot serve
            the mutation (method-not-found or a CONNECT-phase transport
            drop, where the mutation provably never reached the daemon).
            Its return value is propagated to the caller unchanged. NOT
            run on a POST-SEND drop (indeterminate — see step 4).
        idempotency_key: Optional retry key forwarded to the daemon for
            the cross-runtime idempotency window.
        runtime_dir: Optional explicit runtime dir (tests anchor it at a
            temp dir); ``None`` resolves the per-user default.

    Returns:
        The daemon's ``state.mutate`` result dict on the proxied path,
        or the value returned by *fallback* on the carve-out path.

    Raises:
        UserError: When ``--daemonless`` was requested (mutating verbs
            reject it; ``data.kind="InvalidInput"``).
        DaemonUnreachable: When the auto-spawn failed to expose a
            socket (mapped from the spawn timeout).
        DaemonMutationIndeterminate: When the connection drops or the
            read times out AFTER the request was sent (POST-SEND phase) —
            the write may or may not have applied, so the fallback is not
            re-run. Exit code 4 (subclass of ``DaemonUnreachable``).
        ValidationError: When the daemon rejects the mutation with
            ``-32002 validation_failed``.
        CliError: When the daemon returns any other JSON-RPC error
            envelope — the code is mapped onto its specific typed
            subclass (``StateConflict`` / ``UserError`` / ...) via
            :func:`~eawf.cli.errors.cli_error_for_rpc` so the caller's
            ``except CliError`` handler renders it.
    """
    from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
    from eawf.kernel.state.mutations import Mutation

    escalate_mutation(verb, flags=flags, runtime_dir=runtime_dir)

    mutation = Mutation(
        kind=kind,
        scope_id=scope_id,
        mutation_id=uuid.uuid4().hex,
        idempotency_key=idempotency_key,
        params=params,
    )
    repo_root = str(((flags.workspace if flags is not None else None) or Path.cwd()).resolve())

    # Phase split so a transport drop is classified by WHEN it happened:
    #
    # * CONNECT phase (``__enter__``) — the request was never sent, so the
    #   daemon provably never applied the mutation → the in-process
    #   fallback is safe.
    # * POST-SEND phase (``state_mutate`` → ``call``) — the request was
    #   already on the wire when the connection dropped / read timed out,
    #   so the daemon MAY have applied it before dying. Re-running the
    #   fallback would double-apply a non-idempotent kind (EVENT_APPEND),
    #   so this is surfaced as the indeterminate error, NOT a blind
    #   fallback. A ``DaemonRpcError`` is a clean daemon RESPONSE (the
    #   write outcome is determinate) and keeps its specific mapping.
    client = DaemonClient(runtime_dir=runtime_dir)
    try:
        client.__enter__()
    except (OSError, NotImplementedError) as exc:
        logger.debug(f"_mutate_via_daemon connect-fallback kind={kind.value} reason={exc!s}")
        return fallback()
    try:
        return client.state_mutate(
            mutation,
            idempotency_key=idempotency_key,
            repo_root=repo_root,
        )
    except DaemonRpcError as exc:
        if exc.code == -32601 or "NotImplementedError" in (exc.message or ""):
            logger.debug(
                f"_mutate_via_daemon falling back kind={kind.value} "
                f"code={exc.code} message={exc.message!r}"
            )
            return fallback()
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            raise cli_errors.ValidationError(exc.message) from exc
        raise cli_errors.cli_error_for_rpc(exc.code, exc.message) from exc
    except (RuntimeError, OSError, TimeoutError) as exc:
        logger.debug(
            f"_mutate_via_daemon indeterminate kind={kind.value} reason={exc!s}; "
            f"daemon connection lost after the request was sent — not falling back"
        )
        raise cli_errors.DaemonMutationIndeterminate(
            f"daemon connection lost mid-mutation ({kind.value}); "
            f"the state may or may not have applied — re-check before retrying"
        ) from exc
    finally:
        client.__exit__(None, None, None)


__all__ = [
    "_mutate_via_daemon",
    "daemonless_requested",
    "dev_mode_enabled",
    "ensure_daemon",
    "escalate_mutation",
    "reject_daemonless_on_mutating",
]
