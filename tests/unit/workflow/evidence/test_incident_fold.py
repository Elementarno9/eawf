"""Incident store-to-state fold parity.

A stale-cache clobber wiped five of six ``state.incidents`` rows while the
append-only ``incident.jsonl`` store kept the history — store and state
disagreed and nothing noticed. W21 backfills the state map through the
daemon mutators; this suite asserts the committed snapshot's fold is whole.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SNAPSHOT = _REPO_ROOT / "tests" / "fixtures" / "epoch1-snapshot"
_STATE_PATH = _SNAPSHOT / "state.json"
_INCIDENT_STORE = _SNAPSHOT / "store" / "incident.jsonl"


def _store_status_by_base_id() -> dict[str, str]:
    """Fold the append-only store rows to base-id -> expected state status."""
    statuses: dict[str, str] = {}
    for line in _INCIDENT_STORE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record_id = str(json.loads(line).get("id") or "")
        if not record_id:
            continue
        if record_id.endswith("-CLOSE"):
            statuses[record_id.removesuffix("-CLOSE")] = "resolved"
        else:
            statuses.setdefault(record_id, "open")
    return statuses


def test_every_store_base_id_folds_into_state_with_matching_status() -> None:
    """CR-01: distinct store base-ids == state.incidents keys, statuses agree."""
    raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    state_incidents = raw.get("incidents") or {}
    expected = _store_status_by_base_id()
    assert expected, "the incident store is empty — the fold premise is gone"
    for base_id, status in expected.items():
        assert base_id in state_incidents, f"store id {base_id} missing from state"
        assert state_incidents[base_id]["status"] == status, (
            f"{base_id}: store implies {status!r}, state has {state_incidents[base_id]['status']!r}"
        )


def test_backfill_restored_the_known_population() -> None:
    """CR-01: the daemon backfill restored the named incidents + new classes."""
    raw = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    incidents = raw.get("incidents") or {}
    for resolved_id in ("INC-P30-01", "INC-P30-02", "INC-P30-03", "INC-P30-06"):
        assert incidents[resolved_id]["status"] == "resolved"
    # INC-P30-08 closed later in the iter (W31 standing visual gates).
    assert incidents["INC-P30-08"]["status"] == "resolved"
    # INC-P30-07 reconciled citing the W02 I20 backfill audit.
    reconciled = incidents["INC-P30-07"]
    assert reconciled["status"] == "resolved"
    assert "P30-I23-W02" in reconciled["corrective_action_ids"]
    # The three new typed classes are open rows.
    for new_id in ("INC-P30-09", "INC-P30-10", "INC-P30-11"):
        assert incidents[new_id]["status"] == "open"
