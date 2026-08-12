"""Tests for the runtime counter sidecar."""

from __future__ import annotations

import io
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest

from eawf.runtime.runtime_counter_sidecar import (
    RuntimeCounterSidecar,
    sidecar_path_for_statusline_cache,
)
from eawf.runtime.runtimes.claude import statusline
from eawf.runtime.runtimes.claude.runtime_counters import (
    STATUSLINE_MEASURE_VERSION,
    RuntimeCounters,
)


def test_runtime_counter_sidecar_round_trips_counters(tmp_path: Path) -> None:
    sidecar = RuntimeCounterSidecar(tmp_path / "ses.runtime-counters.json")
    counters = RuntimeCounters(
        api_duration_ms=1200,
        total_duration_ms=3400,
        cost_usd=Decimal("0.0175"),
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
    )

    sidecar.write(counters)

    assert sidecar.read() == counters


def test_runtime_counter_sidecar_missing_file_reads_none(tmp_path: Path) -> None:
    sidecar = RuntimeCounterSidecar(tmp_path / "missing.runtime-counters.json")

    assert sidecar.read() is None


def test_runtime_counter_sidecar_corrupt_json_reads_none(tmp_path: Path) -> None:
    path = tmp_path / "bad.runtime-counters.json"
    path.write_text("{", encoding="utf-8")

    assert RuntimeCounterSidecar(path).read() is None


def test_runtime_counter_sidecar_path_sits_beside_statusline_cache() -> None:
    cache_path = Path("cache") / "session-1.json"

    sidecar_path = sidecar_path_for_statusline_cache(cache_path)

    assert sidecar_path.parent == cache_path.parent
    assert sidecar_path.name == "session-1.runtime-counters.json"


def test_statusline_render_writes_runtime_counter_sidecar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(
            json.dumps(
                {
                    "session_id": "session-1",
                    "cost": {"api_duration_ms": 17000, "cost_usd": 0.42},
                    "context_window": {"current_usage": {"input_tokens": 100, "output_tokens": 50}},
                }
            )
        ),
    )
    monkeypatch.setattr(statusline, "render_pipeline", lambda *_args, **_kwargs: "line")

    assert statusline.run_with_cache(workspace=None, theme_name=None) == "line"

    counters = RuntimeCounterSidecar(tmp_path / "session-1.runtime-counters.json").read()
    assert counters == RuntimeCounters(
        api_duration_ms=17000,
        cost_usd=Decimal("0.42"),
        input_tokens=100,
        output_tokens=50,
        # The statusline declares its own measure, so a flip between
        # it and the transcript aggregator reads as a change of measure, not work.
        measure_version=STATUSLINE_MEASURE_VERSION,
        # W19 stamps the parser harness attribution onto every parsed counter
        # set; this payload carries no model block so ``model`` stays None.
        harness="claude-code",
        model=None,
    )
