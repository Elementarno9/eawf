"""Iter + project/subproject lifecycle command handlers.

Split out of :mod:`eawf.cli.commands.lifecycle` (P27-W06). The
``iter_app`` / ``project_app`` / ``subproject_app`` Typer apps and the
shared transaction helpers live in the parent module; this module
attaches the iter command bodies plus the ``project init`` /
``subproject add·switch`` setup verbs via ``@<app>.command(...)`` and
owns the iter-bump-hint heuristic. The phase command bodies live in
:mod:`eawf.cli.commands.lifecycle_phase`.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.commands.lifecycle import (
    _append_event,
    _empty_state_dict,
    _run_mutation,
    _state_version,
    _validate_or_raise,
    _write_state_unlocked,
    iter_app,
    project_app,
    subproject_app,
)
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.kernel.state.enums import (
    AuditVerdict,
    IterStatus,
    ProjectStatus,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.ids import is_iter_id, is_phase_id, is_project_code
from eawf.kernel.state.mutations import MutationKind
from eawf.kernel.state.urn import build as build_urn
from eawf.lock import portalock

if TYPE_CHECKING:
    from eawf.kernel.state.models import Iter, State

logger = logging.getLogger(__name__)


def _wrap_no_return(_value: object) -> None:
    """Adapter so transition helpers can be passed directly to ``mutate=``."""
    return None


# ---- Project handlers -------------------------------------------------------


@project_app.command("init")
def project_init_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Project code (uppercase, alnum/dash).")],
    title: Annotated[str, typer.Option("--title", help="Human-readable project title.")],
    domains: Annotated[
        str,
        typer.Option(
            "--domains",
            help="Comma-separated domain tags (e.g. 'quant,research').",
        ),
    ],
    default_branch: Annotated[
        str, typer.Option("--default-branch", help="Default git branch.")
    ] = "main",
) -> None:
    """Create a new project record at the active state path (creates the file)."""
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.state.models import Project
    from eawf.kernel.store.paths import store_path
    from eawf.lifecycle.transitions import LifecycleError

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid project code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return  # for type-checkers; emit_error never returns
    domains_list = [d.strip() for d in domains.split(",") if d.strip()]
    if not domains_list:
        cli_errors.emit_error(
            cli_errors.UserError("--domains must not be empty", kind="InvalidInput"),
            flags=flags,
        )
        return
    # Resolve target state path. We allow non-existent path (project init
    # creates the file). Be careful: scope.resolve_state_path raises if both
    # EA_STATE and -w are unset and pwd-upward fails. project init demands
    # that the operator chose where to put the state.
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"could not resolve state path; pass -w/--workspace or set EA_STATE: {exc}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    try:
        with portalock.acquire(state_path, timeout=5.0):
            if state_path.exists():
                cli_errors.emit_error(
                    cli_errors.UserError(
                        f"state already exists at {state_path}; refusing to overwrite",
                        kind="InvalidInput",
                    ),
                    flags=flags,
                )
                return
            project_payload = Project(
                code=code,
                slug=code.lower(),
                title=title,
                description=None,
                domains=domains_list,
                default_branch=default_branch,
                status=ProjectStatus.ACTIVE,
                repo_urn=build_urn("repo", owner=code),
            ).model_dump(mode="json")
            payload = _empty_state_dict(project_code=code, project_payload=project_payload)
            _validate_or_raise(payload)
            # events-first ordering: see module docstring §6.
            _append_event(
                store_path(state_path, StoreKind.EVENT),
                command="project init",
                args={"code": code, "title": title, "domains": domains_list},
                scope_id=code,
                before_version="",
                after_version=_state_version(payload),
                summary=f"project {code} initialised",
            )
            _write_state_unlocked(state_path, payload)
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return
    except cli_errors.CliError:
        raise
    except (LifecycleError, PydValidationError, ValueError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    emit_json_or_text(
        {"project": code, "title": title, "state_path": str(state_path)},
        f"project init {code} title={title!r} state={state_path}",
        flags=flags,
    )


# ---- Subproject handlers ----------------------------------------------------


@subproject_app.command("add")
def subproject_add_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Subproject code.")],
    kind: Annotated[str, typer.Option("--kind", help="Subproject kind tag.")],
    title: Annotated[str, typer.Option("--title", help="Subproject title.")],
    domains: Annotated[
        str | None,
        typer.Option("--domains", help="Comma-separated domain tags."),
    ] = None,
) -> None:
    """Add a subproject under the active project."""
    from eawf.lifecycle.transitions import add_subproject

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid subproject code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    domains_list = [d.strip() for d in (domains or "").split(",") if d.strip()]
    _run_mutation(
        ctx,
        command="subproject add",
        args={"code": code, "kind": kind, "title": title, "domains": domains_list},
        scope_id=code,
        text=f"subproject add {code} title={title!r}",
        envelope=lambda: {"subproject": code, "title": title, "kind": kind},
        mutate=lambda state: _wrap_no_return(
            add_subproject(
                state,
                code=code,
                kind=kind,
                title=title,
                domains=domains_list,
            )
        ),
    )


@subproject_app.command("switch")
def subproject_switch_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Subproject code to activate.")],
) -> None:
    """Set the active subproject pointer."""
    from eawf.lifecycle.transitions import switch_subproject

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid subproject code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="subproject switch",
        args={"code": code},
        scope_id=code,
        text=f"subproject switch {code}",
        envelope=lambda: {"subproject": code, "current": True},
        mutate=lambda state: switch_subproject(state, code=code),
    )


# ---- Iter handlers ----------------------------------------------------------


@iter_app.command("activate")
def iter_activate_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="PLANNED iter id to activate.")],
) -> None:
    """Flip a PLANNED iter to ACTIVE."""
    from eawf.lifecycle.transitions import activate_iter

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="iter activate",
        args={"id": iter_id},
        scope_id=iter_id,
        text=f"iter activate {iter_id}",
        envelope=lambda: {"iter": iter_id, "status": "active"},
        mutate=lambda state: _wrap_no_return(activate_iter(state, iter_id=iter_id)),
    )


@iter_app.command("plan")
def iter_plan_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Iter id to stage (e.g. P03-I02).")],
    title: Annotated[str | None, typer.Option("--title", help="Iter title.")] = None,
) -> None:
    """Stage a PLANNED iter under an open phase without moving the current pointer.

    Companion of ``iter open`` (which opens an ACTIVE iter and switches
    ``current.iter_id``). Use ``iter plan`` to queue a follow-up iter under an
    already-active phase while the current iter keeps running; ``iter activate``
    later flips the staged iter to ACTIVE.
    """
    from eawf.lifecycle.transitions import plan_iter

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if title is None:
        cli_errors.emit_error(
            cli_errors.UserError("--title required", kind="InvalidInput"), flags=flags
        )
        return
    phase_id = iter_id.split("-", 1)[0]
    _run_mutation(
        ctx,
        command="iter plan",
        args={"iter_id": iter_id, "phase": phase_id, "title": title},
        scope_id=iter_id,
        text=f"iter plan {iter_id} title={title!r}",
        envelope=lambda: {"iter": iter_id, "title": title, "status": "planned"},
        mutate=lambda state: _wrap_no_return(
            plan_iter(state, iter_id=iter_id, phase_id=phase_id, title=title)
        ),
    )


def _has_failed_iter_audit(state: State, closed_iters: list[Iter]) -> bool:
    """Return whether any closed iter carries a non-pass audit verdict."""
    audits = state.audits or {}
    for it in closed_iters:
        if it.audit_id is None:
            continue
        audit = audits.get(it.audit_id)
        if audit is None or audit.verdict is None:
            continue
        if audit.verdict != AuditVerdict.PASS:
            return True
    return False


def _has_wave_with_many_blockers(state: State, phase_wave_ids: list[str]) -> bool:
    """Return whether any active wave has more than 3 unresolved (non-closed) deps."""
    for wid in phase_wave_ids:
        w = state.waves[wid]
        if w.status not in {WaveStatus.PENDING, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
            continue
        unresolved = [
            d
            for d in w.deps
            if state.waves.get(d) is not None and state.waves[d].status != WaveStatus.CLOSED
        ]
        if len(unresolved) > 3:
            return True
    return False


def _compute_iter_bump_hints(state: State, *, phase_id: str) -> list[str]:
    """Heuristic iter-bump trigger detection. Returns a list of hint tags.

    Triggers:
    - ``previous_iter_audit_failed``: any closed iter in *phase_id* whose
      audit verdict is not ``pass``.
    - ``wave_with_many_blockers``: any active wave in the phase has
      more than 3 unresolved deps (deps that are not in ``closed`` status).
    - ``phase_scope_expanded``: phase already has at least one closed
      iter AND its total wave count exceeds 6.
    """
    hints: list[str] = []
    iter_ids = [iid for iid, it in state.iters.items() if it.phase_id == phase_id]
    closed_iters = [
        state.iters[iid] for iid in iter_ids if state.iters[iid].status == IterStatus.CLOSED
    ]
    phase_wave_ids = [wid for wid, w in state.waves.items() if w.iter_id in set(iter_ids)]
    if _has_failed_iter_audit(state, closed_iters):
        hints.append("previous_iter_audit_failed")
    if _has_wave_with_many_blockers(state, phase_wave_ids):
        hints.append("wave_with_many_blockers")
    if len(closed_iters) >= 1 and len(phase_wave_ids) > 6:
        hints.append("phase_scope_expanded")
    return hints


@iter_app.command("open")
def iter_open_cmd(
    ctx: typer.Context,
    target: Annotated[
        str | None,
        typer.Argument(help="Either an explicit iter ID (P03-I02) or a phase ID (P03)."),
    ] = None,
    phase: Annotated[
        str | None,
        typer.Option("--phase", help="Phase ID (when omitting explicit iter id)."),
    ] = None,
    title: Annotated[str | None, typer.Option("--title", help="Iter title.")] = None,
) -> None:
    """Open an iter. Pass an iter ID or a phase id (auto-allocates iter)."""
    from eawf.lifecycle.allocator import allocate_iter_id
    from eawf.lifecycle.transitions import open_iter

    flags: GlobalFlags = ctx.obj
    if title is None:
        cli_errors.emit_error(
            cli_errors.UserError("--title required", kind="InvalidInput"), flags=flags
        )
        return
    explicit_iter: str | None = None
    explicit_phase: str | None = phase
    if target is not None:
        if is_iter_id(target):
            explicit_iter = target
            inferred_phase = target.split("-", 1)[0]
            if explicit_phase is not None and explicit_phase != inferred_phase:
                cli_errors.emit_error(
                    cli_errors.UserError(
                        f"--phase {explicit_phase!r} disagrees with iter {target!r}",
                        kind="InvalidInput",
                    ),
                    flags=flags,
                )
                return
            explicit_phase = inferred_phase
        elif is_phase_id(target):
            if explicit_phase is not None and explicit_phase != target:
                cli_errors.emit_error(
                    cli_errors.UserError("two different phase ids supplied", kind="InvalidInput"),
                    flags=flags,
                )
                return
            explicit_phase = target
        else:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"target must be a phase or iter id: {target!r}", kind="InvalidInput"
                ),
                flags=flags,
            )
            return
    if explicit_phase is None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "either an explicit iter id or --phase is required", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if not is_phase_id(explicit_phase):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {explicit_phase!r}", kind="InvalidInput"),
            flags=flags,
        )
        return

    chosen: dict[str, Any] = {}

    def _mutator(state: State) -> None:
        target_iter = (
            explicit_iter if explicit_iter is not None else allocate_iter_id(state, explicit_phase)
        )
        chosen["id"] = target_iter
        chosen["hints"] = _compute_iter_bump_hints(state, phase_id=explicit_phase)
        open_iter(
            state,
            iter_id=target_iter,
            phase_id=explicit_phase,
            title=title,
        )

    def _text() -> str:
        hint_suffix = f" hints={','.join(chosen['hints'])}" if chosen.get("hints") else ""
        return f"iter open {chosen['id']} title={title!r}{hint_suffix}"

    def _envelope() -> dict[str, Any]:
        env: dict[str, Any] = {"iter": chosen["id"], "title": title}
        if chosen.get("hints"):
            env["hints"] = list(chosen["hints"])
        return env

    _run_mutation(
        ctx,
        command="iter open",
        args={
            "iter_id": explicit_iter,
            "phase": explicit_phase,
            "title": title,
        },
        scope_id_factory=lambda: chosen["id"],
        text_factory=_text,
        envelope_factory=_envelope,
        mutate=_mutator,
    )


@iter_app.command("close")
def iter_close_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Iter ID to close.")],
    audit: Annotated[str, typer.Option("--audit", help="Audit ID providing closure evidence.")],
) -> None:
    """Close an active iter. Rejects when child waves are still open."""
    from eawf.lifecycle.transitions import close_iter

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="iter close",
        args={"id": iter_id, "audit": audit},
        scope_id=iter_id,
        text=f"iter close {iter_id} audit={audit}",
        envelope=lambda: {"iter": iter_id, "audit": audit},
        mutate=lambda state: _wrap_no_return(close_iter(state, iter_id=iter_id, audit_id=audit)),
        closure_kind=True,
        mutation_kind=MutationKind.ITER_CLOSE,
        params={"iter_id": iter_id, "audit_id": audit},
    )
