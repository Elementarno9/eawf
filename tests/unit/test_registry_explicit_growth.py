"""Tests for the explicit-growth guard + staleness boundaries (P25-W08).

The user-scope registry at ``~/.eawf/registry.json`` grows ONLY via
explicit operator commands (``eawf init`` / ``eawf repo add`` /
``eawf workspace add-repo``). Per the project memory note
``feedback_explicit_registry_only`` any other growth path (scan,
walk, import-from-discovery, auto-discovery) is refused with a
directive error pointing at the supported bootstrap surfaces.

This module covers:

- The :class:`ImplicitRegistryGrowthError` contract on each forbidden
  surface label (scan / walk / import-from-scan / auto-discovery).
- The error message names the explicit-bootstrap commands so an
  operator can copy-paste the fix from the exception.
- The 14-day OR-chain staleness boundary at 13d / 14d / 15d.
- Pydantic ``extra="forbid"`` on both registry models (drift defence).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.registry import (
    EXPLICIT_GROWTH_SURFACES,
    FORBIDDEN_GROWTH_PATHS,
    STALE_AFTER,
    ImplicitRegistryGrowthError,
    Registry,
    RegistryRepoEntry,
    is_stale,
    reject_implicit_growth,
)

# ---- Explicit-growth guard --------------------------------------------------


@pytest.mark.parametrize("surface", FORBIDDEN_GROWTH_PATHS)
def test_reject_implicit_growth_each_forbidden_surface(surface: str) -> None:
    """Every documented forbidden surface trips the guard."""
    with pytest.raises(ImplicitRegistryGrowthError) as excinfo:
        reject_implicit_growth(surface)
    assert excinfo.value.surface == surface
    assert surface in str(excinfo.value)


def test_reject_implicit_growth_error_names_explicit_surfaces() -> None:
    """The error message points at the supported explicit-bootstrap surfaces."""
    with pytest.raises(ImplicitRegistryGrowthError) as excinfo:
        reject_implicit_growth("scan")
    msg = str(excinfo.value)
    for surface in EXPLICIT_GROWTH_SURFACES:
        assert surface in msg, f"explicit surface {surface!r} missing from error: {msg}"


def test_reject_implicit_growth_accepts_arbitrary_label() -> None:
    """The guard is free-form so future labels still fail-fast."""
    custom_label = "project-specific-auto-discovery"
    with pytest.raises(ImplicitRegistryGrowthError) as excinfo:
        reject_implicit_growth(custom_label)
    assert excinfo.value.surface == custom_label
    assert custom_label in str(excinfo.value)


def test_reject_implicit_growth_has_no_success_path() -> None:
    """The guard never returns; every call raises."""
    for surface in ("scan", "walk", "import-from-scan", "auto-discovery", "anything"):
        with pytest.raises(ImplicitRegistryGrowthError):
            reject_implicit_growth(surface)


# ---- Pydantic forbid-extra contract -----------------------------------------


def test_registry_repo_entry_forbids_extra_fields() -> None:
    """A typo in a repo entry fails at validate time, not at use time."""
    with pytest.raises(ValidationError) as excinfo:
        RegistryRepoEntry.model_validate(
            {
                "code": "EAWF",
                "path": "/repos/eawf",
                "title": "Ea",
                "typo_field": "boom",
            },
        )
    assert "typo_field" in str(excinfo.value)


def test_registry_forbids_extra_fields() -> None:
    """An extra top-level key also fails the strict-validate gate."""
    with pytest.raises(ValidationError) as excinfo:
        Registry.model_validate(
            {
                "version": "1",
                "updated_at": "2026-05-01T12:00:00+00:00",
                "active_code": None,
                "repos": {},
                "extra_key": "drift",
            },
        )
    assert "extra_key" in str(excinfo.value)


# ---- 14-day OR-chain staleness boundaries -----------------------------------


@pytest.fixture
def fixed_now() -> datetime:
    """Stable "current" timestamp for boundary tests."""
    return datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)


def _entry(
    tmp_path: Path, *, code: str = "DEMO", state_age: timedelta | None = None
) -> tuple[
    RegistryRepoEntry,
    datetime,
]:
    """Build a RegistryRepoEntry whose state.json mtime is ``now - state_age``.

    Returns ``(entry, fixed_now)`` so the test can pass *fixed_now*
    straight to :func:`is_stale`.
    """
    fixed = datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    repo_dir = tmp_path / code
    (repo_dir / ".ea").mkdir(parents=True)
    state_file = repo_dir / ".ea" / "state.json"
    state_file.write_text("{}", encoding="utf-8")
    if state_age is not None:
        ts = (fixed - state_age).timestamp()
        import os

        os.utime(state_file, (ts, ts))
    entry = RegistryRepoEntry(code=code, path=str(repo_dir), title=code)
    return entry, fixed


def test_is_stale_state_mtime_under_threshold_returns_false(tmp_path: Path) -> None:
    """13 days old: under the 14-day threshold, not stale."""
    entry, fixed = _entry(tmp_path, state_age=timedelta(days=13))
    # registry mtime is fresh (just now), so branch (a) is False.
    assert is_stale(entry, registry_mtime_at=fixed, now=fixed) is False


def test_is_stale_state_mtime_exactly_at_threshold_returns_false(tmp_path: Path) -> None:
    """Exactly STALE_AFTER (14 days): NOT stale — strict ``>`` boundary."""
    entry, fixed = _entry(tmp_path, state_age=STALE_AFTER)
    assert is_stale(entry, registry_mtime_at=fixed, now=fixed) is False


def test_is_stale_state_mtime_over_threshold_returns_true(tmp_path: Path) -> None:
    """15 days old: over the 14-day threshold, stale via branch (b)."""
    entry, fixed = _entry(tmp_path, state_age=timedelta(days=15))
    assert is_stale(entry, registry_mtime_at=fixed, now=fixed) is True


def test_is_stale_one_second_over_threshold_returns_true(tmp_path: Path) -> None:
    """Just-over-the-line: 14d + 1s tips branch (b) into stale."""
    entry, fixed = _entry(tmp_path, state_age=STALE_AFTER + timedelta(seconds=1))
    assert is_stale(entry, registry_mtime_at=fixed, now=fixed) is True


def test_is_stale_one_second_under_threshold_returns_false(tmp_path: Path) -> None:
    """Just-under-the-line: 14d - 1s stays fresh."""
    entry, fixed = _entry(tmp_path, state_age=STALE_AFTER - timedelta(seconds=1))
    assert is_stale(entry, registry_mtime_at=fixed, now=fixed) is False


def test_is_stale_registry_mtime_over_threshold_returns_true(
    tmp_path: Path, fixed_now: datetime
) -> None:
    """Branch (a): registry-level mtime alone tips an otherwise-fresh entry."""
    # State mtime is fresh (just touched), but registry mtime is 30 days old.
    entry, fixed = _entry(tmp_path, state_age=timedelta(hours=1))
    old_registry_mtime = fixed - timedelta(days=30)
    assert is_stale(entry, registry_mtime_at=old_registry_mtime, now=fixed) is True


def test_is_stale_missing_state_file_returns_true(tmp_path: Path, fixed_now: datetime) -> None:
    """Branch (c): missing per-repo state.json is treated as stale."""
    # Build an entry pointing at a directory with NO .ea/state.json.
    repo_dir = tmp_path / "NOWHERE"
    repo_dir.mkdir()
    entry = RegistryRepoEntry(code="NOWHERE", path=str(repo_dir), title="Nowhere")
    assert is_stale(entry, registry_mtime_at=fixed_now, now=fixed_now) is True


def test_is_stale_none_registry_mtime_does_not_fire_branch_a(
    tmp_path: Path, fixed_now: datetime
) -> None:
    """``registry_mtime_at=None`` short-circuits branch (a)."""
    entry, fixed = _entry(tmp_path, state_age=timedelta(hours=1))
    assert is_stale(entry, registry_mtime_at=None, now=fixed) is False


# ---- STALE_AFTER threshold contract -----------------------------------------


def test_stale_after_is_14_days() -> None:
    """Threshold value per brief §5.3: 14 days, no other unit."""
    assert timedelta(days=14) == STALE_AFTER


# ---- Explicit-growth surfaces inventory -------------------------------------


def test_explicit_growth_surfaces_include_init_and_add_repo() -> None:
    """The supported bootstrap path covers init + add-repo + workspace add-repo."""
    joined = " ".join(EXPLICIT_GROWTH_SURFACES)
    assert "eawf init" in joined
    assert "eawf repo add" in joined
    assert "eawf workspace add-repo" in joined


def test_forbidden_growth_paths_include_scan_walk_import() -> None:
    """The forbidden inventory mirrors the memory note's named anti-patterns."""
    assert "scan" in FORBIDDEN_GROWTH_PATHS
    assert "walk" in FORBIDDEN_GROWTH_PATHS
    assert "import-from-scan" in FORBIDDEN_GROWTH_PATHS
