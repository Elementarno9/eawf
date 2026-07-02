"""Incident store-to-state fold parity (P30-I23-W21).

A stale-cache clobber wiped five of six ``state.incidents`` rows while the
append-only ``incident.jsonl`` store kept the history — store and state
disagreed and nothing noticed. W21 backfills the state map through the
daemon mutators and pins parity twice: this suite asserts the committed
repo's fold is whole (CR-01), and the doctor payload carries the two
fold-parity checks that keep it whole (CR-02).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.kernel.state.models import State
from eawf.observability.doctor import checks as doctor_checks
from eawf.observability.doctor.checks import (
    CheckResult,
    check_backlog_fold_parity,
    check_incident_fold_parity,
)
from tests.daemon.test_close_lock_split import _state_payload

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[3]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_INCIDENT_STORE = _REPO_ROOT / ".ea" / "store" / "incident.jsonl"


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
    assert incidents["INC-P30-08"]["status"] == "open"
    # INC-P30-07 reconciled citing the W02 I20 backfill audit.
    reconciled = incidents["INC-P30-07"]
    assert reconciled["status"] == "resolved"
    assert "P30-I23-W02" in reconciled["corrective_action_ids"]
    # The three new typed classes are open rows.
    for new_id in ("INC-P30-09", "INC-P30-10", "INC-P30-11"):
        assert incidents[new_id]["status"] == "open"


def test_doctor_payload_carries_both_fold_parity_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CR-02: run_all's payload includes incident- and backlog-fold-parity."""

    def _stub_probe(**_kwargs: Any) -> CheckResult:
        return CheckResult(name="tools_available", status="ok", detail="stubbed")

    monkeypatch.setattr(doctor_checks, "check_tools_available", _stub_probe)
    results = doctor_checks.run_all(workspace=_REPO_ROOT)
    names = {result.name for result in results}
    assert "incident_fold_parity" in names
    assert "backlog_fold_parity" in names
    by_name = {result.name: result for result in results}
    assert by_name["incident_fold_parity"].status == "ok", by_name["incident_fold_parity"].detail
    assert by_name["backlog_fold_parity"].status == "ok", by_name["backlog_fold_parity"].detail


def _seed_tmp_repo(tmp_path: Path, *, incidents: dict[str, Any]) -> Path:
    payload = _state_payload()
    payload["incidents"] = incidents
    state = State.model_validate(payload)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state_path


def _incident_row(incident_id: str, *, status: str) -> dict[str, Any]:
    return {
        "id": incident_id,
        "scope_id": "ABC",
        "severity": "medium",
        "title": "synthetic incident",
        "status": status,
        "opened_at": "2026-07-02T12:00:00+00:00",
        "closed_at": None,
        "root_cause": None,
        "cause": "unknown",
        "corrective_action_ids": [],
        "report_artifact_id": None,
    }


def test_incident_fold_parity_fails_on_missing_state_row(tmp_path: Path) -> None:
    """A store base-id absent from state.incidents flips the check to fail."""
    state_path = _seed_tmp_repo(tmp_path, incidents={})
    store = state_path.parent / "store" / "incident.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('{"id": "INC-X-01"}\n', encoding="utf-8")
    result = check_incident_fold_parity(workspace=tmp_path)
    assert result.status == "fail"
    assert "INC-X-01" in (result.detail or "")


def test_incident_fold_parity_folds_close_rows_onto_base_id(tmp_path: Path) -> None:
    """An ``<id>-CLOSE`` row is the close event, not a distinct entity."""
    state_path = _seed_tmp_repo(
        tmp_path, incidents={"INC-X-01": _incident_row("INC-X-01", status="resolved")}
    )
    store = state_path.parent / "store" / "incident.jsonl"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text('{"id": "INC-X-01"}\n{"id": "INC-X-01-CLOSE"}\n', encoding="utf-8")
    result = check_incident_fold_parity(workspace=tmp_path)
    assert result.status == "ok"


def test_backlog_fold_parity_ok_without_store(tmp_path: Path) -> None:
    """No backlog store on disk is honest-absent, not a parity break."""
    _seed_tmp_repo(tmp_path, incidents={})
    result = check_backlog_fold_parity(workspace=tmp_path)
    assert result.status == "ok"
    assert "no backlog.jsonl store" in (result.detail or "")
