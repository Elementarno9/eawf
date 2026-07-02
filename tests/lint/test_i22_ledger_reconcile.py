"""Tests for the I23-W02 ledger reconciliation.

Pins the three reconciliation outcomes so they cannot silently regress:
the eight ratified Decision rows exist in both the decision store and
state, iter ``P30-I20`` is closed with a standalone audit artifact (the
INC-P30-07 lesson), and the three ``[P30-I21-W22]`` prefix-rider commit
SHAs are annotated on the wave description so the wave ledger matches
the commit ledger.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE = _REPO_ROOT / ".ea" / "state.json"
_DECISION_STORE = _REPO_ROOT / ".ea" / "store" / "decision.jsonl"
_AUDITS_DIR = _REPO_ROOT / ".ea" / "artifacts" / "audits"

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


def test_iter_p30_i20_closed_with_audit_artifact(state: dict[str, object]) -> None:
    iters = state["iters"]  # type: ignore[index]
    assert iters["P30-I20"]["status"] == "closed", (  # type: ignore[index]
        "P30-I20 is not closed in state.json"
    )
    matches = [p.name for p in _AUDITS_DIR.iterdir() if "P30-I20" in p.name]
    assert matches, (
        "no audit artifact whose filename contains 'P30-I20' under .ea/artifacts/audits/"
    )


def test_w22_rider_shas_annotated_on_wave_description(state: dict[str, object]) -> None:
    waves = state["waves"]  # type: ignore[index]
    description = waves["P30-I21-W22"].get("description") or ""  # type: ignore[index]
    missing = [sha for sha in _W22_RIDER_SHAS if sha not in description]
    assert not missing, f"P30-I21-W22 description missing rider SHAs: {missing}"
