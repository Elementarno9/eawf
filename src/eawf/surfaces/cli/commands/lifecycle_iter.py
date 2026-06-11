"""Iter + project/track lifecycle command handlers.

Split out of :mod:`eawf.surfaces.cli.commands.lifecycle` (P27-W06). The
``iter_app`` / ``project_app`` / ``track_app`` Typer apps and the
shared transaction helpers live in the parent module; this module
attaches the iter command bodies plus the ``project init`` /
``track add·switch`` setup verbs via ``@<app>.command(...)`` and
owns the iter-bump-hint heuristic. The phase command bodies live in
:mod:`eawf.surfaces.cli.commands.lifecycle_phase`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.enums import (
    AuditVerdict,
    IterStatus,
    ProjectStatus,
    StoreKind,
    TrackKind,
    WaveStatus,
)
from eawf.kernel.state.ids import is_iter_id, is_phase_id, is_project_code
from eawf.kernel.state.mutations import MutationKind
from eawf.kernel.state.urn import build as build_urn
from eawf.runtime.lock import portalock
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import (
    _append_event,
    _empty_state_dict,
    _run_mutation,
    _state_version,
    _validate_or_raise,
    _write_state_unlocked,
    iter_app,
    project_app,
    track_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

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
    upgrade: Annotated[
        bool,
        typer.Option(
            "--upgrade",
            help="Fill a missing project record in an existing init-created state.",
        ),
    ] = False,
) -> None:
    """Create or upgrade a project record at the active state path."""
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.state.models import Project
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.lifecycle.transitions import LifecycleError

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
            if state_path.exists():
                if not upgrade:
                    cli_errors.emit_error(
                        cli_errors.UserError(
                            f"state already exists at {state_path}; refusing to overwrite",
                            kind="InvalidInput",
                        ),
                        flags=flags,
                    )
                    return
                import json

                payload = json.loads(state_path.read_text(encoding="utf-8"))
                if payload.get("project") is not None:
                    cli_errors.emit_error(
                        cli_errors.UserError(
                            f"state already has project record at {state_path}",
                            kind="InvalidInput",
                        ),
                        flags=flags,
                    )
                    return
                payload["project"] = project_payload
                payload.setdefault("current", {})["project_code"] = code
                payload.setdefault("indexes", {})["project_title"] = title
                _validate_or_raise(payload)
                _append_event(
                    store_path(state_path, StoreKind.EVENT),
                    command="project init --upgrade",
                    args={
                        "code": code,
                        "title": title,
                        "domains": domains_list,
                        "default_branch": default_branch,
                    },
                    scope_id=code,
                    before_version="",
                    after_version=_state_version(payload),
                    summary=f"project {code} upgraded",
                )
                _write_state_unlocked(state_path, payload)
                emit_json_or_text(
                    {"project": code, "title": title, "state_path": str(state_path)},
                    f"project init --upgrade {code} title={title!r} state={state_path}",
                    flags=flags,
                )
                return
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


# ---- Track handlers ---------------------------------------------------------


@track_app.command("add")
def track_add_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Track code.")],
    kind: Annotated[str, typer.Option("--kind", help="Track kind tag.")],
    title: Annotated[str, typer.Option("--title", help="Track title.")],
    domains: Annotated[
        str | None,
        typer.Option("--domains", help="Comma-separated domain tags."),
    ] = None,
) -> None:
    """Add a track under the active project."""
    from eawf.workflow.lifecycle.transitions import add_track

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid track code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        track_kind = TrackKind(kind)
    except ValueError:
        allowed = ", ".join(k.value for k in TrackKind)
        cli_errors.emit_error(
            cli_errors.UserError(
                f"unknown track kind: {kind!r} (allowed: {allowed})", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    domains_list = [d.strip() for d in (domains or "").split(",") if d.strip()]
    _run_mutation(
        ctx,
        command="track add",
        args={"code": code, "kind": track_kind.value, "title": title, "domains": domains_list},
        scope_id=code,
        text=f"track add {code} title={title!r}",
        envelope=lambda: {"track": code, "title": title, "kind": track_kind.value},
        mutate=lambda state: _wrap_no_return(
            add_track(
                state,
                code=code,
                kind=track_kind,
                title=title,
                domains=domains_list,
            )
        ),
    )


@track_app.command("switch")
def track_switch_cmd(
    ctx: typer.Context,
    code: Annotated[str, typer.Argument(help="Track code to activate.")],
) -> None:
    """Set the active track pointer."""
    from eawf.workflow.lifecycle.transitions import switch_track

    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid track code: {code!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="track switch",
        args={"code": code},
        scope_id=code,
        text=f"track switch {code}",
        envelope=lambda: {"track": code, "current": True},
        mutate=lambda state: switch_track(state, code=code),
    )


# ---- Iter handlers ----------------------------------------------------------


@iter_app.command("activate")
def iter_activate_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="PLANNED iter id to activate.")],
) -> None:
    """Flip a PLANNED iter to ACTIVE."""
    from eawf.workflow.lifecycle.transitions import activate_iter

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
    description: Annotated[
        str | None,
        typer.Option("--description", help="Optional long-form iter description (≤500 chars)."),
    ] = None,
) -> None:
    """Stage a PLANNED iter under an open phase without moving the current pointer.

    Companion of ``iter open`` (which opens an ACTIVE iter and switches
    ``current.iter_id``). Use ``iter plan`` to queue a follow-up iter under an
    already-active phase while the current iter keeps running; ``iter activate``
    later flips the staged iter to ACTIVE.
    """
    from eawf.workflow.lifecycle.transitions import plan_iter

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
        args={
            "iter_id": iter_id,
            "phase": phase_id,
            "title": title,
            "description": description,
        },
        scope_id=iter_id,
        text=f"iter plan {iter_id} title={title!r}",
        envelope=lambda: {
            "iter": iter_id,
            "title": title,
            "description": description,
            "status": "planned",
        },
        mutate=lambda state: _wrap_no_return(
            plan_iter(
                state,
                iter_id=iter_id,
                phase_id=phase_id,
                title=title,
                description=description,
            )
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
    """Drift-budget pulse signals for the iter cadence. Returns hint tags.

    These hints are read as a budget pulse, not a damage report. The
    optimistic drift cadence (see
    :class:`~eawf.platform.profiles.models.CheckpointBlock`) lets
    independent waves keep flowing and reconciles accumulated drift at
    the next checkpoint; each tag below marks a place where the drift
    budget has been spent, so opening a fresh iter is the natural pulse
    that draws a line and resets the window. Under a ``barrier``
    checkpoint mode the same signals fire at a hard stop instead.

    Pulse signals:
    - ``previous_iter_audit_failed``: a closed iter in *phase_id* carries
      an audit verdict other than ``pass`` -- drift the next iter
      reconciles.
    - ``wave_with_many_blockers``: an active wave in the phase has more
      than 3 unresolved deps (deps not in ``closed`` status), so the
      dep frontier has drifted past the per-wave budget.
    - ``phase_scope_expanded``: the phase already has at least one closed
      iter AND its total wave count exceeds 6 -- scope has grown past the
      drift-budget waves window.
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
    description: Annotated[
        str | None,
        typer.Option("--description", help="Optional long-form iter description (≤500 chars)."),
    ] = None,
) -> None:
    """Open an iter. Pass an iter ID or a phase id (auto-allocates iter)."""
    from eawf.workflow.lifecycle.allocator import allocate_iter_id
    from eawf.workflow.lifecycle.transitions import open_iter

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
            description=description,
        )

    def _text() -> str:
        hint_suffix = f" hints={','.join(chosen['hints'])}" if chosen.get("hints") else ""
        return f"iter open {chosen['id']} title={title!r}{hint_suffix}"

    def _envelope() -> dict[str, Any]:
        env: dict[str, Any] = {"iter": chosen["id"], "title": title, "description": description}
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
            "description": description,
        },
        scope_id_factory=lambda: chosen["id"],
        text_factory=_text,
        envelope_factory=_envelope,
        mutate=_mutator,
    )


def _archive_iter_specs_after_close(
    ctx: typer.Context,
    *,
    iter_id: str,
    flags: GlobalFlags,
) -> None:
    """Force-archive every wave spec under *iter_id* after a successful close.

    Reads the freshly-closed state read-only, resolves the iter's wave scope
    ids + the active project code, and routes the batch through the W08
    force-archive path (:func:`~eawf.workflow.lifecycle.spec_archive.archive_specs_for_scopes`).
    A wave with no spec cache row (or an already-archived row) is skipped, so
    the cascade is idempotent. Archive failures surface the canonical error
    envelope without un-doing the close (the iter is already closed).
    """
    from eawf.surfaces.cli.commands.lifecycle import _load_state_readonly
    from eawf.workflow.lifecycle.spec_archive import archive_specs_for_scopes

    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, _ = loaded
    it = state.iters.get(iter_id)
    if it is None or not it.wave_ids:
        return
    repo_code = state.current.project_code
    if repo_code is None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "cannot archive specs: state.current.project_code is unset", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    repo_root = (flags.workspace or Path.cwd()).resolve()
    try:
        archived = archive_specs_for_scopes(
            list(it.wave_ids),
            repo_code=repo_code,
            repo_root=repo_root,
        )
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    logger.info(f"archive_iter_specs iter={iter_id} archived={len(archived)}")


@iter_app.command("close")
def iter_close_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Iter ID to close.")],
    audit: Annotated[str, typer.Option("--audit", help="Audit ID providing closure evidence.")],
    archive_specs: Annotated[
        bool,
        typer.Option(
            "--archive-specs",
            help="After close, git-remove + ARCHIVE every wave spec under the iter.",
        ),
    ] = False,
) -> None:
    """Close an active iter. Rejects when child waves are still open.

    With ``--archive-specs`` the close runs a post-close cascade: every wave
    spec under the iter is git-removed, its cache row flipped to ``ARCHIVED``,
    and its blob SHA recorded so ``eawf spec show <urn> --from-git`` recovers
    the body. Without the flag the specs stay untouched.
    """
    from eawf.workflow.lifecycle.transitions import close_iter

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
    if archive_specs:
        _archive_iter_specs_after_close(ctx, iter_id=iter_id, flags=flags)


@iter_app.command("candidate-tag")
def iter_candidate_tag_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Iter id (e.g. P03-I02).")],
    set_tag: Annotated[
        str | None,
        typer.Option(
            "--set",
            help="Proposed release tag to persist (vMAJOR.MINOR.PATCH, e.g. v0.5.0).",
        ),
    ] = None,
) -> None:
    """Show or set an iter's proposed release tag.

    Without ``--set`` the command reads the iter's current
    ``candidate_tag`` (printing a "none set" line when unset). With
    ``--set <vX.Y.Z>`` it persists the tag through the daemon-backed
    mutation path, the same surface the other iter mutations ride.
    """
    from eawf.surfaces.cli.commands.lifecycle import _load_state_readonly
    from eawf.workflow.lifecycle.transitions import set_iter_candidate_tag

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return

    if set_tag is None:
        loaded = _load_state_readonly(ctx)
        if loaded is None:
            return
        state, _ = loaded
        it = state.iters.get(iter_id)
        if it is None:
            cli_errors.emit_error(
                cli_errors.UserError(f"unknown iter: {iter_id!r}", kind="NotFound"),
                flags=flags,
            )
            return
        tag = it.candidate_tag
        text = (
            f"iter candidate-tag {iter_id} {tag}"
            if tag is not None
            else f"iter candidate-tag {iter_id} none set"
        )
        emit_json_or_text(
            {"iter": iter_id, "candidate_tag": tag},
            text,
            flags=flags,
        )
        return

    _run_mutation(
        ctx,
        command="iter candidate-tag",
        args={"id": iter_id, "tag": set_tag},
        scope_id=iter_id,
        text=f"iter candidate-tag {iter_id} {set_tag}",
        envelope=lambda: {"iter": iter_id, "candidate_tag": set_tag},
        mutate=lambda state: _wrap_no_return(
            set_iter_candidate_tag(state, iter_id=iter_id, tag=set_tag)
        ),
    )
