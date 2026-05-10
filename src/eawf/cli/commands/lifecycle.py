"""Typer handlers for the lifecycle nouns: project/subproject/phase/iter/wave.

Each handler follows the canonical mutation pattern:

1. Resolve the active ``state.json`` path via :func:`scope.resolve_state_path`.
2. Acquire the sibling lockfile via :func:`portalock.acquire`. The lock is held
   for the entire transaction so concurrent claimers see exactly-once
   semantics.
3. Load + parse + Pydantic-validate the current state.
4. Apply the transition / allocator from :mod:`eawf.lifecycle`.
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

import hashlib
import logging
import os
import secrets
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer
from pydantic import ValidationError as PydValidationError

from eawf.budget.policy import BLOCK_TAG, WARN_TAG
from eawf.budget.service import (
    check_budget as budget_check,
)
from eawf.budget.service import (
    record_consumption as budget_record,
)
from eawf.budget.service import (
    set_budget as budget_set,
)
from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.dispatch import render_wave_prompt
from eawf.lifecycle.allocator import (
    allocate_iter_id,
    allocate_phase_id,
)
from eawf.lifecycle.transitions import (
    LifecycleError,
    add_subproject,
    claim_wave,
    close_iter,
    close_phase,
    close_wave,
    fail_wave,
    open_iter,
    open_phase,
    plan_wave,
    switch_subproject,
)
from eawf.lock import portalock
from eawf.state.enums import ProjectStatus, ScopeKind, StoreKind, WaveStatus
from eawf.state.ids import (
    is_iter_id,
    is_phase_id,
    is_project_code,
    is_wave_id,
)
from eawf.state.models import Project, State
from eawf.state.urn import build as build_urn
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.event import EventPayload
from eawf.store.paths import store_path
from eawf.validate.strict import validate_state as validate_state_payload

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
    help="Phase lifecycle (open, close).",
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
    """Read and JSON-decode *path*. Raises ``cli_errors.NotFound`` on miss."""
    if not path.exists():
        raise cli_errors.NotFound(f"state file not found: {path}")
    raw = path.read_bytes()
    try:
        return orjson.loads(raw)  # type: ignore[no-any-return]
    except orjson.JSONDecodeError as exc:
        raise cli_errors.IntegrityViolation(f"corrupted state at {path}: {exc}") from exc


def _write_state_unlocked(path: Path, data: dict[str, Any]) -> None:
    """Write *data* to *path* atomically WITHOUT acquiring the sibling lock.

    Caller must already hold the lock via :func:`portalock.acquire`. The
    locked variant lives in :mod:`eawf.state.writer`; the unlocked variant is
    needed here because the transaction-level lock is held for the entire
    handler.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    suffix = secrets.token_hex(4)
    tmp = path.with_name(f"{path.name}.tmp.{suffix}")
    payload = orjson.dumps(dict(data), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
    try:
        with tmp.open("wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        parent_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        tmp.unlink(missing_ok=True)


def _state_version(payload: dict[str, Any]) -> str:
    """Stable hash of a state payload — used as before/after_state_version."""
    raw = orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)
    return hashlib.sha256(raw).hexdigest()[:16]


def _append_event(
    events_path: Path,
    *,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    before_version: str,
    after_version: str,
    summary: str,
) -> None:
    """Append one ``EVENT``-kind envelope to *events_path*.

    Builds the envelope from command/args/scope_id and routes the write
    through :func:`eawf.store.append.append_envelope`. Caller already
    holds the state-side sibling lock; the events store uses its own
    sibling lock so concurrent appends from unrelated callers stay safe.
    """
    args_blob = orjson.dumps(args, option=orjson.OPT_SORT_KEYS)
    args_hash = hashlib.sha256(args_blob).hexdigest()[:16]
    now = datetime.now(UTC)
    envelope = Envelope(
        schema_version="1.0",
        id=f"EV-{uuid.uuid4().hex[:12]}",
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload=EventPayload(
            timestamp=now,
            event_type=command,
            actor="cli",
            command=command,
            args_hash=args_hash,
            before_state_version=before_version,
            after_state_version=after_version,
            status="ok",
            message=summary,
        ).model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(events_path, envelope)


def _validate_or_raise(payload: dict[str, Any]) -> State:
    """Validate the candidate payload; raise ``ValidationFailed`` on error."""
    report = validate_state_payload(payload, strict_optional=False)
    if not report.ok:
        msgs = list(report.schema_errors)
        msgs.extend(f"{v.code}@{v.path}: {v.message}" for v in report.violations)
        raise cli_errors.ValidationFailed("; ".join(msgs))
    assert report.state is not None  # ok==True guarantees this
    return report.state


def _commit_mutation(
    state_path: Path,
    *,
    candidate: State,
    before_version: str,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    summary: str,
) -> dict[str, Any]:
    """Validate + emit event + persist under the held sibling lock.

    Order: validate -> append event.jsonl -> write state.json. The
    canonical jsonl-first ordering (see commit ``18ee287``) ensures the
    audit record always lands before the state mutation. If the append
    fails, ``state.json`` is unchanged; if the state write fails after a
    successful append, the surplus event is forward-replayable.

    Returns the candidate payload (already JSON-mode-dumped) so the caller
    can compute its own envelope without a second model_dump.
    """
    payload = candidate.model_dump(mode="json")
    # validate the payload that will actually go to disk
    _validate_or_raise(payload)
    after_version = _state_version(payload)
    events_path = store_path(state_path, StoreKind.EVENT)
    _append_event(
        events_path,
        command=command,
        args=args,
        scope_id=scope_id,
        before_version=before_version,
        after_version=after_version,
        summary=summary,
    )
    _write_state_unlocked(state_path, payload)
    return payload


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


# ---- Project handlers -------------------------------------------------------


@project_app.command("init")
def project_init(
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
    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid project code: {code!r}"),
            flags=flags,
        )
        return  # for type-checkers; emit_error never returns
    domains_list = [d.strip() for d in domains.split(",") if d.strip()]
    if not domains_list:
        cli_errors.emit_error(
            cli_errors.InvalidInput("--domains must not be empty"),
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
            cli_errors.InvalidInput(
                f"could not resolve state path; pass -w/--workspace or set EA_STATE: {exc}"
            ),
            flags=flags,
        )
        return
    try:
        with portalock.acquire(state_path, timeout=5.0):
            if state_path.exists():
                cli_errors.emit_error(
                    cli_errors.InvalidInput(
                        f"state already exists at {state_path}; refusing to overwrite"
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
        cli_errors.emit_error(cli_errors.LockConflict(str(exc)), flags=flags)
        return
    except cli_errors.CliError:
        raise
    except (LifecycleError, PydValidationError, ValueError) as exc:
        cli_errors.emit_error(cli_errors.InvalidInput(str(exc)), flags=flags)
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
    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid subproject code: {code!r}"),
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
    flags: GlobalFlags = ctx.obj
    if not is_project_code(code):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid subproject code: {code!r}"),
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


# ---- Phase handlers ---------------------------------------------------------


@phase_app.command("open")
def phase_open_cmd(
    ctx: typer.Context,
    phase_id: Annotated[
        str | None,
        typer.Argument(help="Explicit phase ID like P03 (omit with --auto)."),
    ] = None,
    auto: Annotated[
        bool, typer.Option("--auto", help="Auto-allocate the next free P<NN>.")
    ] = False,
    title: Annotated[str | None, typer.Option("--title", help="Phase title.")] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Phase scope_id (defaults to project code)."),
    ] = None,
) -> None:
    """Open a new phase. Provide an explicit ID or use ``--auto``."""
    flags: GlobalFlags = ctx.obj
    if title is None:
        cli_errors.emit_error(cli_errors.InvalidInput("--title required"), flags=flags)
        return
    if (phase_id is None) == (not auto):
        cli_errors.emit_error(
            cli_errors.InvalidInput("exactly one of <id> or --auto must be provided"),
            flags=flags,
        )
        return
    if phase_id is not None and not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return

    chosen: dict[str, str] = {}

    def _mutator(state: State) -> None:
        target = allocate_phase_id(state) if auto else phase_id
        assert target is not None  # validated above
        chosen["id"] = target
        open_phase(state, phase_id=target, title=title, scope_id=scope)

    _run_mutation(
        ctx,
        command="phase open",
        args={"id": phase_id, "auto": auto, "title": title, "scope": scope},
        scope_id_factory=lambda: chosen["id"],
        text_factory=lambda: f"phase open {chosen['id']} title={title!r}",
        envelope_factory=lambda: {"phase": chosen["id"], "title": title},
        mutate=_mutator,
    )


@phase_app.command("close")
def phase_close_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase ID to close.")],
    audit: Annotated[str, typer.Option("--audit", help="Audit ID providing closure evidence.")],
    checkpoint: Annotated[
        str | None,
        typer.Option("--checkpoint", help="Optional commit SHA marking the close."),
    ] = None,
) -> None:
    """Close an active phase. Rejects when child iters are still open."""
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="phase close",
        args={"id": phase_id, "audit": audit, "checkpoint": checkpoint},
        scope_id=phase_id,
        text=f"phase close {phase_id} audit={audit}",
        envelope=lambda: {
            "phase": phase_id,
            "audit": audit,
            "checkpoint": checkpoint,
        },
        mutate=lambda state: _wrap_no_return(
            close_phase(
                state,
                phase_id=phase_id,
                audit_id=audit,
                checkpoint=checkpoint,
            )
        ),
        closure_kind=True,
    )


# ---- Iter handlers ----------------------------------------------------------


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
    flags: GlobalFlags = ctx.obj
    if title is None:
        cli_errors.emit_error(cli_errors.InvalidInput("--title required"), flags=flags)
        return
    explicit_iter: str | None = None
    explicit_phase: str | None = phase
    if target is not None:
        if is_iter_id(target):
            explicit_iter = target
            inferred_phase = target.split("-", 1)[0]
            if explicit_phase is not None and explicit_phase != inferred_phase:
                cli_errors.emit_error(
                    cli_errors.InvalidInput(
                        f"--phase {explicit_phase!r} disagrees with iter {target!r}"
                    ),
                    flags=flags,
                )
                return
            explicit_phase = inferred_phase
        elif is_phase_id(target):
            if explicit_phase is not None and explicit_phase != target:
                cli_errors.emit_error(
                    cli_errors.InvalidInput("two different phase ids supplied"),
                    flags=flags,
                )
                return
            explicit_phase = target
        else:
            cli_errors.emit_error(
                cli_errors.InvalidInput(f"target must be a phase or iter id: {target!r}"),
                flags=flags,
            )
            return
    if explicit_phase is None:
        cli_errors.emit_error(
            cli_errors.InvalidInput("either an explicit iter id or --phase is required"),
            flags=flags,
        )
        return
    if not is_phase_id(explicit_phase):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {explicit_phase!r}"),
            flags=flags,
        )
        return

    chosen: dict[str, str] = {}

    def _mutator(state: State) -> None:
        target_iter = (
            explicit_iter if explicit_iter is not None else allocate_iter_id(state, explicit_phase)
        )
        chosen["id"] = target_iter
        open_iter(
            state,
            iter_id=target_iter,
            phase_id=explicit_phase,
            title=title,
        )

    _run_mutation(
        ctx,
        command="iter open",
        args={
            "iter_id": explicit_iter,
            "phase": explicit_phase,
            "title": title,
        },
        scope_id_factory=lambda: chosen["id"],
        text_factory=lambda: f"iter open {chosen['id']} title={title!r}",
        envelope_factory=lambda: {"iter": chosen["id"], "title": title},
        mutate=_mutator,
    )


@iter_app.command("close")
def iter_close_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Iter ID to close.")],
    audit: Annotated[str, typer.Option("--audit", help="Audit ID providing closure evidence.")],
) -> None:
    """Close an active iter. Rejects when child waves are still open."""
    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid iter id: {iter_id!r}"),
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
    )


# ---- Wave handlers ----------------------------------------------------------


@wave_app.command("plan")
def wave_plan_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Parent iter ID.")],
    wave_id: Annotated[str, typer.Option("--id", help="Explicit wave ID.")],
    title: Annotated[str, typer.Option("--title", help="Wave title.")],
    files: Annotated[
        str,
        typer.Option(
            "--files",
            help="Comma-separated file globs that the wave covers.",
        ),
    ],
    deps: Annotated[
        str | None,
        typer.Option("--deps", help="Comma-separated dep wave IDs (must already exist)."),
    ] = None,
) -> None:
    """Plan a new pending wave under an open iter."""
    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid iter id: {iter_id!r}"),
            flags=flags,
        )
        return
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if not wave_id.startswith(f"{iter_id}-W"):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"wave id {wave_id!r} does not belong to iter {iter_id!r}"),
            flags=flags,
        )
        return
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    deps_list = [d.strip() for d in (deps or "").split(",") if d.strip()]
    _run_mutation(
        ctx,
        command="wave plan",
        args={
            "iter_id": iter_id,
            "id": wave_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
        },
        scope_id=wave_id,
        text=f"wave plan {wave_id} iter={iter_id} title={title!r}",
        envelope=lambda: {
            "wave": wave_id,
            "iter": iter_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
        },
        mutate=lambda state: _wrap_no_return(
            plan_wave(
                state,
                wave_id=wave_id,
                iter_id=iter_id,
                title=title,
                file_scopes=file_list,
                deps=deps_list,
            )
        ),
    )


@wave_app.command("claim")
def wave_claim_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to claim.")],
    session: Annotated[str, typer.Option("--session", help="Claiming agent/operator session id.")],
    worktree_policy: Annotated[
        str,
        typer.Option(
            "--worktree-policy",
            help="One of current_branch|fresh_branch|inline.",
        ),
    ] = "current_branch",
) -> None:
    """Claim a pending wave for *session*. Exactly-once across concurrent calls."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if worktree_policy not in {"current_branch", "fresh_branch", "inline"}:
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"--worktree-policy must be current_branch|fresh_branch|inline; "
                f"got {worktree_policy!r}"
            ),
            flags=flags,
        )
        return

    def _claim_with_budget_gate(state: State) -> None:
        wave = state.waves.get(wave_id)
        if (
            wave is not None
            and wave.token_budget is not None
            and wave.tokens_consumed >= wave.token_budget
        ):
            raise cli_errors.ValidationFailed(
                f"wave {wave_id!r} is over token budget "
                f"({wave.tokens_consumed}/{wave.token_budget}); raise budget or split work"
            )
        claim_wave(state, wave_id=wave_id, session_id=session)

    _run_mutation(
        ctx,
        command="wave claim",
        args={
            "id": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
        },
        scope_id=wave_id,
        text=f"wave claim {wave_id} session={session}",
        envelope=lambda: {
            "wave": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
        },
        mutate=_claim_with_budget_gate,
    )


@wave_app.command("close")
def wave_close_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to close.")],
    commit: Annotated[
        str | None, typer.Option("--commit", help="Commit SHA evidence (required).")
    ] = None,
    outcome: Annotated[
        str | None, typer.Option("--outcome", help="Outcome description (required).")
    ] = None,
) -> None:
    """Close a claimed/in-progress wave. Both --commit and --outcome are required."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if commit is None or commit == "":
        cli_errors.emit_error(
            cli_errors.InvalidInput("--commit is required for wave close"),
            flags=flags,
        )
        return
    if outcome is None or outcome == "":
        cli_errors.emit_error(
            cli_errors.InvalidInput("--outcome is required for wave close"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="wave close",
        args={"id": wave_id, "commit": commit, "outcome": outcome},
        scope_id=wave_id,
        text=f"wave close {wave_id} commit={commit}",
        envelope=lambda: {
            "wave": wave_id,
            "commit": commit,
            "outcome": outcome,
        },
        mutate=lambda state: _wrap_no_return(
            close_wave(
                state,
                wave_id=wave_id,
                commit=commit,
                outcome=outcome,
            )
        ),
    )


@wave_app.command("fail")
def wave_fail_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to fail.")],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Failure reason (required).")
    ] = None,
) -> None:
    """Mark a claimed/in-progress wave as failed with *reason*."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if reason is None or reason == "":
        cli_errors.emit_error(
            cli_errors.InvalidInput("--reason is required for wave fail"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="wave fail",
        args={"id": wave_id, "reason": reason},
        scope_id=wave_id,
        text=f"wave fail {wave_id} reason={reason!r}",
        envelope=lambda: {"wave": wave_id, "reason": reason},
        mutate=lambda state: _wrap_no_return(fail_wave(state, wave_id=wave_id, reason=reason)),
    )


# ---- Wave DAG read-only verbs (B026) ---------------------------------------


_WAVE_STATUS_EMOJI: dict[WaveStatus, str] = {
    WaveStatus.PENDING: "⏳",  # hourglass
    WaveStatus.CLAIMED: "🚧",  # construction
    WaveStatus.IN_PROGRESS: "🚧",
    WaveStatus.CLOSED: "✅",  # white heavy check mark
    WaveStatus.FAILED: "❌",  # cross mark
    WaveStatus.ABANDONED: "❌",
}


def _load_state_readonly(ctx: typer.Context) -> tuple[State, GlobalFlags] | None:
    """Resolve + read + parse state.json under no lock.

    Read-only verbs ride the same scope-resolution path mutators use, but
    do not need the sibling lock — a stale snapshot is acceptable for
    enumeration. Returns ``None`` after emitting the canonical error
    envelope when resolution / parse fails (caller treats ``None`` as
    "exit was already raised by ``emit_error``").
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return None
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.NotFound(f"state file not found: {state_path}; run `eawf project init`"),
            flags=flags,
        )
        return None
    payload = _read_state_payload(state_path)
    try:
        state = State.model_validate(payload)
    except PydValidationError as exc:
        cli_errors.emit_error(
            cli_errors.IntegrityViolation(f"state at {state_path} fails schema validation: {exc}"),
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
                cli_errors.InvalidInput(f"invalid iter id: {iter_flag!r}"),
                flags=flags,
            )
            return None
        if iter_flag not in state.iters:
            cli_errors.emit_error(
                cli_errors.InvalidInput(f"unknown iter {iter_flag!r}"),
                flags=flags,
            )
            return None
        return iter_flag
    if state.current.iter_id is not None:
        return state.current.iter_id
    cli_errors.emit_error(
        cli_errors.InvalidInput(
            "no --iter given and state.current.iter_id is unset; specify --iter"
        ),
        flags=flags,
    )
    return None


def _topo_order_with_depth(waves: list[tuple[str, list[str]]]) -> list[tuple[str, int]]:
    """Topo-sort *waves* and assign each node its longest-path depth.

    Each entry is ``(wave_id, deps_in_iter)`` — deps that point outside
    *waves* are ignored. The output preserves topological order; nodes
    at the same depth are emitted in ascending id order.
    """
    ids = [wid for wid, _ in waves]
    id_set = set(ids)
    deps_in: dict[str, list[str]] = {wid: [d for d in deps if d in id_set] for wid, deps in waves}
    children: dict[str, list[str]] = {wid: [] for wid in ids}
    for wid, deps in deps_in.items():
        for d in deps:
            children[d].append(wid)
    in_degree = {wid: len(deps_in[wid]) for wid in ids}
    depth: dict[str, int] = dict.fromkeys(ids, 0)
    # Kahn with deterministic id-sorted ready queue.
    ready = sorted([wid for wid, deg in in_degree.items() if deg == 0])
    order: list[tuple[str, int]] = []
    while ready:
        # Pop deterministically: smallest id at the current frontier.
        node = ready.pop(0)
        order.append((node, depth[node]))
        for child in sorted(children[node]):
            in_degree[child] -= 1
            if in_degree[child] == 0:
                depth[child] = max(depth[child], depth[node] + 1)
                # Insert in sort order so the next pop stays deterministic.
                _insort(ready, child)
            else:
                depth[child] = max(depth[child], depth[node] + 1)
    # Any nodes left unprocessed (cycles) get appended at the end in id order
    # — defensive: ``plan_wave`` rejects cycles, so this branch is unreachable
    # for state.json produced by the state CLI alone.
    remaining = sorted(wid for wid in ids if wid not in {n for n, _ in order})
    for wid in remaining:
        order.append((wid, depth[wid]))
    return order


def _insort(target: list[str], item: str) -> None:
    """In-place sorted insert (avoids importing bisect for one call)."""
    lo, hi = 0, len(target)
    while lo < hi:
        mid = (lo + hi) // 2
        if target[mid] < item:
            lo = mid + 1
        else:
            hi = mid
    target.insert(lo, item)


@wave_app.command("graph")
def wave_graph_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to graph (defaults to current iter)."),
    ] = None,
) -> None:
    """Print the wave DAG for an iter in topological order.

    Each row is ``<emoji> <wave-id> <title-truncated-60> blocks=[...]
    blocked_by=[...]`` indented two spaces per topo-depth level. Sort
    order: topo first, then ascending wave id at each frontier.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    rows = [(wid, w) for wid, w in state.waves.items() if w.iter_id == target_iter]
    rows.sort(key=lambda kv: kv[0])
    deps_pairs = [(wid, list(w.deps)) for wid, w in rows]
    order = _topo_order_with_depth(deps_pairs)
    wave_by_id = dict(rows)
    json_rows: list[dict[str, Any]] = []
    text_lines: list[str] = []
    for wid, depth in order:
        w = wave_by_id[wid]
        emoji = _WAVE_STATUS_EMOJI.get(w.status, "?")
        title = w.title if len(w.title) <= 60 else w.title[:57] + "..."
        indent = "  " * depth
        text_lines.append(
            f"{indent}{emoji} {wid} {title} blocks={list(w.blocks)} blocked_by={list(w.deps)}"
        )
        json_rows.append(
            {
                "id": wid,
                "status": w.status.value,
                "title": w.title,
                "depth": depth,
                "blocks": list(w.blocks),
                "blocked_by": list(w.deps),
            }
        )
    payload: dict[str, Any] = {"iter": target_iter, "waves": json_rows}
    text = "\n".join(text_lines) if text_lines else f"iter {target_iter}: no waves"
    emit_json_or_text(payload, text, flags=flags)


@wave_app.command("next-ready")
def wave_next_ready_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to inspect (defaults to current iter)."),
    ] = None,
) -> None:
    """List pending waves whose every dep is ``closed``.

    Failed deps do NOT make a child ready — children of failed deps are
    surfaced in the ``blocked_by_failure`` section so the operator can
    decide whether to re-plan or unblock manually.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    ready: list[str] = []
    blocked_by_failure: list[str] = []
    for wid, w in sorted(state.waves.items()):
        if w.iter_id != target_iter:
            continue
        if w.status != WaveStatus.PENDING:
            continue
        dep_waves = [state.waves[d] for d in w.deps if d in state.waves]
        if any(dw.status == WaveStatus.FAILED for dw in dep_waves):
            blocked_by_failure.append(wid)
            continue
        if all(dw.status == WaveStatus.CLOSED for dw in dep_waves):
            ready.append(wid)
    payload: dict[str, Any] = {
        "iter": target_iter,
        "ready": ready,
        "blocked_by_failure": blocked_by_failure,
    }
    text_lines = [f"ready: {ready}"]
    if blocked_by_failure:
        text_lines.append(f"blocked by failure: {blocked_by_failure}")
    emit_json_or_text(payload, "\n".join(text_lines), flags=flags)


@wave_app.command("blocks-rebuild")
def wave_blocks_rebuild_cmd(
    ctx: typer.Context,
    apply_all: Annotated[
        bool,
        typer.Option("--all", help="Rebuild blocks for every wave (vs no-op)."),
    ] = False,
) -> None:
    """Rebuild ``Wave.blocks`` reverse-index from sister waves' ``deps``.

    Legacy fix-up: waves planned BEFORE the W02 (B026) feature landed
    do not have their ``blocks`` list maintained. This verb walks
    ``state.waves`` and rewrites each wave's ``blocks`` to ``[
    child_id for child in state.waves.values() if wave_id in child.deps
    ]``, sorted.
    """
    flags: GlobalFlags = ctx.obj
    if not apply_all:
        cli_errors.emit_error(
            cli_errors.InvalidInput("pass --all to rebuild every wave's blocks index"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return

    rewritten: list[dict[str, list[str]]] = []
    with portalock.acquire(state_path, timeout=5.0):
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
        state = State.model_validate(payload)
        for wave_id, wave in state.waves.items():
            new_blocks = sorted(
                child_id for child_id, child in state.waves.items() if wave_id in child.deps
            )
            if list(wave.blocks) != new_blocks:
                rewritten.append({"id": wave_id, "from": list(wave.blocks), "to": new_blocks})
                wave.blocks = new_blocks
        if rewritten:
            new_payload = state.model_dump(mode="json")
            _write_state_unlocked(state_path, new_payload)

    emit_json_or_text(
        {"rewritten": rewritten, "count": len(rewritten)},
        f"wave blocks-rebuild: rewrote {len(rewritten)} wave(s)",
        flags=flags,
    )


# ---- Wave dispatch (subagent prompt rendering, B025) ------------------------


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


def _waves_in_iter(state: State, iter_id: str) -> list[tuple[str, Any]]:
    """Return (wave_id, Wave) pairs in id-ascending order for *iter_id*."""
    return sorted(
        ((wid, w) for wid, w in state.waves.items() if w.iter_id == iter_id),
        key=lambda kv: kv[0],
    )


def _ready_wave_ids(state: State, iter_id: str) -> list[str]:
    """Same logic as ``wave next-ready``: pending waves with every dep closed.

    A wave whose dep is FAILED is excluded (matches the
    blocked_by_failure surface in :func:`wave_next_ready_cmd`).
    """
    ready: list[str] = []
    for wid, w in _waves_in_iter(state, iter_id):
        if w.status != WaveStatus.PENDING:
            continue
        dep_waves = [state.waves[d] for d in w.deps if d in state.waves]
        if any(dw.status == WaveStatus.FAILED for dw in dep_waves):
            continue
        if all(dw.status == WaveStatus.CLOSED for dw in dep_waves):
            ready.append(wid)
    return ready


@wave_app.command("dispatch")
def wave_dispatch_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to render a prompt for.")],
    output: Annotated[
        Path | None,
        typer.Option(
            "--output",
            help="Write the prompt to this path atomically (still emit envelope summary).",
        ),
    ] = None,
) -> None:
    """Render the subagent prompt for *wave_id* (read-only).

    Prints the prompt to stdout in text mode or wraps it in a JSON
    envelope under ``--json``. With ``--output PATH`` the prompt is
    instead written to *PATH* atomically and the envelope/summary is
    surfaced to stdout. A wave that is already CLOSED / FAILED /
    ABANDONED still renders successfully (history view); a stderr note
    flags the terminal status.
    """
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    if wave_id not in state.waves:
        cli_errors.emit_error(
            cli_errors.NotFound(f"unknown wave: {wave_id}"),
            flags=flags,
        )
        return
    try:
        prompt = render_wave_prompt(state, wave_id)
    except KeyError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    wave = state.waves[wave_id]
    if wave.status in {WaveStatus.CLOSED, WaveStatus.FAILED, WaveStatus.ABANDONED}:
        print(
            f"note: wave {wave_id!r} has terminal status {wave.status.value!r}; "
            f"prompt rendered for history-only inspection",
            file=sys.stderr,
        )
    if output is not None:
        _atomic_write_text(output, prompt)
        envelope: dict[str, Any] = {
            "wave": wave_id,
            "output": str(output),
            "bytes_written": len(prompt.encode("utf-8")),
        }
        text = f"wave dispatch {wave_id} written to {output}"
        emit_json_or_text(envelope, text, flags=flags)
        return
    envelope = {"wave": wave_id, "prompt": prompt}
    emit_json_or_text(envelope, prompt, flags=flags)


@wave_app.command("dispatch-batch")
def wave_dispatch_batch_cmd(
    ctx: typer.Context,
    iter_flag: Annotated[
        str | None,
        typer.Option("--iter", help="Iter ID to enumerate (defaults to current iter)."),
    ] = None,
    ready_only: Annotated[
        bool,
        typer.Option("--ready-only", help="Restrict output to waves returned by next-ready."),
    ] = False,
) -> None:
    """Render prompts for every (or every ready) pending wave under an iter.

    Without ``--ready-only`` the verb walks every pending wave under
    the iter. With ``--ready-only`` only the waves
    :func:`wave_next_ready_cmd` would surface (deps all closed, no
    failed-dep blockers) are rendered.
    """
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    state, flags = loaded
    target_iter = _resolve_iter_for_query(state, flags, iter_flag=iter_flag)
    if target_iter is None:
        return
    if ready_only:
        wave_ids = _ready_wave_ids(state, target_iter)
    else:
        wave_ids = [
            wid for wid, w in _waves_in_iter(state, target_iter) if w.status == WaveStatus.PENDING
        ]
    prompts: list[dict[str, Any]] = []
    text_chunks: list[str] = []
    for wid in wave_ids:
        try:
            prompt = render_wave_prompt(state, wid)
        except KeyError as exc:
            cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
            return
        prompts.append({"wave": wid, "prompt": prompt})
        text_chunks.append(f"---- WAVE {wid} ----\n{prompt}")
    payload: dict[str, Any] = {"iter": target_iter, "prompts": prompts}
    text = "\n".join(text_chunks) if text_chunks else f"iter {target_iter}: no waves to dispatch"
    emit_json_or_text(payload, text, flags=flags)


# ---- Wave budget handlers --------------------------------------------------


@wave_budget_app.command("set")
def wave_budget_set_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID whose budget is being set.")],
    tokens: Annotated[int, typer.Argument(help="Non-negative token cap (0 allowed).")],
) -> None:
    """Set ``Wave.token_budget`` for *wave_id* (non-negative integer)."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if tokens < 0:
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"--tokens must be non-negative; got {tokens}"),
            flags=flags,
        )
        return

    def _mutator(state: State) -> None:
        try:
            budget_set(state, wave_id, tokens)
        except KeyError as exc:
            raise cli_errors.NotFound(str(exc)) from exc

    _run_mutation(
        ctx,
        command="wave budget set",
        args={"id": wave_id, "tokens": tokens},
        scope_id=wave_id,
        text=f"wave budget set {wave_id} tokens={tokens}",
        envelope=lambda: {"wave": wave_id, "token_budget": tokens},
        mutate=_mutator,
    )


@wave_budget_app.command("consume")
def wave_budget_consume_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID accumulating consumption.")],
    tokens: Annotated[int, typer.Argument(help="Non-negative token delta to add.")],
) -> None:
    """Add *tokens* to ``Wave.tokens_consumed`` and surface the policy verdict.

    Exits ``VALIDATION_FAILED`` (4) when the post-add classification is
    ``block:over-budget``. A ``warn:75-percent`` classification prints a
    stderr warning but exits zero.
    """
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    if tokens < 0:
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"--tokens must be non-negative; got {tokens}"),
            flags=flags,
        )
        return

    result: dict[str, Any] = {}

    def _mutator(state: State) -> None:
        try:
            wave, tag = budget_record(state, wave_id, tokens)
        except KeyError as exc:
            raise cli_errors.NotFound(str(exc)) from exc
        result["classification"] = tag
        result["tokens_consumed"] = wave.tokens_consumed
        result["token_budget"] = wave.token_budget
        if tag == BLOCK_TAG:
            raise cli_errors.ValidationFailed(
                f"wave {wave_id!r} is over token budget "
                f"({wave.tokens_consumed}/{wave.token_budget}); "
                f"raise budget or split work"
            )

    _run_mutation(
        ctx,
        command="wave budget consume",
        args={"id": wave_id, "tokens": tokens},
        scope_id=wave_id,
        text=f"wave budget consume {wave_id} tokens={tokens}",
        envelope=lambda: {
            "wave": wave_id,
            "delta": tokens,
            "tokens_consumed": result.get("tokens_consumed"),
            "token_budget": result.get("token_budget"),
            "classification": result.get("classification"),
        },
        mutate=_mutator,
    )

    if result.get("classification") == WARN_TAG:
        consumed = result.get("tokens_consumed")
        budget_val = result.get("token_budget")
        logger.warning(f"wave {wave_id!r} at 75% of token budget ({consumed}/{budget_val})")
        print(
            f"warn: wave {wave_id!r} at 75% of token budget ({consumed}/{budget_val})",
            file=sys.stderr,
        )


@wave_budget_app.command("show")
def wave_budget_show_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to inspect (read-only).")],
) -> None:
    """Print *wave_id*'s budget, consumption, remainder, and policy verdict."""
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.NotFound(f"state file not found: {state_path}"),
            flags=flags,
        )
        return
    try:
        payload = _read_state_payload(state_path)
        try:
            state = State.model_validate(payload)
        except PydValidationError as exc:
            raise cli_errors.IntegrityViolation(
                f"state at {state_path} fails schema validation: {exc}"
            ) from exc
        try:
            classification = budget_check(state, wave_id)
        except KeyError as exc:
            raise cli_errors.NotFound(str(exc)) from exc
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    wave = state.waves[wave_id]
    budget_val = wave.token_budget
    consumed = wave.tokens_consumed
    remaining = None if budget_val is None else budget_val - consumed
    envelope = {
        "wave": wave_id,
        "token_budget": budget_val,
        "tokens_consumed": consumed,
        "remaining": remaining,
        "classification": classification,
    }
    budget_display = "unset" if budget_val is None else str(budget_val)
    remaining_display = "n/a" if remaining is None else str(remaining)
    text = (
        f"wave {wave_id} budget={budget_display} consumed={consumed} "
        f"remaining={remaining_display} status={classification or 'ok'}"
    )
    emit_json_or_text(envelope, text, flags=flags)


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
) -> None:
    """Shared transactional path for every mutating handler in this module.

    Either *text* + *envelope* (static) or *text_factory* + *envelope_factory*
    (deferred until after the mutation has resolved auto-allocated ids) must
    be provided. Likewise either *scope_id* (eager) or *scope_id_factory*
    (deferred — resolved after ``mutate`` runs so handlers can capture the
    allocator-returned id rather than a placeholder).
    """
    if (scope_id is None) == (scope_id_factory is None):
        raise ValueError("exactly one of scope_id or scope_id_factory must be provided")
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.NotFound(f"state file not found: {state_path}; run `eawf project init`"),
            flags=flags,
        )
        return

    try:
        with portalock.acquire(state_path, timeout=5.0):
            payload = _read_state_payload(state_path)
            before_version = _state_version(payload)
            try:
                state = State.model_validate(payload)
            except PydValidationError as exc:
                raise cli_errors.IntegrityViolation(
                    f"state at {state_path} fails schema validation: {exc}"
                ) from exc
            try:
                mutate(state)
            except LifecycleError as exc:
                if closure_kind:
                    raise cli_errors.ValidationFailed(str(exc)) from exc
                raise cli_errors.InvalidInput(str(exc)) from exc
            except (PydValidationError, ValueError) as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            state.updated_at = datetime.now(UTC)
            resolved_scope_id = scope_id if scope_id is not None else scope_id_factory()
            # ``ValidationFailed`` raised by ``_commit_mutation`` is handled by
            # the surrounding ``except cli_errors.CliError`` clause below — no
            # local catch-and-re-raise is required.
            _commit_mutation(
                state_path,
                candidate=state,
                before_version=before_version,
                command=command,
                args=args,
                scope_id=resolved_scope_id,
                summary=command,
            )
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.LockConflict(str(exc)), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    final_text = text if text is not None else text_factory()
    final_payload = envelope() if envelope is not None else envelope_factory()
    emit_json_or_text(final_payload, final_text, flags=flags)


# ---- Re-exports -------------------------------------------------------------

__all__ = [
    "iter_app",
    "phase_app",
    "project_app",
    "subproject_app",
    "wave_app",
    "wave_budget_app",
]
