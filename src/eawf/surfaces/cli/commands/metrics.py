"""``eawf metrics`` — workflow + telemetry metrics surface.

The bare ``eawf metrics`` invocation renders the estimation roll-up of the
selected generation: how many estimates and actuals its ledgers hold, and
the estimate-actual variance over the subjects that carry both. This view
is read-only. An epoch-1 tree selects no generation, so it is refused as
needing migration.

The C09 telemetry capstone adds four sub-verbs behind the
same ``metrics`` command — selected by a leading positional sub-verb so the
existing single-command registration in :mod:`eawf.surfaces.cli.app` stays intact
(no app-level ``add_typer`` re-wire needed):

- ``eawf metrics show`` — render the rolling telemetry metrics projected
  into the local cache. With ``telemetry.enabled=false`` it prints a
  one-time opt-in nudge and returns cleanly (no projection, no metrics).
- ``eawf metrics export --format prom|json|csv`` — serialise the projected
  metrics through :mod:`eawf.observability.telemetry.exporter` to stdout or ``--out``.
- ``eawf metrics rebuild [--full|--incremental]`` — drive the projector
  (:func:`eawf.observability.telemetry.projector.rebuild`) over the discovered sources,
  then sweeps the local Claude Code session history into the observed
  collection and reports its coverage funnel (seen, parsed, attributed,
  projected) with a typed reason per dropped file.
- ``eawf metrics info`` — print cache stats: DB kind, path, schema +
  pricing version, and row counts.
- ``eawf metrics variance`` — emit the M26
  ``eawf_estimate_actual_variance_pct`` gauge over the selected
  generation's estimate and actual ledgers. Actuals are measured-only; the
  gauge reads "no data" until a subject carries both.
- ``eawf metrics jury-validation`` — render the cross-vendor jury validated
  against its ground-truth cohort (Fleiss kappa / Brier / ECE /
  unanimous-pass-on-known-bad catch rate) built from the selected
  generation's Batch ledger, with the derived BlockAuthority tier. Under the
  cohort floor the reducer refuses to score and the render is the honest
  "insufficient signal (n=k)" banner -- it reads no telemetry cache, so it
  is not gated on ``telemetry.enabled``.
- ``eawf metrics refit --actor KEY`` — re-fit the effort-unit mapping
  through the daemon against the recorded actuals. A move within the
  threshold is stored as the next revision; a larger one files an operator
  decision. It prints whether the re-fit was due, how many actuals it
  excluded and why, and what it stored or asked.
- ``eawf metrics conduct`` — render the conduct deviation rate: deviations
  per completed wave, per obligation (every compiled conduct obligation,
  so a never-breached one reads as zero) and per runtime, read from the
  machine-local deviation store, against the Tasks the selected
  generation's Task ledger records as completed. Not gated on
  ``telemetry.enabled``.

CLI is dispatch (AGENTS rule 1): every handler resolves the state path,
reads the typed config, and routes the heavy lifting into
:mod:`eawf.observability.telemetry`. The shared estimation renderer
lives in :mod:`eawf.surfaces.render.metrics_view`; the telemetry aggregation +
serialisation live in :mod:`eawf.observability.telemetry.exporter` and
:mod:`eawf.observability.telemetry.projector`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.observability.eval.jury_validation import JuryValidationReport
    from eawf.observability.measurement.coverage import PricingCoverage
    from eawf.workflow.estimation.metrics import LedgerMetrics

logger = logging.getLogger(__name__)

_TELEMETRY_SUBCOMMANDS = frozenset({"show", "export", "rebuild", "info", "variance"})

_CONDUCT_SUBCOMMAND = "conduct"

_REFIT_SUBCOMMAND = "refit"

#: The jury-validation sub-verb renders the jury validated against its
#: ground-truth cohort (Fleiss kappa / Brier / ECE / unanimous-pass-on-known-bad
#: catch rate), with an honest "insufficient signal" banner under the cohort
#: floor. It reads no telemetry cache, so it is not gated on ``telemetry.enabled``.
_JURY_VALIDATION_SUBCOMMAND = "jury-validation"

#: The daemon verb ``metrics refit`` forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
_REFIT_METHOD = "runtime.estimation.refit"

_KNOWN_SUBCOMMANDS = _TELEMETRY_SUBCOMMANDS | {
    _JURY_VALIDATION_SUBCOMMAND,
    _CONDUCT_SUBCOMMAND,
    _REFIT_SUBCOMMAND,
}

_OPT_IN_NUDGE = (
    "telemetry is disabled — no metrics are collected.\n"
    "enable it with: eawf config set telemetry.enabled true\n"
    "(strict-local: metrics are projected from local logs only; "
    "nothing is sent off-device)"
)


def metrics_cmd(
    ctx: typer.Context,
    subcommand: Annotated[
        str | None,
        typer.Argument(
            metavar="[show|export|rebuild|info|variance|jury-validation|conduct|refit]",
            help="Metrics sub-verb. Omit for the rolling workflow-metrics view.",
        ),
    ] = None,
    fmt: Annotated[
        str,
        typer.Option("--format", help="Export format for `metrics export` (prom|json|csv)."),
    ] = "prom",
    out: Annotated[
        Path | None,
        typer.Option("--out", help="Write `metrics export` output to a file instead of stdout."),
    ] = None,
    full: Annotated[
        bool,
        typer.Option("--full", help="`metrics rebuild`: re-project every byte of every source."),
    ] = False,
    incremental: Annotated[
        bool,
        typer.Option(
            "--incremental",
            help="`metrics rebuild`: project only the tail appended since the last scan.",
        ),
    ] = False,
    actor: Annotated[
        str | None,
        typer.Option("--actor", help="`metrics refit`: principal key of the person asking."),
    ] = None,
    idempotency_key: Annotated[
        str | None,
        typer.Option(
            "--idempotency-key",
            help="`metrics refit`: the name the request is filed under; defaults to the day.",
        ),
    ] = None,
) -> None:
    """Dispatch the bare workflow-metrics view or a metrics sub-verb.

    Read-only for ``show`` / ``info`` / ``variance`` / the bare view;
    ``refit`` stores a mapping revision or files a decision through the daemon;
    ``export`` may write a file; ``rebuild`` mutates the local telemetry
    cache only (never ``state.json``).

    Raises:
        typer.BadParameter: When *subcommand* is not a recognised sub-verb.
    """
    flags: GlobalFlags = ctx.obj
    if subcommand is None:
        _workflow_metrics(flags)
        return
    if subcommand not in _KNOWN_SUBCOMMANDS:
        raise typer.BadParameter(
            f"unknown metrics sub-verb: {subcommand!r} "
            f"(expected one of {sorted(_KNOWN_SUBCOMMANDS)} or no argument)"
        )
    if subcommand == "show":
        _telemetry_show(flags)
    elif subcommand == "export":
        _telemetry_export(flags, fmt=fmt, out=out)
    elif subcommand == "rebuild":
        _telemetry_rebuild(flags, full=full, incremental=incremental)
    elif subcommand == "variance":
        _estimate_actual_variance(flags)
    elif subcommand == _JURY_VALIDATION_SUBCOMMAND:
        _jury_validation(flags)
    elif subcommand == _CONDUCT_SUBCOMMAND:
        _conduct_deviation_rate(flags)
    elif subcommand == _REFIT_SUBCOMMAND:
        _refit(flags, actor=actor, idempotency_key=idempotency_key)
    else:  # subcommand == "info"
        _telemetry_info(flags)


def _workflow_metrics(flags: GlobalFlags) -> None:
    """Render the selected generation's estimation roll-up.

    Read-only. Failures map to the canonical CLI exit codes: UserError
    (``kind="NotFound"``) when no tree resolves, MigrationRequired on an
    epoch-1 tree.
    """
    from eawf.surfaces.render.metrics_view import render_metrics_plain, render_metrics_table

    metrics = _ledger_metrics_or_emit(flags)
    if metrics is None:
        return
    payload: dict[str, Any] = metrics.model_dump(mode="json")
    text = render_metrics_plain(metrics) if flags.plain_output else render_metrics_table(metrics)
    emit_json_or_text(payload, text, flags=flags)


def _estimate_actual_variance(flags: GlobalFlags) -> None:
    """Emit the M26 estimate-actual variance pct of the selected generation.

    Read-only. Failures map to the canonical CLI exit codes as
    :func:`_workflow_metrics` does.
    """
    metrics = _ledger_metrics_or_emit(flags)
    if metrics is None:
        return
    metric = metrics.variance
    payload: dict[str, Any] = metric.model_dump(mode="json")
    text = _render_variance(metric.variance_pct, metric.sample_count, metric.mapping_revisions)
    emit_json_or_text(payload, text, flags=flags)


def _generation_document_or_emit(flags: GlobalFlags) -> tuple[Path, Path] | None:
    """Return the tree's ``state.json`` slot and its selected generation's document.

    Emits the refusal and returns ``None`` instead when there is none.

    An absent tree is a UserError (``kind="NotFound"``); an epoch-1 tree
    selects no generation, so it is refused as needing migration.
    """
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import resolve_authority
    from eawf.surfaces.cli.flag_day_gate import MIGRATION_REQUIRED_KIND, MIGRATION_VERB

    state_path = _resolve_state_or_emit(flags)
    if state_path is None:
        return None
    if not state_path.parent.is_dir():
        cli_errors.emit_error(
            cli_errors.UserError(f"no eawf tree at {state_path.parent}", kind="NotFound"),
            flags=flags,
        )
        return None
    authority = resolve_authority(state_path.parent)
    if authority.target is None or authority.generation_id is None:
        cli_errors.emit_error(
            cli_errors.MigrationRequired(
                f"{state_path.parent} is an epoch-1 tree and metrics read only an epoch-2 "
                f"generation; migrate it with `{MIGRATION_VERB} --plan` then `--apply`",
                kind=MIGRATION_REQUIRED_KIND,
            ),
            flags=flags,
        )
        return None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return state_path, document


def _ledger_metrics_or_emit(flags: GlobalFlags) -> LedgerMetrics | None:
    """Roll up the selected generation's estimate and actual ledgers, or emit why not."""
    from eawf.kernel.store.ledger import read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection
    from eawf.workflow.estimation.metrics import compute_ledger_metrics

    resolved = _generation_document_or_emit(flags)
    if resolved is None:
        return None
    _state_path, document = resolved
    return compute_ledger_metrics(
        read_ledger_records(ledger_path(document, Epoch2Collection.ESTIMATE)),
        read_ledger_records(ledger_path(document, Epoch2Collection.ACTUAL)),
    )


def _render_variance(
    variance_pct: float | None, sample_count: int, mapping_revisions: list[str]
) -> str:
    """Render the M26 variance gauge as a one-line ship-gate summary.

    A roll-up whose estimates span more than one mapping revision names them.
    """
    if variance_pct is None:
        return f"estimate-actual variance: no data (samples={sample_count})"
    sign = "+" if variance_pct >= 0 else ""
    line = f"estimate-actual variance: {sign}{variance_pct:.1f}% (samples={sample_count})"
    if len(mapping_revisions) > 1:
        line += f" across mapping revisions {', '.join(mapping_revisions)}"
    return line


def _refit(flags: GlobalFlags, *, actor: str | None, idempotency_key: str | None) -> None:
    """Re-fit the effort-unit mapping through the daemon and print what it did.

    The daemon stores a revision the threshold admits and files an operator
    decision for a larger move. Without ``--idempotency-key`` the request is
    keyed by the day, so a re-run the same day answers with what the first
    stored. Failures map to the canonical CLI exit codes: UserError
    (``exit=1``) without ``--actor``, and the daemon's own refusal otherwise.
    """
    from datetime import UTC, datetime

    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer
    from eawf.surfaces.render.metrics_view import render_refit_answer
    from eawf.workflow.estimation.mapping_revisions import RefitAnswer

    if actor is None:
        cli_errors.emit_error(
            cli_errors.UserError("metrics refit needs --actor, the person asking"), flags=flags
        )
        return
    key = idempotency_key or f"refit-{datetime.now(UTC):%Y-%m-%d}"
    try:
        raw = _native_answer(
            _REFIT_METHOD,
            {"actor": actor, "idempotency_key": key},
            flags=flags,
            verb_text="metrics refit",
        )
    except DaemonRpcError as exc:
        cli_errors.emit_error(cli_errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    answer = RefitAnswer.model_validate(raw)
    emit_json_or_text(answer.model_dump(mode="json"), render_refit_answer(answer), flags=flags)


#: The Wilson lower-bound floor the jury's pass-fraction forecast must clear
#: before the cross-vendor jury earns BLOCK authority. Mirrors the Trust-mode
#: ``JURY_WILSON_FLOOR`` so the CLI and the TUI agree on the same bar -- the
#: jury is held ADVISORY (veto logged, close proceeds) until it scores above it.
_JURY_WILSON_FLOOR: float = 0.75

#: BlockAuthority tier labels. ``advisory`` is the honest current state: the
#: jury's veto is logged but the close still proceeds. ``blocking`` is earned
#: only once the validation cohort scored and its catch-rate cleared the floor.
_AUTHORITY_ADVISORY: str = "advisory"
_AUTHORITY_BLOCKING: str = "blocking"


def _jury_validation(flags: GlobalFlags) -> None:
    """Render the jury validated against its native ground-truth cohort.

    Read-only -- builds the labelled cohort from the selected generation's
    Batch ledger (every verdict a verification cycle held, joined to its
    subject's observed outcome or a principal's gold label), scores the jury
    against it, and renders the Fleiss kappa / Brier / ECE /
    unanimous-pass-on-known-bad catch rate plus the cohort ``n`` and the
    derived BlockAuthority tier. Under the cohort floor the reducer refuses to
    score, and the render is the honest "insufficient signal (n=k)" banner --
    never a fabricated number. Failures map to the canonical CLI exit codes
    as :func:`_workflow_metrics` does.
    """
    from eawf.kernel.store.compaction import read_document
    from eawf.kernel.store.ledger import read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection
    from eawf.observability.eval.jury_validation import validate_jury
    from eawf.runtime.daemon.verdict_observations import native_jury_cohort

    resolved = _generation_document_or_emit(flags)
    if resolved is None:
        return
    _state_path, document = resolved
    cohort, ballots = native_jury_cohort(
        document,
        read_document(document),
        batch_lines=read_ledger_records(ledger_path(document, Epoch2Collection.BATCH)),
    )
    report = validate_jury(cohort, ballots)
    payload = report.model_dump(mode="json")
    payload["block_authority"] = _block_authority(report)
    emit_json_or_text(payload, _render_jury_validation(report), flags=flags)


def _conduct_deviation_rate(flags: GlobalFlags) -> None:
    """Render the conduct deviation rate from the machine-local store.

    Read-only. A completed task is a Task the selected generation's Task
    ledger records as COMPLETED. Failures map to the canonical CLI exit
    codes as :func:`_workflow_metrics` does.
    """
    from eawf.kernel.state.epoch2.task_status import TaskStatus
    from eawf.kernel.store.ledger import effective_records, read_ledger_records
    from eawf.kernel.store.paths import ledger_path
    from eawf.kernel.store.tiers import Epoch2Collection
    from eawf.platform.rules.conduct import (
        conduct_deviation_rate,
        conduct_obligation_ids,
        read_conduct_deviations,
    )

    resolved = _generation_document_or_emit(flags)
    if resolved is None:
        return
    state_path, document = resolved
    tasks = effective_records(read_ledger_records(ledger_path(document, Epoch2Collection.TASK)))
    rate = conduct_deviation_rate(
        read_conduct_deviations(state_path),
        obligations=conduct_obligation_ids(),
        completed_task_count=sum(1 for line in tasks if line.status == TaskStatus.COMPLETED),
    )
    per_task = (
        "no completed tasks"
        if rate.rate_per_completed_task is None
        else f"{rate.rate_per_completed_task:.3f} per completed task"
    )
    lines = [
        f"conduct deviations: {rate.deviation_count} ({per_task}, "
        f"tasks={rate.completed_task_count})",
        *(f"  {name}: {count}" for name, count in rate.by_obligation.items() if count),
        f"  never breached: {sum(1 for count in rate.by_obligation.values() if not count)} "
        f"of {len(rate.by_obligation)} obligations",
        *(f"  runtime {name}: {count}" for name, count in rate.by_runtime.items()),
    ]
    emit_json_or_text(rate.model_dump(mode="json"), "\n".join(lines), flags=flags)


def _block_authority(report: JuryValidationReport) -> str:
    """Derive the jury's BlockAuthority tier from a validation report.

    The jury is held ADVISORY (its veto is logged, the close still proceeds)
    until the validation cohort both scores AND its known-bad catch rate clears
    the Wilson floor -- the false-clean rate must stay at or below the
    complement of :data:`_JURY_WILSON_FLOOR`. A starved (insufficient) cohort,
    or one whose unanimous-pass-on-known-bad rate is too high, stays ADVISORY;
    only a scored cohort that caught its known-bad waves earns BLOCKING.

    Args:
        report: The jury-validation report.

    Returns:
        :data:`_AUTHORITY_BLOCKING` when the cohort scored and its catch rate
        cleared the floor, else :data:`_AUTHORITY_ADVISORY`.
    """
    from eawf.observability.eval.jury_validation import JuryValidationStatus

    if report.status is not JuryValidationStatus.SCORED:
        return _AUTHORITY_ADVISORY
    false_clean = report.unanimous_pass_on_known_bad_rate
    if false_clean is not None and false_clean > 1.0 - _JURY_WILSON_FLOOR:
        return _AUTHORITY_ADVISORY
    return _AUTHORITY_BLOCKING


def _fmt_metric(value: float | None, *, suffix: str = "") -> str:
    """Render one validation metric, or the honest dash when it refused to score."""
    if value is None:
        return "--"
    return f"{value:.3f}{suffix}"


def _render_jury_validation(report: JuryValidationReport) -> str:
    """Render the jury-validation report as a plain multi-line summary.

    A scored cohort renders the Fleiss kappa, Brier, ECE, and
    unanimous-pass-on-known-bad catch rate, the cohort ``n``, and the derived
    BlockAuthority tier. A cohort under the floor renders the honest
    "insufficient signal (n=k)" banner: the metric lines stay dashed and the
    authority stays advisory, never a fabricated number.

    Args:
        report: The jury-validation report.

    Returns:
        The human-readable multi-line body.
    """
    from eawf.observability.eval.jury_validation import JuryValidationStatus

    authority = _block_authority(report)
    lines = [
        f"jury validation: {report.status.value} (n={report.n})",
    ]
    if report.status is JuryValidationStatus.INSUFFICIENT:
        lines.append(f"insufficient signal (n={report.n}) -- no metric is scored yet")
    catch_rate = report.unanimous_pass_on_known_bad_rate
    lines.extend(
        [
            f"  fleiss kappa     {_fmt_metric(report.fleiss_kappa)}",
            f"  brier            {_fmt_metric(report.brier)}",
            f"  ece              {_fmt_metric(report.ece)}",
            f"  catch (false-clean) {_fmt_metric(catch_rate)} over {report.known_bad_n} known-bad",
            f"  block authority  {authority}",
        ]
    )
    return "\n".join(lines)


def _resolve_state_or_emit(flags: GlobalFlags) -> Path | None:
    """Resolve the state path or emit a UserError (``kind="NotFound"``) and return ``None``."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return None


def _scope_label(state_path: Path) -> str:
    """Derive the scope label for metric rows from the project directory.

    Uses ``repo/<dir>`` where ``<dir>`` is the project root (the parent of
    ``.ea``). This mirrors the C09 §5.9.5 ``scope="repo/eawf"`` label form
    without leaking an absolute path.
    """
    repo_root = state_path.parent.parent
    return f"repo/{repo_root.name}"


def _read_telemetry_config(state_path: Path) -> tuple[bool, str]:
    """Return ``(enabled, db_kind)`` from the merged layered config.

    Args:
        state_path: The resolved ``.ea/state.json`` path.

    Returns:
        ``(telemetry.enabled, telemetry.db_kind)`` resolved through the
        layered config merge anchored at the project repo root.
    """
    from eawf.kernel.config.layered import get_dotted, merge_config

    repo_root = state_path.parent.parent
    merged, _sources = merge_config(repo=repo_root)
    enabled = bool(get_dotted(merged, "telemetry.enabled"))
    db_kind = str(get_dotted(merged, "telemetry.db_kind"))
    return enabled, db_kind


def _gate_on_opt_in(flags: GlobalFlags) -> tuple[Path, str] | None:
    """Resolve the state path and gate the caller on ``telemetry.enabled``.

    Mirrors the opt-in guard that every store-touching sub-verb shares: the
    opt-in invariant says nothing should collect when telemetry is off, so a
    disabled flag must short-circuit *before* the store is opened (opening it
    via ``init_schema`` would create ``telemetry.db`` as a side effect — a
    silent collection the operator never consented to).

    Args:
        flags: The resolved global CLI flags.

    Returns:
        ``(state_path, db_kind)`` when telemetry is enabled, or ``None`` when
        the state path could not be resolved or telemetry is disabled. In the
        disabled case the opt-in nudge is emitted before returning so the
        caller can simply ``return`` on ``None``.
    """
    state_path = _resolve_state_or_emit(flags)
    if state_path is None:
        return None

    enabled, db_kind = _read_telemetry_config(state_path)
    if not enabled:
        emit_json_or_text(
            {"telemetry_enabled": False, "nudge": _OPT_IN_NUDGE},
            _OPT_IN_NUDGE,
            flags=flags,
        )
        return None
    return state_path, db_kind


def _telemetry_show(flags: GlobalFlags) -> None:
    """Render the rolling telemetry metrics, or the opt-in nudge when disabled."""
    from eawf.observability.telemetry.exporter import build_snapshot, render_prom
    from eawf.observability.telemetry.store import metrics_db_path, open_store

    gated = _gate_on_opt_in(flags)
    if gated is None:
        return
    state_path, db_kind = gated

    db_path = metrics_db_path(state_path)
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        store.init_schema()
        snapshot = build_snapshot(store, scope=_scope_label(state_path))
    finally:
        store.close()
    emit_json_or_text(snapshot.model_dump(mode="json"), render_prom(snapshot), flags=flags)


def _telemetry_export(flags: GlobalFlags, *, fmt: str, out: Path | None) -> None:
    """Serialise the projected metrics in *fmt* to stdout or *out*."""
    from eawf.observability.telemetry.exporter import build_snapshot, render
    from eawf.observability.telemetry.store import metrics_db_path, open_store

    if fmt not in ("prom", "json", "csv"):
        raise typer.BadParameter(f"unknown export format: {fmt!r} (expected prom|json|csv)")

    gated = _gate_on_opt_in(flags)
    if gated is None:
        return
    state_path, db_kind = gated

    db_path = metrics_db_path(state_path)
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        store.init_schema()
        snapshot = build_snapshot(store, scope=_scope_label(state_path))
    finally:
        store.close()

    body = render(snapshot, fmt=fmt)  # type: ignore[arg-type]
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(body, encoding="utf-8")
        logger.info(f"_telemetry_export format={fmt!r} out={str(out)!r} bytes={len(body)}")
        typer.echo(f"wrote {fmt} metrics to {out}")
        return
    typer.echo(body, nl=False)


def _telemetry_rebuild(flags: GlobalFlags, *, full: bool, incremental: bool) -> None:
    """Drive the projector over the discovered sources into the local cache."""
    from eawf.observability.measurement.coverage import pricing_coverage
    from eawf.observability.telemetry.projector import (
        RebuildMode,
        SourceSpec,
        rebuild,
        sweep_session_history,
    )
    from eawf.observability.telemetry.sources.event_jsonl import EventJsonlSource
    from eawf.observability.telemetry.sources.session_history import claude_history_root
    from eawf.observability.telemetry.store import metrics_db_path, open_store

    if full and incremental:
        raise typer.BadParameter("pass only one of --full / --incremental")
    mode = RebuildMode.INCREMENTAL if incremental else RebuildMode.FULL

    gated = _gate_on_opt_in(flags)
    if gated is None:
        return
    state_path, db_kind = gated

    db_path = metrics_db_path(state_path)
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        store.init_schema()
        spec = SourceSpec(
            source=EventJsonlSource(),
            root=state_path,
            project_id=_scope_label(state_path),
        )
        report = rebuild(store, [spec], mode=mode)
        funnel = sweep_session_history(
            store,
            claude_history_root(state_path.parent.parent.resolve()),
            project_id=_scope_label(state_path),
        )
    finally:
        store.close()

    pricing = pricing_coverage(funnel.sessions)
    payload = {
        "mode": mode.value,
        "sessions": report.sessions,
        "incidents": report.incidents,
        "files_scanned": report.files_scanned,
        "files_skipped": report.files_skipped,
        "session_history": {
            "seen": funnel.seen,
            "parsed": funnel.parsed,
            "attributed": funnel.attributed,
            "projected": funnel.projected,
            "durations": funnel.durations,
            "drops": [
                {"file": drop.file_name, "stage": drop.stage.value, "reason": drop.reason.value}
                for drop in funnel.drops
            ],
            "pricing": pricing.model_dump(mode="json"),
        },
    }
    text = (
        f"rebuild {mode.value}: sessions={report.sessions} incidents={report.incidents} "
        f"scanned={report.files_scanned} skipped={report.files_skipped}\n"
        f"session history: seen={funnel.seen} parsed={funnel.parsed} "
        f"attributed={funnel.attributed} projected={funnel.projected}"
    )
    for drop in funnel.drops:
        text += f"\n  dropped {drop.file_name} before {drop.stage.value}: {drop.reason.value}"
    text += "\n" + _render_pricing(pricing)
    emit_json_or_text(payload, text, flags=flags)


def _render_pricing(pricing: PricingCoverage) -> str:
    """Render token-weighted pricing coverage, an undefined share as unavailable."""
    if not pricing.total_tokens:
        return "pricing coverage: unavailable (no tokens swept)"
    shares = " ".join(
        f"{source}={share:.1%}"
        for source, share in pricing.share_by_source.items()
        if share is not None
    )
    causes = ", ".join(
        f"{cause} {tokens}" for cause, tokens in sorted(pricing.unpriced_tokens_by_cause.items())
    )
    line = f"pricing coverage over {pricing.total_tokens} tokens: {shares}"
    return f"{line}; unpriced by cause: {causes}" if causes else line


def _telemetry_info(flags: GlobalFlags) -> None:
    """Print cache stats: DB kind, path, schema + pricing version, row counts."""
    from eawf.observability.telemetry.models import TelemetryIncident, TelemetrySession
    from eawf.observability.telemetry.pricing import PRICING_VERSION
    from eawf.observability.telemetry.store import metrics_db_path, open_store
    from eawf.observability.telemetry.store.base import SCHEMA_VERSION

    gated = _gate_on_opt_in(flags)
    if gated is None:
        return
    state_path, db_kind = gated

    db_path = metrics_db_path(state_path)
    exists = db_path.exists()
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        store.init_schema()
        session_count = len(store.fetch_all("telemetry_sessions", TelemetrySession))
        incident_count = len(store.fetch_all("telemetry_incidents", TelemetryIncident))
        size_bytes = db_path.stat().st_size if db_path.exists() else 0
    finally:
        store.close()

    payload = {
        "db_kind": store.backend,
        "db_path": str(db_path),
        "db_existed": exists,
        "db_size_bytes": size_bytes,
        "schema_version": SCHEMA_VERSION,
        "pricing_version": PRICING_VERSION,
        "telemetry_enabled": True,
        "session_rows": session_count,
        "incident_rows": incident_count,
    }
    text = (
        f"telemetry cache: backend={store.backend} path={db_path}\n"
        f"  schema_version={SCHEMA_VERSION} pricing_version={PRICING_VERSION} "
        f"enabled=True\n"
        f"  rows: sessions={session_count} incidents={incident_count} "
        f"size={size_bytes}B"
    )
    emit_json_or_text(payload, text, flags=flags)


__all__ = ["metrics_cmd"]
