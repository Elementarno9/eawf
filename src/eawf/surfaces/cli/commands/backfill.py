"""``eawf backfill`` Typer sub-app — generalized entity title backfill.

CLI dispatch only (AGENTS rule 1): the handler parses args, resolves the
``state.json`` path, and delegates the migration-transform machinery to
:mod:`eawf.platform.lint.tools.title_backfill`. The original surface
(``eawf backlog backfill-titles``) swept only the backlog; this command
generalizes the same safe mechanism to all five lifecycle / decision kinds
(phase / iter / wave / backlog / decision).

Verbs:

- ``eawf backfill titles`` — dry-run (default): print the proposed
  title diffs and the style-lint sweep without mutating state. This IS the
  batch operator-approval surface.
- ``eawf backfill titles --apply`` — persist the normalized titles through
  the daemon-backed state transaction (re-validating each mutated entity).
- ``eawf backfill titles --kind wave --kind decision`` — restrict the sweep
  to a subset of kinds (repeatable); the default sweeps all five.

The write routes through the same ``state_transaction`` chokepoint every
mutating evidence verb uses (AGENTS rule 4): the daemon owns the canonical
write, with the ``portalocker`` direct-write fallback under the V1 carve-out.
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

# The five backfillable entity kinds, as plain strings. The canonical typed
# tuple is ``title_backfill.ENTITY_KINDS``, but importing it at module level
# would pull the heavy ``eawf.platform.profiles`` chain (jinja2 / yaml) into
# the CLI tree-build path and breach the import-budget gate. The library
# re-validates the kinds regardless, and a unit test asserts this list stays
# in lockstep with ``ENTITY_KINDS`` so the two cannot drift.
_KIND_NAMES: tuple[str, ...] = ("phase", "iter", "wave", "backlog", "decision")


backfill_app = typer.Typer(
    name="backfill",
    help="Backfill entity titles/descriptions across all five kinds.",
    no_args_is_help=True,
    add_completion=False,
)


def _flags(ctx: typer.Context) -> GlobalFlags:
    """Return the resolved :class:`GlobalFlags` from the Typer context."""
    flags = ctx.obj
    if not isinstance(flags, GlobalFlags):
        flags = GlobalFlags()
    return flags


def _state_path(flags: GlobalFlags) -> Path:
    """Resolve the state path or raise :class:`UserError` (``kind="NotFound"``)."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc


def _resolve_kind_filter(kinds: list[str] | None) -> tuple[str, ...] | None:
    """Return the requested kind subset, or ``None`` to sweep all five.

    Args:
        kinds: Repeatable ``--kind`` values from the CLI, or ``None`` / empty
            when the operator passed no filter.

    Returns:
        The requested kinds in canonical :data:`_KIND_NAMES` order, or
        ``None`` when no filter was given.

    Raises:
        UserError: when *kinds* names a value outside :data:`_KIND_NAMES`
            (``kind="InvalidInput"``).
    """
    if not kinds:
        return None
    valid = set(_KIND_NAMES)
    unknown = [k for k in kinds if k not in valid]
    if unknown:
        allowed = ", ".join(_KIND_NAMES)
        raise cli_errors.UserError(
            f"unknown --kind value(s): {', '.join(sorted(set(unknown)))} (allowed: {allowed})",
            kind="InvalidInput",
        )
    requested = set(kinds)
    return tuple(kind for kind in _KIND_NAMES if kind in requested)


def _wave_intent_payload(report: Any) -> dict[str, Any]:
    """Return JSON-friendly report payload for ``backfill wave-intents``."""
    return {
        "apply": report.apply,
        "changed": list(report.changed_wave_ids),
        "pending": list(report.pending_wave_ids),
        "skipped": list(report.skipped_wave_ids),
        "rows": [
            {
                "wave_id": row.wave_id,
                "changed": row.changed,
                "would_change": row.would_change,
                "reason": row.reason,
            }
            for row in report.rows
        ],
    }


def _wave_intent_text(payload: dict[str, Any], *, mode: str) -> str:
    """Render concise operator text for ``backfill wave-intents``."""
    lines = [
        (
            f"backfill wave-intents {mode}: "
            f"{len(payload['changed'])} changed, "
            f"{len(payload['pending'])} pending, "
            f"{len(payload['skipped'])} skipped"
        )
    ]
    for row in payload["rows"]:
        marker = "changed" if row["changed"] else "would-change" if row["would_change"] else "skip"
        lines.append(f"  {row['wave_id']}: {marker} ({row['reason']})")
    return "\n".join(lines)


def _append_wave_intent_event(
    state_path: Path,
    *,
    changed_wave_ids: list[str],
) -> None:
    """Append an audit event for a wave-intent backfill apply pass."""
    from eawf.kernel.state.enums import StoreKind
    from eawf.workflow.evidence._io import append_jsonl, event_envelope, store_paths

    now = datetime.now(UTC)
    event = event_envelope(
        event_id=f"EV-{uuid.uuid4().hex[:12]}",
        scope_id=",".join(changed_wave_ids),
        event_type="backfill.wave_intents",
        actor="cli",
        command="backfill wave-intents",
        args={"changed_wave_ids": changed_wave_ids, "timestamp": now.isoformat()},
        summary=f"backfill wave-intents changed={len(changed_wave_ids)}",
        message=f"backfilled wave intents: {', '.join(changed_wave_ids)}",
    )
    append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)


@backfill_app.command("titles")
def backfill_titles(
    ctx: typer.Context,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help=(
                "Persist normalized titles through the daemon-backed state "
                "transaction. Default --dry-run prints the proposed title "
                "diffs and the title style-lint sweep without mutating state."
            ),
        ),
    ] = False,
    kind: Annotated[
        list[str] | None,
        typer.Option(
            "--kind",
            help=(
                "Restrict the sweep to a subset of kinds (repeatable: "
                "phase / iter / wave / backlog / decision). Default: all five."
            ),
        ),
    ] = None,
) -> None:
    """Sweep + normalize entity titles across all five kinds.

    The default ``--dry-run`` mode IS the batch operator-approval surface: it
    walks every phase / iter / wave / backlog / decision, runs the entity-title
    style-lint, and prints the title each entity *would* get (strip a
    conventional-commit prefix off wave titles, collapse cluster-code soup,
    strip a trailing period, trim an over-cap title to a word boundary, derive
    a candidate from the description when the title is an empty placeholder)
    without touching state. ``--apply`` persists the normalized titles through
    the same state transaction the lifecycle edit verbs use, re-validating each
    mutated entity through its typed model.

    Terminal-status entities (a closed wave, a closed / abandoned iter, a
    superseded / reversed / obsolete decision, a closed backlog item) are
    reported but never mutated.
    """
    from eawf.kernel.state.enums import StoreKind
    from eawf.platform.lint.tools import title_backfill
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence._io import append_jsonl, load_state, store_paths

    flags = _flags(ctx)

    try:
        kinds = _resolve_kind_filter(kind)
        state_path = _state_path(flags)
        if apply:
            with state_transaction(state_path) as state:
                report, event = title_backfill.backfill_entity_titles(
                    state, apply=True, kinds=kinds
                )
                if event is not None:
                    append_jsonl(store_paths(state_path)[StoreKind.EVENT], event)
        else:
            state = load_state(state_path)
            report, _ = title_backfill.backfill_entity_titles(state, apply=False, kinds=kinds)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload: dict[str, Any] = {
        "applied": report.applied,
        "total": report.total,
        "changed": report.changed,
        "violations": report.violations,
        "rows": [
            {
                "kind": row.kind,
                "entity_id": row.entity_id,
                "before": row.before,
                "after": row.after,
                "changed": row.changed,
                "frozen": row.frozen,
                "violations": row.violations,
            }
            for row in report.rows
        ],
    }
    changed_lines = [
        f"  [{row.kind}] {row.entity_id}: {row.before!r} -> {row.after!r}"
        for row in report.rows
        if row.changed
    ]
    violation_lines = [
        f"  [{row.kind}] {row.entity_id}: {v}" for row in report.rows for v in row.violations
    ]
    mode = "applied" if report.applied else "dry-run"
    headline = (
        f"backfill titles {mode}: {report.total} entities, "
        f"{report.changed} title change(s), {report.violations} lint violation(s)"
    )
    body = "\n".join([headline, *changed_lines, *violation_lines])
    emit_json_or_text(payload, body, flags=flags)


@backfill_app.command("wave-intents")
def backfill_wave_intents(
    ctx: typer.Context,
    wave: Annotated[
        list[str] | None,
        typer.Option(
            "--wave",
            help=(
                "Explicit closed synced wave id to inspect or repair. Repeat for "
                "multiple waves; no implicit all-state scan is performed."
            ),
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply/--dry-run",
            help="Persist missing IntentBriefs. Default --dry-run only reports.",
        ),
    ] = False,
    check: Annotated[
        bool,
        typer.Option(
            "--check",
            help="Exit non-zero when any target would need repair or is not repairable.",
        ),
    ] = False,
) -> None:
    """Backfill missing ``Wave.intent`` on explicit closed synced waves.

    This is a recovery surface for stale-daemon drift: a closed wave that
    already has typed criteria + gates from ``spec.sync`` but still has
    ``intent=None`` can be repaired without reopening the wave. It deliberately
    requires one or more ``--wave`` values so old historical rows are never
    swept by accident.
    """
    from eawf.kernel.state.migration import backfill_missing_wave_intents
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.evidence._io import load_state

    flags = _flags(ctx)
    wave_ids = list(wave or [])
    if not wave_ids:
        cli_errors.emit_error(
            cli_errors.UserError(
                "--wave is required for wave-intents backfill", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    try:
        state_path = _state_path(flags)
        if apply:
            with state_transaction(state_path) as state:
                report = backfill_missing_wave_intents(state, wave_ids=wave_ids, apply=True)
                if report.changed:
                    state.updated_at = datetime.now(UTC)
                    _append_wave_intent_event(
                        state_path,
                        changed_wave_ids=list(report.changed_wave_ids),
                    )
        else:
            state = load_state(state_path)
            report = backfill_missing_wave_intents(state, wave_ids=wave_ids, apply=False)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = _wave_intent_payload(report)
    mode = "apply" if apply else "check" if check else "dry-run"
    emit_json_or_text(payload, _wave_intent_text(payload, mode=mode), flags=flags)
    if check:
        bad_rows = [
            row
            for row in report.rows
            if row.would_change or row.reason not in {"already_has_intent"}
        ]
        if bad_rows:
            raise typer.Exit(code=1)


__all__ = ["backfill_app"]
