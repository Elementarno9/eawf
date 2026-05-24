"""Lifecycle nouns: shared transaction core + Typer app registry.

This module owns the five lifecycle Typer apps
(``project_app``/``subproject_app``/``phase_app``/``iter_app``/``wave_app``,
plus the ``wave budget`` sub-app) and the shared transactional helpers
every lifecycle handler composes under a held sibling lock. The concrete
command bodies live in four sibling modules:

- :mod:`eawf.cli.commands.lifecycle_phase` — project / subproject / phase.
- :mod:`eawf.cli.commands.lifecycle_iter` — iter.
- :mod:`eawf.cli.commands.lifecycle_wave` — wave mutators
  (plan / claim / close / show / fail / update).
- :mod:`eawf.cli.commands.lifecycle_wave_read` — wave read / dispatch /
  budget verbs (graph / next-ready / blocks-rebuild / dispatch /
  dispatch-batch / budget set·consume·show).

Each sibling imports the apps and shared helpers from this module and
attaches its handlers via ``@<app>.command(...)``. Importing this module
imports the siblings (at the bottom, after every shared symbol is
defined), so the decorators run and the apps carry their full verb set.
Existing import sites (``app.py``, ``wave_ci``, ``pr_review``,
``wave_policy``, ``worktree``, tests) keep resolving
``from eawf.cli.commands.lifecycle import phase_app`` /
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

Errors are mapped to canonical exit codes via :mod:`eawf.cli.errors`. The
mapping is conservative: schema/invariant violations exit 4, lock timeouts
exit 5, structural rejections (duplicate id, unknown parent, terminal-status
target) exit 3, and anything that is genuinely a missing scope/state exits 2.
"""

from __future__ import annotations

import logging
import os
import secrets
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.enums import (
    ScopeKind,
)
from eawf.kernel.state.ids import (
    is_iter_id,
)
from eawf.kernel.state.io import (
    StateValidationError,
    append_event,
    build_event_envelope,
    commit_mutation,
    fallback_wal_dir,
    state_version,
    write_state_unlocked,
)
from eawf.kernel.state.urn import build as build_urn
from eawf.runtime.lock import portalock

if TYPE_CHECKING:
    from eawf.kernel.state.models import State
    from eawf.kernel.state.mutations import MutationKind

logger = logging.getLogger(__name__)


# ---- Typer apps -------------------------------------------------------------

project_app = typer.Typer(
    name="project",
    help="Project-level lifecycle (init).",
    no_args_is_help=True,
)
subproject_app = typer.Typer(
    name="subproject",
    help="Subproject lifecycle (add, switch).",
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


# ---- Internal helpers -------------------------------------------------------


def _read_state_payload(path: Path) -> dict[str, Any]:
    """Read and JSON-decode *path*.

    Raises ``cli_errors.UserError`` (``kind="NotFound"``) on miss.
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


# State-write primitives moved to :mod:`eawf.kernel.state.io` (the library) so the
# CLI layer stays thin dispatch per the "CLI is dispatch; library implements"
# rule. Re-exported here under their historical private names so the sibling
# command modules (``lifecycle_iter`` / ``lifecycle_phase`` /
# ``lifecycle_wave_read``) and ``tests/property/test_wave_claim_property``
# keep importing them from this module unchanged.
_write_state_unlocked = write_state_unlocked
_state_version = state_version
_build_event_envelope = build_event_envelope
_append_event = append_event
_fallback_wal_dir = fallback_wal_dir
_commit_mutation = commit_mutation


def _empty_state_dict(*, project_code: str, project_payload: dict[str, Any]) -> dict[str, Any]:
    """Build a minimal-but-valid state.json payload for ``project init``."""
    return {
        "schema_version": "1.0",
        "scope_kind": ScopeKind.REPO.value,
        "urn": build_urn("state", owner=project_code),
        "updated_at": datetime.now(UTC).isoformat(),
        "project": project_payload,
        "current": {
            "project_code": project_code,
            "subproject_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _atomic_write_text(path: Path, content: str) -> None:
    """Write *content* to *path* atomically.

    Uses ``tempfile``-style suffix + :func:`os.replace` so partial writes
    are never visible to a peer reader. The parent directory is created
    if it is missing.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = path.with_name(f"{path.name}.tmp.{suffix}")
    try:
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


# ---- Wave git / commit-ref helpers ------------------------------------------

_GIT_REV_PARSE_TIMEOUT_SECONDS: float = 5.0


def _resolve_repo_root_for_drift(workspace: Path | None) -> Path | None:
    """Return the repo root for the criterion-drift advisory, or ``None``.

    The drift check needs a path on disk to glob against. Two cases:

    1. Canonical layout — ``.ea/state.json`` sits at ``<repo>/.ea/state.json``;
       ``state_path.parent.parent`` is the repo root and contains ``.git``.
    2. ``EA_STATE`` override — state file lives outside any repo (test
       fixture, scratch dir, etc.); ``parent.parent`` is not the repo root.
       Falls back to ``git rev-parse --show-toplevel``; returns ``None`` when
       that also fails (no git context at all).
    """
    try:
        state_path = resolve_state_path(workspace)
    except OSError, ValueError:
        return None
    candidate = state_path.parent.parent
    if (candidate / ".git").exists():
        return candidate
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=_GIT_REV_PARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired, OSError:
        return None
    if out.returncode != 0:
        return None
    top = out.stdout.strip()
    if not top:
        return None
    return Path(top)


def _resolve_commit_sha(ref: str) -> str:
    """Resolve *ref* to a canonical 40-char hex commit SHA via ``git rev-parse``.

    Accepts any ref ``git rev-parse`` understands: full SHA, short SHA,
    branch tip, tag, ``HEAD``-relative ref. The ``^{commit}`` suffix
    forces resolution to a commit object rather than a tag or tree.

    Args:
        ref: User-supplied ref to normalise.

    Returns:
        The 40-char lowercase hex commit SHA.

    Raises:
        cli_errors.UserError: If git is not on ``PATH``, the
            subprocess times out, or the ref does not resolve to a
            commit on any branch (``kind="InvalidInput"``).
    """
    cmd = ["git", "rev-parse", f"{ref}^{{commit}}"]
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_GIT_REV_PARSE_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        logger.debug(f"resolve_commit_sha ref={ref!r} status=timeout")
        raise cli_errors.UserError(
            f"cannot resolve commit ref: {ref!r} (git rev-parse timed out)", kind="InvalidInput"
        ) from exc
    except (FileNotFoundError, OSError) as exc:
        logger.debug(f"resolve_commit_sha ref={ref!r} status=os-error err={exc!s}")
        raise cli_errors.UserError(
            f"cannot resolve commit ref: {ref!r} (git unavailable: {exc!s})", kind="InvalidInput"
        ) from exc
    if out.returncode != 0:
        logger.debug(f"resolve_commit_sha ref={ref!r} status=non-zero rc={out.returncode}")
        raise cli_errors.UserError(f"cannot resolve commit ref: {ref!r}", kind="InvalidInput")
    sha = out.stdout.strip()
    if len(sha) != 40 or not all(c in "0123456789abcdef" for c in sha):
        logger.debug(f"resolve_commit_sha ref={ref!r} status=non-canonical sha={sha!r}")
        raise cli_errors.UserError(
            f"cannot resolve commit ref: {ref!r} (got non-canonical sha: {sha!r})",
            kind="InvalidInput",
        )
    logger.info(f"resolve_commit_sha ref={ref!r} sha={sha}")
    return sha


def _wave_close_via_daemon(
    *,
    flags: GlobalFlags,
    wave_id: str,
    outcome: str,
    resolved_sha: str | None,
) -> bool:
    """Proxy a wave close through the daemon's ``state.mutate`` RPC.

    Returns True on a successful daemon-mediated close (caller exits
    early); False when the daemon refuses the kind (caller falls
    through to the in-process path). A daemon-required failure or
    validation rejection emits the error envelope before returning
    False so the caller does not double-emit.
    """
    from eawf.cli._daemon_client import DaemonClient, DaemonRpcError
    from eawf.cli._mutation import _daemon_reachable
    from eawf.kernel.state.mutations import Mutation, MutationKind

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

    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=wave_id,
        mutation_id=uuid.uuid4().hex,
        idempotency_key=None,
        params={"wave_id": wave_id, "outcome": outcome, "commit": resolved_sha},
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
    except (RuntimeError, OSError, TimeoutError) as exc:
        logger.debug(f"_wave_close_via_daemon transport_error={exc!s}")
        return False

    text = f"wave close {wave_id} outcome={outcome!r} (via daemon)"
    payload = {
        "wave": wave_id,
        "outcome": outcome,
        "commit": resolved_sha,
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
                f"state file not found: {state_path}; run `eawf project init`", kind="NotFound"
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


def _resolve_iter_for_query(
    state: State,
    flags: GlobalFlags,
    *,
    iter_flag: str | None,
) -> str | None:
    """Pick the target iter for a read-only DAG verb.

    Precedence: explicit ``--iter`` > ``state.current.iter_id``. Returns
    ``None`` after emitting the canonical envelope when neither is set
    (the caller treats ``None`` as "exit raised").
    """
    if iter_flag is not None:
        if not is_iter_id(iter_flag):
            cli_errors.emit_error(
                cli_errors.UserError(f"invalid iter id: {iter_flag!r}", kind="InvalidInput"),
                flags=flags,
            )
            return None
        if iter_flag not in state.iters:
            cli_errors.emit_error(
                cli_errors.UserError(f"unknown iter {iter_flag!r}", kind="InvalidInput"),
                flags=flags,
            )
            return None
        return iter_flag
    if state.current.iter_id is not None:
        return state.current.iter_id
    cli_errors.emit_error(
        cli_errors.UserError(
            "no --iter given and state.current.iter_id is unset; specify --iter",
            kind="InvalidInput",
        ),
        flags=flags,
    )
    return None


# ---- Mutation runner --------------------------------------------------------


def _wrap_no_return(_value: object) -> None:
    """Adapter so transition helpers can be passed directly to ``mutate=``."""
    return None


def _run_mutation(
    ctx: typer.Context,
    *,
    command: str,
    args: dict[str, Any],
    mutate: Any,
    scope_id: str | None = None,
    scope_id_factory: Any = None,
    text: str | None = None,
    text_factory: Any = None,
    envelope: Any = None,
    envelope_factory: Any = None,
    closure_kind: bool = False,
    mutation_kind: MutationKind | None = None,
    params: dict[str, Any] | None = None,
) -> None:
    """Shared transactional path for every mutating handler in this module.

    Per rule 4 + D-SUP-01 the daemon is the canonical writer. When
    *mutation_kind* is supplied the call routes through the generic
    :func:`eawf.cli._dispatch._mutate_via_daemon` shim — escalate to the
    daemon, marshal one typed :class:`~eawf.kernel.state.mutations.Mutation`, and
    fall back to the in-process WAL-backed write only when the daemon is
    unavailable or predates the kind (the V1 CI/recovery carve-out). Verbs
    whose transition has no :class:`~eawf.kernel.state.mutations.MutationKind`
    yet (``wave update`` / ``subproject add``·``switch`` / ``iter
    activate`` / ``phase reopen`` / ``wave budget set``·``consume``) omit
    *mutation_kind* and run the in-process WAL-backed path directly.

    Either *text* + *envelope* (static) or *text_factory* + *envelope_factory*
    (deferred until after the mutation has resolved auto-allocated ids) must
    be provided. Likewise either *scope_id* (eager) or *scope_id_factory*
    (deferred — resolved after ``mutate`` runs so handlers can capture the
    allocator-returned id rather than a placeholder).

    Args:
        mutation_kind: When set, the discriminator routed across
            ``state.mutate``; the daemon owns the transaction and the
            in-process body becomes the fallback. Verbs that pass this
            MUST use eager *scope_id* (the fallback resolves the id the
            same way the daemon's params do).
        params: Kind-specific param dict carried in :attr:`Mutation.params`
            on the daemon path. Required when *mutation_kind* is set;
            ignored otherwise.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.state.models import State
    from eawf.workflow.lifecycle.transitions import LifecycleError

    if (scope_id is None) == (scope_id_factory is None):
        raise ValueError("exactly one of scope_id or scope_id_factory must be provided")
    if mutation_kind is not None and scope_id is None:
        raise ValueError("mutation_kind requires an eager scope_id")
    if mutation_kind is not None and params is None:
        raise ValueError("mutation_kind requires params")
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(
                f"state file not found: {state_path}; run `eawf project init`", kind="NotFound"
            ),
            flags=flags,
        )
        return

    def _in_process() -> dict[str, Any]:
        """In-process WAL-backed transaction — the daemon-down fallback."""
        with portalock.acquire(state_path, timeout=5.0):
            payload = _read_state_payload(state_path)
            before_version = state_version(payload)
            try:
                state = State.model_validate(payload)
            except PydValidationError as exc:
                raise cli_errors.StateConflict(
                    f"state at {state_path} fails schema validation: {exc}",
                    kind="IntegrityViolation",
                ) from exc
            try:
                mutate(state)
            except LifecycleError as exc:
                if closure_kind:
                    raise cli_errors.ValidationError(str(exc)) from exc
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            except (PydValidationError, ValueError) as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            state.updated_at = datetime.now(UTC)
            resolved_scope_id = scope_id if scope_id is not None else scope_id_factory()
            # The library writer raises ``StateValidationError`` for a
            # post-apply invariant rejection; map it onto the CLI
            # ``ValidationError`` bucket (exit 2) so the surrounding
            # ``except cli_errors.CliError`` clause below surfaces it.
            try:
                return commit_mutation(
                    state_path,
                    candidate=state,
                    before_version=before_version,
                    command=command,
                    args=args,
                    scope_id=resolved_scope_id,
                    summary=command,
                )
            except StateValidationError as exc:
                raise cli_errors.ValidationError(str(exc)) from exc

    # Route through the daemon only when proxying is enabled in the merged
    # config (the post-P24-W10 default). The V1 carve-out
    # (``daemon.proxy_enabled=false`` or ``EAWF_DAEMONLESS=1``) runs the
    # in-process WAL-backed path directly — mirroring ``wave_close_cmd`` so
    # the ``EAWF_DAEMONLESS`` env hatch routes to the fallback rather than
    # hitting the mutating-verb hard-reject inside ``escalate_mutation``.
    from eawf.cli._mutation import _proxy_enabled

    proxy = mutation_kind is not None and _proxy_enabled(flags.workspace)
    try:
        if proxy:
            assert mutation_kind is not None  # narrowed by ``proxy``
            assert scope_id is not None  # guarded above
            assert params is not None  # guarded above
            from eawf.cli._dispatch import _mutate_via_daemon

            _mutate_via_daemon(
                mutation_kind,
                params,
                flags,
                scope_id=scope_id,
                verb=command,
                fallback=_in_process,
            )
        else:
            _in_process()
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    final_text = text if text is not None else text_factory()
    final_payload = envelope() if envelope is not None else envelope_factory()
    emit_json_or_text(final_payload, final_text, flags=flags)


# ---- Command registration ---------------------------------------------------
# Importing the sibling modules runs their ``@<app>.command(...)`` decorators
# so the apps above carry their full verb set. The imports sit at the bottom,
# after every shared symbol is defined, so the siblings can import the apps and
# helpers from this module without a circular-import failure.
from eawf.cli.commands import lifecycle_iter as _lifecycle_iter  # noqa: E402
from eawf.cli.commands import lifecycle_phase as _lifecycle_phase  # noqa: E402
from eawf.cli.commands import lifecycle_wave as _lifecycle_wave  # noqa: E402, F401
from eawf.cli.commands import lifecycle_wave_read as _lifecycle_wave_read  # noqa: E402, F401

# Re-export sibling-owned helpers so existing import sites keep resolving
# them from this module (``tests/unit/test_iter_bump_hint.py`` imports
# ``_compute_iter_bump_hints``; ``tests/unit/test_lifecycle_phase_prepare_close.py``
# imports ``_phase_prepare_close_checklist``).
_compute_iter_bump_hints = _lifecycle_iter._compute_iter_bump_hints
_phase_prepare_close_checklist = _lifecycle_phase._phase_prepare_close_checklist


# ---- Re-exports -------------------------------------------------------------

__all__ = [
    "iter_app",
    "phase_app",
    "project_app",
    "subproject_app",
    "wave_app",
    "wave_budget_app",
]
