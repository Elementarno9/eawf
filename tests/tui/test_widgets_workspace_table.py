"""Unit tests for ``active_phase_completion`` + active-phase ``_phase_cell`` (P27-I04-W24).

The workspace table's ``phase`` column scopes to a repo's *active* phase:
its phase id plus that phase's ``closed/total`` wave progress (e.g.
``P27 ⣿⣶ 119/143``), not the misleading whole-repo wave counts. These are
pure dict-in helpers, so the tests stay unit-level (no Pilot / app mount).

The active phase id resolves from the decoded per-repo state dict via the
``current.phase_id`` pointer when it names an existing ``"active"`` phase,
else the single ``"active"`` phase, else ``None``. Phase ids + repo codes
are abstract placeholders (``P01`` / ``P02`` / ``ABC``), never real-looking
project names.
"""

from __future__ import annotations

from typing import Any

from eawf.surfaces.tui.widgets.eu_bar import EMPTY_STATE
from eawf.surfaces.tui.widgets.workspace_table import (
    RepoRow,
    _phase_cell,
    active_phase_completion,
    build_repo_rows,
    completion_pair,
)


def _repo_row(phase_id: str | None, done: int, total: int) -> RepoRow:
    """Build a minimal :class:`RepoRow` for a ``_phase_cell`` render check."""
    return RepoRow(
        code="ABC",
        path="/abs/path/abc",
        phase_id=phase_id,
        phase_done=done,
        phase_total=total,
        eu_consumed=0.0,
        eu_total=0.0,
        age="—",
    )


# --------------------------------------------------------------------------
# active_phase_completion — active-phase resolution
# --------------------------------------------------------------------------


def test_active_phase_completion_pointer_resolves_active_phase() -> None:
    """``current.phase_id`` pointing at an ``active`` phase selects that phase."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
            "W2": {"iter_id": "P02-I01", "status": "pending"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 2)


def test_active_phase_completion_scans_when_pointer_stale() -> None:
    """A pointer at a non-active / missing phase falls back to the lone active phase."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P01"},  # stale: P01 is closed
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
            "W2": {"iter_id": "P02-I01", "status": "closed"},
            "W3": {"iter_id": "P02-I01", "status": "pending"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 2, 3)


def test_active_phase_completion_scans_when_pointer_absent() -> None:
    """No ``current.phase_id`` pointer at all still resolves the lone active phase."""
    repo_state: dict[str, Any] = {
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            "W1": {"iter_id": "P02-I01", "status": "closed"},
        },
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 1)


def test_active_phase_completion_no_active_phase_is_none() -> None:
    """A state whose phases are all closed / planned yields ``(None, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P01"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "planned"},
        },
        "iters": {"P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "closed"}},
        "waves": {"W1": {"iter_id": "P01-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == (None, 0, 0)


# --------------------------------------------------------------------------
# active_phase_completion — phase-scoped counts across a multi-phase state
# --------------------------------------------------------------------------


def test_active_phase_completion_counts_only_active_phase_waves() -> None:
    """Counts include only the active phase's waves, not a sibling phase's.

    Two phases each own iters + waves. The closed phase has 3 closed
    waves; the active phase has 2 closed of 4. The bar must report the
    active phase's ``(2, 4)``, ignoring the closed phase entirely.
    """
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {
            "P01": {"id": "P01", "status": "closed"},
            "P02": {"id": "P02", "status": "active"},
        },
        "iters": {
            "P01-I01": {"id": "P01-I01", "phase_id": "P01", "status": "closed"},
            "P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"},
            "P02-I02": {"id": "P02-I02", "phase_id": "P02", "status": "active"},
        },
        "waves": {
            # P01 (closed phase) — must NOT count.
            "A1": {"iter_id": "P01-I01", "status": "closed"},
            "A2": {"iter_id": "P01-I01", "status": "closed"},
            "A3": {"iter_id": "P01-I01", "status": "closed"},
            # P02 (active phase) — 2 closed of 4 across both its iters.
            "B1": {"iter_id": "P02-I01", "status": "closed"},
            "B2": {"iter_id": "P02-I01", "status": "pending"},
            "B3": {"iter_id": "P02-I02", "status": "closed"},
            "B4": {"iter_id": "P02-I02", "status": "in_progress"},
        },
    }
    phase_id, closed, total = active_phase_completion(repo_state)
    assert phase_id == "P02"
    assert (closed, total) == (2, 4)
    # Regression guard: the whole-repo count would have reported 5/7.
    assert completion_pair(repo_state) == (5, 7)


# --------------------------------------------------------------------------
# active_phase_completion — boundary / error paths (never raise)
# --------------------------------------------------------------------------


def test_active_phase_completion_none_or_empty_is_none() -> None:
    """``None`` / empty state yields ``(None, 0, 0)`` without raising."""
    assert active_phase_completion(None) == (None, 0, 0)
    assert active_phase_completion({}) == (None, 0, 0)


def test_active_phase_completion_malformed_phases_is_none() -> None:
    """A non-dict ``phases`` yields ``(None, 0, 0)`` without raising."""
    assert active_phase_completion({"phases": "not-a-dict"}) == (None, 0, 0)


def test_active_phase_completion_malformed_iters_keeps_phase_id() -> None:
    """A resolved phase but non-dict ``iters`` yields ``(phase_id, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": "not-a-dict",
        "waves": {"W1": {"iter_id": "P02-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == ("P02", 0, 0)


def test_active_phase_completion_malformed_waves_keeps_phase_id() -> None:
    """A resolved phase but non-dict ``waves`` yields ``(phase_id, 0, 0)``."""
    repo_state: dict[str, Any] = {
        "current": {"phase_id": "P02"},
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": {"P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"}},
        "waves": "x",
    }
    assert active_phase_completion(repo_state) == ("P02", 0, 0)


def test_active_phase_completion_malformed_current_falls_back_to_scan() -> None:
    """A non-dict ``current`` is ignored; the lone active phase is scanned."""
    repo_state: dict[str, Any] = {
        "current": "not-a-dict",
        "phases": {"P02": {"id": "P02", "status": "active"}},
        "iters": {"P02-I01": {"id": "P02-I01", "phase_id": "P02", "status": "active"}},
        "waves": {"W1": {"iter_id": "P02-I01", "status": "closed"}},
    }
    assert active_phase_completion(repo_state) == ("P02", 1, 1)


# --------------------------------------------------------------------------
# _phase_cell — phase-id prefix in both render modes
# --------------------------------------------------------------------------


def test_phase_cell_prefixes_phase_id_unicode() -> None:
    """The phase cell starts with the phase id + a space in unicode mode."""
    cell = _phase_cell(_repo_row("P27", 119, 143), mode="unicode")
    assert cell.startswith("P27 ")
    assert "119/143" in cell


def test_phase_cell_prefixes_phase_id_ascii() -> None:
    """The phase cell starts with the phase id + a space in ascii mode."""
    cell = _phase_cell(_repo_row("P27", 3, 6), mode="ascii")
    assert cell.startswith("P27 ")
    assert "3/6" in cell


def test_phase_cell_dash_prefix_when_no_active_phase() -> None:
    """A ``None`` phase id renders the em-dash prefix, not a blank, in both modes."""
    for mode in ("unicode", "ascii"):
        cell = _phase_cell(_repo_row(None, 0, 0), mode=mode)  # type: ignore[arg-type]
        assert cell.startswith("— ")
        assert EMPTY_STATE in cell


# --------------------------------------------------------------------------
# build_repo_rows — populated phase_id field
# --------------------------------------------------------------------------


def test_build_repo_rows_none_state_is_empty() -> None:
    """A ``None`` workspace state yields no rows (the field plumbing is intact)."""
    assert build_repo_rows(None) == []
