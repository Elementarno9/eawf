"""Unit tests for :mod:`eawf.platform.lint.tools.title_backfill`.

The generalized entity-title backfill (P29-I07-W07) extends the backlog-only
sweep (P29-I02-W09) to all five lifecycle / decision kinds. These tests pin
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
from eawf.platform.lint.tools.title_backfill import (
    ENTITY_KINDS,
    backfill_entity_titles,
    normalize_title,
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


def test_normalize_title_strips_conventional_commit_prefix() -> None:
    """A wave title leading with a conventional-commit type prefix loses it."""
    assert (
        normalize_title("feat: add the spawn pipeline", None, strip_commit_prefix=True)
        == "add the spawn pipeline"
    )


def test_normalize_title_strips_only_when_flagged() -> None:
    """The commit-prefix strip is gated on the flag (default keeps the head)."""
    assert (
        normalize_title("feat: add the spawn pipeline", None, strip_commit_prefix=False)
        == "feat: add the spawn pipeline"
    )


def test_normalize_title_preserves_phase_id_substring() -> None:
    """Linkage hazard: a P<NN> inside a decision title is preserved verbatim."""
    title = "Adopt P29 spawn rebuild from spec"
    assert normalize_title(title, None) == title


def test_normalize_title_collapses_cluster_soup() -> None:
    """Three-or-more +-joined tokens collapse to space-separated words."""
    result = normalize_title("spawn+jury+orchestrator wiring", None)
    assert result == "spawn jury orchestrator wiring"


def test_normalize_title_keeps_two_token_conjunction() -> None:
    """A two-token A+B conjunction is left intact (soup starts at three)."""
    assert normalize_title("spawn+jury wiring", None) == "spawn+jury wiring"


def test_normalize_title_carves_out_cpp() -> None:
    """A legitimate C++ language name is not treated as cluster soup."""
    assert normalize_title("Ship the C++ adapter shim", None) == "Ship the C++ adapter shim"


def test_normalize_title_strips_trailing_period() -> None:
    """A trailing period is stripped (titles are labels, not prose)."""
    assert normalize_title("Add a bounded title.", None) == "Add a bounded title"


def test_normalize_title_is_idempotent() -> None:
    """Re-normalizing a normalized title is a no-op."""
    once = normalize_title(
        "feat: add the spawn pipeline+jury+gate logic.", None, strip_commit_prefix=True
    )
    assert normalize_title(once, None, strip_commit_prefix=True) == once


# --- backfill_entity_titles: dry-run (no mutation) --------------------------


def test_backfill_dry_run_mutates_nothing(tmp_path: Path) -> None:
    """Boundary: --dry-run reports proposed diffs but leaves every title intact."""
    state = _state(tmp_path)
    _seed_phase(state, "P01", "Phase title.", status=PhaseStatus.ACTIVE)
    _seed_wave(state, "P01-I01-W01", "feat: do the thing", status=WaveStatus.PENDING)
    report, event = backfill_entity_titles(state, apply=False)
    assert event is None
    assert report.applied is False
    assert report.changed == 2
    # State untouched.
    assert state.phases["P01"].title == "Phase title."
    assert state.waves["P01-I01-W01"].title == "feat: do the thing"


def test_backfill_dry_run_reports_violations(tmp_path: Path) -> None:
    """A trailing-period title trips the style-lint violation counter."""
    state = _state(tmp_path)
    _seed_phase(state, "P01", "Trailing period.", status=PhaseStatus.PLANNED)
    report, _ = backfill_entity_titles(state, apply=False)
    row = next(r for r in report.rows if r.entity_id == "P01")
    assert row.violations  # trailing-period lint fires
    assert row.changed is True
    assert row.after == "Trailing period"


# --- backfill_entity_titles: apply across all five kinds --------------------


def test_backfill_apply_normalizes_all_five_kinds(tmp_path: Path) -> None:
    """All five entity kinds are swept and normalized under --apply."""
    state = _state(tmp_path)
    _seed_phase(state, "P01", "Phase title with a trailing dot.", status=PhaseStatus.ACTIVE)
    _seed_iter(state, "P01-I01", "Iter title needs a fix.", status=IterStatus.ACTIVE)
    _seed_wave(state, "P01-I01-W01", "feat: wire the spawn path", status=WaveStatus.PENDING)
    _seed_backlog(state, "B001", "Backlog with a dot.", status=BacklogStatus.OPEN)
    _seed_decision(state, "D001", "Decision title with a dot.", status=DecisionStatus.ACTIVE)

    report, event = backfill_entity_titles(state, apply=True)

    assert event is not None
    assert report.applied is True
    assert report.changed == 5
    # Every kind has exactly one changed row.
    changed_kinds = {row.kind for row in report.rows if row.changed}
    assert changed_kinds == set(ENTITY_KINDS)
    # Mutations landed.
    assert state.phases["P01"].title == "Phase title with a trailing dot"
    assert state.iters["P01-I01"].title == "Iter title needs a fix"
    assert state.waves["P01-I01-W01"].title == "wire the spawn path"
    assert state.backlog["B001"].title == "Backlog with a dot"
    assert state.decisions["D001"].title == "Decision title with a dot"
    assert event.payload["event_type"] == "state.backfill_titles"


def test_backfill_apply_strips_commit_prefix_from_wave_title(tmp_path: Path) -> None:
    """A wave title's conventional-commit prefix is stripped on apply."""
    state = _state(tmp_path)
    _seed_wave(state, "P01-I01-W01", "docs: explain the gate-pack", status=WaveStatus.IN_PROGRESS)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is not None
    assert state.waves["P01-I01-W01"].title == "explain the gate-pack"
    row = next(r for r in report.rows if r.entity_id == "P01-I01-W01")
    assert row.changed is True


def test_backfill_apply_preserves_phase_id_in_decision_title(tmp_path: Path) -> None:
    """Regression: a P<NN> inside a decision title survives the apply path."""
    state = _state(tmp_path)
    # Trailing period forces a change so the row goes through the apply branch;
    # the P29 substring must be carried through untouched.
    _seed_decision(
        state, "D001", "Adopt P29 spawn rebuild from spec.", status=DecisionStatus.ACTIVE
    )
    report, event = backfill_entity_titles(state, apply=True)
    assert event is not None
    assert state.decisions["D001"].title == "Adopt P29 spawn rebuild from spec"
    row = next(r for r in report.rows if r.entity_id == "D001")
    assert "P29" in row.after


def test_backfill_apply_no_changes_returns_no_event(tmp_path: Path) -> None:
    """Boundary: --apply over already-clean entities mutates nothing, emits no event."""
    state = _state(tmp_path)
    _seed_phase(state, "P01", "Already a clean label", status=PhaseStatus.ACTIVE)
    _seed_wave(state, "P01-I01-W01", "Already a clean wave label", status=WaveStatus.PENDING)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is None
    assert report.applied is False
    assert report.changed == 0


# --- over-72 rejected by re-validation --------------------------------------


def test_over_cap_title_is_normalized_within_cap_on_apply(tmp_path: Path) -> None:
    """Apply trims an over-72 title to a word boundary within the cap."""
    state = _state(tmp_path)
    over = (
        "Split the workflow state module into layered submodules for long-term "
        "clarity and reviewability across the whole codebase"
    )
    assert len(over) > 72
    _seed_phase(state, "P01", over, status=PhaseStatus.ACTIVE)
    _report, event = backfill_entity_titles(state, apply=True)
    assert event is not None
    new_title = state.phases["P01"].title
    assert len(new_title) <= 72
    assert over.startswith(new_title)


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


def test_backfill_leaves_closed_wave_unchanged(tmp_path: Path) -> None:
    """A closed wave is swept for reporting but never mutated under --apply."""
    state = _state(tmp_path)
    _seed_wave(state, "P01-I01-W01", "feat: closed work.", status=WaveStatus.CLOSED)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is None
    assert state.waves["P01-I01-W01"].title == "feat: closed work."
    row = next(r for r in report.rows if r.entity_id == "P01-I01-W01")
    assert row.changed is False
    assert row.frozen is True
    assert row.violations  # trailing-period lint still records the violation


def test_backfill_leaves_superseded_decision_unchanged(tmp_path: Path) -> None:
    """A superseded decision is reported but frozen against mutation."""
    state = _state(tmp_path)
    _seed_decision(state, "D001", "Old decision.", status=DecisionStatus.SUPERSEDED)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is None
    assert state.decisions["D001"].title == "Old decision."
    row = next(r for r in report.rows if r.entity_id == "D001")
    assert row.frozen is True
    assert row.changed is False


def test_backfill_leaves_closed_iter_unchanged(tmp_path: Path) -> None:
    """A closed iter is frozen (status-agnostic edit does not extend to terminal)."""
    state = _state(tmp_path)
    _seed_iter(state, "P01-I01", "Closed iter title.", status=IterStatus.CLOSED)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is None
    assert state.iters["P01-I01"].title == "Closed iter title."
    row = next(r for r in report.rows if r.entity_id == "P01-I01")
    assert row.frozen is True
    assert row.changed is False


# --- kind filter + boundaries -----------------------------------------------


def test_backfill_kind_filter_restricts_sweep(tmp_path: Path) -> None:
    """--kind restricts the sweep to the named kinds only."""
    state = _state(tmp_path)
    _seed_phase(state, "P01", "Phase dot.", status=PhaseStatus.ACTIVE)
    _seed_wave(state, "P01-I01-W01", "feat: wave work", status=WaveStatus.PENDING)
    report, _ = backfill_entity_titles(state, apply=False, kinds=["wave"])
    kinds_in_report = {row.kind for row in report.rows}
    assert kinds_in_report == {"wave"}
    assert report.total == 1


def test_backfill_unknown_kind_raises() -> None:
    """Error path: an unknown kind name raises ValueError."""
    state = State.model_construct()  # empty shell; sweep never reads it before the guard
    with pytest.raises(ValueError, match="unknown entity kind"):
        backfill_entity_titles(state, apply=False, kinds=["bogus"])


def test_backfill_empty_state(tmp_path: Path) -> None:
    """Boundary: a state with no entities returns a zero-row report and no event."""
    state = _state(tmp_path)
    report, event = backfill_entity_titles(state, apply=True)
    assert event is None
    assert report.total == 0
    assert report.rows == []
    assert report.applied is False
