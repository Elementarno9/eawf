"""The frozen pack-to-port normalisation map: strict loading, coverage, and binding.

Three things are asserted. The map loads strict, so an unknown key or a half-written
entry is a load failure rather than a silently ignored field. Every entry is applied at
least once -- an entry whose golden ids select nothing is a dead admission that would
survive a regeneration unnoticed -- and the residual diff over the replayed contract is
empty. Finally the two classification corrections the map carries are bound in the
tracked route registry rather than restated in a second hand-maintained table.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from .console_chassis.chassis import registry
from .console_chassis.goldens import NORMALISATION_MAP
from .console_chassis.harness.replay import Result
from .console_chassis.normalisation import (
    NormalisationEntry,
    NormalisationMap,
    load_map,
)

MAP = load_map()


def _raw() -> dict[str, Any]:
    """Return the tracked map as plain JSON, for the mutation cases below."""
    return json.loads(NORMALISATION_MAP.read_text(encoding="utf-8"))


def _entry(**overrides: Any) -> dict[str, Any]:
    """Return one well-formed raw entry with ``overrides`` applied."""
    body: dict[str, Any] = {
        "entry": "probe",
        "pack": "what the pack renders",
        "port": "what the port renders",
        "golden_ids": ["route/notifications@*"],
        "rulings": ["X-14"],
    }
    body.update(overrides)
    return body


def test_tracked_map_loads_strict() -> None:
    assert MAP.spec == "CON-147"
    assert MAP.entries
    assert len({entry.entry for entry in MAP.entries}) == len(MAP.entries)


def test_load_map_raises_for_a_missing_file(tmp_path: Path) -> None:
    load_map.cache_clear()
    with pytest.raises(FileNotFoundError):
        load_map(tmp_path / "absent.json")


def test_load_map_rejects_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalisationEntry.model_validate(_entry(admitted_by="a reviewer"))


def test_load_map_rejects_an_entry_naming_no_golden_id() -> None:
    with pytest.raises(ValidationError, match="names no golden id"):
        NormalisationEntry.model_validate(_entry(golden_ids=[]))


def test_load_map_rejects_an_entry_naming_no_ruling() -> None:
    with pytest.raises(ValidationError, match="names no ruling"):
        NormalisationEntry.model_validate(_entry(rulings=[]))


def test_load_map_rejects_a_half_written_route_correction() -> None:
    with pytest.raises(ValidationError, match="half-written route correction"):
        NormalisationEntry.model_validate(_entry(route_id="timeline", route_key="roadmap"))


def test_load_map_rejects_an_empty_entry_list() -> None:
    with pytest.raises(ValidationError, match="carries no entry"):
        NormalisationMap.model_validate({"spec": "CON-147", "source": "probe", "entries": []})


def test_load_map_rejects_duplicate_entry_names() -> None:
    raw = _raw()
    raw["entries"] = [_entry(), _entry()]
    with pytest.raises(ValidationError, match="duplicate normalisation entries: probe"):
        NormalisationMap.model_validate(raw)


def test_selects_matches_an_exact_id_a_glob_and_nothing_else() -> None:
    entry = NormalisationEntry.model_validate(
        _entry(golden_ids=["route/attention@80", "conn/*/attention"])
    )
    assert entry.selects("route/attention@80")
    assert entry.selects("conn/DEGRADED/attention")
    assert not entry.selects("route/attention@120")
    assert not entry.selects("")


def test_every_entry_is_applied_at_least_once(contract_ids: tuple[str, ...]) -> None:
    dead = [
        entry.entry
        for entry in MAP.entries
        if not any(entry.selects(contract_id) for contract_id in contract_ids)
    ]
    assert not dead, f"normalisation entries selecting no golden: {', '.join(dead)}"


def test_every_golden_id_pattern_selects_at_least_one_record(
    contract_ids: tuple[str, ...],
) -> None:
    dead = [
        f"{entry.entry}: {pattern}"
        for entry in MAP.entries
        for pattern in entry.golden_ids
        if not any(fnmatch.fnmatchcase(contract_id, pattern) for contract_id in contract_ids)
    ]
    assert not dead, f"golden id patterns selecting nothing: {'; '.join(dead)}"


def test_residual_diff_is_empty(replay: dict[str, Result]) -> None:
    residual = sorted(
        f"{result.id}: {result.detail}" for result in replay.values() if not result.ok
    )
    assert not residual, "differences outside the map:\n" + "\n".join(residual)


def test_roadmap_is_the_planning_route_key() -> None:
    spec = registry.ROUTE_BY_KEY["roadmap"]
    assert spec.family == "planning"
    assert spec.id == "timeline"
    assert "timeline" not in registry.ROUTE_BY_KEY


def test_notifications_is_a_global_diagnostics_route() -> None:
    spec = registry.ROUTE_BY_KEY["notifications"]
    assert spec.family == "diagnostics"
    assert not spec.subject_required
    assert spec.family != "entity_sub_surfaces"


def test_every_route_correction_is_bound_in_the_tracked_registry() -> None:
    corrections = MAP.route_corrections()
    assert corrections, "the map carries no classification correction"
    for route_id, correction in corrections.items():
        spec = registry.ROUTE_BY_ID[route_id]
        assert spec.key == correction.key
        assert spec.family == correction.family


def test_uncorrected_routes_keep_the_pack_id_as_their_key() -> None:
    corrected = set(MAP.route_corrections())
    for spec in registry.ROUTES:
        if spec.id not in corrected:
            assert spec.key == spec.id
