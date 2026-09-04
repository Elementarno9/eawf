"""Unit tests for :mod:`eawf.runtime.sandbox.cwd_guard`.

Pin the path-containment predicate + the gate-runner cwd guard:

- :func:`is_path_inside` is true for self, descendants, and resolves
  ``..`` segments before comparing.
- :func:`assert_cwd_inside` raises :class:`CwdGuardError` for any
  cwd outside *root*.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.runtime.sandbox.cwd_guard import (
    CwdGuardError,
    assert_cwd_inside,
    is_path_inside,
)


def test_is_path_inside_same_dir(tmp_path: Path) -> None:
    """A directory is considered inside itself (closed comparison)."""
    assert is_path_inside(tmp_path, root=tmp_path) is True


def test_is_path_inside_descendant(tmp_path: Path) -> None:
    """A nested descendant resolves inside the root."""
    nested = tmp_path / "a" / "b" / "c"
    assert is_path_inside(nested, root=tmp_path) is True


def test_is_path_inside_sibling(tmp_path: Path) -> None:
    """A sibling of *root* is not inside."""
    root = tmp_path / "root"
    sibling = tmp_path / "sibling"
    root.mkdir()
    sibling.mkdir()
    assert is_path_inside(sibling, root=root) is False


def test_is_path_inside_parent(tmp_path: Path) -> None:
    """The parent of *root* is not inside *root*."""
    root = tmp_path / "root"
    root.mkdir()
    assert is_path_inside(tmp_path, root=root) is False


def test_is_path_inside_resolves_dotdot(tmp_path: Path) -> None:
    """A ``..`` segment that escapes *root* rejects after resolution."""
    root = tmp_path / "root"
    root.mkdir()
    candidate = root / ".." / "sibling"
    assert is_path_inside(candidate, root=root) is False


def test_is_path_inside_handles_nonexistent_paths(tmp_path: Path) -> None:
    """Predicate works for paths that do not yet exist on disk."""
    not_yet = tmp_path / "freshly-computed" / "wt"
    assert is_path_inside(not_yet, root=tmp_path) is True


def test_assert_cwd_inside_passes_when_contained(tmp_path: Path) -> None:
    """Containment passes silently — no return value, no exception."""
    nested = tmp_path / "a"
    assert assert_cwd_inside(nested, root=tmp_path) is None


def test_assert_cwd_inside_rejects_outside(tmp_path: Path) -> None:
    """A cwd outside *root* raises :class:`CwdGuardError`."""
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    with pytest.raises(CwdGuardError, match="outside repo root"):
        assert_cwd_inside(outside, root=root)


def test_assert_cwd_inside_rejects_dotdot_escape(tmp_path: Path) -> None:
    """A ``..`` escape rejects after resolution."""
    root = tmp_path / "root"
    root.mkdir()
    escape = root / ".." / "sibling"
    with pytest.raises(CwdGuardError, match="outside repo root"):
        assert_cwd_inside(escape, root=root)


def test_cwd_guard_violation_subclasses_value_error() -> None:
    """The guard exception subclasses :class:`ValueError` for catch-broad."""
    assert issubclass(CwdGuardError, ValueError)
