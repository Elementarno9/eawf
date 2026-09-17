"""The cold-paint clock starts at process start and names the import graph.

A first-paint figure measured from ``run_test()`` entry is the one an operator never
experiences: by then the interpreter has booted and the console's import graph, the
toolkit underneath it included, is already resident. This suite holds the measurement to
the operator's clock -- it starts when the process is asked for -- and to naming the
import graph as its own component, so a first-paint regression can be attributed to
imports rather than blamed on the renderer.

One child process is spawned for the whole module. The reading it produces is a property
of this host at this moment, so nothing here asserts a wall-clock figure; the assertions
are on the clock's origin, the named components, and the frame being meaningful.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.perf import (
    COLD_PAINT_T0_ENV,
    ColdPaint,
    PerformanceHarness,
)

TESTS_ROOT = Path(__file__).resolve().parents[4]
FIXTURE_DIR = TESTS_ROOT / "fixtures" / "console" / "golden" / "fixture"

#: The components of a first meaningful paint, in the order they happen.
EXPECTED_COMPONENTS = ("spawn", "import", "fixture", "paint")


@pytest.fixture(scope="module")
def paint() -> ColdPaint:
    """Measure one cold paint of the tracked console fixture."""
    return PerformanceHarness(FIXTURE_DIR).cold_paint()


def test_the_clock_origin_is_handed_to_the_child_through_the_environment() -> None:
    """The parent's reading travels to the child, which is what makes the origin shared."""
    assert COLD_PAINT_T0_ENV == "EAWF_CONSOLE_COLD_PAINT_T0"


def test_the_clock_starts_before_the_process_exists(paint: ColdPaint) -> None:
    """Process creation and interpreter start-up land inside the reading, not before it."""
    assert paint.spawn_ms > 0.0


def test_the_import_graph_is_a_named_component(paint: ColdPaint) -> None:
    assert "import" in paint.components()
    assert paint.import_ms > 0.0


def test_every_component_of_the_paint_is_named_in_order(paint: ColdPaint) -> None:
    assert tuple(paint.components()) == EXPECTED_COMPONENTS


def test_the_named_components_account_for_the_whole_paint(paint: ColdPaint) -> None:
    assert sum(paint.components().values()) == pytest.approx(paint.total_ms, abs=1e-3)


def test_the_total_is_more_than_the_paint_alone(paint: ColdPaint) -> None:
    """A clock started at mount would report ``paint_ms``; this one reports the walk to it."""
    assert paint.total_ms > paint.paint_ms


def test_the_first_paint_is_meaningful(paint: ColdPaint) -> None:
    assert paint.painted_rows >= 1


def _reading(**overrides: float | int) -> dict[str, Any]:
    """Return a self-consistent cold-paint payload with ``overrides`` applied."""
    payload: dict[str, Any] = {
        "spawn_ms": 20.0,
        "import_ms": 700.0,
        "fixture_ms": 3.0,
        "paint_ms": 50.0,
        "total_ms": 773.0,
        "painted_rows": 24,
    }
    payload.update(overrides)
    return payload


def test_a_self_consistent_reading_is_accepted() -> None:
    accepted = ColdPaint.model_validate(_reading())
    assert accepted.components() == {
        "spawn": 20.0,
        "import": 700.0,
        "fixture": 3.0,
        "paint": 50.0,
    }


def test_a_reading_at_the_edge_of_the_sum_tolerance_is_accepted() -> None:
    assert ColdPaint.model_validate(_reading(total_ms=773.0 + 1e-4)).total_ms > 773.0


def test_a_reading_whose_components_miss_its_total_is_refused() -> None:
    with pytest.raises(ValidationError, match="components miss the total"):
        ColdPaint.model_validate(_reading(total_ms=900.0))


def test_a_reading_that_leaves_the_import_graph_out_of_its_total_is_refused() -> None:
    """Dropping the import cost is the exact defect the named components exist to stop."""
    with pytest.raises(ValidationError, match="components miss the total"):
        ColdPaint.model_validate(_reading(import_ms=0.0))


def test_a_reading_with_a_negative_component_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 0"):
        ColdPaint.model_validate(_reading(spawn_ms=-1.0, total_ms=752.0))


def test_a_reading_of_an_empty_first_frame_is_refused() -> None:
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        ColdPaint.model_validate(_reading(painted_rows=0))


def test_a_reading_carrying_an_unknown_component_is_refused() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        ColdPaint.model_validate(_reading() | {"teardown_ms": 1.0})


def test_a_cold_paint_over_a_directory_with_no_registers_is_refused(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        PerformanceHarness(tmp_path).cold_paint()
