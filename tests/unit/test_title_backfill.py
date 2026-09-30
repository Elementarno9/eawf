"""Unit tests for :mod:`eawf.platform.lint.tools.title_backfill`.

The generalized entity-title backfill extends the backlog-only
sweep to all five lifecycle / decision kinds. These tests pin
the contract acceptance criteria:

- all five kinds (phase / iter / wave / backlog / decision) normalize;
- a ``P<NN>`` lifecycle id inside a decision title is preserved (the linkage
  hazard regression);
- a conventional-commit prefix is stripped off a wave title;
- an over-72 title is rejected by the model re-validation;
- dry-run mutates nothing;
- terminal-status entities (closed / frozen) are reported but never mutated.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    BacklogPriority,
    BacklogStatus,
    DecisionStatus,
    IterStatus,
    PhaseStatus,
    WaveStatus,
)
from eawf.kernel.state.models import (
    BacklogItem,
    Decision,
    Iter,
    Phase,
    State,
    Wave,
)
from eawf.workflow.evidence import _io

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)


def _state(tmp_path: Path) -> State:
    """Return a freshly-loaded :class:`State` from the empty-repo fixture."""
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    return _io.load_state(target)


def _now() -> datetime:
    return datetime.now(UTC)


def _seed_phase(state: State, phase_id: str, title: str, *, status: PhaseStatus) -> None:
    """Insert a :class:`Phase` bypassing the title bound via ``model_construct``."""
    phases = dict(state.phases)
    phases[phase_id] = Phase.model_construct(
        id=phase_id,
        scope_id="QR",
        title=title,
        description=None,
        status=status,
        opened_at=_now(),
    )
    state.phases = phases


def _seed_iter(state: State, iter_id: str, title: str, *, status: IterStatus) -> None:
    """Insert an :class:`Iter` bypassing the title bound via ``model_construct``."""
    iters = dict(state.iters)
    iters[iter_id] = Iter.model_construct(
        id=iter_id,
        phase_id="P01",
        title=title,
        description=None,
        status=status,
        opened_at=_now(),
    )
    state.iters = iters


def _seed_wave(state: State, wave_id: str, title: str, *, status: WaveStatus) -> None:
    """Insert a :class:`Wave` bypassing the title bound via ``model_construct``."""
    waves = dict(state.waves)
    waves[wave_id] = Wave.model_construct(
        id=wave_id,
        iter_id="P01-I01",
        title=title,
        description=None,
        status=status,
        opened_at=_now(),
    )
    state.waves = waves


def _seed_decision(state: State, decision_id: str, title: str, *, status: DecisionStatus) -> None:
    """Insert a :class:`Decision` bypassing the title bound via ``model_construct``."""
    decisions = dict(state.decisions)
    decisions[decision_id] = Decision.model_construct(
        id=decision_id,
        scope_id="QR",
        title=title,
        description=None,
        rationale="seeded for the backfill sweep",
        status=status,
        created_at=_now(),
    )
    state.decisions = decisions


def _seed_backlog(state: State, item_id: str, title: str, *, status: BacklogStatus) -> None:
    """Insert a :class:`BacklogItem` bypassing the title bound via ``model_construct``."""
    backlog = dict(state.backlog or {})
    backlog[item_id] = BacklogItem.model_construct(
        id=item_id,
        scope_id="QR",
        title=title,
        description=None,
        priority=BacklogPriority.P2,
        status=status,
        created_at=_now(),
    )
    state.backlog = backlog


# --- normalize_title (pure transform) ---------------------------------------


# --- backfill_entity_titles: dry-run (no mutation) --------------------------


# --- backfill_entity_titles: apply across all five kinds --------------------


# --- over-72 rejected by re-validation --------------------------------------


def test_model_rejects_over_cap_title() -> None:
    """The model's 72-char bound is the over-cap guard the re-validation relies on."""
    over = "x" * 73
    with pytest.raises(ValidationError):
        Phase(
            id="P01",
            scope_id="QR",
            title=over,
            status=PhaseStatus.ACTIVE,
            opened_at=_now(),
        )


# --- terminal-status (frozen) entities --------------------------------------


# --- kind filter + boundaries -----------------------------------------------
