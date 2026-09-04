"""Tests for the ``Iter.wave_ids`` natural-id normalization field-validator."""

from __future__ import annotations

from datetime import UTC, datetime

from eawf.kernel.state.enums import IterStatus
from eawf.kernel.state.models import Iter

_DT = datetime(2026, 6, 11, tzinfo=UTC)


def _iter(wave_ids: list[str]) -> Iter:
    return Iter(
        id="P29-I01",
        phase_id="P29",
        title="I",
        status=IterStatus.CLOSED,
        wave_ids=wave_ids,
        estimate_id=None,
        audit_id=None,
        opened_at=_DT,
        closed_at=_DT,
    )


def test_normalize_wave_ids_resorts_non_monotonic_input() -> None:
    """A non-monotonic stored list (the P29-I01 shape) re-validates ascending."""
    stored = [
        "P29-I01-W01",
        "P29-I01-W10",
        "P29-I01-W02",
        "P29-I01-W09",
        "P29-I01-W100",
        "P29-I01-W99",
    ]
    iter_row = _iter(stored)
    assert iter_row.wave_ids == [
        "P29-I01-W01",
        "P29-I01-W02",
        "P29-I01-W09",
        "P29-I01-W10",
        "P29-I01-W99",
        "P29-I01-W100",
    ]


def test_normalize_wave_ids_preserves_membership() -> None:
    """Normalization only reorders; the set of ids is unchanged."""
    stored = ["P29-I01-W05", "P29-I01-W01", "P29-I01-W03"]
    iter_row = _iter(stored)
    assert set(iter_row.wave_ids) == set(stored)
    assert len(iter_row.wave_ids) == len(stored)


def test_normalize_wave_ids_is_idempotent_on_sorted_input() -> None:
    """An already-ascending list is returned unchanged."""
    sorted_ids = ["P29-I01-W01", "P29-I01-W02", "P29-I01-W03"]
    iter_row = _iter(sorted_ids)
    assert iter_row.wave_ids == sorted_ids


def test_normalize_wave_ids_empty_list() -> None:
    """The empty-list boundary stays empty."""
    iter_row = _iter([])
    assert iter_row.wave_ids == []


def test_normalize_wave_ids_single_element() -> None:
    """A single-element list is returned unchanged."""
    iter_row = _iter(["P29-I01-W07"])
    assert iter_row.wave_ids == ["P29-I01-W07"]


def test_normalize_wave_ids_self_heals_on_round_trip() -> None:
    """Re-validating a model (load-then-persist) keeps ascending order stable."""
    stored = ["P29-I01-W10", "P29-I01-W02", "P29-I01-W01"]
    first = _iter(stored)
    second = Iter.model_validate(first.model_dump())
    assert second.wave_ids == ["P29-I01-W01", "P29-I01-W02", "P29-I01-W10"]
    assert first.wave_ids == second.wave_ids
