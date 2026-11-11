"""The price book reads its overrides from the repository the state resolver names.

A linked worktree checks out a frozen copy of the repository's ``.ea``, so
pricing from one must read the main checkout's ``reflect.pricing.overrides``,
as must a run naming the tree through ``EA_STATE`` or ``-w``. The book is
cached per repository, so two repositories in one process never share one.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from eawf.observability.telemetry import pricing
from eawf.observability.telemetry.pricing import active_price_book, price_book_root
from eawf.runtime.daemon.runtime_dir import pin_workspace
from tests._linked_worktree import git, repo_with_linked_worktree

pytestmark = pytest.mark.unit

_MODEL = "acme-test-model"


def _override(root: Path, note: str) -> None:
    """Write a repository-layer override for :data:`_MODEL` noted *note* under *root*."""
    rates = {"input": "1", "output": "2", "cache_read": "0.1", "cache_write": {}}
    body = {"reflect": {"pricing": {"overrides": {_MODEL: {"rates": rates, "note": note}}}}}
    (root / ".ea").mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


def _note(root: Path | None = None) -> str | None:
    override = active_price_book(root).overrides.get(_MODEL)
    return None if override is None else override.note


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Keep the global layer, ``EA_STATE`` and the workspace pin out of each test."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("EA_STATE", raising=False)
    previous = pin_workspace(None)
    pricing._price_book.cache_clear()
    yield
    pricing._price_book.cache_clear()
    pin_workspace(previous)


@pytest.fixture
def trees(tmp_path: Path) -> tuple[Path, Path]:
    """Return a main checkout and its linked worktree, each with its own override note."""
    main, linked = repo_with_linked_worktree(tmp_path / "repo")
    _override(main, "main checkout")
    _override(linked, "frozen copy")
    return main, linked


def test_overrides_from_main_checkout(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked = trees
    monkeypatch.chdir(linked / ".ea")

    assert price_book_root() == main
    assert _note() == "main checkout"


def test_ea_state_names_the_repository(
    trees: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _linked = trees
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(outside)
    monkeypatch.setenv("EA_STATE", str(main / ".ea" / "state.json"))

    assert price_book_root() == main
    assert _note() == "main checkout"


def test_the_workspace_flag_names_the_repository(
    trees: tuple[Path, Path], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, _linked = trees
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    _override(other, "other repository")
    monkeypatch.chdir(other)
    pin_workspace(main)

    assert price_book_root() == main
    assert _note() == "main checkout"


def test_outside_any_repository_there_are_no_overrides(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bare_dir = tmp_path / "plain"
    bare_dir.mkdir()
    monkeypatch.chdir(bare_dir)

    assert price_book_root() is None
    assert _note() is None


def test_a_linked_worktree_named_by_ea_state_prices_without_overrides(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    _main, linked = trees
    monkeypatch.setenv("EA_STATE", str(linked / ".ea" / "state.json"))

    assert price_book_root() is None
    assert _note() is None


def test_each_repository_keeps_its_own_book(trees: tuple[Path, Path], tmp_path: Path) -> None:
    main, _linked = trees
    other = tmp_path / "other"
    _override(other, "other repository")

    assert _note(main) == "main checkout"
    assert _note(other) == "other repository"
    assert active_price_book(main) is active_price_book(main)
    assert active_price_book(main) is not active_price_book(other)


def test_an_explicit_root_is_used_as_given(
    trees: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    main, linked = trees
    monkeypatch.chdir(main)

    assert _note(linked) == "frozen copy"
