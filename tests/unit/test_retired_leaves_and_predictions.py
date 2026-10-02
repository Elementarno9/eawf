"""Dead configuration leaves, prediction notices and the effort ladder stay retired.

Each test names the requirement row it proves: the two session-shape
preference leaves are deleted rather than wired (AUTH-023, AUTH-057), a
budget notice fires only on an observed crossing (AUTH-036), every wave is
costed at one effort constant with no label multiplied (AUTH-041), and the
legacy Track states ``planned`` and ``deferred`` are dropped and refused at
cutover rather than folded into ``active`` (AUTH-056).
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry import is_known_leaf_key, registry_lookup
from eawf.kernel.config.schema import EstimationConfig, PreferencesConfig
from eawf.kernel.migration.epoch2.errors import MigrationPlanNotApplicableError
from eawf.kernel.migration.epoch2.manifest import UnresolvedReason
from eawf.kernel.migration.epoch2.plan_mode import (
    DROPPED_TRACK_STATUSES,
    Epoch2PlanRequest,
    plan_cutover,
)
from eawf.kernel.state import enums
from eawf.kernel.state.enums import EffortBucket, WaveStatus
from eawf.kernel.state.models import Track, Wave
from eawf.runtime.budget import policy
from eawf.runtime.budget.policy import BLOCK_TAG, classify
from eawf.surfaces.cli.registry import COMMAND_REGISTRY, GroupRow
from eawf.workflow.estimation.buckets import critical_path_eu, sum_wave_eu
from eawf.workflow.estimation.mapping import CURRENT_EFFORT_MAPPING

WAVE_EU = CURRENT_EFFORT_MAPPING.effort_eu

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "migration"
FULL_SNAPSHOT = FIXTURES / "epoch1-full" / "snapshot"
ALLOWLIST = FIXTURES / "allowed_legacy_symbols.txt"
DEAD_PREFERENCE_LEAVES = ("preferences.solution_bias", "preferences.scope_size")


def _wave(wave_id: str, bucket: EffortBucket | None, deps: list[str] | None = None) -> Wave:
    """Return a minimal wave carrying *bucket* as its size label."""
    return Wave(
        id=wave_id,
        iter_id="P01-I01",
        title=wave_id,
        status=WaveStatus.PENDING,
        effort_bucket=bucket,
        deps=deps or [],
        opened_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


# --- AUTH-023 / AUTH-057: the dead preference leaves are deleted ------------


@pytest.mark.parametrize("key", DEAD_PREFERENCE_LEAVES)
def test_auth_023_no_catalog_or_menu_row_names_a_deleted_leaf(key: str) -> None:
    """Neither the leaf catalog nor the config menu offers a deleted leaf."""
    assert is_known_leaf_key(key) is False
    assert registry_lookup(key) is None


@pytest.mark.parametrize("field", ["solution_bias", "scope_size"])
def test_auth_023_preferences_model_refuses_a_deleted_leaf(field: str) -> None:
    """A deleted leaf is an unknown key at the loader, not a silent default."""
    with pytest.raises(ValidationError, match=field):
        PreferencesConfig.model_validate({field: "M"})


def test_auth_057_the_kept_auto_choose_leaf_is_the_whole_preferences_section() -> None:
    """Only the leaf with a real consumer survives in defaults and model."""
    assert built_in_defaults()["preferences"] == {"auto_choose": "off"}
    assert set(PreferencesConfig.model_fields) == {"auto_choose"}


def test_auth_057_a_persisted_layer_sheds_the_deleted_leaves_and_keeps_the_rest() -> None:
    """A config written before the deletion still loads, minus the dead leaves."""
    payload = {
        "schema_version": "1.0",
        "preferences": {"solution_bias": "thorough", "scope_size": "XL", "auto_choose": "always"},
    }

    upgraded, changed = migrate_config_payload(payload)

    assert changed is True
    assert upgraded["preferences"] == {"auto_choose": "always"}
    PreferencesConfig.model_validate(upgraded["preferences"])


def test_auth_057_a_section_of_only_deleted_leaves_is_pruned() -> None:
    """A section left empty by the deletion is removed rather than kept empty."""
    upgraded, changed = migrate_config_payload(
        {"schema_version": "1.0", "preferences": {"solution_bias": "simple"}}
    )

    assert changed is True
    assert "preferences" not in upgraded


def test_auth_057_a_layer_without_the_leaves_is_left_untouched() -> None:
    """The migration is a no-op on a body that never carried the leaves."""
    payload = {"schema_version": "1.0", "preferences": {"auto_choose": "off"}}

    upgraded, changed = migrate_config_payload(payload)

    assert changed is False
    assert upgraded == payload


# --- AUTH-036: a notice fires on an observed crossing, never a prediction ---


@pytest.mark.parametrize("consumed", [0, 750, 800, 999])
def test_auth_036_consumption_short_of_the_budget_classifies_as_nothing(consumed: int) -> None:
    """No fraction of a budget below it yields a warning tag."""
    assert classify(consumed=consumed, budget=1000) is None


@pytest.mark.parametrize("consumed", [1000, 1001, 10**9])
def test_auth_036_a_reached_budget_is_the_only_classified_crossing(consumed: int) -> None:
    """Reaching the budget is the observed event that classifies."""
    assert classify(consumed=consumed, budget=1000) == BLOCK_TAG


def test_auth_036_the_policy_declares_no_warning_fraction() -> None:
    """No sub-unity threshold is left for a producer to reintroduce."""
    assert not hasattr(policy, "WARN_FRACTION")
    assert not hasattr(policy, "WARN_TAG")


# --- AUTH-041: one effort constant replaces the five-point ladder -----------


def test_auth_041_effort_is_one_constant_with_its_measured_dispersion() -> None:
    """Effort is 0.8 EU, 24 minutes, shipped with p10/p50/p90."""
    mapping = CURRENT_EFFORT_MAPPING
    assert pytest.approx(0.8) == mapping.effort_eu
    assert pytest.approx(30.0) == mapping.eu_minutes
    assert pytest.approx(24.0) == mapping.effort_minutes
    assert mapping.dispersion.model_dump() == pytest.approx({"p10": 8.8, "p50": 23.9, "p90": 123.1})


def test_auth_041_the_serial_sum_ignores_every_size_label() -> None:
    """An XS wave and an XL wave cost the same; an unlabelled one too."""
    labels: list[EffortBucket | None] = [*EffortBucket, None]
    waves = [_wave(f"P01-I01-W{index:02d}", bucket) for index, bucket in enumerate(labels, 1)]

    assert sum_wave_eu(waves) == pytest.approx(len(labels) * WAVE_EU)
    assert sum_wave_eu([_wave("P01-I01-W01", EffortBucket.XS)]) == sum_wave_eu(
        [_wave("P01-I01-W01", EffortBucket.XL)]
    )


def test_auth_041_the_critical_path_counts_waves_not_labels() -> None:
    """A three-wave chain costs three constants whatever its labels say."""
    chain = [
        _wave("P01-I01-W01", EffortBucket.XL),
        _wave("P01-I01-W02", EffortBucket.XS, deps=["P01-I01-W01"]),
        _wave("P01-I01-W03", None, deps=["P01-I01-W02"]),
        _wave("P01-I01-W04", EffortBucket.XL),
    ]

    assert critical_path_eu(chain) == pytest.approx(3 * WAVE_EU)


@pytest.mark.parametrize(
    ("waves", "expected"),
    [([], 0.0), ([_wave("P01-I01-W01", EffortBucket.L)], WAVE_EU)],
)
def test_auth_041_roll_ups_at_the_empty_and_single_boundaries(
    waves: list[Wave], expected: float
) -> None:
    """No waves cost nothing; one wave costs exactly the constant."""
    assert sum_wave_eu(waves) == pytest.approx(expected)
    assert critical_path_eu(waves) == pytest.approx(expected)


def test_auth_041_a_dependency_cycle_is_cut_rather_than_followed() -> None:
    """A cyclic graph returns a finite path; the closing wave counts once more."""
    cycle = [
        _wave("P01-I01-W01", None, deps=["P01-I01-W02"]),
        _wave("P01-I01-W02", None, deps=["P01-I01-W01"]),
    ]

    assert critical_path_eu(cycle) == pytest.approx(3 * WAVE_EU)


def test_auth_041_the_ladder_calibration_config_is_retired() -> None:
    """The per-label override table is an unknown key, and its leaves are gone."""
    with pytest.raises(ValidationError, match="buckets"):
        EstimationConfig.model_validate({"buckets": {"overrides": {"M": {"expected_eu": 1.0}}}})
    assert "buckets" not in built_in_defaults()["estimation"]
    assert is_known_leaf_key("estimation.buckets.overrides.M") is False


def test_auth_041_a_persisted_calibration_table_is_shed_on_load() -> None:
    """A config written by the old init wizard still loads."""
    upgraded, changed = migrate_config_payload(
        {
            "schema_version": "1.0",
            "estimation": {"eu_minutes": 30, "buckets": {"overrides": {"M": {"expected_eu": 1.0}}}},
        }
    )

    assert changed is True
    assert upgraded["estimation"] == {"eu_minutes": 30}
    EstimationConfig.model_validate(upgraded["estimation"])


def test_auth_041_the_calibrate_command_group_is_retired() -> None:
    """No CLI group re-fits or applies per-label centroids."""
    groups = {row.name for row in COMMAND_REGISTRY if isinstance(row, GroupRow)}

    assert "migrate" in groups
    assert "calibrate" not in groups


# --- AUTH-056: planned and deferred Tracks are dropped, never folded --------


def test_auth_056_the_legacy_track_status_holds_only_the_two_live_states() -> None:
    """``planned`` and ``deferred`` are gone from the epoch-1 enum."""
    assert {member.value for member in enums.TrackStatus} == {"active", "retired"}
    assert frozenset({"planned", "deferred"}) == DROPPED_TRACK_STATUSES


@pytest.mark.parametrize("status", sorted(DROPPED_TRACK_STATUSES))
def test_auth_056_a_track_in_a_dropped_state_fails_validation(status: str) -> None:
    """A dropped state is refused at the model boundary, not coerced."""
    with pytest.raises(ValidationError, match="status"):
        Track(
            id="QR-P",
            code="QR",
            slug="quant",
            title="Quant",
            kind="strategy",
            domains=["quant"],
            status=status,
        )


def test_auth_056_the_committed_state_holds_zero_rows_in_a_dropped_state() -> None:
    """The census the drop rests on: no live Track sits in either state."""
    document = json.loads((REPO_ROOT / ".ea" / "state.json").read_text(encoding="utf-8"))
    tracks: dict[str, Any] = document.get("tracks") or {}

    assert [key for key, row in tracks.items() if row["status"] in DROPPED_TRACK_STATUSES] == []


def _snapshot_with_tracks(root: Path, tracks: dict[str, str]) -> Path:
    """Copy the full-shape corpus under *root* and add one Track per status."""
    snapshot = root / "snapshot"
    shutil.copytree(FULL_SNAPSHOT, snapshot)
    document_path = snapshot / "document.json"
    document = json.loads(document_path.read_text(encoding="utf-8"))
    document["tracks"] = {
        track_id: {
            "id": track_id,
            "code": "QR",
            "slug": track_id.lower(),
            "title": track_id,
            "kind": "strategy",
            "domains": ["quant"],
            "status": status,
        }
        for track_id, status in tracks.items()
    }
    document_path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return snapshot


def _plan(snapshot: Path) -> Any:
    """Plan the cutover over *snapshot* under a fixed seal."""
    return plan_cutover(
        Epoch2PlanRequest(
            snapshot_root=str(snapshot),
            allowlist_path=str(ALLOWLIST),
            workspace_key="WSP-DEFAULT",
            project_key="PRJ-DEMO",
            repository_key="REP-DEMO",
            sealed_by="retired-leaves-test",
        ),
        sealed_at=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_auth_056_a_dropped_track_state_refuses_the_cutover_by_name(tmp_path: Path) -> None:
    """Each dropped row is named with its state; an active row is not folded."""
    snapshot = _snapshot_with_tracks(
        tmp_path, {"QR-A": "active", "QR-D": "deferred", "QR-P": "planned"}
    )

    plan = _plan(snapshot)
    rows = {row.address: row for row in plan.manifest.unresolved_rows}

    assert rows["tracks/QR-P"].reason is UnresolvedReason.DROPPED_STATUS
    assert "'planned'" in rows["tracks/QR-P"].detail
    assert rows["tracks/QR-D"].reason is UnresolvedReason.DROPPED_STATUS
    assert "'deferred'" in rows["tracks/QR-D"].detail
    assert rows["tracks/QR-A"].reason is UnresolvedReason.NO_CONVERTER
    with pytest.raises(MigrationPlanNotApplicableError, match="dropped_status"):
        plan.require_applicable()
