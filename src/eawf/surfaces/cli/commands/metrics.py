"""``eawf metrics`` — workflow + telemetry metrics surface.

The bare ``eawf metrics`` invocation renders the rolling **workflow**
metrics (estimation calibration) from ``state.json`` — EU variance, audit
pass rate, wave elapsed, and the planned-vs-reactive split. This is the
read-only estimation view shipped in P20-W08.

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
  (:func:`eawf.observability.telemetry.projector.rebuild`) over the discovered sources.
- ``eawf metrics info`` — print cache stats: DB kind, path, schema +
  pricing version, and row counts.
- ``eawf metrics variance`` — emit the C09 §5.9.6 M26
  ``eawf_estimate_actual_variance_pct`` gauge from ``state.json`` and feed
  the ship-gate Variance section + the C06 VarianceTile. Actuals are
  measured-only (manual ``eawf actual start/stop`` segments now, per-wave
  token accounting in v0.4); the variance gauge reads the empty state
  until a measured actual exists. The old wall-clock auto-record +
  ``backfill-actuals`` derivation were retired in P27-I05-W28.
- ``eawf metrics jury-validation`` — render the cross-vendor jury validated
  against its ground-truth cohort (Fleiss kappa / Brier / ECE /
  unanimous-pass-on-known-bad catch rate) from ``state.json`` plus the
  append-only gold-label + AUDITOR stores under it, with the derived
  BlockAuthority tier. Under the cohort floor the reducer refuses to score
  and the render is the honest "insufficient signal (n=k)" banner -- it reads
  no telemetry cache, so it is not gated on ``telemetry.enabled``.

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

logger = logging.getLogger(__name__)

_TELEMETRY_SUBCOMMANDS = frozenset({"show", "export", "rebuild", "info", "variance"})

#: The jury-validation sub-verb renders the jury validated against its
#: ground-truth cohort (Fleiss kappa / Brier / ECE / unanimous-pass-on-known-bad
#: catch rate), with an honest "insufficient signal" banner under the cohort
#: floor. It reads no telemetry cache, so it is not gated on ``telemetry.enabled``.
_JURY_VALIDATION_SUBCOMMAND = "jury-validation"

_KNOWN_SUBCOMMANDS = _TELEMETRY_SUBCOMMANDS | {_JURY_VALIDATION_SUBCOMMAND}

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
            metavar="[show|export|rebuild|info|variance|jury-validation]",
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
) -> None:
    """Dispatch the bare workflow-metrics view or a metrics sub-verb.

    Read-only for ``show`` / ``info`` / ``variance`` / the bare view;
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
    else:  # subcommand == "info"
        _telemetry_info(flags)


def _workflow_metrics(flags: GlobalFlags) -> None:
    """Render the rolling estimation metrics from ``state.json``.

    Read-only — does not acquire a lock, append events, or mutate
    ``state.json``. Failures map to the canonical CLI exit codes:

    - :class:`~eawf.surfaces.cli.errors.UserError` (``kind="NotFound"``, ``exit=1``)
      when no ``.ea/state.json`` is locatable from the cwd / ``-w`` /
      ``EA_STATE`` precedence chain.
    - :class:`~eawf.surfaces.cli.errors.ValidationError` (``exit=2``) when the
      on-disk payload fails strict schema validation.
    """
    from eawf.surfaces.render.metrics_view import render_metrics_plain, render_metrics_table
    from eawf.workflow.estimation.metrics import compute_metrics
    from eawf.workflow.evidence._io import load_state

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    summary = compute_metrics(state)
    payload: dict[str, Any] = summary.model_dump(mode="json")
    text = render_metrics_plain(summary) if flags.plain_output else render_metrics_table(summary)
    emit_json_or_text(payload, text, flags=flags)


def _estimate_actual_variance(flags: GlobalFlags) -> None:
    """Emit the M26 estimate-actual variance pct from ``state.json``.

    Read-only — computes the C09 §5.9.6 M26 gauge over CLOSED waves with
    both an estimate and an actual and feeds the ship-gate Variance section
    + the C06 VarianceTile. Failures map to the canonical CLI exit codes:
    UserError (``kind="NotFound"``, ``exit=1``) when no ``state.json``
    resolves, ValidationError (``exit=2``) on a schema mismatch.
    """
    from eawf.workflow.estimation.metrics import compute_estimate_actual_variance
    from eawf.workflow.evidence._io import load_state

    state_path = _resolve_state_or_emit(flags)
    if state_path is None:
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    metric = compute_estimate_actual_variance(state)
    payload: dict[str, Any] = metric.model_dump(mode="json")
    text = _render_variance(metric.variance_pct, metric.sample_count)
    emit_json_or_text(payload, text, flags=flags)


def _render_variance(variance_pct: float | None, sample_count: int) -> str:
    """Render the M26 variance gauge as a one-line ship-gate summary."""
    if variance_pct is None:
        return f"estimate-actual variance: no data (samples={sample_count})"
    sign = "+" if variance_pct >= 0 else ""
    return f"estimate-actual variance: {sign}{variance_pct:.1f}% (samples={sample_count})"


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
    """Render the jury validated against its ground-truth cohort.

    Read-only -- builds the validation cohort from ``state.json`` + the
    append-only gold-label / AUDITOR stores under it, scores the jury against
    it, and renders the Fleiss kappa / Brier / ECE / unanimous-pass-on-known-bad
    catch rate plus the cohort ``n`` and the derived BlockAuthority tier. Under
    the cohort floor the reducer refuses to score, and the render is the honest
    "insufficient signal (n=k)" banner -- never a fabricated number. Failures
    map to the canonical CLI exit codes: UserError (``kind="NotFound"``,
    ``exit=1``) when no ``state.json`` resolves, ValidationError (``exit=2``) on
    a schema mismatch.
    """
    from eawf.observability.eval.jury_validation import (
        ValidationCohort,
        build_jury_validation_cohort,
        read_recorded_ballots,
        validate_jury,
    )
    from eawf.workflow.evidence._io import load_state

    state_path = _resolve_state_or_emit(flags)
    if state_path is None:
        return

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    cohort = build_jury_validation_cohort(state, state_path)
    ballots_by_wave = read_recorded_ballots(state_path)
    # Only cohort rows whose wave has recorded juror ballots can score
    # juror-level metrics; a labelled wave from before the ballot store
    # existed is excluded from scoring, never fabricated -- the excluded
    # count is surfaced so the starvation stays visible.
    scorable = ValidationCohort(
        silver=[row for row in cohort.silver if row.outcome.base_id in ballots_by_wave],
        gold=[row for row in cohort.gold if row.outcome.base_id in ballots_by_wave],
    )
    labelled_rows = len(cohort.silver) + len(cohort.gold)
    ballotless_rows = labelled_rows - (len(scorable.silver) + len(scorable.gold))
    report = validate_jury(scorable, ballots_by_wave=ballots_by_wave)
    payload = report.model_dump(mode="json")
    payload["block_authority"] = _block_authority(report)
    payload["labelled_rows"] = labelled_rows
    payload["ballotless_rows"] = ballotless_rows
    text = _render_jury_validation(report)
    if ballotless_rows:
        text += (
            f"\nlabelled cohort rows: {labelled_rows} "
            f"({ballotless_rows} without recorded ballots, excluded from scoring)"
        )
    emit_json_or_text(payload, text, flags=flags)


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
    from eawf.observability.telemetry.projector import RebuildMode, SourceSpec, rebuild
    from eawf.observability.telemetry.sources.event_jsonl import EventJsonlSource
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
    finally:
        store.close()

    payload = {
        "mode": mode.value,
        "sessions": report.sessions,
        "incidents": report.incidents,
        "files_scanned": report.files_scanned,
        "files_skipped": report.files_skipped,
    }
    text = (
        f"rebuild {mode.value}: sessions={report.sessions} incidents={report.incidents} "
        f"scanned={report.files_scanned} skipped={report.files_skipped}"
    )
    emit_json_or_text(payload, text, flags=flags)


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
