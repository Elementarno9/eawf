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
    spread = {_atomic._render_lock_target(target.with_name(f"F{n}.md")) for n in range(64)}

    assert first == second
    assert len(spread) > 1
    assert first.parent.parent == control_root / "eawf-render-locks"
    assert str(target) not in first.name
    assert target.name not in first.name


def test_render_lock_files_do_not_grow_with_distinct_targets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rendering many distinct targets reuses a bounded set of lock files.

    One lock file per target leaked tens of thousands of empty files into
    the shared temp dir per test-suite run, because every ``tmp_path`` tree is
    a fresh target. Lock files must stay capped at the stripe count, and
    rewriting targets that were already rendered must create none.
    """
    control_root = tmp_path / "control"
    monkeypatch.setattr(_atomic.tempfile, "gettempdir", lambda: str(control_root))
    monkeypatch.setattr(_atomic, "RENDER_LOCK_STRIPES", 8, raising=False)
    targets = [tmp_path / "out" / f"run-{n}" / "AGENTS.md" for n in range(40)]

    for target in targets:
        _atomic.atomic_write_text(target, "body\n")
    lock_files = [path for path in sorted(control_root.rglob("*")) if path.is_file()]

    assert 0 < len(lock_files) <= 8

    for _ in range(2):
        for target in targets:
            _atomic.atomic_write_text(target, "again\n")
    after = [path for path in sorted(control_root.rglob("*")) if path.is_file()]

    assert after == lock_files
    assert not list((tmp_path / "out").rglob("*.lock"))
    assert not list((tmp_path / "out").rglob("*.tmp.*"))
