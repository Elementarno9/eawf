from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from eawf.kernel.state.migration import backfill_missing_wave_intents
from eawf.kernel.state.models import State

_TS = "2026-06-10T00:00:00Z"

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "pytest proves the synced wave criterion remains covered by repair",
    "kind": "behavioral",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "pytest asserts the synced wave criterion remains covered",
    "gate_ids": ["G-01"],
}

_GATE: dict[str, Any] = {
    "id": "G-01",
    "criterion_id": "CR-01",
    "kind": "command_exit_zero",
    "args": {"argv": ["uv", "run", "pytest", "-q"]},
    "policy": "block",
    "cadence": "every-wave",
}

_INTENT: dict[str, Any] = {
    "problem": "wave already has intent",
    "desired_outcome": "wave remains unchanged",
    "priority_rationale": "test fixture",
}


def _wave(
    wave_id: str,
    *,
    status: str = "closed",
    criteria: list[dict[str, Any]] | None = None,
    gates: list[dict[str, Any]] | None = None,
    intent: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": wave_id,
        "iter_id": "P01-I01",
        "title": "Repair fixture wave",
        "status": status,
        "deps": [],
        "blocks": [],
        "file_scopes": ["src/example.py"],
        "success_criteria": list(criteria if criteria is not None else [_CRITERION]),
        "gates": list(gates if gates is not None else [_GATE]),
        "agent_role": "executor",
        "effort_bucket": "S",
        "opened_at": _TS,
        "closed_at": _TS if status == "closed" else None,
        "outcome": "done" if status == "closed" else None,
        "intent": intent,
        "sessions": {},
    }


def _state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": "repo",
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _TS,
            "project": {
                "code": "QR",
                "slug": "quant-research",
                "title": "Quant Research",
                "description": "",
                "domains": ["quant"],
                "default_branch": "main",
                "status": "active",
                "repo_urn": "urn:eawf:v1:repo:QR",
            },
            "current": {
                "project_code": "QR",
                "track_id": None,
                "phase_id": "P01",
                "iter_id": "P01-I01",
                "active_wave_ids": [],
                "active_session_ids": [],
            },
            "workspace": None,
            "phases": {
                "P01": {
                    "id": "P01",
                    "scope_id": "QR",
                    "track_id": None,
                    "title": "Phase one",
                    "status": "active",
                    "iter_ids": ["P01-I01"],
                    "outcome_ids": [],
                    "opened_at": _TS,
                    "closed_at": None,
                    "audit_id": None,
                }
            },
            "iters": {
                "P01-I01": {
                    "id": "P01-I01",
                    "phase_id": "P01",
                    "title": "Iter one",
                    "status": "active",
                    "wave_ids": [
                        "P01-I01-W01",
                        "P01-I01-W02",
                        "P01-I01-W03",
                        "P01-I01-W04",
                    ],
                    "estimate_id": None,
                    "audit_id": None,
                    "opened_at": _TS,
                    "closed_at": None,
                }
            },
            "waves": {
                "P01-I01-W01": _wave("P01-I01-W01"),
                "P01-I01-W02": _wave("P01-I01-W02", status="pending"),
                "P01-I01-W03": _wave("P01-I01-W03", intent=_INTENT),
                "P01-I01-W04": _wave("P01-I01-W04", gates=[]),
            },
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def test_backfill_missing_wave_intents_dry_run_reports_without_mutating() -> None:
    state = _state()

    report = backfill_missing_wave_intents(
        state,
        wave_ids=["P01-I01-W01", "P01-I01-W03", "P01-I01-W04", "P01-I01-W99"],
        apply=False,
    )

    assert report.pending_wave_ids == ("P01-I01-W01",)
    assert [row.reason for row in report.rows] == [
        "missing_intent",
        "already_has_intent",
        "missing_gates",
        "unknown_wave",
    ]
    assert state.waves["P01-I01-W01"].intent is None


def test_backfill_missing_wave_intents_apply_sets_metadata_intent() -> None:
    state = _state()

    report = backfill_missing_wave_intents(
        state,
        wave_ids=["P01-I01-W01", "P01-I01-W02", "P01-I01-W01"],
        apply=True,
    )

    assert report.changed_wave_ids == ("P01-I01-W01",)
    assert [row.reason for row in report.rows] == ["backfilled", "not_closed:pending"]
    intent = state.waves["P01-I01-W01"].intent
    assert intent is not None
    assert intent.problem == "wave P01-I01-W01 was synced without typed intent"
    assert intent.planned_steps == [_CRITERION["text"]]
    assert intent.risks == ["metadata-only repair must not change the closed wave outcome"]
    assert state.waves["P01-I01-W02"].intent is None


def test_backfill_missing_wave_intents_repaired_wave_is_clean() -> None:
    state = _state()
    backfill_missing_wave_intents(state, wave_ids=["P01-I01-W01"], apply=True)

    report = backfill_missing_wave_intents(state, wave_ids=["P01-I01-W01"], apply=False)

    assert report.clean is True
    assert report.pending_wave_ids == ()
    assert report.rows[0].reason == "already_has_intent"
    assert state.updated_at == datetime.fromisoformat(_TS.replace("Z", "+00:00")).astimezone(UTC)
