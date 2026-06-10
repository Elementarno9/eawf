"""Tests for the runtime counter sidecar."""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from eawf.runtime.runtime_counter_sidecar import (
    RuntimeCounterSidecar,
    sidecar_path_for_statusline_cache,
)
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters


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
