"""The pack-to-port normalisation map loads strict and governs every comparison.

Three things are held here. The loader forbids an unknown key, so a half-written entry or
a field somebody invented is a load failure rather than a silently ignored admission.
Every golden id pattern the tracked map names selects at least one record of the tracked
contract, so an admission cannot outlive the frame it was written for. And the comparison
the map governs is strict in both directions: a difference the map admits is rewritten
away before the frames are compared, and a difference anywhere else fails.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.surfaces.tui.console.harness import load_contract
from eawf.surfaces.tui.console.normalisation import (
    REWRITES,
    NormalisationEntry,
    NormalisationMap,
    Normaliser,
    Rewrite,
    first_diff,
    load_map,
    unknown_golden_ids,
)

TESTS_ROOT = Path(__file__).resolve().parents[4]
GOLDEN_ROOT = TESTS_ROOT / "fixtures" / "console" / "golden"
NORMALISATION_MAP = GOLDEN_ROOT / "normalisation-map.json"
SPEC = "CON-147"


@pytest.fixture(scope="module")
def tracked() -> NormalisationMap:
    """Return the tracked map, loaded strict."""
    return load_map(NORMALISATION_MAP)


@pytest.fixture(scope="module")
def contract_ids() -> tuple[str, ...]:
    """Return every journey id then every frame id of the tracked contract."""
    return load_contract(GOLDEN_ROOT / "sequences").ids


@pytest.fixture(scope="module")
def frames() -> dict[str, str]:
    """Return every recorded frame of the tracked contract, by id."""
    return load_contract(GOLDEN_ROOT / "sequences").frames_by_id()


def _raw() -> dict[str, Any]:
    """Return the tracked map as plain JSON, for the mutation cases below."""
    data: dict[str, Any] = json.loads(NORMALISATION_MAP.read_text(encoding="utf-8"))
    return data


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


def test_tracked_map_loads_strict(tracked: NormalisationMap) -> None:
    assert tracked.spec == SPEC
    assert tracked.entries
    assert len({entry.entry for entry in tracked.entries}) == len(tracked.entries)


def test_load_map_raises_for_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_map(tmp_path / "absent.json")


def test_load_map_rejects_an_unknown_key() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalisationEntry.model_validate(_entry(admitted_by="a reviewer"))


def test_load_map_rejects_an_unknown_key_on_the_map_itself() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        NormalisationMap.model_validate({**_raw(), "approved": True})


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
        NormalisationMap.model_validate({"spec": SPEC, "source": "probe", "entries": []})


def test_load_map_rejects_duplicate_entry_names() -> None:
    raw = _raw()
    raw["entries"] = [_entry(), _entry()]
    with pytest.raises(ValidationError, match="duplicate normalisation entries: probe"):
        NormalisationMap.model_validate(raw)


def test_load_map_rejects_a_malformed_file(tmp_path: Path) -> None:
    path = tmp_path / "map.json"
    path.write_text("{}", encoding="utf-8")
    with pytest.raises(ValidationError, match="Field required"):
        load_map(path)


def test_selects_matches_an_exact_id_a_glob_and_nothing_else() -> None:
    entry = NormalisationEntry.model_validate(
        _entry(golden_ids=["route/attention@80", "conn/*/attention"])
    )
    assert entry.selects("route/attention@80")
    assert entry.selects("conn/DEGRADED/attention")
    assert not entry.selects("route/attention@120")
    assert not entry.selects("")


def test_by_name_returns_an_entry_and_raises_for_an_unnamed_one(
    tracked: NormalisationMap,
) -> None:
    first = tracked.entries[0]
    assert tracked.by_name(first.entry) is first
    with pytest.raises(KeyError, match="no normalisation entry named"):
        tracked.by_name("no such entry")


def test_every_golden_id_the_map_names_is_present_in_the_contract(
    tracked: NormalisationMap, contract_ids: tuple[str, ...]
) -> None:
    assert unknown_golden_ids(tracked, contract_ids) == []


def test_every_entry_selects_at_least_one_record(
    tracked: NormalisationMap, contract_ids: tuple[str, ...]
) -> None:
    dead = [
        entry.entry
        for entry in tracked.entries
        if not any(entry.selects(contract_id) for contract_id in contract_ids)
    ]
    assert dead == []


def test_unknown_golden_ids_names_the_entry_and_the_pattern() -> None:
    table = NormalisationMap.model_validate(
        {"spec": SPEC, "source": "probe", "entries": [_entry(golden_ids=["never/matched"])]}
    )
    assert unknown_golden_ids(table, ["route/attention@80"]) == ["probe: never/matched"]
    assert unknown_golden_ids(table, []) == ["probe: never/matched"]


def test_every_route_correction_names_a_key_and_a_family(tracked: NormalisationMap) -> None:
    corrections = tracked.route_corrections()
    assert corrections, "the map carries no classification correction"
    assert all(c.key and c.family for c in corrections.values())


def test_a_frame_difference_outside_the_map_fails_the_comparison(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    contract_id = "route/attention@80"
    recorded = frames[contract_id]
    normaliser = Normaliser(tracked, frames)
    expected = normaliser.expected(contract_id, recorded)
    assert normaliser.compare(contract_id, recorded, expected).ok
    rows = expected.split("\n")
    rows[4] = "x" + rows[4][1:]
    verdict = normaliser.compare(contract_id, recorded, "\n".join(rows))
    assert not verdict.ok
    assert verdict.row == 4
    assert verdict.detail == "row 4 differs"


def test_a_frame_with_the_wrong_row_count_fails_the_comparison(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    contract_id = "route/attention@80"
    normaliser = Normaliser(tracked, frames)
    expected = normaliser.expected(contract_id, frames[contract_id])
    verdict = normaliser.compare(contract_id, frames[contract_id], expected + "\nextra")
    assert not verdict.ok
    assert verdict.row == -1
    assert "row count" in verdict.detail


def test_a_rewrite_runs_only_on_the_ids_its_entry_selects(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    normaliser = Normaliser(tracked, frames)
    entry = tracked.by_name("window indicator")
    touched = next(cid for cid in frames if entry.selects(cid))
    untouched = next(cid for cid in frames if not normaliser.applied(cid))
    assert "window indicator" in normaliser.applied(touched)
    assert normaliser.expected(touched, frames[touched]) != frames[touched]
    assert normaliser.expected(untouched, frames[untouched]) == frames[untouched]


def test_a_rewrite_outside_the_map_is_refused(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    with pytest.raises(ValueError, match="rewrites outside the normalisation map: invented"):
        Normaliser(tracked, frames, [Rewrite(entry="invented")])


def test_one_entry_realised_twice_is_refused(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    name = REWRITES[0].entry
    with pytest.raises(ValueError, match=f"entries realised twice: {name}"):
        Normaliser(tracked, frames, [REWRITES[0], REWRITES[0]])


def test_every_realised_rewrite_names_a_tracked_entry(tracked: NormalisationMap) -> None:
    names = {entry.entry for entry in tracked.entries}
    assert [r.entry for r in REWRITES if r.entry not in names] == []


def test_simulated_keys_are_the_ones_the_selecting_entries_carry(
    tracked: NormalisationMap, frames: dict[str, str]
) -> None:
    normaliser = Normaliser(tracked, frames)
    entry = tracked.by_name("help simulator row")
    touched = next(cid for cid in frames if entry.selects(cid))
    assert "w" in normaliser.simulated_keys(touched)
    assert normaliser.simulated_keys("no/such/frame") == frozenset()


def test_first_diff_matches_an_identical_frame_and_an_empty_one() -> None:
    assert first_diff("", "").ok
    assert first_diff("a\nb", "a\nb").ok
    assert not first_diff("a", "b").ok
    assert first_diff("a", "b").row == 0
