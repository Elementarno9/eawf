"""The calibration store reader for persisted juror ballots.

``read_recorded_ballots`` rebuilds the per-wave :class:`JurorBallot` map
from the ``jury_ballot`` envelope rows, and the daemon's
``_load_recorded_ballots`` seam reads it live.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.kernel.state.enums import AgentReportVerdict, StoreKind
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.jury_validation import read_recorded_ballots

pytestmark = pytest.mark.unit

_WAVE = "P29-I04-W08"
_RUNTIMES = ("claude-code", "codex", "opencode")


def _seed_state(tmp_path: Path) -> Path:
    """Create ``<tmp>/.ea/state.json`` and return its path."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text("{}", encoding="utf-8")
    return state_path


def _write_ballot_rows(state_path: Path, payloads: list[dict[str, object]]) -> None:
    """Append one envelope-shaped row per payload to the ballot store."""
    ballot_path = store_path(state_path, StoreKind.JURY_BALLOT)
    ballot_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps({"payload": payload}) for payload in payloads]
    ballot_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_read_recorded_ballots_rebuilds_per_wave_map(tmp_path: Path) -> None:
    """The reader folds the persisted rows into the validate_jury map shape."""
    state_path = _seed_state(tmp_path)
    _write_ballot_rows(
        state_path,
        [{"wave_id": _WAVE, "runtime": runtime, "verdict": "pass"} for runtime in _RUNTIMES],
    )

    ballots = read_recorded_ballots(state_path)
    assert set(ballots) == {_WAVE}
    assert len(ballots[_WAVE]) == len(_RUNTIMES)
    assert {b.runtime for b in ballots[_WAVE]} == set(_RUNTIMES)
    assert all(b.verdict is AgentReportVerdict.PASS for b in ballots[_WAVE])


def test_read_recorded_ballots_skips_abstentions(tmp_path: Path) -> None:
    """A row with an error and no verdict is an abstention and casts no vote."""
    state_path = _seed_state(tmp_path)
    _write_ballot_rows(
        state_path,
        [
            {"wave_id": _WAVE, "runtime": "codex", "verdict": "fail"},
            {"wave_id": _WAVE, "runtime": "opencode", "error": "spawn failed"},
        ],
    )

    ballots = read_recorded_ballots(state_path)
    assert [b.runtime for b in ballots[_WAVE]] == ["codex"]
    assert ballots[_WAVE][0].verdict is AgentReportVerdict.FAIL


def test_read_recorded_ballots_honest_empty_and_malformed(tmp_path: Path) -> None:
    """Missing store -> {}; malformed lines are skipped, not raised."""
    state_path = _seed_state(tmp_path)
    assert read_recorded_ballots(state_path) == {}

    ballot_path = store_path(state_path, StoreKind.JURY_BALLOT)
    ballot_path.parent.mkdir(parents=True, exist_ok=True)
    ballot_path.write_text("not json\n", encoding="utf-8")
    assert read_recorded_ballots(state_path) == {}
