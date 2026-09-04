from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations.v1_8_to_v1_9 import MigrationV18ToV19
from eawf.kernel.state.enums import WaveStatus
from eawf.kernel.state.models import RuntimeBaseline, State, Wave

_TS = "2026-06-10T00:00:00Z"

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the migrated wave validates against the typed model",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the migrated wave validates against the typed model",
}


def _runtime_baseline_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "api_duration_ms": 100,
        "total_duration_ms": 150,
        "cost_usd": 0.25,
        "input_tokens": 10,
        "output_tokens": 20,
        "cache_creation_input_tokens": 3,
        "cache_read_input_tokens": 7,
        "captured_at": _TS,
    }
    payload.update(overrides)
    return payload


def _wave_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.PENDING.value,
        "deps": [],
        "blocks": [],
        "file_scopes": [],
        "success_criteria": [_CRITERION],
        "gates": [],
        "opened_at": _TS,
        "closed_at": None,
    }
    payload.update(overrides)
    return payload


def _state_v1_8(*, include_runtime_baseline: bool = False) -> dict[str, Any]:
    wave = _wave_payload()
    if include_runtime_baseline:
        wave["runtime_baseline"] = _runtime_baseline_payload()
    return {
        "schema_version": "1.8",
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
            "phase_id": "P00",
            "iter_id": "P00-I01",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "dispatch_paused": False,
        "phases": {
            "P00": {
                "id": "P00",
                "scope_id": "QR",
                "track_id": None,
                "title": "Phase zero",
                "status": "active",
                "iter_ids": ["P00-I01"],
                "outcome_ids": [],
                "depends_on": [],
                "source_brief_ids": [],
                "opened_at": _TS,
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P00-I01": {
                "id": "P00-I01",
                "phase_id": "P00",
                "title": "Iter one",
                "status": "active",
                "wave_ids": ["P00-I01-W01"],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _TS,
                "closed_at": None,
            }
        },
        "waves": {"P00-I01-W01": wave},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def test_runtime_baseline_requires_captured_at() -> None:
    payload = _runtime_baseline_payload()
    del payload["captured_at"]
    with pytest.raises(ValidationError):
        RuntimeBaseline.model_validate(payload)


def test_runtime_baseline_rejects_extra_fields() -> None:
    payload = _runtime_baseline_payload(extra_counter=1)
    with pytest.raises(ValidationError):
        RuntimeBaseline.model_validate(payload)


def test_wave_runtime_baseline_defaults_to_none() -> None:
    wave = Wave.model_validate(_wave_payload())
    assert wave.runtime_baseline is None


def test_wave_runtime_baseline_round_trips_when_set() -> None:
    baseline = _runtime_baseline_payload()
    wave = Wave.model_validate(_wave_payload(runtime_baseline=baseline))
    assert wave.runtime_baseline is not None
    assert wave.runtime_baseline.captured_at == datetime.fromisoformat(
        _TS.replace("Z", "+00:00")
    ).astimezone(UTC)
    reloaded = Wave.model_validate(wave.model_dump(mode="json"))
    assert reloaded.runtime_baseline == wave.runtime_baseline


def test_v1_8_to_v1_9_backfills_runtime_baseline_none() -> None:
    out = MigrationV18ToV19().apply(_state_v1_8())
    assert out["schema_version"] == "1.9"
    assert out["waves"]["P00-I01-W01"]["runtime_baseline"] is None


def test_v1_8_to_v1_9_preserves_existing_runtime_baseline() -> None:
    out = MigrationV18ToV19().apply(_state_v1_8(include_runtime_baseline=True))
    baseline = out["waves"]["P00-I01-W01"]["runtime_baseline"]
    assert baseline["api_duration_ms"] == 100
    assert baseline["captured_at"] == _TS


def test_v1_8_to_v1_9_does_not_mutate_input() -> None:
    src = _state_v1_8()
    out = MigrationV18ToV19().apply(src)

    assert out["schema_version"] == "1.9"
    assert src["schema_version"] == "1.8"
    assert "runtime_baseline" not in src["waves"]["P00-I01-W01"]


def test_v1_8_to_v1_9_output_validates_against_live_model() -> None:
    out = MigrationV18ToV19().apply(_state_v1_8())
    state = State.model_validate(out)
    assert state.schema_version == "1.9"
    assert state.waves["P00-I01-W01"].runtime_baseline is None


def test_v1_8_to_v1_9_pre_post_version_guards() -> None:
    step = MigrationV18ToV19()
    with pytest.raises(ValidationError):
        step.check_pre({"schema_version": "1.7"})
    with pytest.raises(ValidationError):
        step.check_post({"schema_version": "1.8"})
