from __future__ import annotations

import pytest

from eawf.state import ids


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
    assert not ids.is_phase_id("P1")
    assert not ids.is_phase_id("P001")
    assert not ids.is_phase_id("P13-I04")


def test_iter_id_format() -> None:
    assert ids.is_iter_id("P13-I04")
    assert not ids.is_iter_id("P13")
    assert not ids.is_iter_id("P13-I04-W01")


def test_wave_id_format() -> None:
    assert ids.is_wave_id("P13-I04-W01")
    assert ids.is_wave_id("P01-I02-W99")
    assert not ids.is_wave_id("P13-I04")
    assert not ids.is_wave_id("P13-I04-W1")
    assert not ids.is_wave_id("P13-I04-W001")


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
