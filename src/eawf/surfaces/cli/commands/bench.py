"""``eawf bench`` Typer sub-app — perf bench harness.

CLI dispatch only (AGENTS rule 1): every handler parses args, calls into
:mod:`eawf.observability.bench`, and routes output through
:func:`eawf.surfaces.cli.output.emit_json_or_text`. The harness catalog, corpus
seeding, and regression logic all live in the library.

Verbs:

- ``eawf bench list`` — show every fixture x harness in the catalog.
- ``eawf bench run`` — seed a corpus in-memory and time each harness;
  optionally write/compare a per-OS baseline.
- ``eawf bench compare`` — diff two result files and flag regressions
  (``after >= before * (1 + threshold)``); exits ``2`` on regression.
- ``eawf bench fixture seed`` — write the deterministic corpus files for
  one size (re-seeding is byte-identical).
- ``eawf bench turn-cost`` — render the wall-clock + cost record per
  completed unit of work, and optionally check it against a recorded
  baseline.

Exit codes:

- ``0`` — success / no regression / nothing measurable.
- ``1`` (``USER_ERROR``) — bad size / harness / unreadable input, and the
  ``turn-cost`` refusals: an unknown fixture, a ``--check`` with nothing to
  measure, and a baseline that is not comparable to the current record.
- ``2`` (``VALIDATION_ERROR``) — ``compare`` or ``turn-cost --check``
  detected a regression.

The two non-zero ``turn-cost --check`` codes are deliberately distinct: a
``2`` says the measurement moved, a ``1`` says the two artifacts were never
comparable. Collapsing them would let a stale baseline read as a
performance regression (or, worse, invite a silent rebaseline).
"""

from __future__ import annotations

import logging
import platform
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.observability.bench.harness import BenchResult
    from eawf.observability.bench.turn_cost import (
        CorpusResolution,
        TurnCostComparison,
    )
    from eawf.observability.telemetry.turn_cost import TurnCostRecord

logger = logging.getLogger(__name__)


bench_app = typer.Typer(
    name="bench",
    help="Perf bench harness — seed corpora, time harnesses, flag regressions.",
    no_args_is_help=True,
    add_completion=False,
)

fixture_app = typer.Typer(
    name="fixture",
    help="Bench fixture corpora (seed).",
    no_args_is_help=True,
    add_completion=False,
)
bench_app.add_typer(fixture_app, name="fixture")


# Default on-disk locations, repo-relative to the resolved workspace.
_DEFAULT_FIXTURE_DIR = Path("tests/fixtures/bench")
_DEFAULT_THRESHOLDS = Path(".ea/bench/thresholds.yaml")


def _result_to_dict(result: BenchResult) -> dict[str, object]:
    """Render one :class:`BenchResult` as a JSON-safe dict."""
    return {
        "name": result.name,
        "size": result.size,
        "iterations": result.iterations,
        "best_ms": result.best_ms,
    }


@bench_app.command("list")
def bench_list(ctx: typer.Context) -> None:
    """List every fixture size x harness in the catalog."""
    from eawf.observability.bench import FIXTURE_SIZES, HARNESS_CATALOG

    flags: GlobalFlags = ctx.obj
    payload: dict[str, object] = {
        "sizes": list(FIXTURE_SIZES),
        "harnesses": [
            {"name": spec.name, "description": spec.description}
            for spec in HARNESS_CATALOG.values()
        ],
    }
    lines = ["sizes: " + ", ".join(FIXTURE_SIZES), "harnesses:"]
    lines += [f"  {spec.name} — {spec.description}" for spec in HARNESS_CATALOG.values()]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


@bench_app.command("run")
def bench_run(
    ctx: typer.Context,
    size: Annotated[
        str,
        typer.Option("--size", help="Corpus size to seed in-memory."),
    ] = "small",
    harness: Annotated[
        str | None,
        typer.Option("--harness", help="Run only this harness (default: all)."),
    ] = None,
    iterations: Annotated[
        int,
        typer.Option("--iterations", help="Timed iterations per harness."),
    ] = 50,
) -> None:
    """Seed a corpus in-memory and time each harness against it."""
    from eawf.observability.bench import seed_corpus
    from eawf.observability.bench.harness import run_all, run_harness

    flags: GlobalFlags = ctx.obj
    try:
        corpus = seed_corpus(size)  # type: ignore[arg-type]
        if harness is None:
            results = run_all(corpus, iterations)
        else:
            results = [run_harness(harness, corpus, iterations)]
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return

    payload: dict[str, object] = {
        "size": size,
        "os": platform.system(),
        "iterations": iterations,
        "results": [_result_to_dict(r) for r in results],
    }
    lines = [f"bench run: size={size} os={platform.system()} iterations={iterations}"]
    lines += [f"  {r.name}: {r.best_ms:.4f} ms" for r in results]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


@bench_app.command("compare")
def bench_compare(
    ctx: typer.Context,
    before: Annotated[
        Path,
        typer.Option("--before", help="Baseline results JSON (eawf bench run --json)."),
    ],
    after: Annotated[
        Path,
        typer.Option("--after", help="Candidate results JSON (eawf bench run --json)."),
    ],
    threshold: Annotated[
        float | None,
        typer.Option(
            "--threshold",
            help="Override the per-OS regression threshold (fraction, e.g. 0.10).",
        ),
    ] = None,
) -> None:
    """Flag any harness that regressed past the per-OS threshold.

    Exits ``2`` (``VALIDATION_ERROR``) when at least one harness crosses
    ``after >= before * (1 + threshold)``.
    """
    from eawf.observability.bench.harness import (
        BenchResult,
        compare_results,
        load_thresholds,
        threshold_for_os,
    )

    flags: GlobalFlags = ctx.obj
    try:
        before_results = _load_results(before)
        after_results = _load_results(after)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    if threshold is None:
        thresholds = load_thresholds(_resolve_path(flags, _DEFAULT_THRESHOLDS))
        active_threshold = threshold_for_os(thresholds)
    else:
        active_threshold = threshold

    comparisons = compare_results(before_results, after_results, active_threshold)
    regressions = [c for c in comparisons if c.regressed]

    payload: dict[str, object] = {
        "os": platform.system(),
        "threshold": active_threshold,
        "regressed": bool(regressions),
        "comparisons": [
            {
                "name": c.name,
                "before_ms": c.before_ms,
                "after_ms": c.after_ms,
                "ratio": c.ratio,
                "regressed": c.regressed,
            }
            for c in comparisons
        ],
    }
    verb = "REGRESSED" if regressions else "ok"
    lines = [f"bench compare: {verb} (threshold={active_threshold:.2f}, os={platform.system()})"]
    for c in comparisons:
        marker = "!!" if c.regressed else "  "
        lines.append(
            f"{marker} {c.name}: {c.before_ms:.4f} -> {c.after_ms:.4f} ms (x{c.ratio:.3f})"
        )
    emit_json_or_text(payload, "\n".join(lines), flags=flags)

    if regressions:
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)

    # Reference BenchResult so the import is not flagged unused; the type
    # is used implicitly by the loader above.
    _ = BenchResult


@bench_app.command("turn-cost")
def bench_turn_cost(
    ctx: typer.Context,
    fixture: Annotated[
        str,
        typer.Option(
            "--fixture",
            help="Corpus to measure: 'live' (state + telemetry cache) or a seeded fixture id.",
        ),
    ] = "live",
    check: Annotated[
        bool,
        typer.Option("--check", help="Compare the record against --baseline instead of rendering."),
    ] = False,
    baseline: Annotated[
        Path | None,
        typer.Option("--baseline", help="Baseline artifact to check against (requires --check)."),
    ] = None,
    write_baseline: Annotated[
        Path | None,
        typer.Option("--write-baseline", help="Write the measured record out as a baseline."),
    ] = None,
    threshold: Annotated[
        float,
        typer.Option(
            "--threshold",
            help="Tolerance recorded into --write-baseline (fraction, e.g. 0.10).",
        ),
    ] = 0.10,
) -> None:
    """Render wall clock + cost per completed unit of work, or check it.

    Without ``--check`` this renders the record for the selected corpus.
    With ``--check --baseline <path>`` it instead compares the record's p90
    wall clock and p90 cost against the baseline, under the threshold the
    baseline artifact itself recorded, and exits ``2`` when either crosses.

    A baseline measured from a different fixture, harness revision, runtime
    or model is refused with ``comparison_invalid`` (exit ``1``) naming the
    differing fields — never compared, and never silently replaced.

    Raises:
        typer.Exit: ``1`` on a flag/artifact refusal, ``2`` on a regression.
    """
    from eawf.observability.bench.turn_cost import (
        TurnCostVerdict,
        baseline_from_record,
        build_corpus_record,
        compare_turn_cost,
        load_baseline,
    )
    from eawf.observability.bench.turn_cost import write_baseline as write_baseline_artifact

    flags: GlobalFlags = ctx.obj
    if check and baseline is None:
        cli_errors.emit_error(
            cli_errors.UserError("--check requires --baseline <path>"), flags=flags
        )
    if baseline is not None and not check:
        cli_errors.emit_error(
            cli_errors.UserError("--baseline is only read under --check"), flags=flags
        )
    if threshold < 0:
        cli_errors.emit_error(
            cli_errors.UserError(f"--threshold must be >= 0, got {threshold}"), flags=flags
        )

    resolution = _resolve_turn_cost_corpus(flags, fixture=fixture)
    if resolution.corpus is None:
        _emit_turn_cost_empty(flags, resolution, fixture=fixture, check=check)
        return

    record = build_corpus_record(resolution.corpus)
    if write_baseline is not None:
        write_baseline_artifact(
            baseline_from_record(record, threshold=Decimal(str(threshold))),
            write_baseline,
        )

    if baseline is None:
        _emit_turn_cost_record(flags, record, skipped=resolution.skipped_session_count)
        return

    try:
        recorded = load_baseline(baseline)
    except (FileNotFoundError, ValueError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)

    comparison = compare_turn_cost(baseline=recorded, record=record)
    if comparison.verdict is TurnCostVerdict.COMPARISON_INVALID:
        fields = ", ".join(comparison.mismatched_fields)
        cli_errors.emit_error(
            cli_errors.UserError(
                f"comparison_invalid: baseline {baseline} differs from the current "
                f"record on {fields} — re-measure the baseline, do not rebaseline"
            ),
            flags=flags,
            data=comparison.model_dump(mode="json"),
        )

    _emit_turn_cost_check(flags, comparison, record=record)
    if comparison.verdict is TurnCostVerdict.REGRESSED:
        raise typer.Exit(code=exit_codes.VALIDATION_ERROR)


@fixture_app.command("seed")
def fixture_seed(
    ctx: typer.Context,
    size: Annotated[
        str,
        typer.Option("--size", help="Corpus size to write (small|medium|large)."),
    ],
    output_dir: Annotated[
        Path | None,
        typer.Option(
            "--out",
            help="Directory the fixture files land in (default: tests/fixtures/bench).",
        ),
    ] = None,
) -> None:
    """Write the deterministic corpus files for *size*.

    Re-running with the same size overwrites both files with
    byte-identical content.
    """
    from eawf.observability.bench import seed_fixture

    flags: GlobalFlags = ctx.obj
    target = output_dir if output_dir is not None else _resolve_path(flags, _DEFAULT_FIXTURE_DIR)
    try:
        state_path, event_path = seed_fixture(size, target)  # type: ignore[arg-type]
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
        return

    payload: dict[str, object] = {
        "size": size,
        "state_path": str(state_path),
        "event_path": str(event_path),
    }
    text = f"seeded {size}: {state_path} + {event_path}"
    emit_json_or_text(payload, text, flags=flags)


# --- Internal helpers ------------------------------------------------------


def _resolve_path(flags: GlobalFlags, rel: Path) -> Path:
    """Resolve a repo-relative default against the workspace flag.

    When ``-w/--workspace`` is set, *rel* is joined onto it; otherwise it
    stays relative to the current working directory.
    """
    if flags.workspace is not None:
        return flags.workspace / rel
    return rel


def _resolve_turn_cost_corpus(flags: GlobalFlags, *, fixture: str) -> CorpusResolution:
    """Resolve the corpus *fixture* names, seeded or live.

    Args:
        flags: The resolved global CLI flags.
        fixture: ``"live"`` or a seeded fixture id.

    Returns:
        The resolution, whose ``corpus`` is ``None`` when nothing is
        measurable.

    Raises:
        typer.Exit: ``1`` when *fixture* is not a known seeded fixture.
    """
    from eawf.observability.bench.turn_cost import (
        LIVE_FIXTURE_ID,
        CorpusResolution,
        seed_turn_cost_corpus,
    )

    if fixture == LIVE_FIXTURE_ID:
        return _resolve_live_turn_cost_corpus(flags)
    try:
        corpus = seed_turn_cost_corpus(fixture)
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc)), flags=flags)
    return CorpusResolution(corpus=corpus, skipped_session_count=0, reason=None)


def _resolve_live_turn_cost_corpus(flags: GlobalFlags) -> CorpusResolution:
    """Join the projected telemetry sessions onto the closed waves in state.

    The telemetry cache is read but never created. Opening a store through
    ``init_schema`` writes ``telemetry.db`` as a side effect, and a bench
    render must not be the thing that starts collecting for an operator who
    never opted in — so a missing cache resolves to "nothing measured"
    instead. Its existence is itself the evidence that telemetry was
    enabled, which is why no separate opt-in check is repeated here.

    Args:
        flags: The resolved global CLI flags.

    Returns:
        The live corpus resolution.

    Raises:
        typer.Exit: ``1`` when the state path or file cannot be read.
    """
    from eawf.observability.bench.turn_cost import CorpusResolution, collect_live_corpus
    from eawf.observability.telemetry.models import TelemetrySession
    from eawf.observability.telemetry.store import metrics_db_path, open_store
    from eawf.surfaces.cli.scope import resolve_state_path

    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)

    db_path = metrics_db_path(state_path)
    if not db_path.exists():
        return CorpusResolution(
            corpus=None,
            skipped_session_count=0,
            reason="no telemetry cache projected yet; run `eawf metrics rebuild` first",
        )

    from eawf.kernel.config.layered import get_dotted, merge_config
    from eawf.workflow.evidence._io import load_state

    try:
        state = load_state(state_path)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)

    merged, _sources = merge_config(repo=state_path.parent.parent)
    db_kind = str(get_dotted(merged, "telemetry.db_kind"))
    store = open_store(db_kind, db_path)  # type: ignore[arg-type]
    try:
        store.init_schema()
        rows = store.fetch_all("telemetry_sessions", TelemetrySession)
    finally:
        store.close()
    sessions = [row for row in rows if isinstance(row, TelemetrySession)]
    return collect_live_corpus(state=state, sessions=sessions)


def _emit_turn_cost_empty(
    flags: GlobalFlags,
    resolution: CorpusResolution,
    *,
    fixture: str,
    check: bool,
) -> None:
    """Emit the honest "nothing measured" outcome.

    Under ``--check`` this is a refusal rather than a pass: a check that
    silently succeeds because there was nothing to measure is a false green.

    Raises:
        typer.Exit: ``1`` when *check* is set.
    """
    payload: dict[str, object] = {
        "measured": False,
        "fixture": fixture,
        "reason": resolution.reason,
        "skipped_session_count": resolution.skipped_session_count,
    }
    if check:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot check turn-cost: {resolution.reason}"),
            flags=flags,
            data=payload,
        )
    emit_json_or_text(payload, f"turn-cost: nothing measured ({resolution.reason})", flags=flags)


def _emit_turn_cost_record(flags: GlobalFlags, record: TurnCostRecord, *, skipped: int) -> None:
    """Render one turn-cost record."""
    payload: dict[str, object] = {
        "measured": True,
        "record": record.model_dump(mode="json"),
        "skipped_session_count": skipped,
    }
    lines = [
        f"turn-cost: fixture={record.fixture_id} harness={record.harness_revision} "
        f"runtime={record.runtime} model={record.model}",
        f"  units: {record.unit_count}",
        f"  wall clock: p50={record.p50_wall_clock_ms} ms p90={record.p90_wall_clock_ms} ms",
        f"  cost: p50={record.p50_cost_usd} USD p90={record.p90_cost_usd} USD",
        f"  verification: {record.verification_cost_usd} USD "
        f"execution: {record.execution_cost_usd} USD",
        f"  excluded: unattributed={record.unattributed_run_count} "
        f"unpriced={record.unpriced_run_count}",
    ]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


def _emit_turn_cost_check(
    flags: GlobalFlags,
    comparison: TurnCostComparison,
    *,
    record: TurnCostRecord,
) -> None:
    """Render one baseline check outcome (never the invalid-comparison one)."""
    from eawf.observability.bench.turn_cost import TurnCostVerdict

    payload: dict[str, object] = {
        "check": comparison.model_dump(mode="json"),
        "record": record.model_dump(mode="json"),
    }
    regressed = comparison.verdict is TurnCostVerdict.REGRESSED
    lines = [
        f"turn-cost check: {'REGRESSED' if regressed else 'ok'} "
        f"(verdict={comparison.verdict.value} threshold={comparison.threshold})",
        f"{'!!' if comparison.wall_clock_regressed else '  '} p90 wall clock: "
        f"{comparison.baseline_p90_wall_clock_ms} -> "
        f"{comparison.candidate_p90_wall_clock_ms} ms",
        f"{'!!' if comparison.cost_regressed else '  '} p90 cost: "
        f"{comparison.baseline_p90_cost_usd} -> {comparison.candidate_p90_cost_usd} USD",
    ]
    emit_json_or_text(payload, "\n".join(lines), flags=flags)


def _load_results(path: Path) -> list[BenchResult]:
    """Load a ``bench run --json`` results file into typed results.

    Args:
        path: Path to a JSON file holding a top-level ``results`` array
            (the shape ``eawf bench run --json`` emits).

    Returns:
        The parsed :class:`BenchResult` rows.

    Raises:
        UserError: When the file is missing, unparseable, or malformed.
    """
    from eawf.observability.bench.harness import BenchResult

    if not path.exists():
        raise cli_errors.UserError(f"results file not found: {path}")
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as exc:
        raise cli_errors.UserError(f"malformed results JSON in {path}: {exc}") from exc

    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise cli_errors.UserError(f"results file missing 'results' array: {path}")

    out: list[BenchResult] = []
    for row in rows:
        try:
            out.append(
                BenchResult(
                    name=str(row["name"]),
                    size=str(row["size"]),
                    iterations=int(row["iterations"]),
                    best_ms=float(row["best_ms"]),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise cli_errors.UserError(f"malformed result row in {path}: {exc}") from exc
    return out
