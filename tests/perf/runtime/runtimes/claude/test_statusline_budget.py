"""Measure the status line against its render budgets.

Budgets are 95th percentiles (p95): a cold redraw in a fresh interpreter
should take at most 300 ms, the host's redraw debounce, and the cache
fallback, which prints the session's last line when the render raises or
overruns, at most 100 ms. The fallback budget is asserted; the cold one is
reported, because a fresh interpreter's import time is the machine's as much
as the code's. Every line must render under :data:`LINE_CEILING_SECONDS`.
What the line says is asserted in the blocking suite.
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.store.ledger import append_ledger_record
from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.runtime.runtimes.claude.statusline_theme import render_key
from eawf.runtime.statusline_cache import StatuslineCache
from tests.unit.runtime.runtimes.claude.test_statusline_epoch2_producers import (  # noqa: F401  (fixture)
    PAYLOAD,
    isolated_runtime,
    memory_ledger,
    memory_record,
    session_canary,
)

#: The ceiling a whole line must render under.
LINE_CEILING_SECONDS = 1.0

#: The p95 the cache fallback must stay under.
FALLBACK_P95_SECONDS = 0.100

#: The p95 a cold redraw should stay under; reported, not asserted.
COLD_P95_SECONDS = 0.300

_RUNS = 10

_skip_perf = pytest.mark.skipif(
    os.environ.get("EAWF_SKIP_PERF") == "1",
    reason="EAWF_SKIP_PERF=1 - perf gate skipped",
)


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=20, method="inclusive")[18]


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


@_skip_perf
def test_the_cache_fallback_stays_inside_its_p95_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The render raises at once, so each sample is the fallback alone: read
    # the entry, start the render thread, claim the refresh. The first sample
    # spawns the prewarm, a real detached process running an empty program,
    # and the later ones find the refresh in flight, as a run of redraws does.
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "cache"))
    raw = json.dumps({"session_id": "ses-perf", "model": "haiku"})
    StatuslineCache(
        orchestrator.cache_path_for("ses-perf"), key=render_key(None, os.environ)
    ).write("cached line", stamp=time.time())

    def _raise(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError("render failed")

    real_popen = subprocess.Popen

    def _empty_prewarm(_argv: list[str], **kwargs: Any) -> subprocess.Popen[bytes]:
        return real_popen([sys.executable, "-c", "pass"], **kwargs)

    monkeypatch.setattr(orchestrator, "_render_session", _raise)
    monkeypatch.setattr(subprocess, "Popen", _empty_prewarm)

    samples: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        served = orchestrator.serve_line(raw, workspace=None, theme_name=None)
        samples.append(time.perf_counter() - started)
        assert served.text == "cached line"

    assert _p95(samples) <= FALLBACK_P95_SECONDS


@_skip_perf
def test_a_cold_redraw_is_reported_against_its_p95_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # A fresh interpreter per sample with an empty cache, outside any managed
    # repository, as a new session's first redraw is.
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    payload = json.dumps({"session_id": "ses-cold", "model": "haiku"})
    samples: list[float] = []
    for run in range(_RUNS):
        env = {
            **os.environ,
            "HOME": str(home),
            "EAWF_STATUSLINE_CACHE": str(tmp_path / f"cache-{run}"),
        }
        started = time.perf_counter()
        proc = subprocess.run(
            [sys.executable, "-m", "eawf", "cc", "statusline"],
            input=payload,
            capture_output=True,
            text=True,
            env=env,
            cwd=tmp_path,
            timeout=60,
            check=True,
        )
        samples.append(time.perf_counter() - started)
        assert " haiku " in proc.stdout

    cold_p95 = _p95(samples)
    verdict = "within" if cold_p95 <= COLD_P95_SECONDS else "over"
    with capsys.disabled():
        print(
            f"\nstatusline cold redraw p95 {cold_p95 * 1000:.0f} ms over {_RUNS} runs,"
            f" {verdict} the {COLD_P95_SECONDS * 1000:.0f} ms budget"
        )
