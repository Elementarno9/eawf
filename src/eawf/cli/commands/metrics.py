"""``eawf metrics`` — workflow + telemetry metrics surface.

The bare ``eawf metrics`` invocation renders the rolling **workflow**
metrics (estimation calibration) from ``state.json`` — EU variance, audit
pass rate, wave elapsed, and the planned-vs-reactive split. This is the
read-only estimation view shipped in P20-W08.

The C09 telemetry capstone (P27-I01-W16) adds four sub-verbs behind the
same ``metrics`` command — selected by a leading positional sub-verb so the
existing single-command registration in :mod:`eawf.cli.app` stays intact
(no app-level ``add_typer`` re-wire needed):

- ``eawf metrics show`` — render the rolling telemetry metrics projected
  into the local cache. With ``telemetry.enabled=false`` it prints a
  one-time opt-in nudge and returns cleanly (no projection, no metrics).
- ``eawf metrics export --format prom|json|csv`` — serialise the projected
  metrics through :mod:`eawf.telemetry.exporter` to stdout or ``--out``.
- ``eawf metrics rebuild [--full|--incremental]`` — drive the projector
  (:func:`eawf.telemetry.projector.rebuild`) over the discovered sources.
- ``eawf metrics info`` — print cache stats: DB kind, path, schema +
  pricing version, and row counts.
- ``eawf metrics variance`` — emit the C09 §5.9.6 M26
  ``eawf_estimate_actual_variance_pct`` gauge from ``state.json`` and feed
  the ship-gate Variance section + the C06 VarianceTile.
- ``eawf metrics backfill-actuals`` — attach retroactive
  :class:`~eawf.state.models.ActualSummary` rows to historical CLOSED
  waves that closed before the W25 auto-record wiring landed, so the
  variance gauge + bucket calibration fit against the full closed-wave
  history instead of the handful of post-W25 samples. Mutates
  ``state.json`` through the canonical writer; idempotent on a re-run.

CLI is dispatch (AGENTS rule 1): every handler resolves the state path,
reads the typed config, and routes the heavy lifting into
:mod:`eawf.telemetry` (or, for ``backfill-actuals``, the pure transform in
:mod:`eawf.migrations.backfill_actuals`). The shared estimation renderer
lives in :mod:`eawf.render.metrics_view`; the telemetry aggregation +
serialisation live in :mod:`eawf.telemetry.exporter` and
:mod:`eawf.telemetry.projector`.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path

logger = logging.getLogger(__name__)

_TELEMETRY_SUBCOMMANDS = frozenset({"show", "export", "rebuild", "info", "variance"})

# Estimation sub-verbs that operate on ``state.json`` only — they never open
# the telemetry cache, so they skip the opt-in gate the telemetry sub-verbs
# share (``backfill-actuals`` mutates state through the canonical writer).
_ESTIMATION_SUBCOMMANDS = frozenset({"backfill-actuals"})

_KNOWN_SUBCOMMANDS = _TELEMETRY_SUBCOMMANDS | _ESTIMATION_SUBCOMMANDS

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
            metavar="[show|export|rebuild|info|variance|backfill-actuals]",
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
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="`metrics backfill-actuals`: report the count without writing state.json.",
        ),
    ] = False,
) -> None:
    """Dispatch the bare workflow-metrics view or a metrics sub-verb.

    Read-only for ``show`` / ``info`` / ``variance`` / the bare view;
    ``export`` may write a file; ``rebuild`` mutates the local telemetry
    cache only (never ``state.json``); ``backfill-actuals`` mutates
    ``state.json`` through the canonical writer (unless ``--dry-run``).

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
    elif subcommand == "backfill-actuals":
        _backfill_actuals(flags, dry_run=dry_run)
    else:  # subcommand == "info"
        _telemetry_info(flags)


def _workflow_metrics(flags: GlobalFlags) -> None:
    """Render the rolling estimation metrics from ``state.json`` (P20-W08).

    Read-only — does not acquire a lock, append events, or mutate
    ``state.json``. Failures map to the canonical CLI exit codes:

    - :class:`~eawf.cli.errors.NotFound` (``exit=1``) when no
      ``.ea/state.json`` is locatable from the cwd / ``-w`` / ``EA_STATE``
      precedence chain.
    - :class:`~eawf.cli.errors.ValidationFailed` (``exit=2``) when the
      on-disk payload fails strict schema validation.
    """
    from eawf.estimation.metrics import compute_metrics
    from eawf.evidence._io import load_state
    from eawf.render.metrics_view import render_metrics_plain, render_metrics_table

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
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
    NotFound (``exit=1``) when no ``state.json`` resolves, ValidationFailed
    (``exit=2``) on a schema mismatch.
    """
    from eawf.estimation.metrics import compute_estimate_actual_variance
    from eawf.evidence._io import load_state

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


def _backfill_actuals(flags: GlobalFlags, *, dry_run: bool) -> None:
    """Attach retroactive actuals to historical CLOSED waves (idempotent).

    Threads the pure :func:`eawf.migrations.backfill_actuals.backfill_actuals`
    transform through the canonical writer: the mutation runs inside
    :func:`eawf.cli._mutation.state_transaction`, which routes to the daemon
    (or the WAL-safe portalock fallback) per AGENTS rule 4. The verb operates
    on ``state.json`` waves only — it never opens or touches the telemetry
    cache (``telemetry.db``), so it works regardless of ``telemetry.enabled``.
    Idempotent: a second run derives no new actuals and reports ``0``.

    With ``dry_run`` the count is computed against an in-memory snapshot and
    nothing is persisted (no lock, no write).

    Failures map to the canonical CLI exit codes: NotFound (``exit=1``) when
    no ``state.json`` resolves, ValidationFailed (``exit=2``) on a schema
    mismatch.
    """
    from eawf.cli._mutation import state_transaction
    from eawf.evidence._io import load_state
    from eawf.migrations.backfill_actuals import backfill_actuals

    state_path = _resolve_state_or_emit(flags)
    if state_path is None:
        return

    try:
        if dry_run:
            snapshot = load_state(state_path)
            _state, added = backfill_actuals(snapshot)
        else:
            with state_transaction(state_path) as state:
                _state, added = backfill_actuals(state)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    payload: dict[str, Any] = {"actuals_added": added, "dry_run": dry_run}
    suffix = " (dry-run, not written)" if dry_run else ""
    text = f"backfilled {added} actual(s){suffix}"
    emit_json_or_text(payload, text, flags=flags)


def _render_variance(variance_pct: float | None, sample_count: int) -> str:
    """Render the M26 variance gauge as a one-line ship-gate summary."""
    if variance_pct is None:
        return f"estimate-actual variance: no data (samples={sample_count})"
    sign = "+" if variance_pct >= 0 else ""
    return f"estimate-actual variance: {sign}{variance_pct:.1f}% (samples={sample_count})"


def _resolve_state_or_emit(flags: GlobalFlags) -> Path | None:
    """Resolve the state path or emit a NotFound error and return ``None``."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.NotFound(str(exc)), flags=flags)
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
    from eawf.config.layered import get_dotted, merge_config

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
    from eawf.telemetry.exporter import build_snapshot, render_prom
    from eawf.telemetry.store import metrics_db_path, open_store

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
    from eawf.telemetry.exporter import build_snapshot, render
    from eawf.telemetry.store import metrics_db_path, open_store

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
    from eawf.telemetry.projector import RebuildMode, SourceSpec, rebuild
    from eawf.telemetry.sources.event_jsonl import EventJsonlSource
    from eawf.telemetry.store import metrics_db_path, open_store

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
    from eawf.telemetry.models import TelemetryIncident, TelemetrySession
    from eawf.telemetry.pricing import PRICING_VERSION
    from eawf.telemetry.store import metrics_db_path, open_store
    from eawf.telemetry.store.base import SCHEMA_VERSION

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
