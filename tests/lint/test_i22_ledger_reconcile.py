"""Tests for the I23-W02 ledger reconciliation.

Pins two reconciliation outcomes so they cannot silently regress: the
eight ratified Decision rows exist in both the decision store and state,
and the three ``[P30-I21-W22]`` prefix-rider commit SHAs are annotated on
the wave description so the wave ledger matches the commit ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SNAPSHOT = _REPO_ROOT / "tests" / "fixtures" / "epoch1-snapshot"
_STATE = _SNAPSHOT / "state.json"
_DECISION_STORE = _SNAPSHOT / "store" / "decision.jsonl"

_RATIFIED_DECISION_IDS = (
    "D-LOCK-SPLIT",
    "D-RPC-TERMINAL",
    "D-TEETH",
    "D-EU-CAPTURE",
    "D-I21-RATIFY",
    "D-WINDOWS-DESCOPE",
    "D-HISTORY-ACCEPT",
    "D-BRANCH-GC",
)

_W22_RIDER_SHAS = ("e77834ee", "46149415", "fcc2bdad")


@pytest.fixture(scope="module")
def state() -> dict[str, object]:
    return json.loads(_STATE.read_text(encoding="utf-8"))


def test_ratified_decision_rows_in_store_and_state(state: dict[str, object]) -> None:
    store_ids = {
        json.loads(line)["id"]
        for line in _DECISION_STORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    state_ids = set(state["decisions"])  # type: ignore[arg-type]
    missing_store = [d for d in _RATIFIED_DECISION_IDS if d not in store_ids]
    missing_state = [d for d in _RATIFIED_DECISION_IDS if d not in state_ids]
    assert not missing_store, f"decision store missing ratified rows: {missing_store}"
    assert not missing_state, f"state.json missing ratified decision keys: {missing_state}"


def test_w22_rider_shas_annotated_on_wave_description(state: dict[str, object]) -> None:
    waves = state["waves"]  # type: ignore[index]
    description = waves["P30-I21-W22"].get("description") or ""  # type: ignore[index]
    missing = [sha for sha in _W22_RIDER_SHAS if sha not in description]
    assert not missing, f"P30-I21-W22 description missing rider SHAs: {missing}"
