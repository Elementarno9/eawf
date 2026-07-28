"""``eawf roadmap`` — planner CLI for PLANNED-scope phases.

P19-W06 turns ``/roadmap`` from a read-only reporter into a planner.
The CLI mutates the PLANNED queue inside :data:`state.phases` via
the lifecycle transitions introduced in P19-W01:

- ``roadmap propose --phase PXX --title TEXT`` calls
  :func:`eawf.workflow.lifecycle.transitions.plan_phase` and an immediate
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
from typing import TYPE_CHECKING, Annotated, Any, cast

import orjson
import typer
from rich.console import Console
from rich.table import Table

from eawf.kernel.config.schema import VerifyWaiverMode
from eawf.kernel.state.enums import (
    AgentSessionRole,
    DependencyStage,
    EffortBucket,
    IterStatus,
    PhaseStatus,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.ids import is_iter_id, is_phase_id, is_wave_id, natural_key
from eawf.kernel.state.models import wave_dependency_key
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.kernel.spec.intent import IntentBrief
    from eawf.kernel.state.models import Iter, Phase, State

logger = logging.getLogger(__name__)

# Freshness window for PLANNED phases / iters and dormant iters. The
# renderer is a read-only diagnostic — 14 days mirrors the
# memory-staleness default in :mod:`eawf.platform.memory.staleness`.
_STALE_AGE_DAYS = 14

roadmap_app = typer.Typer(
    name="roadmap",
    help="Roadmap planner (propose / revise / apply / drop / show).",
    no_args_is_help=True,
)


def _effective_waiver_mode(state: State, *, scope_id: str, state_path: Path) -> VerifyWaiverMode:
    """Resolve strict layered waiver policy for roadmap authoring."""
    from eawf.workflow.verify.readiness import load_active_waiver_mode

    config_root = state_path.parent.parent if state_path.parent.name == ".ea" else state_path.parent
    try:
        return load_active_waiver_mode(
            scope_id,
            state,
            repo_root=config_root,
            config_root=config_root,
        )
    except (OSError, ValueError, KeyError) as exc:
        raise cli_errors.ValidationError(f"verify config invalid: {exc}") from exc


def _append_roadmap_event(
    state_path: Path,
    *,
    command: str,
    args: dict[str, Any],
    scope_id: str,
    summary: str,
) -> None:
    """Append one ``EVENT`` envelope to ``store/event.jsonl``.

    Mirrors :func:`eawf.surfaces.cli.commands.lifecycle._append_event` so every
    ``/roadmap``-driven state mutation lands an audit row alongside the
    state-side change. Callers invoke this inside the
    :func:`state_transaction` block so the EVENT precedes the
    ``state.json`` write under the same sibling-lock window.
    """
    from eawf.kernel.store.append import append_envelope
    from eawf.kernel.store.envelope import Envelope
    from eawf.kernel.store.kinds.event import EventPayload
    from eawf.kernel.store.paths import store_path

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


#: Sentinel returned by :func:`_build_intent_from_flags` when the caller
#: passed an ``--intent-*`` flag without one of the required canonical
#: pair (``--intent-problem`` + ``--intent-desired-outcome``). The CLI
#: handler tests for identity and emits a clean InvalidInput error
#: rather than letting :class:`IntentBrief` raise an opaque
#: :class:`pydantic.ValidationError`.
_INTENT_FLAG_ERROR: object = object()


def _build_intent_from_flags(
    *,
    intent_problem: str | None,
    intent_desired_outcome: str | None,
    intent_priority_rationale: str | None = None,
    intent_planned_steps: str | None = None,
    intent_risks: str | None = None,
    intent_evidence_refs: str | None = None,
    intent_source_brief_ids: str | None = None,
) -> IntentBrief | None | object:
    """Return an :class:`IntentBrief` built from the ``--intent-*`` CLI flags.

    Returns ``None`` when no ``--intent-*`` flag was passed (the caller
    skips the intent edit). Returns :data:`_INTENT_FLAG_ERROR` when at
    least one ``--intent-*`` flag was passed but either
    ``--intent-problem`` or ``--intent-desired-outcome`` is missing —
    these are the two required fields on the brief, so the handler
    surfaces a clean InvalidInput rather than a Pydantic stack trace.
    Otherwise returns a constructed :class:`IntentBrief`; a model bound
    violation propagates as :class:`pydantic.ValidationError` and is
    mapped to InvalidInput by the handler's existing except clause.

    Args:
        intent_problem: ``--intent-problem`` value (required; <=200 chars).
        intent_desired_outcome: ``--intent-desired-outcome`` value
            (required; <=200 chars).
        intent_priority_rationale: ``--intent-priority-rationale``
            value (<=1000 chars).
        intent_planned_steps: ``--intent-planned-steps`` comma-separated
            list, max 10 entries; each entry <=500 chars.
        intent_risks: ``--intent-risks`` comma-separated list, max 10
            entries; each entry <=500 chars.
        intent_evidence_refs: ``--intent-evidence-refs`` comma-separated list.
        intent_source_brief_ids: ``--intent-source-brief-ids`` comma-separated list.
    """
    any_flag = any(
        v is not None
        for v in (
            intent_problem,
            intent_desired_outcome,
            intent_priority_rationale,
            intent_planned_steps,
            intent_risks,
            intent_evidence_refs,
            intent_source_brief_ids,
        )
    )
    if not any_flag:
        return None
    if not intent_problem or not intent_desired_outcome:
        return _INTENT_FLAG_ERROR
    from eawf.kernel.spec.intent import IntentBrief

    return IntentBrief(
        problem=intent_problem,
        desired_outcome=intent_desired_outcome,
        priority_rationale=intent_priority_rationale,
        planned_steps=_split_csv(intent_planned_steps),
        risks=_split_csv(intent_risks),
        evidence_refs=_split_csv(intent_evidence_refs),
        source_brief_ids=_split_csv(intent_source_brief_ids),
    )


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
        "release": phase.release,
    }


def _collect_pending_waves(state: State, phase_id: str) -> list[dict[str, Any]]:
    """Project a phase's PENDING waves into ordered DAG rows.

    Walks ``phase.iter_ids`` in order; for each iter walks ``iter.wave_ids``
    and emits one row per wave whose status is :attr:`WaveStatus.PENDING`.
    Each row mirrors the wave's id, parent iter, deps, and file scopes so a
    caller can render the full wave DAG (the plan that ``apply`` confirms)
    without re-reading the state map.

    Args:
        state: The validated state document holding the ``iters`` / ``waves``
            maps.
        phase_id: The target phase id (assumed present in ``state.phases``).

    Returns:
        DAG rows in iter-then-wave order; empty when the phase has no
        PENDING waves.
    """
    rows: list[dict[str, Any]] = []
    phase = state.phases[phase_id]
    for iter_id in phase.iter_ids:
        iter_record = state.iters.get(iter_id)
        if iter_record is None:
            continue
        for wave_id in iter_record.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None or wave.status != WaveStatus.PENDING:
                continue
            rows.append(
                {
                    "id": wave.id,
                    "iter_id": wave.iter_id,
                    "title": wave.title,
                    "deps": list(wave.deps),
                    "file_scopes": list(wave.file_scopes),
                }
            )
    return rows


def _collect_coverage_gaps(
    state: State, phase_id: str, *, repo_root: Path | None = None
) -> list[dict[str, Any]]:
    """Project EAWF022 coverage gaps over a phase's PENDING waves.

    For each PENDING wave that carries an
    :class:`~eawf.kernel.spec.intent.IntentBrief`, runs two coverage diffs:

    - :func:`eawf.workflow.propose.coverage.coverage_gaps` over the wave's
      authored success criteria vs its ``planned_steps`` -- a step no criterion
      topically covers is a gap.
    - :func:`eawf.workflow.propose.coverage.source_brief_coverage_gaps` over the
      wave's referenced source-brief document(s) -- a source-brief deliverable
      no criterion / step covers is a gap. This leg fires even when
      ``planned_steps`` is empty for a required-intent wave (one that names a
      ``source_brief_ids`` document), closing the boundary the planned-step
      diff cannot see.

    A wave with no intent contributes no rows; a wave with neither planned
    steps nor a source brief likewise has nothing to diff. The diffs are the
    same deterministic token-overlap ones the daemon ``spec.sync`` path runs,
    so the propose render surfaces the gap the sync would later reject.

    Args:
        state: The validated state document holding the ``iters`` / ``waves``
            maps.
        phase_id: The target phase id (assumed present in ``state.phases``).
        repo_root: The repo working-tree root the source-brief paths resolve
            under; defaults to :func:`pathlib.Path.cwd`.

    Returns:
        One row per gapped wave, each carrying the ``wave_id`` and the list of
        uncovered span ids; empty when every staged wave's brief detail is
        covered.
    """
    from eawf.workflow.propose.coverage import coverage_gaps, source_brief_coverage_gaps

    root = repo_root if repo_root is not None else Path.cwd()
    gaps: list[dict[str, Any]] = []
    phase = state.phases[phase_id]
    for iter_id in phase.iter_ids:
        iter_record = state.iters.get(iter_id)
        if iter_record is None:
            continue
        for wave_id in iter_record.wave_ids:
            wave = state.waves.get(wave_id)
            if wave is None or wave.status != WaveStatus.PENDING:
                continue
            if wave.intent is None:
                continue
            criteria = list(wave.success_criteria)
            findings = coverage_gaps(criteria, planned_steps=list(wave.intent.planned_steps))
            findings += source_brief_coverage_gaps(criteria, intent=wave.intent, repo_root=root)
            if findings:
                gaps.append(
                    {
                        "wave_id": wave.id,
                        "uncovered_spans": [f.snippet for f in findings],
                    }
                )
    return gaps


def _render_apply_dag_text(state: State, phase_id: str, waves: list[dict[str, Any]]) -> str:
    """Render the full wave DAG of a phase as markdown plan text.

    This is the plan-mode surface ``apply`` shows before approval: phase
    header followed by every PENDING wave with its deps and file scopes so
    the operator reviews the DAG, not a one-line summary.

    Args:
        state: The validated state document.
        phase_id: The target phase id (assumed present in ``state.phases``).
        waves: The ordered DAG rows from :func:`_collect_pending_waves`.

    Returns:
        A markdown string covering the phase title plus one bullet per
        PENDING wave.
    """
    phase = state.phases[phase_id]
    lines = [
        f"# Apply plan: {phase.id}",
        "",
        f"**Title:** {phase.title}",
        f"**Waves planned:** {len(waves)}",
        "",
        "## Wave DAG",
        "",
    ]
    for row in waves:
        deps = ", ".join(row["deps"]) or "-"
        files = ", ".join(row["file_scopes"]) or "-"
        lines.append(f"- `{row['id']}` — {row['title']}  (deps: {deps}; files: {files})")
    lines.append("")
    lines.append(
        f"Approve with `eawf roadmap apply {phase.id} --approve` "
        f"or reshape with `eawf roadmap revise {phase.id} ...`."
    )
    return "\n".join(lines)


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
    phase_id: Annotated[str | None, typer.Option("--phase", help="Phase id like P21.")] = None,
    title: Annotated[str | None, typer.Option("--title", help="Phase title.")] = None,
    from_plan: Annotated[
        Path | None,
        typer.Option(
            "--from-plan",
            exists=True,
            dir_okay=False,
            readable=True,
            help="Strict YAML/JSON RoadmapPlan file to stage in one transaction.",
        ),
    ] = None,
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
    description: Annotated[
        str | None,
        typer.Option(
            "--description",
            help="Optional long-form phase description (≤500 chars).",
        ),
    ] = None,
    iter_description: Annotated[
        str | None,
        typer.Option(
            "--iter-description",
            help="Optional long-form description for the auto-created P##-I01 iter (≤500 chars).",
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help=(
                "Render the plan text + EAWF022 coverage lint WITHOUT persisting: "
                "state.json and the event store are left byte-identical."
            ),
        ),
    ] = False,
    criteria_from_brief: Annotated[
        Path | None,
        typer.Option(
            "--criteria-from-brief",
            exists=True,
            dir_okay=False,
            readable=True,
            help=(
                "Brief document whose spans seed typed success criteria. "
                "Parsed and surfaced on the propose envelope in this release; "
                "the criteria generator lands in a later phase."
            ),
        ),
    ] = None,
) -> None:
    """Propose a PLANNED phase from flags or a strict roadmap plan file."""
    from pydantic import ValidationError as PydValidationError

    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.transitions import (
        LifecycleError,
        plan_iter,
        plan_phase,
    )

    flags: GlobalFlags = ctx.obj
    if from_plan is not None:
        _roadmap_propose_from_plan(ctx, from_plan=from_plan, dry_run=dry_run)
        return
    if phase_id is None or title is None:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--phase and --title are required unless --from-plan is passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
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
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    plan_text = ""
    coverage_gaps_rows: list[dict[str, Any]] = []
    criteria_from_brief_str = str(criteria_from_brief) if criteria_from_brief is not None else None
    try:
        # A ``--dry-run`` opens the transaction read-only, so the mutated state
        # is rendered for the preview but never written back -- state.json stays
        # byte-identical. The event append is also skipped so no store row lands.
        with state_transaction(state_path, read_only=dry_run) as state:
            try:
                plan_phase(
                    state,
                    phase_id=phase_id,
                    title=title,
                    depends_on=depends_on_list,
                    source_brief_ids=source_brief_list,
                    description=description,
                )
                plan_iter(
                    state,
                    iter_id=iter_id,
                    phase_id=phase_id,
                    title=final_iter_title,
                    description=iter_description,
                )
            except LifecycleError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            except PydValidationError as exc:
                # The ≤500-char description bound trips on the model, not on
                # the lifecycle guard — translate it to the same InvalidInput
                # bucket so the CLI surfaces a clean error rather than a
                # Pydantic stack trace.
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            state.updated_at = datetime.now(UTC)
            plan_text = _render_propose_plan_text(state, phase_id)
            # EAWF022 coverage is ADVISORY at propose: the gaps surface in the
            # needs_user envelope so the operator sees dropped planned steps
            # before apply, but the propose itself never blocks on them.
            coverage_gaps_rows = _collect_coverage_gaps(state, phase_id)
            if not dry_run:
                _append_roadmap_event(
                    state_path,
                    command="roadmap propose",
                    args={
                        "phase_id": phase_id,
                        "title": title,
                        "iter_id": iter_id,
                        "depends_on": depends_on_list,
                        "source_brief_ids": source_brief_list,
                        "description": description,
                        "iter_description": iter_description,
                        "criteria_from_brief": criteria_from_brief_str,
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
        "description": description,
        "iter_description": iter_description,
        "coverage_gaps": coverage_gaps_rows,
        "coverage_advisory": True,
        "dry_run": dry_run,
        "criteria_from_brief": criteria_from_brief_str,
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


def _roadmap_propose_from_plan(
    ctx: typer.Context, *, from_plan: Path, dry_run: bool = False
) -> None:
    """Stage a strict roadmap plan file and emit the normal needs_user envelope.

    When *dry_run* is set the plan is validated and rendered but the
    transaction opens read-only so nothing persists (state.json + event store
    stay byte-identical).
    """
    from pydantic import ValidationError as PydValidationError
    from yaml import YAMLError

    from eawf.kernel.spec.roadmap_plan import load_roadmap_plan
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.transitions import LifecycleError, plan_roadmap

    flags: GlobalFlags = ctx.obj
    try:
        plan = load_roadmap_plan(from_plan)
    except (OSError, ValueError, YAMLError, orjson.JSONDecodeError, PydValidationError) as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid roadmap plan: {exc}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    phase_id = plan.phase.id
    plan_text = ""
    iter_ids: list[str] = []
    wave_ids: list[str] = []
    coverage_gaps_rows: list[dict[str, Any]] = []
    try:
        with state_transaction(state_path, read_only=dry_run) as state:
            try:
                planned = plan_roadmap(
                    state,
                    plan=plan,
                    waiver_mode=_effective_waiver_mode(
                        state,
                        scope_id=phase_id,
                        state_path=state_path,
                    ),
                )
            except LifecycleError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            except PydValidationError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            iter_ids = planned.iter_ids
            wave_ids = planned.wave_ids
            state.updated_at = datetime.now(UTC)
            plan_text = _render_propose_plan_text(state, phase_id)
            # EAWF022 coverage is ADVISORY at propose: the staged waves' dropped
            # planned steps surface in the envelope without blocking the propose.
            coverage_gaps_rows = _collect_coverage_gaps(state, phase_id)
            if not dry_run:
                _append_roadmap_event(
                    state_path,
                    command="roadmap propose",
                    args={
                        "phase_id": phase_id,
                        "from_plan": True,
                        "iter_ids": iter_ids,
                        "wave_ids": wave_ids,
                    },
                    scope_id=phase_id,
                    summary=f"roadmap propose {phase_id} from_plan=True",
                )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    first_iter_id = iter_ids[0] if iter_ids else None
    envelope = {
        "status": "needs_user",
        "decision_kind": "approve_plan",
        "phase_id": phase_id,
        "iter_id": first_iter_id,
        "iter_ids": iter_ids,
        "wave_ids": wave_ids,
        "wave_count": len(wave_ids),
        "title": plan.phase.title,
        "depends_on": list(plan.phase.depends_on),
        "source_brief_ids": list(plan.phase.source_brief_ids),
        "plan_text": plan_text,
        "description": plan.phase.description,
        "coverage_gaps": coverage_gaps_rows,
        "coverage_advisory": True,
        "dry_run": dry_run,
        "options": [
            {"label": "approve", "next": f"eawf roadmap apply {phase_id}"},
            {"label": "revise", "next": f"eawf roadmap revise {phase_id} --add-wave WNN ..."},
            {"label": "drop", "next": f"eawf roadmap drop {phase_id}"},
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
        raise cli_errors.UserError(f"unknown phase {phase_id!r}", kind="NotFound")
    phase = state.phases[phase_id]
    if phase.status not in {PhaseStatus.PLANNED, PhaseStatus.ACTIVE}:
        raise cli_errors.UserError(
            f"phase {phase_id!r} has status {phase.status.value!r}; "
            "revise only works on PLANNED or ACTIVE phases",
            kind="InvalidInput",
        )


def _iter_id_for_phase(state: State, phase_id: str) -> str:
    phase = state.phases[phase_id]
    if not phase.iter_ids:
        raise cli_errors.UserError(
            f"phase {phase_id!r} has no iter; propose should have created P##-I01",
            kind="InvalidInput",
        )
    return phase.iter_ids[0]


@roadmap_app.command("revise")
def roadmap_revise_cmd(
    ctx: typer.Context,
    phase_id: Annotated[
        str,
        typer.Argument(help="Phase id to revise (PLANNED, or ACTIVE for PENDING waves)."),
    ],
    iter_opt: Annotated[
        str | None,
        typer.Option(
            "--iter",
            help=(
                "Iter id (P##-I## or bare I##) the plan edit targets. "
                "Defaults to the phase's first iter (P##-I01) when omitted."
            ),
        ),
    ] = None,
    add_wave: Annotated[
        str | None,
        typer.Option(
            "--add-wave",
            help="Add a wave under the target iter (--iter, else I01); pass the wave id.",
        ),
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
    set_dep_barrier: Annotated[
        str | None,
        typer.Option(
            "--set-dep-barrier",
            help=("Set one dependency threshold. Form: 'W04:W01:integrated:verified' or full ids."),
        ),
    ] = None,
    barrier_reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="Optional rationale for --set-dep-barrier.",
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
    criteria_floor_waiver: Annotated[
        str | None,
        typer.Option(
            "--criteria-floor-waiver",
            help=(
                "Waive the typed-criteria floor for --add-wave with legacy --success "
                "strings; pass a >= 20-char reason. The waiver persists on the wave."
            ),
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
    description: Annotated[
        str | None,
        typer.Option(
            "--description",
            help=(
                "Long-form description (≤500 chars) — applied to the new wave with "
                "--add-wave, or to the retitle target (iter or wave) with --retitle."
            ),
        ),
    ] = None,
    release: Annotated[
        str | None,
        typer.Option(
            "--release",
            help=(
                "Release band (vMAJOR.MINOR.PATCH, e.g. v0.5.0) for a phase retitle "
                "target (--retitle P##). Bands the phase in the rendered roadmap."
            ),
        ),
    ] = None,
    intent_problem: Annotated[
        str | None,
        typer.Option(
            "--intent-problem",
            help=(
                "Required problem statement on the IntentBrief (<=200 chars). "
                "Passing any --intent-* flag activates the brief; both "
                "--intent-problem and --intent-desired-outcome are required "
                "when any --intent-* flag is present."
            ),
        ),
    ] = None,
    intent_desired_outcome: Annotated[
        str | None,
        typer.Option(
            "--intent-desired-outcome",
            help="Required desired-outcome statement on the IntentBrief (<=200 chars).",
        ),
    ] = None,
    intent_priority_rationale: Annotated[
        str | None,
        typer.Option(
            "--intent-priority-rationale",
            help="Optional priority rationale on the IntentBrief (<=1000 chars).",
        ),
    ] = None,
    intent_planned_steps: Annotated[
        str | None,
        typer.Option(
            "--intent-planned-steps",
            help=(
                "Comma-separated planner steps on the IntentBrief "
                "(max 10 entries, each <=500 chars)."
            ),
        ),
    ] = None,
    intent_risks: Annotated[
        str | None,
        typer.Option(
            "--intent-risks",
            help="Comma-separated risks on the IntentBrief (max 10 entries, each <=500 chars).",
        ),
    ] = None,
    intent_evidence_refs: Annotated[
        str | None,
        typer.Option(
            "--intent-evidence-refs",
            help="Comma-separated evidence refs (repo-relative paths / URNs / URLs).",
        ),
    ] = None,
    intent_source_brief_ids: Annotated[
        str | None,
        typer.Option(
            "--intent-source-brief-ids",
            help="Comma-separated originating research / spike brief ids or paths.",
        ),
    ] = None,
) -> None:
    """Edit a PLANNED or ACTIVE phase's wave plan via structured flags.

    For an ACTIVE parent only PENDING waves are mutable — the wave-level
    PENDING check inside the lifecycle transitions rejects edits aimed
    at CLOSED/CLAIMED/IN_PROGRESS waves.

    Any ``--intent-*`` flag activates an :class:`~eawf.kernel.spec.intent.IntentBrief`
    attachment on the target wave (with ``--add-wave`` / ``--retitle``)
    or iter (with ``--retitle`` against an iter id). Both
    ``--intent-problem`` and ``--intent-desired-outcome`` are required
    when any ``--intent-*`` flag is present; the others are optional.
    The brief is additive + replay-safe — entities without an intent
    re-validate unchanged.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.spec.common import grandfather_criterion
    from eawf.kernel.state.models import CriteriaFloorWaiver
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.transitions import (
        LifecycleError,
        edit_iter_plan,
        edit_phase_plan,
        edit_wave_plan,
        plan_wave,
        remove_wave_plan,
        set_wave_deps,
    )

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    selected = [opt for opt in (add_wave, remove_wave, set_deps, set_dep_barrier, retitle) if opt]
    if len(selected) != 1:
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of --add-wave/--remove-wave/--set-deps/"
                "--set-dep-barrier/--retitle must be passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    intent_result = _build_intent_from_flags(
        intent_problem=intent_problem,
        intent_desired_outcome=intent_desired_outcome,
        intent_priority_rationale=intent_priority_rationale,
        intent_planned_steps=intent_planned_steps,
        intent_risks=intent_risks,
        intent_evidence_refs=intent_evidence_refs,
        intent_source_brief_ids=intent_source_brief_ids,
    )
    if intent_result is _INTENT_FLAG_ERROR:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--intent-problem and --intent-desired-outcome are required "
                "when any --intent-* flag is passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    intent = cast("IntentBrief | None", intent_result)

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    if set_dep_barrier:
        from eawf.surfaces.cli import _dispatch
        from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonRpcError

        parts = [part.strip() for part in set_dep_barrier.split(":")]
        if len(parts) != 4 or any(not part for part in parts):
            cli_errors.emit_error(
                cli_errors.UserError(
                    "--set-dep-barrier must use 'DOWNSTREAM:UPSTREAM:START_AFTER:LAND_AFTER'",
                    kind="InvalidInput",
                ),
                flags=flags,
            )
            return
        downstream_ref, upstream_ref, start_raw, land_raw = parts
        try:
            start_after = DependencyStage(start_raw)
            land_after = DependencyStage(land_raw)
            with state_transaction(state_path, read_only=True) as state:
                _resolve_revisable_phase(state, phase_id)
                barrier_iter_id = _resolve_target_iter(state, phase_id, iter_opt)
                downstream_id = _coerce_full_wave_id(
                    state,
                    phase_id,
                    downstream_ref,
                    iter_id=barrier_iter_id,
                )
                upstream_id = _coerce_full_wave_id(
                    state,
                    phase_id,
                    upstream_ref,
                    iter_id=barrier_iter_id,
                )
            _dispatch.escalate_mutation(
                "roadmap revise --set-dep-barrier",
                flags=flags,
            )
            repo_root = str((flags.workspace or Path.cwd()).resolve())
            with DaemonClient() as client:
                result = client.call(
                    "dependency_barrier.set",
                    {
                        "repo_root": repo_root,
                        "wave_id": downstream_id,
                        "dep_wave_id": upstream_id,
                        "start_after": start_after.value,
                        "land_after": land_after.value,
                        "reason": (
                            barrier_reason
                            or "explicit dependency barrier authored via roadmap revise"
                        ),
                    },
                )
        except ValueError as exc:
            cli_errors.emit_error(
                cli_errors.UserError(
                    f"invalid dependency stage: {exc}",
                    kind="InvalidInput",
                ),
                flags=flags,
            )
            return
        except DaemonRpcError as exc:
            if exc.code == cli_errors.RPC_VALIDATION_FAILED:
                err: cli_errors.CliError = cli_errors.ValidationError(exc.message)
            else:
                err = cli_errors.cli_error_for_rpc(exc.code, exc.message)
            cli_errors.emit_error(err, flags=flags)
            return
        except cli_errors.CliError as exc:
            cli_errors.emit_error(exc, flags=flags)
            return
        except (OSError, RuntimeError, TimeoutError) as exc:
            cli_errors.emit_error(
                cli_errors.DaemonUnreachable(
                    f"daemon unavailable for dependency_barrier.set: {exc}"
                ),
                flags=flags,
            )
            return
        action_summary = (
            f"set barrier {downstream_id} <- {upstream_id}: {start_after.value}/{land_after.value}"
        )
        emit_json_or_text(
            {
                "phase_id": phase_id,
                "action": action_summary,
                "status": "ok",
                **result,
            },
            f"roadmap revise {phase_id}: {action_summary}",
            flags=flags,
        )
        return

    action_summary = ""
    try:
        with state_transaction(state_path) as state:
            try:
                # An iter retitle is cosmetic (id preserved, no lifecycle
                # change), so it is status-agnostic and skips the phase gate
                # that guards wave-plan edits — a CLOSED-phase iter title can
                # still be normalized. Wave edits keep the PLANNED/ACTIVE gate.
                is_iter_retitle = bool(retitle and is_iter_id(retitle.partition("=")[0].strip()))
                if not is_iter_retitle:
                    _resolve_revisable_phase(state, phase_id)
                # Resolve the iter the plan edit targets. ``--iter`` lets the
                # operator aim a non-I01 iter; omitted keeps the I01 default.
                # The iter retitle path resolves its own target from the
                # ``--retitle`` left-hand side, so it skips this resolution.
                target_iter_id = (
                    None if is_iter_retitle else _resolve_target_iter(state, phase_id, iter_opt)
                )
                if add_wave:
                    if not wave_title or not files or not effort_bucket:
                        raise cli_errors.UserError(
                            "--add-wave requires --title, --files, and --effort-bucket",
                            kind="InvalidInput",
                        )
                    full_wave_id = _coerce_full_wave_id(
                        state, phase_id, add_wave, iter_id=target_iter_id
                    )
                    role = AgentSessionRole(agent_role) if agent_role else None
                    try:
                        bucket = EffortBucket(effort_bucket)
                    except ValueError as exc:
                        raise cli_errors.UserError(
                            f"invalid effort_bucket: {effort_bucket!r}; "
                            "expected one of XS|S|M|L|XL",
                            kind="InvalidInput",
                        ) from exc
                    plan_wave(
                        state,
                        wave_id=full_wave_id,
                        iter_id=cast("str", target_iter_id),
                        title=wave_title,
                        file_scopes=_split_csv(files),
                        deps=[
                            _coerce_full_wave_id(state, phase_id, d, iter_id=target_iter_id)
                            for d in _split_csv(deps)
                        ],
                        success_criteria=[
                            grandfather_criterion(text, index=idx)
                            for idx, text in enumerate(_split_csv(success), start=1)
                        ],
                        agent_role=role,
                        effort_bucket=bucket,
                        description=description,
                        intent=intent,
                        criteria_floor_waiver=(
                            CriteriaFloorWaiver(
                                reason=criteria_floor_waiver,
                                waived_at=datetime.now(UTC),
                            )
                            if criteria_floor_waiver is not None
                            else None
                        ),
                        waiver_mode=_effective_waiver_mode(
                            state,
                            scope_id=full_wave_id,
                            state_path=state_path,
                        ),
                    )
                    action_summary = f"added wave {full_wave_id}"
                elif remove_wave:
                    full_wave_id = _coerce_full_wave_id(
                        state, phase_id, remove_wave, iter_id=target_iter_id
                    )
                    remove_wave_plan(state, wave_id=full_wave_id)
                    action_summary = f"removed wave {full_wave_id}"
                elif set_deps:
                    target, _, deps_csv = set_deps.partition("=")
                    full_wave_id = _coerce_full_wave_id(
                        state, phase_id, target.strip(), iter_id=target_iter_id
                    )
                    new_deps = [
                        _coerce_full_wave_id(state, phase_id, d, iter_id=target_iter_id)
                        for d in _split_csv(deps_csv)
                    ]
                    set_wave_deps(state, wave_id=full_wave_id, deps=new_deps)
                    action_summary = f"set deps on {full_wave_id}: {new_deps}"
                elif retitle:
                    target, _, new_title = retitle.partition("=")
                    target = target.strip()
                    if is_iter_id(target):
                        edit_iter_plan(
                            state,
                            iter_id=target,
                            title=new_title.strip(),
                            description=description,
                            intent=intent,
                        )
                        action_summary = f"retitled iter {target}: {new_title.strip()!r}"
                    elif is_phase_id(target):
                        # Phase-level metadata edit. An empty right-hand side
                        # leaves the title untouched so a --description-only
                        # phase edit (--retitle P## --description ...) works;
                        # the helper re-validates the title bound otherwise.
                        new_phase_title = new_title.strip() or None
                        edit_phase_plan(
                            state,
                            phase_id=target,
                            title=new_phase_title,
                            description=description,
                            release=release,
                            intent=intent,
                        )
                        action_summary = (
                            f"edited phase {target}: title={new_phase_title!r} release={release!r}"
                        )
                    else:
                        full_wave_id = _coerce_full_wave_id(
                            state, phase_id, target, iter_id=target_iter_id
                        )
                        edit_wave_plan(
                            state,
                            wave_id=full_wave_id,
                            title=new_title.strip(),
                            description=description,
                            intent=intent,
                        )
                        action_summary = f"retitled {full_wave_id}: {new_title.strip()!r}"
            except LifecycleError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            except PydValidationError as exc:
                # The ≤500-char description bound trips on the model, not on
                # the lifecycle guard — translate it to InvalidInput so the
                # CLI surfaces a clean error rather than a Pydantic stack
                # trace.
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            state.updated_at = datetime.now(UTC)
            _append_roadmap_event(
                state_path,
                command="roadmap revise",
                args={
                    "phase_id": phase_id,
                    "add_wave": add_wave,
                    "remove_wave": remove_wave,
                    "set_deps": set_deps,
                    "set_dep_barrier": set_dep_barrier,
                    "retitle": retitle,
                    "release": release,
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


def _resolve_target_iter(state: State, phase_id: str, iter_opt: str | None) -> str:
    """Resolve the iter that ``revise`` plan edits target.

    When *iter_opt* is ``None`` the phase's first iter (``P##-I01``) is
    returned for back-compat with the pre-``--iter`` default. When supplied
    the iter id is validated: it must be a well-formed iter id, exist in
    state, and belong to *phase_id*. A bare ``I##`` suffix is expanded
    against the phase (``P##-I##``).

    Args:
        state: The validated state document holding the ``iters`` map.
        phase_id: The target phase id (assumed present in ``state.phases``).
        iter_opt: The raw ``--iter`` value, or ``None`` for the I01 default.

    Returns:
        The canonical iter id the plan edit should target.

    Raises:
        cli_errors.UserError: when *iter_opt* is malformed, names an iter
            absent from state, or names an iter under a different phase
            (``kind="InvalidInput"`` / ``"NotFound"``).
    """
    if iter_opt is None:
        return _iter_id_for_phase(state, phase_id)
    candidate = iter_opt.strip()
    # Accept the bare ``I##`` form and expand against the phase.
    if not is_iter_id(candidate) and candidate.startswith("I") and candidate[1:].isdigit():
        candidate = f"{phase_id}-{candidate}"
    if not is_iter_id(candidate):
        raise cli_errors.UserError(f"invalid iter id: {iter_opt!r}", kind="InvalidInput")
    it = state.iters.get(candidate)
    if it is None:
        raise cli_errors.UserError(f"unknown iter {candidate!r}", kind="NotFound")
    if it.phase_id != phase_id:
        raise cli_errors.UserError(
            f"iter {candidate!r} belongs to phase {it.phase_id!r}, not {phase_id!r}",
            kind="InvalidInput",
        )
    return candidate


def _coerce_full_wave_id(
    state: State, phase_id: str, candidate: str, *, iter_id: str | None = None
) -> str:
    """Accept either the bare ``W##`` form or the full ``P##-I##-W##`` id.

    Bare ``W##`` is expanded against *iter_id* when supplied, else against
    the phase's first iter (``P##-I01``) for back-compat.
    """
    if is_wave_id(candidate):
        return candidate
    if candidate.startswith("W") and candidate[1:].isdigit():
        target_iter = iter_id if iter_id is not None else _iter_id_for_phase(state, phase_id)
        full = f"{target_iter}-{candidate}"
        if is_wave_id(full):
            return full
    raise cli_errors.UserError(f"invalid wave id reference: {candidate!r}", kind="InvalidInput")


@roadmap_app.command("apply")
def roadmap_apply_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase id to apply.")],
    approve: Annotated[
        bool,
        typer.Option(
            "--approve",
            help="Confirm the rendered wave DAG and finalise apply (emits ok).",
        ),
    ] = False,
) -> None:
    """Confirm a PLANNED phase's wave DAG before handing off to ``/prep``.

    Without ``--approve`` this renders the phase's full wave DAG (the
    PENDING waves with their deps + file scopes) and emits a
    ``needs_user`` envelope carrying an ``approve_plan`` decision so the
    active runtime gates the apply through its native confirm UI (Claude
    plan-mode, Codex text-prompt). With ``--approve`` it validates the
    plan and emits an ``ok`` envelope. Either way the underlying PLANNED
    scope is already persisted by ``propose`` / ``revise``; this verb is
    the confirmation surface, never a state mutation of the wave set.

    Raises:
        UserError: ``phase_id`` is malformed, the phase is not PLANNED,
            or the phase has no waves to apply (``kind="InvalidInput"``);
            or ``phase_id`` is not present in state, or no ``state.json``
            resolves for the workspace (``kind="NotFound"``).
    """
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    dag_text = ""
    pending_waves: list[dict[str, Any]] = []
    coverage_gaps_rows: list[dict[str, Any]] = []
    try:
        # read_only when not approving: rendering the DAG must not trip the
        # mutating-verb gate, and only the approve path appends an EVENT.
        with state_transaction(state_path, read_only=not approve) as state:
            if phase_id not in state.phases:
                raise cli_errors.UserError(f"unknown phase {phase_id!r}", kind="NotFound")
            phase = state.phases[phase_id]
            if phase.status != PhaseStatus.PLANNED:
                raise cli_errors.UserError(
                    f"phase {phase_id!r} has status {phase.status.value!r}; "
                    "only PLANNED phases can be applied",
                    kind="InvalidInput",
                )
            pending_waves = _collect_pending_waves(state, phase_id)
            if not pending_waves:
                raise cli_errors.UserError(
                    f"phase {phase_id!r} has no pending waves; revise --add-wave before apply",
                    kind="InvalidInput",
                )
            coverage_gaps_rows = _collect_coverage_gaps(state, phase_id)
            dag_text = _render_apply_dag_text(state, phase_id, pending_waves)
            if approve:
                # EAWF022 coverage is BLOCKING at apply: a planned step a wave's
                # criteria silently dropped refuses the apply so the gap is
                # closed (or explicitly deferred) before /prep dispatches.
                if coverage_gaps_rows:
                    gap_bodies = "; ".join(
                        f"{row['wave_id']}: {row['uncovered_spans']}" for row in coverage_gaps_rows
                    )
                    raise cli_errors.UserError(
                        f"phase {phase_id!r} has uncovered planned steps (EAWF022): "
                        f"{gap_bodies}; cover or defer them via revise before apply",
                        kind="InvalidInput",
                    )
                state.updated_at = datetime.now(UTC)
                _append_roadmap_event(
                    state_path,
                    command="roadmap apply",
                    args={"phase_id": phase_id, "wave_count": len(pending_waves)},
                    scope_id=phase_id,
                    summary=f"roadmap apply {phase_id} waves={len(pending_waves)}",
                )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    if not approve:
        envelope = {
            "status": "needs_user",
            "decision_kind": "approve_plan",
            "phase_id": phase_id,
            "wave_count": len(pending_waves),
            "waves": pending_waves,
            "plan_text": dag_text,
            "coverage_gaps": coverage_gaps_rows,
            "coverage_advisory": False,
            "options": [
                {"label": "approve", "next": f"eawf roadmap apply {phase_id} --approve"},
                {"label": "revise", "next": f"eawf roadmap revise {phase_id} --add-wave WNN ..."},
                {"label": "cancel", "next": f"eawf roadmap drop {phase_id}"},
            ],
        }
        emit_json_or_text(envelope, dag_text, flags=flags)
        return

    emit_json_or_text(
        {
            "phase_id": phase_id,
            "status": "ok",
            "wave_count": len(pending_waves),
            "next": f"eawf prep {phase_id}",
        },
        f"phase {phase_id} ready for /prep ({len(pending_waves)} waves planned)",
        flags=flags,
    )


@roadmap_app.command("drop")
def roadmap_drop_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="PLANNED phase id to archive.")],
) -> None:
    """Archive a PLANNED phase (PLANNED → ARCHIVED). Irreversible via the
    roadmap surface; recover with ``git restore`` if needed."""
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.transitions import LifecycleError, archive_phase

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    try:
        with state_transaction(state_path) as state:
            try:
                archive_phase(state, phase_id=phase_id)
            except LifecycleError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
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
    from eawf.surfaces.cli._mutation import state_transaction

    flags: GlobalFlags = ctx.obj
    if phase is not None and not is_phase_id(phase):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    try:
        # Read-only view: read_only=True bypasses the §5.5 --daemonless
        # mutating-verb gate so `roadmap show --daemonless` still works.
        with state_transaction(state_path, read_only=True) as state:
            phases = sorted(state.phases.values(), key=lambda p: natural_key(p.id))
            if phase is not None:
                phases = [p for p in phases if p.id == phase]
            nested = [_phase_node(state, p.id, now=datetime.now(UTC)) for p in phases]
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # Markdown branch: delegate to the canonical ``plan_view`` renderer so
    # ``roadmap show --md``, ``/prep`` plan-mode, and the TUI all draw from
    # one projection (P28-W18). The rich/plain branches stay local because
    # they carry presentation chrome (Rich table styling, ANSI dimming) that
    # the markdown surface does not.
    if md:
        from eawf.kernel.config.layered import merge_config
        from eawf.surfaces.render.plan_view import render_roadmap_markdown

        merged_config, _sources = merge_config(workspace=flags.workspace, repo=Path.cwd())
        # Re-enter the read-only transaction so the renderer sees the same
        # state snapshot the rows projection used.
        with state_transaction(state_path, read_only=True) as state:
            text = render_roadmap_markdown(state, phase_id_filter=phase, config=merged_config)
    else:
        text = _render_show_rich(nested, plain=flags.plain_output)
    emit_json_or_text({"phases": nested}, text, flags=flags)


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
    from eawf.workflow.lifecycle.integration import dependency_barrier

    w = state.waves[wave_id]
    barriers: list[dict[str, Any]] = []
    for dep_wave_id in w.deps:
        key = wave_dependency_key(wave_id, dep_wave_id)
        barrier = dependency_barrier(
            state,
            wave_id=wave_id,
            dep_wave_id=dep_wave_id,
        )
        explicit = key in state.wave_dependency_barriers
        barriers.append(
            {
                "dep_wave_id": dep_wave_id,
                "start_after": barrier.start_after.value,
                "land_after": barrier.land_after.value,
                "explicit": explicit,
                "reason": barrier.reason if explicit else None,
            }
        )
    return {
        "id": w.id,
        "iter_id": w.iter_id,
        "status": w.status.value,
        "title": w.title,
        "deps": list(w.deps),
        "dependency_barriers": barriers,
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
            w_deps = (
                ", ".join(
                    f"{row['dep_wave_id']}[{row['start_after']}/{row['land_after']}]"
                    for row in w["dependency_barriers"]
                )
                or "-"
            )
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
                barriers = (
                    ", ".join(
                        f"{row['dep_wave_id']}[{row['start_after']}/{row['land_after']}]"
                        for row in w["dependency_barriers"]
                    )
                    or "-"
                )
                lines.append(
                    f"    wave {w['id']:<13} {w['status']:<12} deps={barriers}  {w['title']}{w_tag}"
                )
    return "\n".join(lines)


def _render_show_md(rows: list[dict[str, Any]]) -> str:
    """Render the roadmap-show markdown table from a row projection.

    Thin wrapper retained for back-compat callers that already have the
    ``_phase_summary`` row dicts in hand. The canonical surface (the
    ``roadmap show --md`` command path) calls
    :func:`eawf.surfaces.render.plan_view.render_roadmap_markdown`
    directly so the renderer lives in one place — this helper just
    mirrors the same row → markdown layout (including release banding)
    so existing tests that pass a dict list keep working.

    Rows are banded by their optional ``release`` key when at least one
    row carries one (``### <version>`` headers, newest first, with an
    ``### Unreleased`` band trailing); otherwise a single unbanded table
    is emitted, matching the pre-banding layout.
    """
    if not rows:
        return "_(no phases in state)_"
    header = ["| Phase | Status | Waves | Depends on | Title |", "|---|---|---|---|---|"]
    if any(row.get("release") is not None for row in rows):
        out: list[str] = []
        for label in _md_band_labels(rows):
            if label == _UNBANDED_MD_LABEL:
                band_rows = [row for row in rows if row.get("release") is None]
            else:
                band_rows = [row for row in rows if row.get("release") == label]
            out.append(f"### {label}")
            out.append("")
            out.extend(header)
            out.extend(_render_show_md_body(band_rows))
            out.append("")
        if out and out[-1] == "":
            out.pop()
        return "\n".join(out)
    out = list(header)
    out.extend(_render_show_md_body(rows))
    return "\n".join(out)


#: Trailing band label for dict-rows that carry no ``release`` key.
_UNBANDED_MD_LABEL = "Unreleased"


def _md_band_labels(rows: list[dict[str, Any]]) -> list[str]:
    """Return ordered release-band labels for dict rows (newest first)."""
    from eawf.surfaces.render.plan_view import _release_sort_key

    releases = {row.get("release") for row in rows if row.get("release") is not None}
    ordered = sorted((cast("str", r) for r in releases), key=_release_sort_key, reverse=True)
    if any(row.get("release") is None for row in rows):
        ordered.append(_UNBANDED_MD_LABEL)
    return ordered


def _render_show_md_body(rows: list[dict[str, Any]]) -> list[str]:
    """Render per-phase markdown body rows (no header) for dict rows."""
    body: list[str] = []
    for row in rows:
        deps = ", ".join(row["depends_on"]) or "—"
        body.append(
            f"| `{row['id']}` | `{row['status']}` | {row['wave_count']} | {deps} | {row['title']} |"
        )
    return body
