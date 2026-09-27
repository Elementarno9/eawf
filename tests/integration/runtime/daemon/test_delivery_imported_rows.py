"""Native delivery reads only native Tasks from a tree the cutover migrated."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.delivery import stored_tasks

#: A native Task row as the live tree stores it.
NATIVE_TASK: dict[str, Any] = {
    "active_run_ref": "eawf://EAWF/EAWF/EAWF/run/RUN-00000005",
    "batch_ref": "eawf://EAWF/EAWF/EAWF/batch/BAT-0101",
    "contract_revision": 1,
    "created_at": "2026-09-26T22:44:48.921387Z",
    "criteria": [
        {
            "acceptance_style": "binary",
            "accepted_risk_decision_ref": None,
            "contract_refs": [],
            "evidence_kind": "deterministic",
            "gate_ids": ["G-01"],
            "grounding": "measured",
            "id": "CR-001",
            "kind": "behavioral",
            "measurable_signal": "uv run pytest over the tests this Task names exits zero",
            "oracle_tier": None,
            "quality_dimension": "functional_suitability",
            "required": True,
            "response": {
                "expected": None,
                "gate_ref": "command_exit_zero",
                "jury_reason": None,
                "locus": "pytest",
                "object": "zero from the tests this Task names",
                "observe": "exits",
                "quantifier": "single",
            },
            "text": "Each of UI-004, UI-011, UI-033, UI-034, UI-039, UI-040, "
            "UI-058, UI-059 holds as its packet row states, and a test "
            "module that names the id proves it",
            "waiver_reason": None,
        }
    ],
    "due_scope": "eawf://EAWF/EAWF/EAWF/milestone/MLS-0101",
    "integrated_binding": None,
    "intent": "Close the console route registry and its pane and rail composition, so "
    "every native frame is drawn from one declared route table rather than "
    "per-route code.",
    "key": "EAWF-0101",
    "origin": {
        "confidence": "exact",
        "kind": "native",
        "mapping_basis": "native",
        "source_digest": None,
        "source_id": None,
        "source_kind": None,
        "source_schema_version": None,
        "source_urn": None,
    },
    "priority": "P1",
    "revision": 3,
    "status": "RUNNING",
    "uid": "b1d39360-a15f-5d8b-b221-4a7d133428f1",
    "updated_at": "2026-09-27T19:12:53.799576Z",
    "urn": "eawf://EAWF/EAWF/EAWF/task/EAWF-0101",
}

#: A Task the cutover imported: the epoch-1 record wrapped in a payload, no URN.
IMPORTED_ROW: dict[str, Any] = {
    "payload": {"annotations": [], "legacy_refs": {"title": "an imported backlog row"}},
    "recorded_at": "2026-09-26T21:14:53.351831+00:00",
    "status": "DROPPED",
}

#: A legacy ledger line as the cutover wrote it for a terminated wave.
LEGACY_LEDGER_LINE = json.dumps(
    {
        "schema_version": "2.0",
        "collection": "task",
        "record_key": "P08-I01-W01",
        "status": "COMPLETED",
        "recorded_at": "2026-09-26T21:14:53.351831Z",
        "supersedes": None,
        "payload": {
            "lifecycle": "wave",
            "source_collection": "waves",
            "target": "task",
            "target_status": "COMPLETED",
            "origin": {
                "kind": "legacy",
                "source_schema_version": "1.20",
                "source_kind": "waves",
                "source_id": "P08-I01-W01",
                "source_urn": None,
                "source_digest": None,
                "mapping_basis": "mechanical",
                "confidence": "supported",
            },
            "record": {
                "batch_ref": "P08-I01",
                "created_at": "2026-05-10T21:46:45.260116Z",
                "intent": "B025 subagent prompt renderer (wave dispatch)",
                "priority": "P2",
                "status": "COMPLETED",
            },
            "legacy_refs": {
                "title": "B025 subagent prompt renderer (wave dispatch)",
                "description": None,
                "deps": ["P08-I01-W02"],
                "blocks": [],
                "file_scopes": [
                    "src/eawf/dispatch/**",
                    "src/eawf/cli/commands/wave_dispatch.py",
                    "tests/dispatch/**",
                ],
                "gates": [],
                "agent_role": None,
                "effort_bucket": None,
                "claim_session_id": "SES-20260510T214731Z-operator-claude",
                "worktree_id": "WT-P08-I01-W01-1778451220",
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": "subagent prompt renderer + wave dispatch/dispatch-batch CLI (17 tests)",
                "commit": None,
                "commit_identity_digest": None,
                "claimed_at": None,
                "closed_at": "2026-05-10T22:29:29.132215Z",
                "runtime_baseline": None,
                "runtime_latest": None,
                "runtime_carry": None,
                "runtime_preference": None,
                "dispatch_history": [],
                "sessions": {},
                "criteria_floor_waiver": None,
            },
            "legacy_session_ref": "SES-20260510T214731Z-operator-claude",
            "criteria": [],
            "minted_runs": [
                {
                    "run_source": "resolving_claimed_wave_id",
                    "source_id": "SES-20260510T214731Z-operator-claude",
                    "wave_id": "P08-I01-W01",
                    "attempt_key": None,
                    "exit_status": None,
                    "binding": None,
                    "status": "TERMINAL_UNCLASSIFIED",
                }
            ],
            "deferred_fields": [
                {
                    "target_field": "contract_revision",
                    "reason": "source_has_no_field",
                    "candidates": [],
                },
                {"target_field": "due_scope", "reason": "source_has_no_field", "candidates": []},
                {"target_field": "criteria", "reason": "source_has_no_field", "candidates": []},
                {
                    "target_field": "integrated_binding",
                    "reason": "source_has_no_field",
                    "candidates": [],
                },
            ],
            "resolution": None,
            "obsolete": False,
            "annotations": ["priority_default_p2", "intent_from_title"],
            "ledger_annotations": ["priority_default_p2", "intent_from_title"],
        },
    }
)


class _Session:
    """The two reads stored_tasks makes of a root session."""

    def __init__(self, document: dict[str, Any], ledger: Path) -> None:
        self._document = document
        self._ledger = ledger

    def read_document(self) -> dict[str, Any]:
        return self._document

    def ledger_path(self, collection: Epoch2Collection) -> Path:
        assert collection is Epoch2Collection.TASK
        return self._ledger


def _session(tmp_path: Path, rows: dict[str, Any], ledger_lines: list[str]) -> Any:
    ledger = tmp_path / "task.jsonl"
    ledger.write_text("".join(f"{line}\n" for line in ledger_lines), encoding="utf-8")
    return _Session({"task": rows}, ledger)


def test_stored_tasks_skips_imported_document_rows_and_legacy_ledger_lines(
    tmp_path: Path,
) -> None:
    session = _session(
        tmp_path, {"EAWF-0101": NATIVE_TASK, "B001": IMPORTED_ROW}, [LEGACY_LEDGER_LINE]
    )
    assert [task.urn.entity_key for task in stored_tasks(session)] == ["EAWF-0101"]


def test_stored_tasks_on_a_tree_holding_only_imported_rows_is_empty(tmp_path: Path) -> None:
    session = _session(tmp_path, {"B001": IMPORTED_ROW}, [LEGACY_LEDGER_LINE])
    assert stored_tasks(session) == ()


def test_stored_tasks_on_an_empty_tree_is_empty(tmp_path: Path) -> None:
    assert stored_tasks(_session(tmp_path, {}, [])) == ()
