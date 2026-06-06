"""Tests for the ``1.6 -> 1.7`` typed-success-criteria migration.

The v1.7 edge retypes :attr:`eawf.kernel.state.models.Wave.success_criteria`
from ``list[str]`` to ``list[CriterionSpec]`` and backfills every legacy
string into a grandfathered :class:`~eawf.kernel.spec.common.CriterionSpec`
row. These tests pin the backfill shape (id / kind / quality_dimension /
measurable_signal), the empty-list boundary, the short-string fallback signal,
the pre/post invariant rejections, and a full pre-migration state re-loading
under the live model after ``apply``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.migrations.v1_6_to_v1_7 import MigrationV16ToV17
from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.state.models import State

_TS = "2026-06-06T00:00:00Z"


def _state_v1_6(*, wave_criteria: list[str]) -> dict[str, Any]:
    """Return a referentially complete v1.6 state with one wave + criteria.

    The wave's ``success_criteria`` carries the legacy ``list[str]`` shape so
    the migration's per-wave backfill has a body to rewrite.
    """
    return {
        "schema_version": "1.6",
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
            "subproject_id": None,
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
                "subproject_id": None,
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
        "waves": {
            "P00-I01-W01": {
                "id": "P00-I01-W01",
                "iter_id": "P00-I01",
                "title": "Wave one",
                "status": "pending",
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "success_criteria": wave_criteria,
                "opened_at": _TS,
                "closed_at": None,
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _migrated_wave_criteria(wave_criteria: list[str]) -> list[dict[str, Any]]:
    """Apply the migration and return the migrated wave's criteria dicts."""
    out = MigrationV16ToV17().apply(_state_v1_6(wave_criteria=wave_criteria))
    return out["waves"]["P00-I01-W01"]["success_criteria"]


def test_apply_empty_criteria_migrates_to_empty_list() -> None:
    """Boundary: an empty ``success_criteria`` list migrates to ``[]``."""
    criteria = _migrated_wave_criteria([])
    assert criteria == []


def test_apply_single_legacy_criterion_becomes_one_grandfathered_row() -> None:
    """A one-entry legacy list migrates to exactly one grandfathered CriterionSpec."""
    text = "the migrated criterion validates against the typed model"
    criteria = _migrated_wave_criteria([text])

    assert len(criteria) == 1
    row = criteria[0]
    assert row["id"] == "CR-01"
    assert row["kind"] == "legacy"
    assert row["text"] == text
    assert row["quality_dimension"] == "functional_suitability"
    assert len(row["measurable_signal"]) >= 20
    # The row validates as a real CriterionSpec (the live model accepts it).
    spec = CriterionSpec.model_validate(row)
    assert spec.kind == "legacy"


def test_apply_short_legacy_string_uses_fallback_signal() -> None:
    """A <20-char legacy string yields the grandfathered fallback signal."""
    criteria = _migrated_wave_criteria(["c1"])
    assert criteria[0]["measurable_signal"] == "grandfathered legacy criterion"
    # And still validates against the model (the floor is satisfied).
    assert CriterionSpec.model_validate(criteria[0]).measurable_signal == (
        "grandfathered legacy criterion"
    )


def test_apply_enumerates_ids_one_based() -> None:
    """Boundary: multiple criteria get 1-based zero-padded CR ids in order."""
    criteria = _migrated_wave_criteria(
        [
            "first criterion text that clears the signal floor",
            "second criterion text that clears the signal floor",
        ]
    )
    assert [row["id"] for row in criteria] == ["CR-01", "CR-02"]


def test_apply_bumps_schema_version_and_does_not_mutate_input() -> None:
    """``apply`` bumps the version on a deep copy without touching the input."""
    src = _state_v1_6(wave_criteria=["a legacy criterion long enough to keep"])
    out = MigrationV16ToV17().apply(src)

    assert out["schema_version"] == "1.7"
    # The input is unchanged (deep-copied): still 1.6 with bare-string criteria.
    assert src["schema_version"] == "1.6"
    assert src["waves"]["P00-I01-W01"]["success_criteria"] == [
        "a legacy criterion long enough to keep"
    ]


def test_apply_then_model_validate_reloads_without_validation_error() -> None:
    """Error-path: a v1.6 fixture re-loads under the live model after apply.

    A pre-migration state with bare-string criteria would reject the typed
    field, but the migrated dict validates cleanly.
    """
    out = MigrationV16ToV17().apply(
        _state_v1_6(wave_criteria=["criterion that clears the twenty char floor"])
    )
    state = State.model_validate(out)
    assert state.schema_version == "1.7"
    wave = state.waves["P00-I01-W01"]
    assert len(wave.success_criteria) == 1
    assert wave.success_criteria[0].kind == "legacy"


def test_unmigrated_state_with_bare_strings_rejects_under_live_model() -> None:
    """Error-path: the un-migrated 1.6 bare-string shape fails the typed field."""
    with pytest.raises(ValidationError):
        State.model_validate(_state_v1_6(wave_criteria=["a bare legacy string"]))


def test_check_pre_rejects_non_1_6_dict() -> None:
    """``check_pre`` rejects a payload that is not at schema 1.6."""
    step = MigrationV16ToV17()
    with pytest.raises(ValidationError):
        step.check_pre({"schema_version": "1.5"})


def test_check_post_rejects_non_1_7_dict() -> None:
    """``check_post`` rejects a payload that is not at schema 1.7."""
    step = MigrationV16ToV17()
    with pytest.raises(ValidationError):
        step.check_post({"schema_version": "1.6"})
