"""Claim-time runtime baseline capture (P30-I25-W26).

``claim_wave`` stamps ``Wave.runtime_baseline`` so the close-time delta has a
"before" snapshot to subtract against. That baseline used to come only from the
statusline runtime-counter sidecar, which only ``eawf statusline`` writes -- an
operator running any other statusline never got one, and
``compute_runtime_delta`` bails on a null baseline BEFORE it reads
``runtime_latest``, so EU stayed at zero even with the Stop hook fixed.

The baseline now comes from the claiming session's transcript (every Claude Code
session writes one), with the sidecar kept as the fallback.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtime_counter_sidecar import (
    RuntimeCounterSidecar,
    sidecar_path_for_statusline_cache,
)
from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters
from eawf.runtime.runtimes.claude.statusline import cache_path_for
from eawf.workflow.lifecycle.wave import _capture_runtime_baseline

_SESSION = "sess-placeholder-eu26"


@pytest.fixture(autouse=True)
def _isolated_counter_sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point both counter sources at empty tmp roots (no ambient session data)."""
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))
    monkeypatch.setenv("EAWF_STATUSLINE_CACHE", str(tmp_path / "statusline-cache"))
    (tmp_path / "projects").mkdir()
    (tmp_path / "statusline-cache").mkdir()


def _write_transcript(tmp_path: Path, *, cwd: Path) -> None:
    """Write a two-message transcript for ``_SESSION`` under *cwd*'s project dir."""
    project = tmp_path / "projects" / str(cwd).replace("/", "-").replace(".", "-")
    project.mkdir(parents=True, exist_ok=True)
    # Timestamped, and spanning the turn it reports: the duration is bounded by the
    # lifetime the rows demonstrate, so a turn of 61s needs a transcript that lived
    # at least that long (P30-I25-W50).
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-07-13T09:00:00.000Z",
            "message": {
                "id": "msg_0001",
                "model": "claude-opus-4-8",
                "usage": {
                    "input_tokens": 12,
                    "output_tokens": 34,
                    "cache_creation_input_tokens": 500,
                    "cache_read_input_tokens": 900,
                },
            },
        },
        {
            "type": "system",
            "subtype": "turn_duration",
            "timestamp": "2026-07-13T09:01:05.000Z",
            "durationMs": 61_000,
        },
    ]
    (project / f"{_SESSION}.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def _write_sidecar() -> None:
    """Write a statusline runtime-counter sidecar for ``_SESSION``."""
    sidecar = RuntimeCounterSidecar(sidecar_path_for_statusline_cache(cache_path_for(_SESSION)))
    sidecar.write(
        RuntimeCounters(
            api_duration_ms=7_000,
            total_duration_ms=9_000,
            cost_usd=None,
            input_tokens=1,
            output_tokens=2,
            harness="claude-code",
            model="claude-sonnet-5",
        )
    )


def test_baseline_from_claiming_session_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _write_transcript(tmp_path, cwd=cwd)
    monkeypatch.chdir(cwd)

    baseline = _capture_runtime_baseline(_SESSION)

    assert baseline is not None
    assert baseline.api_duration_ms == 61_000
    assert baseline.input_tokens == 12
    assert baseline.output_tokens == 34
    assert baseline.cache_creation_input_tokens == 500
    assert baseline.cache_read_input_tokens == 900
    # Attribution rides the baseline so the close-time actual is calibratable.
    assert baseline.harness == "claude-code"
    assert baseline.model == "claude-opus-4-8"
    assert baseline.cost_usd is not None


def test_baseline_falls_back_to_statusline_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No transcript for the session, but an `eawf statusline` operator's sidecar
    # is there -- current behaviour must survive.
    monkeypatch.chdir(tmp_path)
    _write_sidecar()

    baseline = _capture_runtime_baseline(_SESSION)

    assert baseline is not None
    assert baseline.api_duration_ms == 7_000
    assert baseline.total_duration_ms == 9_000
    assert baseline.model == "claude-sonnet-5"


def test_baseline_prefers_transcript_over_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    cwd = tmp_path / "repo"
    cwd.mkdir()
    _write_transcript(tmp_path, cwd=cwd)
    monkeypatch.chdir(cwd)
    _write_sidecar()

    baseline = _capture_runtime_baseline(_SESSION)

    assert baseline is not None
    assert baseline.api_duration_ms == 61_000
    assert baseline.model == "claude-opus-4-8"


def test_baseline_none_without_transcript_or_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)

    # Honest degrade: an uninstrumented claim records no baseline rather than
    # subtracting the close-time counters against a phantom zero.
    assert _capture_runtime_baseline(_SESSION) is None
