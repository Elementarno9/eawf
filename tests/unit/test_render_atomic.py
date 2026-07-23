"""Atomic render-writer control-plane lock tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.render import _atomic


def test_atomic_write_text_keeps_stable_lock_outside_rendered_tree(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stable portalock inode persists in control-plane storage, not output."""
    control_root = tmp_path / "control"
    monkeypatch.setattr(_atomic.tempfile, "gettempdir", lambda: str(control_root))
    target = tmp_path / "published" / "agents" / "executor.md"

    _atomic.atomic_write_text(target, "first\n")
    lock_target = _atomic._render_lock_target(target)
    lock_path = lock_target.with_name(f"{lock_target.name}.lock")
    first_inode = lock_path.stat().st_ino

    assert target.read_text(encoding="utf-8") == "first\n"
    assert not list((tmp_path / "published").rglob("*.lock"))
    assert lock_path.read_text(encoding="utf-8") == ""

    _atomic.atomic_write_text(target, "second\n")

    assert target.read_text(encoding="utf-8") == "second\n"
    assert lock_path.stat().st_ino == first_inode
    assert lock_path.read_text(encoding="utf-8") == ""


def test_render_lock_target_is_deterministic_and_target_specific(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Canonical target identity selects one stable, non-PII lock filename."""
    control_root = tmp_path / "control"
    monkeypatch.setattr(_atomic.tempfile, "gettempdir", lambda: str(control_root))
    target = tmp_path / "rendered" / "SKILL.md"

    first = _atomic._render_lock_target(target)
    second = _atomic._render_lock_target(target.parent / "." / target.name)
    sibling = _atomic._render_lock_target(target.with_name("OTHER.md"))

    assert first == second
    assert first != sibling
    assert first.parent.parent == control_root / "eawf-render-locks"
    assert str(target) not in first.name
