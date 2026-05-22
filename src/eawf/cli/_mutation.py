"""Transactional wrapper + daemon-proxy resolution for mutating verbs.

This module owns the in-process write path and the predicates the
daemon-proxy entry (:func:`eawf.cli._dispatch._mutate_via_daemon`)
consults to decide proxy-vs-fallback:

* :func:`state_transaction` — the in-process context manager and the
  common chokepoint every state-mutating CLI verb routes through (and
  the fallback the daemon-proxy entry falls back to). Acquires the
  sibling lock for ``state.json``, yields the typed
  :class:`~eawf.state.models.State` for the caller to mutate in place,
  then validates and atomically writes it back. The full read-modify-
  write runs under one lock acquisition so concurrent writers serialise.
  Because it is the shared write path, the ``--daemonless`` rejection
  for mutating verbs lives here: when the operator passed the
  ``--daemonless`` flag (recorded process-wide by the root callback via
  :func:`set_daemonless_flag`) and the caller did not opt out with
  ``read_only=True``, the transaction refuses before acquiring the lock.

* :func:`_proxy_enabled` / :func:`_daemon_reachable` — the predicates a
  proxy callsite consults before marshalling a mutation across the
  daemon's ``state.mutate`` RPC; ``daemon.proxy_enabled=false`` (CI
  carve-out) or an unreachable daemon routes back to the in-process
  fallback.

Library mutators consumed by :func:`state_transaction` must:

1. Take the typed ``State`` and mutate it in place (e.g.
   ``state.goals = goals``; ``state.updated_at = now``).
2. Return any envelope/event records that the handler appends after
   the transaction commits the new state.

The handler is responsible for appending events to ``events.jsonl``
(and any kind-specific JSONL) inside the transaction body — the
helper holds ``portalock(state.json)`` only; sibling locks for
``events.jsonl`` etc. are acquired separately by the appender.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import orjson

from eawf.cli import errors as cli_errors
from eawf.lock import portalock
from eawf.state.models import State
from eawf.state.writer import atomic_write_json_locked
from eawf.validate.strict import validate_state

logger = logging.getLogger(__name__)


# Process-wide record of whether the operator passed the ``--daemonless``
# *flag* on this invocation. Set once per process by the Typer root
# callback (:func:`eawf.cli.app._root`) from :class:`GlobalFlags`. The
# chokepoint in :func:`state_transaction` reads it to enforce the rule
# that mutating verbs reject ``--daemonless``.
#
# This keys on the *flag* deliberately, NOT on ``EAWF_DAEMONLESS=1``:
# the env hatch routes mutating verbs to the in-process fallback for the
# CI / one-shot / recovery carve-out (handled in :func:`_proxy_enabled`)
# and must NOT hard-reject — the integration suite forces the env var
# autouse precisely so its in-process mutating verbs keep working. The
# explicit ``--daemonless`` flag, by contrast, is the operator asserting
# "no daemon", which a mutating verb cannot honour.
_DAEMONLESS_FLAG_REQUESTED: bool = False


def set_daemonless_flag(requested: bool) -> None:
    """Record whether the ``--daemonless`` flag was passed this invocation.

    Called once by the Typer root callback after it builds
    :class:`~eawf.cli.flags.GlobalFlags`. Always called (with ``False``
    when the flag is absent) so the process-wide record reflects only the
    current invocation and never leaks across CliRunner calls in tests.

    Args:
        requested: ``True`` when ``--daemonless`` was on the command line.
    """
    global _DAEMONLESS_FLAG_REQUESTED
    _DAEMONLESS_FLAG_REQUESTED = requested


def daemonless_flag_requested() -> bool:
    """Return whether the ``--daemonless`` flag was passed this invocation.

    Reads the process-wide record set by :func:`set_daemonless_flag`.
    Distinct from :func:`eawf.cli._dispatch.daemonless_requested`, which
    also returns ``True`` for the ``EAWF_DAEMONLESS=1`` env hatch — this
    helper is flag-only because the chokepoint must reject the explicit
    flag while letting the env hatch fall through to the in-process path.

    Returns:
        ``True`` when the operator passed ``--daemonless`` on this call.
    """
    return _DAEMONLESS_FLAG_REQUESTED


@contextmanager
def state_transaction(
    state_path: Path,
    *,
    timeout: float = 5.0,
    read_only: bool = False,
) -> Iterator[State]:
    """Yield a typed :class:`State` under ``portalock(state_path)``.

    Procedure:

    0. Daemonless gate: when this is a mutating transaction
       (``read_only=False``, the default) AND the operator passed the
       ``--daemonless`` flag (per :func:`daemonless_flag_requested`),
       refuse with :class:`~eawf.cli.errors.InvalidInput` *before*
       touching the file — mutating verbs are daemon-only. Read-only
       callers (``worktree list``, ``roadmap show``) pass
       ``read_only=True`` to bypass this gate.
    1. Acquire :func:`eawf.lock.portalock.acquire` on *state_path* with
       *timeout* (default 5 s, matching the rest of the CLI).
    2. Read + decode + schema-validate the on-disk state. Schema errors
       raise :class:`~eawf.cli.errors.ValidationFailed`.
    3. ``yield`` the typed :class:`State` to the caller for in-place
       mutation.
    4. On caller success, re-validate the mutated state (schema +
       invariants). Failures raise :class:`ValidationFailed` and the
       on-disk file is left unchanged.
    5. ``atomic_write_json_locked`` persists the new payload while
       the lock is still held.

    Args:
        state_path: Absolute path to ``state.json``.
        timeout: Lock-acquisition timeout in seconds.
        read_only: When ``True`` the caller only reads the yielded state
            for a consistent snapshot (e.g. listing / show verbs) and the
            ``--daemonless`` rejection is skipped. Defaults to ``False``
            (mutating) so every write path inherits the gate.

    Raises:
        InvalidInput: When ``read_only=False`` and the ``--daemonless``
            flag was passed (``data.kind="InvalidInput"``) — mutating
            verbs cannot run daemonless.
        NotFound: When *state_path* does not exist.
        ValidationFailed: When the loaded payload fails schema
            validation, or the post-mutation payload fails schema
            or invariant checks.
        LockConflict: When the sibling lock cannot be acquired within
            *timeout*.

    .. warning::

        This context manager is **not re-entrant**. Calling
        ``state_transaction`` from inside an already-active
        ``state_transaction`` body will deadlock — ``portalock`` (and
        ``flock`` underneath it) is non-recursive. Composition pattern:
        *outer handler opens the transaction, inner helpers receive the
        already-loaded* :class:`State` *as a parameter and return mutations
        rather than acquiring the lock themselves.*
    """
    if not read_only and daemonless_flag_requested():
        raise cli_errors.InvalidInput(
            "--daemonless rejected: this is a mutating verb "
            "(requires daemon-mediated transactions per V1)"
        )
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    try:
        with portalock.acquire(state_path, timeout=timeout):
            raw = state_path.read_bytes()
            payload = orjson.loads(raw)
            report = validate_state(payload, strict_optional=False)
            if report.state is None:
                raise cli_errors.ValidationFailed(
                    "state schema invalid: " + "; ".join(report.schema_errors[:3])
                )
            state = report.state
            yield state
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise cli_errors.ValidationFailed(
                    "post-mutation schema invalid: " + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise cli_errors.ValidationFailed(
                    f"post-mutation invariants violated: {violation_codes}"
                )
            atomic_write_json_locked(state_path, new_payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.LockConflict(str(exc)) from exc


# ---- daemon-proxy resolution helpers ---------------------------------------


def _proxy_enabled(workspace: Path | None) -> bool:
    """Return True when the merged config enables daemon proxying.

    Resolves ``daemon.proxy_enabled`` through the layered-config merge
    (built-in → global → workspace → repo → local → env). Default since
    P24-W10 is ``True`` — the V1 daemonless carve-out runs only when
    the merged config explicitly opts out OR the process has
    ``EAWF_DAEMONLESS=1`` set.
    """
    import os

    if os.environ.get("EAWF_DAEMONLESS", "") == "1":
        return False
    from eawf.config.layered import merge_config

    repo = workspace if workspace is not None else Path.cwd()
    try:
        merged, _ = merge_config(workspace=workspace, repo=repo)
    except (OSError, ValueError, KeyError) as exc:
        logger.debug(f"_proxy_enabled merge_config_failed={exc!s} default_proxy_enabled=False")
        return False
    daemon_cfg = merged.get("daemon")
    if not isinstance(daemon_cfg, dict):
        return False
    value = daemon_cfg.get("proxy_enabled", False)
    return bool(value)


def _daemon_reachable(runtime_dir: Path | None = None) -> bool:
    """Return True when the daemon is up and answering ``daemon.ping``.

    Used by the bespoke ``_wave_close_via_daemon`` proxy to decide
    between the proxy path and the daemonless fallback (per V1 carve-
    out: reads + recovery shell + CI). Probes via
    :class:`~eawf.cli._daemon_client.DaemonClient`
    so the helper exercises the same wire format as the production
    path; transport errors map to a False return without raising so
    the caller can fall through to the in-process path on a clean
    "daemon down" verdict.
    """
    from eawf.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(runtime_dir=runtime_dir) as client:
            client.call("daemon.ping")
    except (DaemonRpcError, RuntimeError, OSError, TimeoutError, NotImplementedError) as exc:
        logger.debug(f"_daemon_reachable False reason={exc!s}")
        return False
    return True
