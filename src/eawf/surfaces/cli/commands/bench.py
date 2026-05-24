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

Exit codes:

- ``0`` — success / no regression.
- ``1`` (``USER_ERROR``) — bad size / harness / unreadable input.
- ``2`` (``VALIDATION_ERROR``) — ``compare`` detected a regression.
"""

from __future__ import annotations

import logging
import platform
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
