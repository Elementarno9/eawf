"""The ``live`` turn-cost corpus: this repository's own measured Task Runs.

The seeded fixtures measure a corpus built in memory. The live one reads
the Runs the main checkout recorded, groups them by the Task they served
and keeps the Runs measured on the most common runtime and model, so the
release's telemetry gate measures real work rather than a stand-in.
"""

from __future__ import annotations

import subprocess
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.enums import AgentSessionRole, WaveStatus
from eawf.kernel.state.epoch2.measurement import (
    CaptureSource,
    CounterName,
    MeasuredRuntime,
    Observed,
    Unobserved,
)
from eawf.observability.bench.turn_cost import (
    LIVE_FIXTURE_ID,
    build_corpus_record,
    live_turn_cost_corpus,
)
from eawf.observability.telemetry.models import PriceSourceKind
from tests.contract.surfaces.cli._reflect_tree import CODE, native_tree, run_row

pytestmark = pytest.mark.integration

_TASK_PREFIX = f"eawf://WSP-{CODE}/PRJ-{CODE}/REP-{CODE}/task"


def _captured(
    *, cost: str | None, harness: str = "claude-code", model: str = "claude-opus-5-5"
) -> dict[str, Any]:
    counts = {
        CounterName.DURATION_MS: Observed(value=Decimal(0)),
        CounterName.INPUT_TOKENS: Observed(value=Decimal("100.5")),
        CounterName.OUTPUT_TOKENS: Observed(value=Decimal(20)),
        CounterName.CACHE_CREATION_INPUT_TOKENS: Observed(value=Decimal(30)),
        CounterName.CACHE_READ_INPUT_TOKENS: Observed(value=Decimal(400)),
        CounterName.COST_USD: (
            Unobserved(reason="no price") if cost is None else Observed(value=Decimal(cost))
        ),
    }
    return MeasuredRuntime(
        source=CaptureSource.TRANSCRIPT,
        harness=harness,
        model=model,
        measurement_version=1,
        divisor=1,
        derived=False,
        measurement_quality="measured",
        counters=counts,
        spans=Unobserved(reason="no spans"),
    ).model_dump(mode="json")


def _run(
    key: str,
    task: str,
    *,
    status: str = "COMPLETED",
    hours: int = 1,
    purpose: str = "implement",
    captured: dict[str, Any] | None = None,
) -> dict[str, Any]:
    row = run_row(key, status=status, hours=hours)
    row["scope"] = row["scope"] | {"task_ref": f"{_TASK_PREFIX}/{task}", "purpose": purpose}
    if captured is not None:
        row["captured_runtime"] = captured
    return row


def _tree(repo_root: Path, runs: list[dict[str, Any]]) -> Path:
    repo_root.mkdir(parents=True, exist_ok=True)
    return native_tree(repo_root, runs, [])


def test_the_live_fixture_id_is_live() -> None:
    assert LIVE_FIXTURE_ID == "live"


def test_a_tree_without_recorded_runs_refuses_rather_than_measuring_nothing(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="live turn-cost corpus is empty"):
        live_turn_cost_corpus(tmp_path / ".ea")


def test_a_tree_whose_runs_carry_no_measurement_refuses(tmp_path: Path) -> None:
    tree = _tree(tmp_path / "repo", [_run("RUN-00000001", f"{CODE}-0001")])

    with pytest.raises(ValueError, match="no ended Task Run carries a measured capture"):
        live_turn_cost_corpus(tree)


def test_one_measured_run_is_a_one_unit_corpus(tmp_path: Path) -> None:
    tree = _tree(
        tmp_path / "repo",
        [_run("RUN-00000001", f"{CODE}-0001", captured=_captured(cost="1.25"))],
    )

    corpus = live_turn_cost_corpus(tree)

    assert corpus.fixture_id == LIVE_FIXTURE_ID
    assert (corpus.runtime, corpus.model) == ("claude", "claude-opus-5-5")
    assert corpus.waves[0].id == f"{CODE}-0001"
    assert corpus.waves[0].status is WaveStatus.CLOSED
    (run,) = corpus.runs
    assert run.role is AgentSessionRole.EXECUTOR
    assert run.wall_clock_ms == 3_600_000
    assert (run.input_tokens, run.output_tokens) == (100, 20)
    assert (run.cache_write_tokens, run.cache_read_tokens) == (30, 400)
    assert run.cost_usd == Decimal("1.25")
    assert run.price_source is PriceSourceKind.BILLED


def test_the_live_corpus_measures_each_task_on_the_dominant_tuple(tmp_path: Path) -> None:
    codex = _captured(cost="9", harness="codex", model="gpt-5")
    tree = _tree(
        tmp_path / "repo",
        [
            _run("RUN-00000001", f"{CODE}-0001", captured=_captured(cost="1.5")),
            _run(
                "RUN-00000002",
                f"{CODE}-0001",
                purpose="repair",
                hours=2,
                captured=_captured(cost="0.5"),
            ),
            _run("RUN-00000003", f"{CODE}-0002", captured=_captured(cost=None)),
            _run("RUN-00000004", f"{CODE}-0003", captured=codex),
            _run("RUN-00000005", f"{CODE}-0004", status="RUNNING"),
            _run("RUN-00000006", f"{CODE}-0004", captured=_captured(cost="7")),
            _run("RUN-00000007", f"{CODE}-0001"),
        ],
    )

    corpus = live_turn_cost_corpus(tree)
    record = build_corpus_record(corpus)

    assert {wave.id: wave.status for wave in corpus.waves} == {
        f"{CODE}-0001": WaveStatus.CLOSED,
        f"{CODE}-0002": WaveStatus.CLOSED,
        f"{CODE}-0004": WaveStatus.IN_PROGRESS,
    }
    assert sorted(run.run_id for run in corpus.runs) == [
        "RUN-00000001",
        "RUN-00000002",
        "RUN-00000003",
        "RUN-00000006",
    ]
    # The open Task is not a completed unit, and the unpriced Run is counted, not summed.
    assert record.unit_count == 2
    assert record.unpriced_run_count == 1
    assert record.execution_cost_usd == Decimal("2.0")
    assert record.p90_wall_clock_ms == 3 * 3_600_000


def test_a_tie_between_tuples_is_broken_the_same_way_every_time(tmp_path: Path) -> None:
    tree = _tree(
        tmp_path / "repo",
        [
            _run("RUN-00000001", f"{CODE}-0001", captured=_captured(cost="1")),
            _run(
                "RUN-00000002",
                f"{CODE}-0002",
                captured=_captured(cost="1", harness="codex", model="gpt-5"),
            ),
        ],
    )

    assert {live_turn_cost_corpus(tree).runtime for _ in range(3)} == {"codex"}


def test_a_review_run_is_charged_to_verification(tmp_path: Path) -> None:
    tree = _tree(
        tmp_path / "repo",
        [
            _run("RUN-00000001", f"{CODE}-0001", captured=_captured(cost="1")),
            _run("RUN-00000002", f"{CODE}-0001", purpose="audit", captured=_captured(cost="2")),
        ],
    )

    record = build_corpus_record(live_turn_cost_corpus(tree))

    assert record.verification_cost_usd == Decimal(2)
    assert record.execution_cost_usd == Decimal(1)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_a_linked_worktree_measures_the_main_checkouts_runs(tmp_path: Path) -> None:
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(
        main,
        "-c",
        "user.name=ci",
        "-c",
        "user.email=ci@example.com",
        "commit",
        "-q",
        "--allow-empty",
        "-m",
        "root",
    )
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    _tree(main, [_run("RUN-00000001", f"{CODE}-0001", captured=_captured(cost="1"))])

    corpus = live_turn_cost_corpus(linked / ".ea")

    assert [run.run_id for run in corpus.runs] == ["RUN-00000001"]
