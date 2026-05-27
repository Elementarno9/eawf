from __future__ import annotations

import pytest

from eawf.kernel.state import ids


@pytest.mark.parametrize(
    "code",
    ["QR", "EA", "COLLAR", "PLATFORM", "AO-SERVER", "CP_PATCH"],
)
def test_project_code_accepts(code: str) -> None:
    assert ids.is_project_code(code)


@pytest.mark.parametrize(
    "code",
    ["Q", "qr", "1Q", "TOO-LONG-PROJECT-CODE-XYZ", "CP PATCH"],
)
def test_project_code_rejects(code: str) -> None:
    assert not ids.is_project_code(code)


def test_phase_id_format() -> None:
    assert ids.is_phase_id("P01")
    assert ids.is_phase_id("P13")
    # 3-digit ids accepted per the AGENTS ``\d{2,}`` widening so the queue
    # can grow past P99 without re-fitting the grammar.
    assert ids.is_phase_id("P100")
    assert not ids.is_phase_id("P1")
    assert not ids.is_phase_id("P13-I04")


def test_iter_id_format() -> None:
    assert ids.is_iter_id("P13-I04")
    assert ids.is_iter_id("P100-I100")
    assert not ids.is_iter_id("P13")
    assert not ids.is_iter_id("P13-I04-W01")


def test_wave_id_format() -> None:
    assert ids.is_wave_id("P13-I04-W01")
    assert ids.is_wave_id("P01-I02-W99")
    assert ids.is_wave_id("P100-I100-W100")
    assert not ids.is_wave_id("P13-I04")
    assert not ids.is_wave_id("P13-I04-W1")


def test_hypothesis_id_format() -> None:
    assert ids.is_hypothesis_id("H03-12")
    assert ids.is_hypothesis_id("H01-01")
    assert ids.is_hypothesis_id("QR-H03-12")
    assert ids.is_hypothesis_id("PLATFORM-H99-99")
    assert not ids.is_hypothesis_id("H1-01")
    assert not ids.is_hypothesis_id("H03-1")
    assert not ids.is_hypothesis_id("H03_12")


def test_parents_of_phase_is_empty() -> None:
    assert ids.parents_of("P03") == ()


def test_parents_of_iter() -> None:
    assert ids.parents_of("P03-I02") == ("P03",)


def test_parents_of_wave() -> None:
    parents = ids.parents_of("P13-I04-W01")
    assert parents == ("P13", "P13-I04")


def test_parents_of_invalid_raises() -> None:
    with pytest.raises(ValueError):
        ids.parents_of("not-an-id")


def test_allocate_next_phase_id_picks_smallest_free() -> None:
    existing = {"P01", "P02", "P04"}
    assert ids.allocate_next_phase_id(existing) == "P03"
    assert ids.allocate_next_phase_id({"P01", "P02", "P03"}) == "P04"
    assert ids.allocate_next_phase_id(set()) == "P01"


def test_allocate_next_phase_id_saturation() -> None:
    saturated = {f"P{n:02d}" for n in range(1, 100)}
    with pytest.raises(ValueError):
        ids.allocate_next_phase_id(saturated)


def test_allocate_next_iter_id_picks_smallest_free() -> None:
    existing = {"P13-I01", "P13-I02"}
    assert ids.allocate_next_iter_id("P13", existing) == "P13-I03"
    assert ids.allocate_next_iter_id("P13", set()) == "P13-I01"


def test_allocate_next_iter_id_saturation() -> None:
    saturated = {f"P13-I{n:02d}" for n in range(1, 100)}
    with pytest.raises(ValueError):
        ids.allocate_next_iter_id("P13", saturated)


def test_allocate_next_iter_id_invalid_phase() -> None:
    with pytest.raises(ValueError):
        ids.allocate_next_iter_id("not-a-phase", set())


def test_allocate_next_wave_id_picks_smallest_free() -> None:
    existing = {"P13-I04-W01", "P13-I04-W02"}
    assert ids.allocate_next_wave_id("P13-I04", existing) == "P13-I04-W03"
    assert ids.allocate_next_wave_id("P13-I04", set()) == "P13-I04-W01"


def test_allocate_next_wave_id_saturation() -> None:
    saturated = {f"P13-I04-W{n:02d}" for n in range(1, 100)}
    with pytest.raises(ValueError):
        ids.allocate_next_wave_id("P13-I04", saturated)


def test_allocate_next_wave_id_invalid_iter() -> None:
    with pytest.raises(ValueError):
        ids.allocate_next_wave_id("P13", set())


def test_natural_key_orders_phases_numerically() -> None:
    phases = ["P10", "P9", "P100", "P01", "P2"]
    assert sorted(phases, key=ids.natural_key) == ["P01", "P2", "P9", "P10", "P100"]


def test_natural_key_orders_waves_numerically() -> None:
    waves = ["P13-I04-W10", "P13-I04-W09", "P13-I04-W100", "P13-I04-W01"]
    assert sorted(waves, key=ids.natural_key) == [
        "P13-I04-W01",
        "P13-I04-W09",
        "P13-I04-W10",
        "P13-I04-W100",
    ]


def test_natural_key_orders_iters_across_phases() -> None:
    iters = ["P9-I10", "P10-I01", "P9-I02", "P100-I01"]
    assert sorted(iters, key=ids.natural_key) == [
        "P9-I02",
        "P9-I10",
        "P10-I01",
        "P100-I01",
    ]


def test_natural_key_orders_backlog_ids() -> None:
    backlog = ["B100", "B009", "B010", "B001"]
    assert sorted(backlog, key=ids.natural_key) == ["B001", "B009", "B010", "B100"]


def test_natural_key_mixed_phase_iter_wave_levels() -> None:
    # Mixed-shape ids still compare deterministically — same prefix sorts
    # by trailing structure, different prefixes by their first numeric
    # divergence.
    mixed = ["P09", "P10-I01", "P09-I02-W03", "P10"]
    assert sorted(mixed, key=ids.natural_key) == [
        "P09",
        "P09-I02-W03",
        "P10",
        "P10-I01",
    ]


def test_natural_key_handles_empty_and_pure_alpha() -> None:
    # No digit runs: the key is a single lower-cased string chunk.
    assert ids.natural_key("") == ("",)
    assert ids.natural_key("ABC") == ("abc",)
