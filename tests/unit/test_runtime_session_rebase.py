"""Cross-session runtime carry: the state migrations that backfill it.

Runtime counters are cumulative *within a session*, so a wave spanning N
sessions carries each finished session's total in ``Wave.runtime_carry``.
These tests pin how the v1.14 and v1.17 migrations backfill it.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations.v1_14_to_v1_15 import MigrationV114ToV115
from eawf.kernel.migrations.v1_17_to_v1_18 import MigrationV117ToV118
from eawf.kernel.state.enums import WaveStatus

_CRITERION: dict[str, Any] = {
    "id": "CR-01",
    "text": "the wave records a captured runtime at close",
    "kind": "legacy",
    "acceptance_style": "binary",
    "evidence_kind": "attested",
    "quality_dimension": "functional_suitability",
    "measurable_signal": "the wave records a captured runtime at close",
}


# --- v1.14 -> v1.15 migration ---------------------------------------------


def _state_v1_14(*, with_snapshots: bool) -> dict[str, Any]:
    wave: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.PENDING.value,
        "success_criteria": [_CRITERION],
        "opened_at": "2026-07-13T00:00:00Z",
    }
    if with_snapshots:
        wave["runtime_baseline"] = {"api_duration_ms": 10, "captured_at": "2026-07-13T00:00:00Z"}
        wave["runtime_latest"] = {"api_duration_ms": 90, "captured_at": "2026-07-13T00:00:00Z"}
    return {"schema_version": "1.14", "waves": {"P00-I01-W01": wave}}


def test_migration_backfills_session_id_and_carry() -> None:
    out = MigrationV114ToV115().apply(_state_v1_14(with_snapshots=True))

    assert out["schema_version"] == "1.15"
    wave = out["waves"]["P00-I01-W01"]
    assert wave["runtime_baseline"]["session_id"] is None
    assert wave["runtime_latest"]["session_id"] is None
    assert wave["runtime_carry"] is None
    # The historical counters are untouched.
    assert wave["runtime_baseline"]["api_duration_ms"] == 10


def test_migration_handles_waves_without_snapshots() -> None:
    out = MigrationV114ToV115().apply(_state_v1_14(with_snapshots=False))

    assert out["waves"]["P00-I01-W01"]["runtime_carry"] is None


def test_migration_does_not_mutate_input() -> None:
    src = _state_v1_14(with_snapshots=True)
    out = MigrationV114ToV115().apply(src)

    assert out["schema_version"] == "1.15"
    assert src["schema_version"] == "1.14"
    assert "runtime_carry" not in src["waves"]["P00-I01-W01"]


def test_migration_pre_post_version_guards() -> None:
    step = MigrationV114ToV115()
    with pytest.raises(Exception, match="schema_version"):
        step.check_pre({"schema_version": "1.13"})
    with pytest.raises(Exception, match="schema_version"):
        step.check_post({"schema_version": "1.14"})


# --- interactive attempt row: wave delta, not session totals ---------------


# --- P30-I25-W36: the rebase must not double-count or lose runtime ----------


# --- P30-I25-W37: a backwards counter must never strand a wave --------------


# --- P30-I25-W42: a changed measure is not work, in EITHER direction --------


# --- v1.17 -> v1.18 migration ----------------------------------------------


def _state_v1_17(*, with_snapshots: bool) -> dict[str, Any]:
    wave: dict[str, Any] = {
        "id": "P00-I01-W01",
        "iter_id": "P00-I01",
        "title": "Wave one",
        "status": WaveStatus.PENDING.value,
        "success_criteria": [_CRITERION],
        "opened_at": "2026-07-13T00:00:00Z",
    }
    if with_snapshots:
        wave["runtime_baseline"] = {"api_duration_ms": 10, "captured_at": "2026-07-13T00:00:00Z"}
        wave["runtime_latest"] = {"api_duration_ms": 90, "captured_at": "2026-07-13T00:00:00Z"}
    return {"schema_version": "1.17", "waves": {"P00-I01-W01": wave}}


def test_migration_backfills_shared_wave_count() -> None:
    out = MigrationV117ToV118().apply(_state_v1_17(with_snapshots=True))

    assert out["schema_version"] == "1.18"
    wave = out["waves"]["P00-I01-W01"]
    # A null count reads as a divisor of one -- what the delta effectively used
    # before the field existed, so no historical figure changes.
    assert wave["runtime_baseline"]["shared_wave_count"] is None
    assert wave["runtime_latest"]["shared_wave_count"] is None


def test_migration_v118_handles_waves_without_snapshots() -> None:
    out = MigrationV117ToV118().apply(_state_v1_17(with_snapshots=False))

    assert out["schema_version"] == "1.18"
    assert "runtime_baseline" not in out["waves"]["P00-I01-W01"]


def test_migration_v118_does_not_mutate_input() -> None:
    payload = _state_v1_17(with_snapshots=True)

    MigrationV117ToV118().apply(payload)

    assert payload["schema_version"] == "1.17"
    assert "shared_wave_count" not in payload["waves"]["P00-I01-W01"]["runtime_baseline"]


def test_migration_v118_pre_post_version_guards() -> None:
    step = MigrationV117ToV118()
    payload = _state_v1_17(with_snapshots=False)

    step.check_pre(payload)
    step.check_post(step.apply(payload))

    with pytest.raises(ValidationError):
        step.check_pre({"schema_version": "1.16"})
