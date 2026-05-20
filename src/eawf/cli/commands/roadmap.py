"""``eawf roadmap`` — planner CLI for PLANNED-scope phases.

P19-W06 turns ``/roadmap`` from a read-only reporter into a planner.
The CLI mutates the PLANNED queue inside :data:`state.phases` via
the lifecycle transitions introduced in P19-W01:

- ``roadmap propose --phase PXX --title TEXT`` calls
  :func:`eawf.lifecycle.transitions.plan_phase` and an immediate
  :func:`plan_iter` so subsequent ``revise --add-wave`` calls have
  somewhere to attach. The envelope status is ``needs_user`` so the
  active runtime (Claude plan-mode, Codex text-prompt) gets a chance
  to approve before any waves are added.
- ``roadmap revise PXX ...`` is the structured-flag editor for
  PENDING waves: ``--add-wave`` plans, ``--remove-wave`` removes,
  ``--set-deps`` reshapes the dep set, ``--retitle`` rewrites the
  title. All mutations route through the P19-W01 transitions which
  enforce the PENDING-only invariant on their own. The phase-status
  gate (P19-W12) accepts PLANNED or ACTIVE parents; for an ACTIVE
  parent the wave-level PENDING check is the load-bearing invariant
  so CLOSED/CLAIMED/IN_PROGRESS waves under the same phase stay
  immutable.
- ``roadmap apply PXX`` is informational once propose has run — the
  PLANNED scope is already persisted. It validates that the phase
  exists in PLANNED and emits an ``ok`` envelope so flow orchestrators
  can chain to ``/prep`` cleanly.
- ``roadmap drop PXX`` calls :func:`archive_phase` (PLANNED → ARCHIVED).
- ``roadmap show`` renders the queue (text / markdown / JSON).
"""

from __future__ import annotations

import hashlib
import io
import logging
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import orjson
import typer
from rich.console import Console
from rich.table import Table

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.lifecycle.transitions import (
    LifecycleError,
    archive_phase,
    edit_wave_plan,
    plan_iter,
    plan_phase,
    plan_wave,
    remove_wave_plan,
    set_wave_deps,
)
from eawf.state.enums import (
    AgentSessionRole,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    StoreKind,
    WaveStatus,
)
from eawf.state.ids import is_phase_id, is_wave_id
from eawf.state.models import Iter, Phase, State
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.event import EventPayload
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)

# Freshness window for PLANNED phases / iters and dormant iters (D17
# iter-bump triggers note repair-cycle thresholds, but the renderer is a
# read-only diagnostic — 14 days mirrors the memory-staleness default in
# :mod:`eawf.memory.staleness`).
_STALE_AGE_DAYS = 14

roadmap_app = typer.Typer(
    name="roadmap",
    help="Roadmap planner (propose / revise / apply / drop / show).",
    no_args_is_help=True,
)


def _append_roadmap_event(
    state_path: Path,
    *,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    summary: str,
) -> None:
    """Append one ``EVENT`` envelope to ``store/event.jsonl``.

    Mirrors :func:`eawf.cli.commands.lifecycle._append_event` so every
    ``/roadmap``-driven state mutation lands an audit row alongside the
    state-side change. Callers invoke this inside the
    :func:`state_transaction` block so the EVENT precedes the
    ``state.json`` write under the same sibling-lock window.
    """
    args_blob = orjson.dumps(args, option=orjson.OPT_SORT_KEYS)
    args_hash = hashlib.sha256(args_blob).hexdigest()[:16]
    now = datetime.now(UTC)
    events_path = store_path(state_path, StoreKind.EVENT)
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
            before_state_version="",
            after_state_version="",
            status="ok",
            message=summary,
        ).model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(events_path, envelope)


def _split_csv(value: str | None) -> list[str]:
    if not value:
        return []
    return [item.strip() for item in value.split(",") if item.strip()]


def _phase_summary(state: State, phase_id: str) -> dict[str, Any]:
    phase = state.phases[phase_id]
    iter_ids = [iid for iid in phase.iter_ids if iid in state.iters]
    wave_count = sum(1 for w in state.waves.values() if w.iter_id in set(iter_ids))
    return {
        "id": phase.id,
        "status": phase.status.value,
        "title": phase.title,
        "depends_on": list(phase.depends_on),
        "source_brief_ids": list(phase.source_brief_ids),
        "iter_ids": iter_ids,
        "wave_count": wave_count,
    }


def _render_propose_plan_text(state: State, phase_id: str) -> str:
    summary = _phase_summary(state, phase_id)
    lines = [
        f"# Proposed phase: {summary['id']}",
        "",
        f"**Title:** {summary['title']}",
        f"**Status:** {summary['status']}",
    ]
    if summary["depends_on"]:
        lines.append(f"**Depends on:** {', '.join(summary['depends_on'])}")
    if summary["source_brief_ids"]:
        lines.append(f"**Source briefs:** {', '.join(summary['source_brief_ids'])}")
    lines.append("")
    if summary["wave_count"] == 0:
        lines.append("_No waves yet — add via_ `eawf roadmap revise --add-wave`")
    else:
        lines.append(f"**Waves planned:** {summary['wave_count']}")
    lines.append("")
    lines.append(
        f"Approve with `eawf roadmap apply {summary['id']}` "
        f"(emits ok envelope) or discard with `eawf roadmap drop {summary['id']}`."
    )
    return "\n".join(lines)


@roadmap_app.command("propose")
def roadmap_propose_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Option("--phase", help="Phase id like P21.")],
    title: Annotated[str, typer.Option("--title", help="Phase title.")],
    from_briefs: Annotated[
        str | None,
        typer.Option(
            "--from-briefs",
            help="Comma-separated brief ids (RES-YYYY-MM-DD-NNN) that motivated this phase.",
        ),
    ] = None,
    depends_on: Annotated[
        str | None,
        typer.Option("--depends-on", help="Comma-separated phase ids this phase depends on."),
    ] = None,
    iter_title: Annotated[
        str | None,
        typer.Option(
            "--iter-title",
            help="Title for the auto-created P##-I01 iter (defaults to phase title).",
        ),
    ] = None,
) -> None:
    """Propose a new PLANNED phase + I01 iter; emits needs_user envelope."""
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return
    depends_on_list = _split_csv(depends_on)
    source_brief_list = _split_csv(from_briefs)
    iter_id = f"{phase_id}-I01"
    final_iter_title = iter_title or title

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    plan_text = ""
    try:
        with state_transaction(state_path) as state:
            try:
                plan_phase(
                    state,
                    phase_id=phase_id,
                    title=title,
                    depends_on=depends_on_list,
                    source_brief_ids=source_brief_list,
                )
                plan_iter(
                    state,
                    iter_id=iter_id,
                    phase_id=phase_id,
                    title=final_iter_title,
                )
            except LifecycleError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            state.updated_at = datetime.now(UTC)
            plan_text = _render_propose_plan_text(state, phase_id)
            _append_roadmap_event(
                state_path,
                command="roadmap propose",
                args={
                    "phase_id": phase_id,
                    "title": title,
                    "iter_id": iter_id,
                    "depends_on": depends_on_list,
                    "source_brief_ids": source_brief_list,
                },
                scope_id=phase_id,
                summary=f"roadmap propose {phase_id} title={title!r}",
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    envelope = {
        "status": "needs_user",
        "decision_kind": "approve_plan",
        "phase_id": phase_id,
        "iter_id": iter_id,
        "title": title,
        "depends_on": depends_on_list,
        "source_brief_ids": source_brief_list,
        "plan_text": plan_text,
        "options": [
            {
                "label": "approve",
                "next": f"eawf roadmap apply {phase_id}",
            },
            {
                "label": "revise",
                "next": f"eawf roadmap revise {phase_id} --add-wave WNN ...",
            },
            {
                "label": "drop",
                "next": f"eawf roadmap drop {phase_id}",
            },
        ],
    }
    emit_json_or_text(envelope, plan_text, flags=flags)


def _resolve_revisable_phase(state: State, phase_id: str) -> None:
    """Reject the revise call when *phase_id* is not PLANNED or ACTIVE.

    PLANNED phases are freely revisable. ACTIVE phases are revisable too
    (P19-W12) but only for PENDING waves under them — the wave-level
    PENDING check inside the lifecycle transitions enforces that
    invariant on its own. CLOSED and ARCHIVED phases are immutable.
    """
    if phase_id not in state.phases:
        raise cli_errors.NotFound(f"unknown phase {phase_id!r}")
    phase = state.phases[phase_id]
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise cli_errors.InvalidInput(
            f"phase {phase_id!r} has status {phase.status.value!r}; "
            "revise only works on PLANNED or ACTIVE phases"
        )


def _iter_id_for_phase(state: State, phase_id: str) -> str:
    phase = state.phases[phase_id]
    if not phase.iter_ids:
        raise cli_errors.InvalidInput(
            f"phase {phase_id!r} has no iter; propose should have created P##-I01"
        )
    return phase.iter_ids[0]


@roadmap_app.command("revise")
def roadmap_revise_cmd(
    ctx: typer.Context,
    phase_id: Annotated[
        str,
        typer.Argument(help="Phase id to revise (PLANNED, or ACTIVE for PENDING waves)."),
    ],
    add_wave: Annotated[
        str | None,
        typer.Option("--add-wave", help="Add a wave under the phase's I01 iter; pass the wave id."),
    ] = None,
    remove_wave: Annotated[
        str | None,
        typer.Option("--remove-wave", help="Remove a PENDING wave from the phase plan."),
    ] = None,
    set_deps: Annotated[
        str | None,
        typer.Option(
            "--set-deps",
            help="Replace a wave's deps. Form: 'W04=W01,W02' or full ids.",
        ),
    ] = None,
    retitle: Annotated[
        str | None,
        typer.Option(
            "--retitle",
            help="Replace a wave title. Form: 'W04=feat: new title' (uses '=' as separator).",
        ),
    ] = None,
    wave_title: Annotated[
        str | None,
        typer.Option("--title", help="Wave title (only meaningful with --add-wave)."),
    ] = None,
    deps: Annotated[
        str | None,
        typer.Option("--deps", help="Comma-separated dep wave ids (only with --add-wave)."),
    ] = None,
    success: Annotated[
        str | None,
        typer.Option(
            "--success",
            help="Comma-separated success-criterion strings (only with --add-wave).",
        ),
    ] = None,
    files: Annotated[
        str | None,
        typer.Option("--files", help="Comma-separated file globs (only with --add-wave)."),
    ] = None,
    agent_role: Annotated[
        str | None,
        typer.Option("--agent-role", help="Wave agent role (only with --add-wave)."),
    ] = None,
    effort_bucket: Annotated[
        str | None,
        typer.Option("--effort-bucket", help="One of XS|S|M|L|XL (only with --add-wave)."),
    ] = None,
) -> None:
    """Edit a PLANNED or ACTIVE phase's wave plan via structured flags.

    For an ACTIVE parent only PENDING waves are mutable — the wave-level
    PENDING check inside the lifecycle transitions rejects edits aimed
    at CLOSED/CLAIMED/IN_PROGRESS waves.
    """
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return
    selected = [opt for opt in (add_wave, remove_wave, set_deps, retitle) if opt]
    if len(selected) != 1:
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                "exactly one of --add-wave/--remove-wave/--set-deps/--retitle must be passed"
            ),
            flags=flags,
        )
        return

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return

    action_summary = ""
    try:
        with state_transaction(state_path) as state:
            try:
                _resolve_revisable_phase(state, phase_id)
                if add_wave:
                    if not wave_title or not files:
                        raise cli_errors.InvalidInput("--add-wave requires --title and --files")
                    full_wave_id = _coerce_full_wave_id(state, phase_id, add_wave)
                    iter_id = _iter_id_for_phase(state, phase_id)
                    role = AgentSessionRole(agent_role) if agent_role else None
                    bucket = EffortBucket(effort_bucket) if effort_bucket else None
                    plan_wave(
                        state,
                        wave_id=full_wave_id,
                        iter_id=iter_id,
                        title=wave_title,
                        file_scopes=_split_csv(files),
                        deps=[_coerce_full_wave_id(state, phase_id, d) for d in _split_csv(deps)],
                        success_criteria=_split_csv(success),
                        agent_role=role,
                        effort_bucket=bucket,
                    )
                    action_summary = f"added wave {full_wave_id}"
                elif remove_wave:
                    full_wave_id = _coerce_full_wave_id(state, phase_id, remove_wave)
                    remove_wave_plan(state, wave_id=full_wave_id)
                    action_summary = f"removed wave {full_wave_id}"
                elif set_deps:
                    target, _, deps_csv = set_deps.partition("=")
                    full_wave_id = _coerce_full_wave_id(state, phase_id, target.strip())
                    new_deps = [
                        _coerce_full_wave_id(state, phase_id, d) for d in _split_csv(deps_csv)
                    ]
                    set_wave_deps(state, wave_id=full_wave_id, deps=new_deps)
                    action_summary = f"set deps on {full_wave_id}: {new_deps}"
                elif retitle:
                    target, _, new_title = retitle.partition("=")
                    full_wave_id = _coerce_full_wave_id(state, phase_id, target.strip())
                    edit_wave_plan(state, wave_id=full_wave_id, title=new_title.strip())
                    action_summary = f"retitled {full_wave_id}: {new_title.strip()!r}"
            except LifecycleError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            state.updated_at = datetime.now(UTC)
            _append_roadmap_event(
                state_path,
                command="roadmap revise",
                args={
                    "phase_id": phase_id,
                    "add_wave": add_wave,
                    "remove_wave": remove_wave,
                    "set_deps": set_deps,
                    "retitle": retitle,
                },
                scope_id=phase_id,
                summary=f"roadmap revise {phase_id}: {action_summary}",
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    emit_json_or_text(
        {"phase_id": phase_id, "action": action_summary, "status": "ok"},
        f"roadmap revise {phase_id}: {action_summary}",
        flags=flags,
    )


def _coerce_full_wave_id(state: State, phase_id: str, candidate: str) -> str:
    """Accept either the bare ``W##`` form or the full ``P##-I##-W##`` id.

    Bare ``W##`` is expanded against the phase's first iter (``P##-I01``).
    """
    if is_wave_id(candidate):
        return candidate
    if candidate.startswith("W") and candidate[1:].isdigit():
        iter_id = _iter_id_for_phase(state, phase_id)
        full = f"{iter_id}-{candidate}"
        if is_wave_id(full):
            return full
    raise cli_errors.InvalidInput(f"invalid wave id reference: {candidate!r}")


@roadmap_app.command("apply")
def roadmap_apply_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase id to apply (informational).")],
) -> None:
    """Finalise a PLANNED phase. Currently informational — propose
    already persists the PLANNED scope; apply confirms readiness for
    ``/prep``."""
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    try:
        with state_transaction(state_path) as state:
            if phase_id not in state.phases:
                raise cli_errors.NotFound(f"unknown phase {phase_id!r}")
            phase = state.phases[phase_id]
            if phase.status != PhaseStatus.PLANNED:
                raise cli_errors.InvalidInput(
                    f"phase {phase_id!r} has status {phase.status.value!r}; "
                    "only PLANNED phases can be applied"
                )
            wave_count = sum(1 for w in state.waves.values() if w.iter_id in set(phase.iter_ids))
            if wave_count == 0:
                raise cli_errors.InvalidInput(
                    f"phase {phase_id!r} has no waves; revise --add-wave before apply"
                )
            state.updated_at = datetime.now(UTC)
            _append_roadmap_event(
                state_path,
                command="roadmap apply",
                args={"phase_id": phase_id, "wave_count": wave_count},
                scope_id=phase_id,
                summary=f"roadmap apply {phase_id} waves={wave_count}",
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {
            "phase_id": phase_id,
            "status": "ok",
            "next": f"eawf prep {phase_id}",
        },
        f"phase {phase_id} ready for /prep ({wave_count} waves planned)",
        flags=flags,
    )


@roadmap_app.command("drop")
def roadmap_drop_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="PLANNED phase id to archive.")],
) -> None:
    """Archive a PLANNED phase (PLANNED → ARCHIVED). Irreversible via the
    state CLI; recover with ``git restore`` if needed."""
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase_id!r}"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    try:
        with state_transaction(state_path) as state:
            try:
                archive_phase(state, phase_id=phase_id)
            except LifecycleError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            state.updated_at = datetime.now(UTC)
            _append_roadmap_event(
                state_path,
                command="roadmap drop",
                args={"phase_id": phase_id},
                scope_id=phase_id,
                summary=f"roadmap drop {phase_id}",
            )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    emit_json_or_text(
        {"phase_id": phase_id, "status": "archived"},
        f"phase {phase_id} archived",
        flags=flags,
    )


@roadmap_app.command("show")
def roadmap_show_cmd(
    ctx: typer.Context,
    phase: Annotated[
        str | None,
        typer.Option("--phase", help="Restrict the rendered queue to one phase id."),
    ] = None,
    md: Annotated[bool, typer.Option("--md", help="Render as a markdown table.")] = False,
) -> None:
    """Render the PLANNED queue plus the ACTIVE phase summary.

    The default text renderer builds a :class:`rich.table.Table` with
    nested phases / iters / waves rows; stale items (PLANNED phases or
    iters past the freshness window, dormant iters with no recent wave
    activity) render in a dim style so they read as muted in a terminal.
    ``--md`` emits a markdown table; ``--json`` (top-level flag) emits
    the JSON envelope only. ``--plain`` (top-level flag) bypasses Rich
    markup for terminals that cannot render ANSI.
    """
    flags: GlobalFlags = ctx.obj
    if phase is not None and not is_phase_id(phase):
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"invalid phase id: {phase!r}"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
        return
    try:
        # Read-only view: read_only=True bypasses the §5.5 --daemonless
        # mutating-verb gate so `roadmap show --daemonless` still works.
        with state_transaction(state_path, read_only=True) as state:
            phases = sorted(state.phases.values(), key=lambda p: p.id)
            if phase is not None:
                phases = [p for p in phases if p.id == phase]
            rows = [_phase_summary(state, p.id) for p in phases]
            nested = [_phase_node(state, p.id, now=datetime.now(UTC)) for p in phases]
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    text = _render_show_md(rows) if md else _render_show_rich(nested, plain=flags.plain_output)
    emit_json_or_text({"phases": rows}, text, flags=flags)


def _iter_summary(state: State, iter_id: str) -> dict[str, Any]:
    """Return a JSON-friendly summary row for an iter."""
    it = state.iters[iter_id]
    wave_ids = [wid for wid in it.wave_ids if wid in state.waves]
    return {
        "id": it.id,
        "phase_id": it.phase_id,
        "status": it.status.value,
        "title": it.title,
        "wave_ids": wave_ids,
        "opened_at": it.opened_at.isoformat(),
    }


def _wave_summary(state: State, wave_id: str) -> dict[str, Any]:
    """Return a JSON-friendly summary row for a wave."""
    w = state.waves[wave_id]
    return {
        "id": w.id,
        "iter_id": w.iter_id,
        "status": w.status.value,
        "title": w.title,
        "deps": list(w.deps),
        "opened_at": w.opened_at.isoformat(),
    }


def _phase_node(state: State, phase_id: str, *, now: datetime) -> dict[str, Any]:
    """Return a nested phase node with its iters and waves materialised.

    The node is consumed by :func:`_render_show_rich` to build a single
    rich table covering phases / iters / waves. Stale annotations are
    pre-computed here so the renderer stays presentation-only.
    """
    phase = state.phases[phase_id]
    iter_nodes: list[dict[str, Any]] = []
    for iter_id in phase.iter_ids:
        if iter_id not in state.iters:
            continue
        it = state.iters[iter_id]
        wave_nodes: list[dict[str, Any]] = []
        for wave_id in it.wave_ids:
            if wave_id not in state.waves:
                continue
            wave_nodes.append(_wave_summary(state, wave_id))
        iter_nodes.append(
            {
                **_iter_summary(state, iter_id),
                "waves": wave_nodes,
                "stale": _is_stale_iter(state, it, now=now),
            }
        )
    return {
        **_phase_summary(state, phase_id),
        "iters": iter_nodes,
        "opened_at": phase.opened_at.isoformat(),
        "stale": _is_stale_phase(phase, now=now),
    }


def _is_stale_phase(phase: Phase, *, now: datetime) -> bool:
    """Return True when *phase* is PLANNED past the freshness window.

    ACTIVE phases never read as stale (they are the live workspace).
    CLOSED / ARCHIVED phases are terminal so staleness is meaningless.
    PLANNED phases stale at :data:`_STALE_AGE_DAYS` (default 14) since
    ``opened_at``.
    """
    if phase.status != PhaseStatus.PLANNED:
        return False
    return (now - phase.opened_at) > timedelta(days=_STALE_AGE_DAYS)


def _is_stale_iter(state: State, it: Iter, *, now: datetime) -> bool:
    """Return True when *it* is past the freshness window or dormant.

    Two staleness triggers:

    - the iter itself is PLANNED past :data:`_STALE_AGE_DAYS`; or
    - the iter is PLANNED or ACTIVE but every wave under it is still
      ``PENDING`` and the iter opened more than :data:`_STALE_AGE_DAYS`
      ago — no recent execution activity.

    CLOSED / ABANDONED iters never read as stale (terminal status).
    """
    if it.status in {IterStatus.CLOSED, IterStatus.ABANDONED}:
        return False
    age = now - it.opened_at
    if it.status == IterStatus.PLANNED and age > timedelta(days=_STALE_AGE_DAYS):
        return True
    if age <= timedelta(days=_STALE_AGE_DAYS):
        return False
    waves = [state.waves[wid] for wid in it.wave_ids if wid in state.waves]
    if not waves:
        # Iter older than the window with no waves at all reads as dormant.
        return True
    return all(w.status == WaveStatus.PENDING for w in waves)


def _render_show_rich(nodes: list[dict[str, Any]], *, plain: bool) -> str:
    """Render the nested phase/iter/wave queue.

    The Rich branch builds a single :class:`Table` with a ``kind``
    column distinguishing phases / iters / waves; stale rows render in
    dim style. The plain branch emits the same shape without ANSI so
    terminals without colour stay readable.

    Args:
        nodes: Phase nodes from :func:`_phase_node`, in render order.
        plain: When True, bypass Rich markup entirely.
    """
    if not nodes:
        return "(no phases in state)"
    if plain:
        return _render_show_plain(nodes)

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    table = Table(title="eawf roadmap", show_lines=False)
    table.add_column("kind", style="bold")
    table.add_column("id", style="cyan")
    table.add_column("status")
    table.add_column("waves", justify="right")
    table.add_column("deps")
    table.add_column("title")
    for phase in nodes:
        _add_phase_rows(table, phase)
    console.print(table)
    return buf.getvalue().rstrip()


def _add_phase_rows(table: Table, phase: dict[str, Any]) -> None:
    """Append a phase row plus its iter / wave descendants to *table*."""
    p_style = "dim" if phase["stale"] else ""
    deps = ", ".join(phase["depends_on"]) or "-"
    table.add_row(
        _styled("phase", p_style),
        _styled(phase["id"], p_style),
        _styled(phase["status"], p_style),
        _styled(str(phase["wave_count"]), p_style),
        _styled(deps, p_style),
        _styled(phase["title"], p_style),
    )
    for it in phase["iters"]:
        i_style = "dim" if it["stale"] else ""
        table.add_row(
            _styled("  iter", i_style),
            _styled(it["id"], i_style),
            _styled(it["status"], i_style),
            _styled(str(len(it["waves"])), i_style),
            _styled("-", i_style),
            _styled(it["title"], i_style),
        )
        for w in it["waves"]:
            # Wave staleness inherits from the parent iter — the wave model
            # tracks status (PENDING/CLAIMED/...), and aging is captured at
            # the iter level via _is_stale_iter (no recent activity).
            w_style = "dim" if it["stale"] else ""
            w_deps = ", ".join(w["deps"]) or "-"
            table.add_row(
                _styled("    wave", w_style),
                _styled(w["id"], w_style),
                _styled(w["status"], w_style),
                _styled("", w_style),
                _styled(w_deps, w_style),
                _styled(w["title"], w_style),
            )


def _styled(text: str, style: str) -> str:
    """Wrap *text* in a rich style marker when *style* is non-empty."""
    if not style:
        return text
    return f"[{style}]{text}[/{style}]"


def _render_show_plain(nodes: list[dict[str, Any]]) -> str:
    """Plain-text fallback for :func:`_render_show_rich`.

    Stale items are tagged with a trailing ``(stale)`` marker since
    ANSI dimming is unavailable.
    """
    lines = ["kind   id              status       waves  title"]
    for phase in nodes:
        stale_tag = " (stale)" if phase["stale"] else ""
        lines.append(
            f"phase  {phase['id']:<15} {phase['status']:<12} "
            f"{phase['wave_count']:>5}  {phase['title']}{stale_tag}"
        )
        for it in phase["iters"]:
            i_tag = " (stale)" if it["stale"] else ""
            lines.append(
                f"  iter {it['id']:<15} {it['status']:<12} "
                f"{len(it['waves']):>5}  {it['title']}{i_tag}"
            )
            for w in it["waves"]:
                w_tag = " (stale)" if it["stale"] else ""
                lines.append(f"    wave {w['id']:<13} {w['status']:<12}        {w['title']}{w_tag}")
    return "\n".join(lines)


def _render_show_md(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return "_(no phases in state)_"
    out = ["| Phase | Status | Waves | Depends on | Title |", "|---|---|---|---|---|"]
    for row in rows:
        deps = ", ".join(row["depends_on"]) or "—"
        out.append(
            f"| `{row['id']}` | `{row['status']}` | {row['wave_count']} | {deps} | {row['title']} |"
        )
    return "\n".join(out)
