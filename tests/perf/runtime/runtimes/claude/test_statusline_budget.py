"""Measure the epoch-2 statusline against its render budget.

The line has a 100 ms cold target; the ceiling is generous because runners
are slow. What the line says is asserted in the blocking suite.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from eawf.kernel.store.ledger import append_ledger_record
from eawf.runtime.runtimes.claude import statusline as orchestrator
from tests.unit.runtime.runtimes.claude.test_statusline_epoch2_producers import (  # noqa: F401  (fixture)
    PAYLOAD,
    isolated_runtime,
    memory_ledger,
    memory_record,
    session_canary,
)

#: The ceiling a whole line must render under.
LINE_CEILING_SECONDS = 1.0

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


@_skip_perf
def test_epoch2_producers_render_the_line_inside_the_statusline_budget(tmp_path: Path) -> None:
    state_path = session_canary(tmp_path)
    append_ledger_record(memory_ledger(state_path), memory_record("MEM-1"))
    payload = {**PAYLOAD, "cwd": str(tmp_path)}

    started = time.perf_counter()
    segments = orchestrator._build_segments(payload, state_path)
    elapsed = time.perf_counter() - started

    assert segments
    assert elapsed < LINE_CEILING_SECONDS
