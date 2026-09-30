"""Lifecycle nouns: shared transaction core + Typer app registry.

This module owns the five lifecycle Typer apps
(``project_app``/``track_app``/``phase_app``/``iter_app``/``wave_app``,
plus the ``wave budget`` sub-app) and the shared transactional helpers
every lifecycle handler composes under a held sibling lock. The concrete
command bodies live in four sibling modules:

- :mod:`eawf.surfaces.cli.commands.lifecycle_phase` — project / track / phase.
- :mod:`eawf.surfaces.cli.commands.lifecycle_iter` — iter.
- :mod:`eawf.surfaces.cli.commands.lifecycle_wave` — wave mutators
  (plan / claim / close / show / fail / update).
- :mod:`eawf.surfaces.cli.commands.lifecycle_wave_read` — wave read / dispatch /
  budget verbs (graph / next-ready / blocks-rebuild / dispatch /
  dispatch-batch / budget set·consume·show).
- :mod:`eawf.surfaces.cli.commands.lifecycle_wave_prune` — the
  ``wave prune-branches`` verb.
- :mod:`eawf.surfaces.cli.commands.domain` — the native epoch-2 verbs of
  the ``milestone`` / ``batch`` / ``task`` apps plus ``track retire``.
  Those forward to the daemon's per-entity lifecycle RPCs rather than
  running the shared transaction below, so they share this module's apps
  and none of its helpers.

Each sibling imports the apps and shared helpers from this module and
attaches its handlers via ``@<app>.command(...)``. Importing this module
imports the siblings (at the bottom, after every shared symbol is
defined), so the decorators run and the apps carry their full verb set.
Existing import sites (``app.py``, ``wave_ci``, ``pr_review``,
``wave_policy``, ``worktree``, tests) keep resolving
``from eawf.surfaces.cli.commands.lifecycle import phase_app`` /
``_load_state_readonly`` / ``_compute_iter_bump_hints`` unchanged.

Each handler follows the canonical mutation pattern:

1. Resolve the active ``state.json`` path via :func:`scope.resolve_state_path`.
2. Acquire the sibling lockfile via :func:`portalock.acquire`. The lock is held
   for the entire transaction so concurrent claimers see exactly-once
   semantics.
3. Load + parse + Pydantic-validate the current state.
4. Apply the transition / allocator from :mod:`eawf.workflow.lifecycle`.
5. Run :func:`validate_state` over the candidate state — schema and
   cross-entity invariants must pass before we persist.
6. Append a single ``EVENT``-kind record to
   ``<state>/store/event.jsonl`` *before* writing ``state.json``. This
   matches the canonical evidence-side ordering established in commit
   ``18ee287``: the JSONL audit record always lands first, then the
   state mutation. The surrounding ``portalock`` on ``state.json`` is
   held continuously, so the half-applied transaction is never visible
   to another writer. If the event append fails, ``state.json`` is
   unchanged. If the state write fails after a successful append, the
   store carries a "future" event for a mutation that did not commit —
   recoverable by forward-replay or audit, and strictly preferable to
   losing the audit trail entirely (the prior state-first ordering left
   a mutated ``state.json`` with no event).
7. Persist ``state.json`` atomically (tmp + ``os.replace``), fsync the
   directory.
8. Emit the ``--json`` envelope or human-readable text via
   :func:`emit_json_or_text`.

Errors are mapped to canonical exit codes via :mod:`eawf.surfaces.cli.errors`. The
mapping is conservative: schema/invariant violations exit 4, lock timeouts
exit 5, structural rejections (duplicate id, unknown parent, terminal-status
target) exit 3, and anything that is genuinely a missing scope/state exits 2.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import typer

from eawf.kernel.state.enums import (
    CloseFailureKind,
)
from eawf.kernel.state.io import (
    append_event,
    build_event_envelope,
    commit_mutation,
    fallback_wal_dir,
    state_version,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


# ---- Typer apps -------------------------------------------------------------

project_app = typer.Typer(
    name="project",
    help="Project-level lifecycle (init).",
    no_args_is_help=True,
)
track_app = typer.Typer(
    name="track",
    help="Track lifecycle (add, switch).",
    no_args_is_help=True,
)
phase_app = typer.Typer(
    name="phase",
    help="Phase lifecycle (open, close, reopen).",
    no_args_is_help=True,
)
iter_app = typer.Typer(
    name="iter",
    help="Iteration lifecycle (open, close).",
    no_args_is_help=True,
)
wave_app = typer.Typer(
    name="wave",
    help="Wave lifecycle (plan, claim, close, fail, graph, next-ready).",
    no_args_is_help=True,
)

wave_budget_app = typer.Typer(
    name="budget",
    help="Per-wave token-budget cap (set, consume, show).",
    no_args_is_help=True,
)
wave_app.add_typer(wave_budget_app, name="budget")

# The epoch-2 lifecycle nouns. They carry no epoch-1 verbs at all: each
# one is a native per-entity move, and the handlers live in the sibling
# ``domain`` module beside the Track verb that joins this app's own.
milestone_app = typer.Typer(
    name="milestone",
    help="Milestone lifecycle (activate, open-review, open-approval, accept, cancel).",
    no_args_is_help=True,
)
batch_app = typer.Typer(
    name="batch",
    help=(
        "Delivery-batch lifecycle (activate, integrate, adopt-landed, ready, merge, reconcile, "
        "observe-merge, complete)."
    ),
    no_args_is_help=True,
)
task_app = typer.Typer(
    name="task",
    help="Task lifecycle (promote, claim, start, submit, seal, prove, assess, ready, complete).",
    no_args_is_help=True,
)
run_app = typer.Typer(
    name="run",
    help="Run lifecycle (create, start, finish, fail).",
    no_args_is_help=True,
)
repository_app = typer.Typer(
    name="repository",
    help="Repository rows a plan binds its head to (create).",
    no_args_is_help=True,
)


# ---- Internal helpers -------------------------------------------------------


def _read_state_payload(path: Path) -> dict[str, Any]:
    """Read and JSON-decode *path*.

    Raises:
        cli_errors.UserError: State file does not exist.
    """
    if not path.exists():
        raise cli_errors.UserError(f"state file not found: {path}", kind="NotFound")
    raw = path.read_bytes()
    try:
        return orjson.loads(raw)  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.StateConflict(
            f"corrupted state at {path}: {exc}", kind="IntegrityViolation"
        ) from exc


def _validate_or_raise(payload: dict[str, Any]) -> State:
    """Validate the candidate payload; raise ``ValidationError`` on error."""
    from eawf.kernel.validate.strict import validate_state as validate_state_payload

    report = validate_state_payload(payload, strict_optional=False)
    if not report.ok:
        msgs = list(report.schema_errors)
        msgs.extend(f"{v.code}@{v.path}: {v.message}" for v in report.violations)
        raise cli_errors.ValidationError("; ".join(msgs))
    assert report.state is not None  # ok==True guarantees this
    return report.state


_state_version = state_version
_build_event_envelope = build_event_envelope
_append_event = append_event
_fallback_wal_dir = fallback_wal_dir
_commit_mutation = commit_mutation


# ---- Wave git / commit-ref helpers ------------------------------------------

_GIT_REV_PARSE_TIMEOUT_SECONDS: float = 5.0


def _close_failure_kind(exc: BaseException) -> CloseFailureKind:
    """Type one CLI-side close failure from the exception chain.

    The close RPC helpers fold every transport fault onto one
    :class:`~eawf.surfaces.cli.errors.DaemonUnreachable`, so the original
    ``TimeoutError`` only survives on ``__cause__``. Walking the chain keeps
    the operator-facing kind identical to the one the daemon would persist
    for the same fault instead of flattening a timeout into generic
    harness breakage.
    """
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, TimeoutError):
            return CloseFailureKind.TIMED_OUT
        seen = seen.__cause__
    return CloseFailureKind.HARNESS_FAULT


def _wave_close_via_daemon(
    *,
    flags: GlobalFlags,
    wave_id: str,
    outcome: str,
    resolved_sha: str | None,
    tokens_consumed: int | None,
    no_runtime_waiver: bool = False,
    transport_fallback: list[bool] | None = None,
) -> bool:
    """Proxy a wave close through the daemon's ``state.mutate`` RPC.

    Returns True on a successful daemon-mediated close (caller exits
    early); False when the daemon refuses the kind or the close RPC hit
    a transport error (RuntimeError / OSError), in which case the caller
    falls through to the in-process path. A daemon-required failure or
    validation rejection emits the error envelope before returning True
    so the caller does not double-emit.

    A close-RPC ``TimeoutError`` is TERMINAL, not a fallback: the daemon
    may still be mid-close under the lock, so silently re-running the
    close in-process could double-apply it. On a timeout the helper emits
    a typed :class:`~eawf.surfaces.cli.errors.DaemonMutationIndeterminate`
    envelope and exits non-zero rather than returning False.

    Args:
        transport_fallback: Optional one-element sink. When the close RPC
            fails with a RuntimeError / OSError, ``[0]`` is set to ``True``
            so the caller's in-process close can stamp the event's
            ``close_mechanism`` as ``"daemon-fallback"`` (distinct from a
            gate-passed ``"daemon"`` close).
    """
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError
    from eawf.surfaces.cli._mutation import _daemon_reachable

    if not _daemon_reachable():
        cli_errors.emit_error(
            cli_errors.StateConflict(
                "daemon_required: daemon.proxy_enabled=true but the daemon is unreachable; "
                "run `eawf daemon start` or unset daemon.proxy_enabled for the V1 carve-out",
                kind="IntegrityViolation",
            ),
            flags=flags,
        )
        return True  # error already emitted; treat as handled

    params: dict[str, Any] = {"wave_id": wave_id, "outcome": outcome, "commit": resolved_sha}
    if tokens_consumed is not None:
        params["tokens_consumed"] = tokens_consumed
    if no_runtime_waiver:
        params["no_runtime_waiver"] = True

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=wave_id,
        mutation_id=uuid.uuid4().hex,
        idempotency_key=None,
        params=params,
    )
    repo_root = str((flags.workspace or Path.cwd()).resolve())
    try:
        with DaemonClient() as client:
            result = client.state_mutate(mutation, repo_root=repo_root)
    except DaemonRpcError as exc:
        if exc.code == -32601 or "NotImplementedError" in (exc.message or ""):
            logger.debug(
                f"_wave_close_via_daemon falling back mutation_kind={mutation.kind.value} "
                f"code={exc.code} message={exc.message!r}"
            )
            return False
        if exc.code == cli_errors.RPC_VALIDATION_FAILED:
            cli_errors.emit_error(cli_errors.ValidationError(exc.message), flags=flags)
            return True
        cli_errors.emit_error(
            cli_errors.StateConflict(exc.message, kind="IntegrityViolation"), flags=flags
        )
        return True
    except TimeoutError as exc:
        # Terminal: the daemon may still be mid-close under the lock, so a
        # silent in-process retry could double-apply the close. Emit a typed
        # indeterminate error and exit non-zero rather than fall through.
        logger.debug(f"_wave_close_via_daemon close_rpc_timeout={exc!s}")
        cli_errors.emit_error(
            cli_errors.DaemonMutationIndeterminate(
                f"close RPC timed out: the close of wave {wave_id!r} may still be "
                "running in the daemon; re-check with 'eawf status' before retrying"
            ),
            flags=flags,
            data={
                "wave": wave_id,
                "failure_kind": _close_failure_kind(exc).value,
            },
        )
        return True  # emit_error raised typer.Exit; kept for the return type
    except (RuntimeError, OSError) as exc:
        logger.debug(f"_wave_close_via_daemon transport_error={exc!s}")
        if transport_fallback is not None:
            transport_fallback[0] = True
        return False

    text = f"wave close {wave_id} outcome={outcome!r} (via daemon)"
    payload = {
        "wave": wave_id,
        "outcome": outcome,
        "commit": resolved_sha,
        "tokens_consumed": tokens_consumed,
        "no_runtime_waiver": no_runtime_waiver,
        "proxied": True,
        "event": result.get("event"),
        "before_version": result.get("before_version"),
        "after_version": result.get("after_version"),
    }
    emit_json_or_text(payload, text, flags=flags)
    return True


# ---- Read-only state loaders ------------------------------------------------


def _load_state_readonly(ctx: typer.Context) -> tuple[State, GlobalFlags] | None:
    """Resolve + read + parse state.json under no lock.

    Read-only verbs ride the same scope-resolution path mutators use, but
    do not need the sibling lock — a stale snapshot is acceptable for
    enumeration. Returns ``None`` after emitting the canonical error
    envelope when resolution / parse fails (caller treats ``None`` as
    "exit was already raised by ``emit_error``").
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.state.models import State

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return None
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(
                f"state file not found: {state_path}; run `eawf repository create`", kind="NotFound"
            ),
            flags=flags,
        )
        return None
    payload = _read_state_payload(state_path)
    try:
        state = State.model_validate(payload)
    except PydValidationError as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(
                f"state at {state_path} fails schema validation: {exc}", kind="IntegrityViolation"
            ),
            flags=flags,
        )
        return None
    return state, flags


# ---- Mutation runner --------------------------------------------------------


# ---- Command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@<app>.command(...)`` decorators
# so the apps above carry their full verb set. The imports sit at the bottom,
# after every shared symbol is defined, so the siblings can import the apps and
# helpers from this module without a circular-import failure.
from eawf.surfaces.cli.commands import domain as _domain  # noqa: E402, F401
from eawf.surfaces.cli.commands import domain_delivery as _domain_delivery  # noqa: E402, F401
from eawf.surfaces.cli.commands import (  # noqa: E402
    domain_integration as _domain_integration,  # noqa: F401
)
from eawf.surfaces.cli.commands import domain_legacy as _domain_legacy  # noqa: E402, F401
from eawf.surfaces.cli.commands import track as _track  # noqa: E402, F401

# ---- Re-exports -------------------------------------------------------------

__all__ = [
    "batch_app",
    "iter_app",
    "milestone_app",
    "phase_app",
    "project_app",
    "run_app",
    "task_app",
    "track_app",
    "wave_app",
    "wave_budget_app",
]
