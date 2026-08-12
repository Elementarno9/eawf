"""Juror-ballot persistence + the calibration store readers.

The calibration substrate was empty by construction: every convened
jury's ballots were dropped. Now the convener appends one
``jury_ballot`` envelope per juror, ``read_recorded_ballots`` rebuilds
the per-wave :class:`JurorBallot` map, and the daemon's
``_load_recorded_ballots`` seam (idle since P30-I23-W50) reads it live.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, StoreKind
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import (
    JURY_RUNTIME_FAMILIES,
    convene_cross_vendor_jury,
)
from eawf.observability.eval.jury_validation import read_recorded_ballots
from tests.eval.jury.test_cross_vendor_jury import (
    _WAVE_ID as _SIBLING_WAVE_ID,
)
from tests.eval.jury.test_cross_vendor_jury import (
    _auditor_body_json,
    _RecordingFactory,
    _RecordingSpawn,
    _write_state,
)

pytestmark = pytest.mark.unit

_WAVE = _SIBLING_WAVE_ID


def test_convened_jury_persists_one_ballot_row_per_juror(tmp_path: Path) -> None:
    """CR-01: three stub jurors -> three envelope rows in jury_ballot.jsonl."""
    state, state_path, events_path = _write_state(tmp_path)
    wave = state.waves[_WAVE]
    factory = _RecordingFactory(
        {
            "claude-code": _RecordingSpawn("claude-code", [_auditor_body_json(verdict="pass")]),
            "codex": _RecordingSpawn("codex", [_auditor_body_json(verdict="fail")]),
            "opencode": _RecordingSpawn("opencode", [_auditor_body_json(verdict="pass")]),
        }
    )

    asyncio.run(
        convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=factory,
            repo_root=tmp_path,
        )
    )

    ballot_path = store_path(state_path, StoreKind.JURY_BALLOT)
    assert ballot_path.is_file()
    rows = [json.loads(line) for line in ballot_path.read_text().splitlines() if line.strip()]
    assert len(rows) == len(JURY_RUNTIME_FAMILIES)
    runtimes = {row["payload"]["runtime"] for row in rows}
    assert runtimes == set(JURY_RUNTIME_FAMILIES)
    verdicts = {row["payload"]["runtime"]: row["payload"]["verdict"] for row in rows}
    assert verdicts["codex"] == "fail"


def test_read_recorded_ballots_rebuilds_per_wave_map(tmp_path: Path) -> None:
    """The reader folds the persisted rows into the validate_jury map shape."""
    state, state_path, events_path = _write_state(tmp_path)
    wave = state.waves[_WAVE]
    factory = _RecordingFactory(
        {
            runtime: _RecordingSpawn(runtime, [_auditor_body_json(verdict="pass")])
            for runtime in JURY_RUNTIME_FAMILIES
        }
    )
    asyncio.run(
        convene_cross_vendor_jury(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn_factory=factory,
            repo_root=tmp_path,
        )
    )

    ballots = read_recorded_ballots(state_path)
    assert set(ballots) == {_WAVE}
    assert len(ballots[_WAVE]) == len(JURY_RUNTIME_FAMILIES)
    assert all(b.verdict is AgentReportVerdict.PASS for b in ballots[_WAVE])


def test_read_recorded_ballots_honest_empty_and_malformed(tmp_path: Path) -> None:
    """Missing store -> {}; malformed lines are skipped, not raised."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    assert read_recorded_ballots(state_path) == {}

    ballot_path = store_path(state_path, StoreKind.JURY_BALLOT)
    ballot_path.parent.mkdir(parents=True, exist_ok=True)
    ballot_path.write_text("not json\n", encoding="utf-8")
    assert read_recorded_ballots(state_path) == {}
