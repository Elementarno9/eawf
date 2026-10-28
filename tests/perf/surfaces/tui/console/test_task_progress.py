"""Measure console frame budgets in the serial performance lane."""

from __future__ import annotations

import statistics
import time
from collections.abc import Callable
from typing import Any

import pytest

from eawf.surfaces.tui.console.chrome import load_chrome
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.frame import View
from eawf.surfaces.tui.console.perf import KEY_FLOOR_CEILING_MS, ROW_INCREMENT_CEILING_US
from eawf.surfaces.tui.console.renderers import render_route
from eawf.surfaces.tui.console.session import SIZES, Session
from tests.tui.surfaces.tui.console.test_task_progress import _batch, _model, _task, document


def big_document(tasks: int) -> dict[str, Any]:
    """Return the probe Track with *tasks* Tasks spread ten to a Batch."""
    doc = document()
    doc["batch"] = {}
    doc["task"] = {}
    for n in range(tasks // 10):
        key = f"BAT-{n:04d}"
        doc["batch"][key] = _batch(key, "ACTIVE" if n == 0 else "PLANNED")
        for m in range(10):
            task = f"TSK-{n * 10 + m:05d}"
            doc["task"][task] = _task(task, "READY_TO_INTEGRATE", key)
    return doc


def _median_ms(draw: Callable[[], object], samples: int = 15) -> float:
    draw()
    times = []
    for _ in range(samples):
        start = time.perf_counter()
        draw()
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


@pytest.mark.parametrize("route", ["scope.home", "milestone"])
def test_a_thousand_tasks_render_within_the_frame_budget(route: str) -> None:
    subject = "MLS-0100" if route == "milestone" else None

    def draw(model: Any) -> Callable[[], object]:
        session = Session()
        session.route = route
        session.subj_id = subject
        view = View(
            session=session,
            fixture=Fixture.from_chrome(load_chrome()),
            w=120,
            h=dict(SIZES)[120],
            linked=True,
            projection=model,
        )
        return lambda: render_route(view)

    small = _median_ms(draw(_model(route, big_document(1000))))
    large = _median_ms(draw(_model(route, big_document(4000))))
    assert small < KEY_FLOOR_CEILING_MS, f"{route} took {small:.2f}ms over 1,000 Tasks"
    per_row_us = (large - small) * 1000 / 3000
    assert per_row_us < ROW_INCREMENT_CEILING_US, f"{route} adds {per_row_us:.2f}us per Task"
