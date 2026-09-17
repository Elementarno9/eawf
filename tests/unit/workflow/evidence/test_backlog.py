"""Unit tests for the backlog id grammar :func:`add_backlog` enforces.

A two-digit id is a prefix of a three-digit one, so the two read as the same
row in prose and one item was already closed carrying another item's
resolution. These tests pin both halves of the fix: a NEW id that misses
``^B\\d{3,}$`` is refused and writes nothing, while rows already on disk keep
loading and keep their stored values.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.state.enums import BacklogPriority, BacklogStatus
from eawf.kernel.state.models import BacklogItem, State
from eawf.surfaces.cli import errors as cli_errors
from eawf.workflow.evidence import _io, backlog

FIXTURE = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

#: The two-digit ids that already collide with three-digit rows on disk.
LEGACY_MALFORMED_IDS = ("B70", "B71", "B73")

_SEEDED_AT = datetime(2026, 5, 8, tzinfo=UTC)


def _row(item_id: str) -> dict[str, object]:
    """Return one on-disk backlog row payload for *item_id*."""
    item = BacklogItem(
        id=item_id,
        scope_id="QR",
        title=f"Legacy row {item_id}",
        description=None,
        priority=BacklogPriority.P2,
        status=BacklogStatus.OPEN,
        created_at=_SEEDED_AT,
        closed_at=None,
        resolution=None,
        commit=None,
    )
    return item.model_dump(mode="json")


def _state_path(tmp_path: Path, *, seeded_ids: tuple[str, ...] = ()) -> Path:
    """Write a state file seeded with *seeded_ids* and return its path."""
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload["backlog"] = {item_id: _row(item_id) for item_id in seeded_ids}
    target = tmp_path / "state.json"
    target.write_text(json.dumps(payload), encoding="utf-8")
    return target


def _add(state: State, item_id: str) -> None:
    backlog.add_backlog(
        state,
        item_id=item_id,
        title=f"Title for {item_id}",
        priority=BacklogPriority.P2,
        scope_id="QR",
    )


@pytest.mark.parametrize(
    "item_id",
    ["B70", "B7", "", "b123", "B123a", " B123"],
    ids=["two_digit", "one_digit", "empty", "lowercase_prefix", "trailing_suffix", "leading_space"],
)
def test_add_backlog_rejects_malformed_id(tmp_path: Path, item_id: str) -> None:
    state = _io.load_state(_state_path(tmp_path))
    before = state.updated_at

    with pytest.raises(cli_errors.UserError, match="invalid backlog id") as excinfo:
        _add(state, item_id)

    assert excinfo.value.kind == "InvalidInput"
    assert state.backlog == {}
    assert state.updated_at == before


def test_add_backlog_rejects_two_digit_id(tmp_path: Path) -> None:
    state = _io.load_state(_state_path(tmp_path))

    with pytest.raises(cli_errors.UserError, match="invalid backlog id") as excinfo:
        _add(state, "B70")

    assert excinfo.value.kind == "InvalidInput"
    assert "B70" not in state.backlog
    assert state.backlog == {}


def test_add_backlog_rejects_unprefixed_id(tmp_path: Path) -> None:
    state = _io.load_state(_state_path(tmp_path))

    with pytest.raises(cli_errors.UserError, match="invalid backlog id") as excinfo:
        _add(state, "X123")

    assert excinfo.value.kind == "InvalidInput"
    assert "X123" not in state.backlog
    assert state.backlog == {}


def test_add_backlog_rejects_re_add_of_an_existing_malformed_id(tmp_path: Path) -> None:
    """A legacy two-digit row cannot be re-added, and keeps its stored value."""
    state = _io.load_state(_state_path(tmp_path, seeded_ids=LEGACY_MALFORMED_IDS))
    prior = state.backlog["B71"]

    with pytest.raises(cli_errors.UserError, match="invalid backlog id"):
        _add(state, "B71")

    assert state.backlog["B71"] == prior


def test_add_backlog_keeps_existing_malformed_rows(tmp_path: Path) -> None:
    state = _io.load_state(_state_path(tmp_path, seeded_ids=LEGACY_MALFORMED_IDS))
    prior = dict(state.backlog)
    assert sorted(prior) == sorted(LEGACY_MALFORMED_IDS)

    _add(state, "B152")

    assert state.backlog["B152"].status == BacklogStatus.OPEN
    for item_id in LEGACY_MALFORMED_IDS:
        assert state.backlog[item_id] == prior[item_id]


@pytest.mark.parametrize("item_id", ["B001", "B152", "B1234"])
def test_add_backlog_keeps_accepting_three_digit_ids(tmp_path: Path, item_id: str) -> None:
    state = _io.load_state(_state_path(tmp_path, seeded_ids=LEGACY_MALFORMED_IDS))

    _add(state, item_id)

    assert state.backlog[item_id].id == item_id
    assert state.backlog[item_id].priority == BacklogPriority.P2
    assert len(state.backlog) == len(LEGACY_MALFORMED_IDS) + 1
