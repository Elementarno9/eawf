"""The ``1.18`` -> ``1.19`` migration backfills ``calibration_excluded``."""

from __future__ import annotations

import pytest

from eawf.kernel.migrations.v1_18_to_v1_19 import MigrationV118ToV119


def _state_v1_18_with_actual() -> dict[str, object]:
    return {
        "schema_version": "1.18",
        "actuals": {
            "P01-I01-W01": {
                "id": "ACT-P01-I01-W01",
                "scope_id": "P01-I01-W01",
                "status": "done",
                "elapsed_eu": 1.0,
                "current_store_record_id": "REC-P01-I01-W01",
                "updated_at": "2026-07-13T00:00:00Z",
            }
        },
    }


def test_migration_backfills_calibration_excluded() -> None:
    out = MigrationV118ToV119().apply(_state_v1_18_with_actual())

    assert out["schema_version"] == "1.19"
    # False is what an unmarked row has always meant: nothing known to disqualify it.
    assert out["actuals"]["P01-I01-W01"]["calibration_excluded"] is False


def test_migration_v119_does_not_mutate_input() -> None:
    payload = _state_v1_18_with_actual()

    MigrationV118ToV119().apply(payload)

    assert payload["schema_version"] == "1.18"
    assert "calibration_excluded" not in payload["actuals"]["P01-I01-W01"]  # type: ignore[index,operator]


def test_migration_v119_pre_post_version_guards() -> None:
    step = MigrationV118ToV119()
    payload = _state_v1_18_with_actual()

    step.check_pre(payload)
    step.check_post(step.apply(payload))

    with pytest.raises(Exception, match="schema_version"):
        step.check_pre({"schema_version": "1.17"})
