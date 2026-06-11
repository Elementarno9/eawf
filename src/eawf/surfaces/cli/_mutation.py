"""Transactional wrapper + daemon-proxy resolution for mutating verbs.

This module owns the in-process write path and the predicates the
daemon-proxy entry (:func:`eawf.surfaces.cli._dispatch._mutate_via_daemon`)
consults to decide proxy-vs-fallback:

* :func:`state_transaction` — the in-process context manager and the
  common chokepoint every state-mutating CLI verb routes through (and
  the fallback the daemon-proxy entry falls back to). Acquires the
  sibling lock for ``state.json``, yields the typed
  :class:`~eawf.kernel.state.models.State` for the caller to mutate in place,
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
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Literal

import orjson

from eawf.kernel.state.models import State, Wave
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.kernel.validate.strict import validate_state
from eawf.runtime.lock import portalock
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)

#: How a wave close reached ``state.json``. Stamped on the close event's
#: :attr:`~eawf.kernel.store.kinds.event.EventPayload.extras` map under the
#: ``close_mechanism`` key so an audit can tell a daemon-mediated close from a
#: daemonless-with-waiver bypass without re-deriving it:
#:
#: - ``"daemon"`` -- the canonical daemon-mediated path (rule 4);
#: - ``"daemonless"`` -- the V1 carve-out fallback for a NON-gate-bearing
#:   wave (the env hatch keeps working with no waiver needed);
#: - ``"daemonless-waiver"`` -- a GATE-BEARING wave force-closed daemonless
#:   under an explicit per-invocation operator waiver. The bypass-door event
#:   names the wave + reason so the override is auditable.
CloseMechanism = Literal["daemon", "daemonless", "daemonless-waiver"]

#: Event-store ``event_type`` for the daemonless gate-bearing bypass record.
DAEMONLESS_WAIVER_EVENT_TYPE = "wave.close.daemonless_waiver"


# Process-wide record of whether the operator passed the ``--daemonless``
# *flag* on this invocation. Set once per process by the Typer root
# callback (:func:`eawf.surfaces.cli.app._root`) from :class:`GlobalFlags`. The
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
    :class:`~eawf.surfaces.cli.flags.GlobalFlags`. Always called (with ``False``
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
    Distinct from :func:`eawf.surfaces.cli._dispatch.daemonless_requested`, which
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
       refuse with :class:`~eawf.surfaces.cli.errors.UserError`
       (``kind="InvalidInput"``) *before* touching the file — mutating
       verbs are daemon-only. Read-only
       callers (``worktree list``, ``roadmap show``) pass
       ``read_only=True`` to bypass this gate.
    1. Acquire :func:`eawf.runtime.lock.portalock.acquire` on *state_path* with
       *timeout* (default 5 s, matching the rest of the CLI).
    2. Read + decode + schema-validate the on-disk state. Schema errors
       raise :class:`~eawf.surfaces.cli.errors.ValidationError`.
    3. ``yield`` the typed :class:`State` to the caller for in-place
       mutation.
    4. On caller success, re-validate the mutated state (schema +
       invariants). Failures raise :class:`ValidationError` and the
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
        UserError: When ``read_only=False`` and the ``--daemonless``
            flag was passed (``kind="InvalidInput"`` — mutating verbs
            cannot run daemonless); or when *state_path* does not exist
            (``kind="NotFound"``).
        ValidationError: When the loaded payload fails schema
            validation, or the post-mutation payload fails schema
            or invariant checks.
        StateConflict: When the sibling lock cannot be acquired within
            *timeout* (``kind="LockConflict"``).

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
        raise cli_errors.UserError(
            "--daemonless rejected: this is a mutating verb "
            "(requires daemon-mediated transactions per V1)",
            kind="InvalidInput",
        )
    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    try:
        with portalock.acquire(state_path, timeout=timeout):
            raw = state_path.read_bytes()
            payload = orjson.loads(raw)
            report = validate_state(payload, strict_optional=False)
            if report.state is None:
                raise cli_errors.ValidationError(
                    "state schema invalid: " + "; ".join(report.schema_errors[:3])
                )
            state = report.state
            yield state
            new_payload = state.model_dump(mode="json")
            post = validate_state(new_payload, strict_optional=False)
            if post.state is None:
                raise cli_errors.ValidationError(
                    "post-mutation schema invalid: " + "; ".join(post.schema_errors[:3])
                )
            if post.violations:
                violation_codes = ",".join(v.code for v in post.violations)
                raise cli_errors.ValidationError(
                    f"post-mutation invariants violated: {violation_codes}"
                )
            atomic_write_json_locked(state_path, new_payload)
    except portalock.LockTimeout as exc:
        raise cli_errors.StateConflict(str(exc), kind="LockConflict") from exc


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
    from eawf.kernel.config.layered import merge_config

    repo = workspace if workspace is not None else Path.cwd()
    try:
        merged, _ = merge_config(workspace=workspace, repo=repo)
    except (OSError, ValueError, KeyError) as exc:
        logger.debug(f"_proxy_enabled False reason={exc!s}")
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
    :class:`~eawf.surfaces.cli._daemon_client.DaemonClient`
    so the helper exercises the same wire format as the production
    path; transport errors map to a False return without raising so
    the caller can fall through to the in-process path on a clean
    "daemon down" verdict.
    """
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

    try:
        with DaemonClient(runtime_dir=runtime_dir) as client:
            client.call("daemon.ping")
    except (DaemonRpcError, RuntimeError, OSError, TimeoutError, NotImplementedError) as exc:
        logger.debug(f"_daemon_reachable False reason={exc!s}")
        return False
    return True


# ---- daemonless close-with-waiver door + close-mechanism stamp -------------


def _daemonless_env_set() -> bool:
    """Return True when the ``EAWF_DAEMONLESS=1`` env hatch is active.

    The env hatch (distinct from the ``--daemonless`` flag) routes mutating
    verbs to the in-process WAL-backed fallback for the V1 carve-out (CI /
    one-shot / recovery shell). The gate-bearing close-waiver door fires only
    under this hatch -- a daemon-mediated close needs no waiver because the
    daemon close gate already runs every falsifier.

    Returns:
        ``True`` when ``EAWF_DAEMONLESS=1`` is set in the environment.
    """
    return os.environ.get("EAWF_DAEMONLESS", "") == "1"


def wave_is_gate_bearing(wave: Wave) -> bool:
    """Return whether *wave* carries a typed close gate.

    A wave is *gate-bearing* when it attaches at least one typed
    :class:`~eawf.kernel.spec.common.GateSpec` (:attr:`Wave.gates`). Those
    gates ARE the wave's falsifiers -- the deterministic floor / jury / oracle
    the daemon close gate runs. A gate-bearing wave's close therefore needs the
    gate to have run (or an explicit operator waiver); a wave with no gate has
    nothing to falsify, so the daemonless env hatch keeps working for it with
    no waiver required.

    Args:
        wave: The wave being closed. Read-only.

    Returns:
        ``True`` when the wave attaches one or more typed gates.
    """
    return bool(wave.gates)


def resolve_close_mechanism(*, gate_bearing: bool, waived: bool) -> CloseMechanism:
    """Return the :data:`CloseMechanism` to stamp on the close event.

    Pure function of the current invocation's daemonless state + waiver:

    * a daemon-mediated close (the env hatch is NOT set) -> ``"daemon"``;
    * a daemonless close of a GATE-BEARING wave under an operator waiver ->
      ``"daemonless-waiver"`` (the bypass door fired);
    * any other daemonless close (non-gate-bearing, or the gate did not
      apply) -> ``"daemonless"`` -- the V1 carve-out fallback.

    Args:
        gate_bearing: Whether the closing wave attaches typed gates
            (:func:`wave_is_gate_bearing`).
        waived: Whether the operator passed the per-invocation daemonless
            close waiver this call.

    Returns:
        The mechanism literal for the close event's ``close_mechanism`` extra.
    """
    if not _daemonless_env_set():
        return "daemon"
    if gate_bearing and waived:
        return "daemonless-waiver"
    return "daemonless"


def close_event_extras(
    base_extras: dict[str, str | int | float | bool] | None,
    *,
    gate_bearing: bool,
    waived: bool,
) -> dict[str, str | int | float | bool]:
    """Return *base_extras* with the ``close_mechanism`` field stamped on.

    Every wave-close event carries ``close_mechanism`` so a downstream audit
    can distinguish a daemon-mediated close from a daemonless-with-waiver
    bypass without re-deriving it. Additive -- existing extras are preserved
    and the mechanism is folded in (overwriting any stale value).

    Args:
        base_extras: The rolled-up advisory extras already destined for the
            close event (e.g. ``readiness_warnings_count``), or ``None``.
        gate_bearing: Whether the closing wave attaches typed gates.
        waived: Whether the per-invocation daemonless close waiver was passed.

    Returns:
        A new extras dict carrying ``close_mechanism`` plus every base extra.
    """
    extras: dict[str, str | int | float | bool] = dict(base_extras) if base_extras else {}
    extras["close_mechanism"] = resolve_close_mechanism(gate_bearing=gate_bearing, waived=waived)
    return extras


def enforce_daemonless_close_waiver(
    wave: Wave,
    *,
    state_path: Path,
    waived: bool,
    reason: str | None = None,
) -> CloseMechanism:
    """Gate a daemonless wave close on an explicit operator waiver.

    The bypass-door guard for AGENTS rule 4's V1 carve-out: under the
    ``EAWF_DAEMONLESS=1`` env hatch the daemon close gate never runs, so a
    GATE-BEARING wave (:func:`wave_is_gate_bearing`) would otherwise slip its
    falsifiers entirely. This guard REQUIRES an explicit per-invocation waiver
    for such a close:

    * **not daemonless** -- the daemon mediates the close and runs every gate;
      no waiver is needed. Returns ``"daemon"``.
    * **daemonless + NOT gate-bearing** -- the wave has nothing to falsify, so
      the env hatch keeps working with no waiver. Returns ``"daemonless"``.
    * **daemonless + gate-bearing + NOT waived** -- REJECT with a typed
      :class:`~eawf.surfaces.cli.errors.UserError` (``kind="InvalidInput"``):
      the gate-bearing close cannot run daemonless without an operator
      override.
    * **daemonless + gate-bearing + waived** -- ALLOW: append a waiver EVENT to
      ``event.jsonl`` naming the wave + reason (so the override is auditable),
      then return ``"daemonless-waiver"`` for the close event's mechanism.

    Args:
        wave: The wave being closed. Read-only.
        state_path: Path to ``state.json`` -- anchors the sibling event store
            the waiver record lands in.
        waived: Whether the operator passed the per-invocation daemonless close
            waiver this call.
        reason: Operator reason for the bypass. Recorded verbatim on the waiver
            event. ``None`` records a generic ``"unspecified"`` reason.

    Returns:
        The :data:`CloseMechanism` the close event should stamp.

    Raises:
        UserError: When the close is daemonless + gate-bearing + NOT waived
            (``kind="InvalidInput"``) -- the gate-bearing close needs the
            explicit waiver.
    """
    gate_bearing = wave_is_gate_bearing(wave)
    if not _daemonless_env_set():
        return "daemon"
    if not gate_bearing:
        return "daemonless"
    if not waived:
        raise cli_errors.UserError(
            f"daemonless close rejected: wave {wave.id!r} is gate-bearing "
            f"({len(wave.gates)} gate(s)); a gate-bearing close cannot run "
            "daemonless without an explicit operator waiver",
            kind="InvalidInput",
        )
    _append_daemonless_waiver_event(wave, state_path=state_path, reason=reason)
    return "daemonless-waiver"


def _append_daemonless_waiver_event(
    wave: Wave,
    *,
    state_path: Path,
    reason: str | None,
) -> None:
    """Append the daemonless-bypass waiver EVENT naming *wave* + *reason*.

    The bypass override is auditable: the row names the wave id and the
    operator's reason under the event's
    :attr:`~eawf.kernel.store.kinds.event.EventPayload.extras` map so an audit
    can reconstruct *which* gate-bearing wave was force-closed daemonless and
    *why*. Routed through :func:`eawf.kernel.store.append.append_envelope` so
    the on-disk row is indistinguishable from a daemon-written event.

    Args:
        wave: The waived wave. Its id names the override.
        state_path: Path to ``state.json`` (anchors the event store).
        reason: Operator reason recorded verbatim; ``None`` -> ``"unspecified"``.
    """
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.append import append_envelope
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload
    from eawf.kernel.store.paths import store_path

    reason_text = reason if reason else "unspecified"
    now = datetime.now(UTC)
    summary = f"daemonless close waiver wave={wave.id} reason={reason_text!r}"
    payload = EventPayload(
        timestamp=now,
        event_type=DAEMONLESS_WAIVER_EVENT_TYPE,
        actor="cli",
        command="wave close",
        args_hash="",
        status="warn",
        message=summary,
        extras={
            "close_mechanism": "daemonless-waiver",
            "wave": wave.id,
            "reason": reason_text,
        },
    ).model_dump(mode="json")
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{wave.id}-daemonless-waiver",
        kind=StoreKind.EVENT,
        scope_id=wave.id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=payload,
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(state_path, StoreKind.EVENT), envelope)
    logger.warning(
        f"enforce_daemonless_close_waiver wave={wave.id!r} status=waived "
        f"reason={reason_text!r} mechanism=daemonless-waiver"
    )
