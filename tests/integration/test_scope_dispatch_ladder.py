"""Tests for :func:`eawf.surfaces.cli.scope.resolve_scope_tier` (P25-W08).

The scope dispatch ladder per C07b §5.3 is "cwd → workspace > repo >
user" with first-match-wins semantics. Concretely:

- **repo tier**: cwd is at-or-under a registered repo's path.
- **workspace tier**: cwd is outside every registered repo path but
  the registry is non-empty.
- **user tier**: registry is missing, empty, or unreadable.

These integration tests build a hermetic registry under ``tmp_path``
plus a small repo tree, then walk each cwd through the ladder. No
real ``~/.eawf/registry.json`` is ever touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest

from eawf.platform.registry import Registry, RegistryRepoEntry
from eawf.surfaces.cli.scope import (
    ScopeResolution,
    ScopeTier,
    resolve_scope_tier,
)

# ---- Fixtures ---------------------------------------------------------------


def _write_registry(
    registry_path: Path,
    *,
    repos: dict[str, tuple[str, str]],
    active_code: str | None = None,
) -> None:
    """Build a registry JSON file under *registry_path*.

    *repos* maps ``code -> (on_disk_path, title)``.
    """
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry = Registry(
        version="1",
        updated_at=datetime.now(UTC),
        active_code=active_code,
        repos={
            code: RegistryRepoEntry(code=code, path=path, title=title)
            for code, (path, title) in repos.items()
        },
    )
    registry_path.write_bytes(orjson.dumps(registry.model_dump(mode="json")))


@pytest.fixture
def registry_path(tmp_path: Path) -> Path:
    """Path to the hermetic registry used by every test."""
    return tmp_path / "registry.json"


# ---- Repo-tier resolution ---------------------------------------------------


def test_resolve_repo_tier_cwd_at_repo_root(tmp_path: Path, registry_path: Path) -> None:
    """cwd exactly equal to a registered repo path → repo tier."""
    repo = tmp_path / "Repos" / "demo"
    repo.mkdir(parents=True)
    _write_registry(registry_path, repos={"DEMO": (str(repo), "Demo")})

    resolution = resolve_scope_tier(cwd=repo, registry_path=registry_path)

    assert resolution.tier is ScopeTier.REPO
    assert resolution.repo_entry is not None
    assert resolution.repo_entry.code == "DEMO"
    assert resolution.registry_path == registry_path


def test_resolve_repo_tier_cwd_deep_inside_repo(tmp_path: Path, registry_path: Path) -> None:
    """cwd nested under a registered repo path → repo tier."""
    repo = tmp_path / "Repos" / "demo"
    deep = repo / "src" / "pkg" / "sub"
    deep.mkdir(parents=True)
    _write_registry(registry_path, repos={"DEMO": (str(repo), "Demo")})

    resolution = resolve_scope_tier(cwd=deep, registry_path=registry_path)

    assert resolution.tier is ScopeTier.REPO
    assert resolution.repo_entry is not None
    assert resolution.repo_entry.code == "DEMO"


def test_resolve_repo_tier_deepest_match_wins(tmp_path: Path, registry_path: Path) -> None:
    """Nested repo layout: the deepest registered ancestor wins.

    A monorepo with a registered child repo should dispatch to the
    child, not the outer parent.
    """
    parent = tmp_path / "Repos" / "monorepo"
    child = parent / "packages" / "child"
    child.mkdir(parents=True)
    _write_registry(
        registry_path,
        repos={
            "PARENT": (str(parent), "Parent"),
            "CHILD": (str(child), "Child"),
        },
    )

    resolution = resolve_scope_tier(cwd=child / "src", registry_path=registry_path)
    child.joinpath("src").mkdir()

    assert resolution.tier is ScopeTier.REPO
    assert resolution.repo_entry is not None
    assert resolution.repo_entry.code == "CHILD"


def test_resolve_repo_tier_first_match_short_circuits_workspace(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """Even when other repos exist (workspace surface available), a
    repo-cwd match always wins per the brief's first-match-wins rule.
    """
    repo_a = tmp_path / "Repos" / "alpha"
    repo_b = tmp_path / "Repos" / "bravo"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    _write_registry(
        registry_path,
        repos={
            "ALPHA": (str(repo_a), "Alpha"),
            "BRAVO": (str(repo_b), "Bravo"),
        },
        active_code="ALPHA",
    )

    resolution = resolve_scope_tier(cwd=repo_b, registry_path=registry_path)

    assert resolution.tier is ScopeTier.REPO
    assert resolution.repo_entry is not None
    assert resolution.repo_entry.code == "BRAVO"


# ---- Workspace-tier resolution ----------------------------------------------


def test_resolve_workspace_tier_when_cwd_outside_all_repos(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """Non-empty registry but cwd is outside every registered path."""
    repo = tmp_path / "Repos" / "demo"
    repo.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_registry(registry_path, repos={"DEMO": (str(repo), "Demo")})

    resolution = resolve_scope_tier(cwd=elsewhere, registry_path=registry_path)

    assert resolution.tier is ScopeTier.WORKSPACE
    assert resolution.repo_entry is None
    assert resolution.registry is not None
    assert "DEMO" in resolution.registry.repos


def test_resolve_workspace_tier_sees_full_registry(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """Workspace-tier callers get the full registry for dashboard rendering."""
    repo_a = tmp_path / "Repos" / "alpha"
    repo_b = tmp_path / "Repos" / "bravo"
    repo_a.mkdir(parents=True)
    repo_b.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_registry(
        registry_path,
        repos={
            "ALPHA": (str(repo_a), "Alpha"),
            "BRAVO": (str(repo_b), "Bravo"),
        },
        active_code="ALPHA",
    )

    resolution = resolve_scope_tier(cwd=elsewhere, registry_path=registry_path)

    assert resolution.tier is ScopeTier.WORKSPACE
    assert resolution.registry is not None
    assert resolution.registry.active_code == "ALPHA"
    assert set(resolution.registry.repos.keys()) == {"ALPHA", "BRAVO"}


# ---- User-tier resolution ---------------------------------------------------


def test_resolve_user_tier_when_registry_missing(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """No registry file → user tier, registry=None."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    # Note: registry_path is NOT created.

    resolution = resolve_scope_tier(cwd=elsewhere, registry_path=registry_path)

    assert resolution.tier is ScopeTier.USER
    assert resolution.repo_entry is None
    assert resolution.registry is None
    assert resolution.registry_path == registry_path


def test_resolve_user_tier_when_registry_empty(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """Registry file exists but has zero entries → user tier."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _write_registry(registry_path, repos={})

    resolution = resolve_scope_tier(cwd=elsewhere, registry_path=registry_path)

    assert resolution.tier is ScopeTier.USER
    assert resolution.repo_entry is None
    # Empty registry was loaded, so the typed Registry comes back populated.
    assert resolution.registry is not None
    assert resolution.registry.repos == {}


def test_resolve_user_tier_when_registry_corrupted(
    tmp_path: Path,
    registry_path: Path,
) -> None:
    """Unreadable registry → user tier (read errors do not propagate)."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    registry_path.parent.mkdir(parents=True, exist_ok=True)
    registry_path.write_text("{not json", encoding="utf-8")

    resolution = resolve_scope_tier(cwd=elsewhere, registry_path=registry_path)

    assert resolution.tier is ScopeTier.USER
    assert resolution.registry is None


# ---- Default-path branch ----------------------------------------------------


def test_resolve_uses_default_registry_path_when_unset(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``registry_path=None`` falls back to ``<home>/.eawf/registry.json``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: fake_home)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    resolution = resolve_scope_tier(cwd=elsewhere)

    assert resolution.tier is ScopeTier.USER
    assert resolution.registry_path == fake_home / ".eawf" / "registry.json"


def test_resolve_uses_cwd_default(
    tmp_path: Path,
    registry_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``cwd=None`` falls back to :func:`Path.cwd`."""
    repo = tmp_path / "Repos" / "demo"
    repo.mkdir(parents=True)
    _write_registry(registry_path, repos={"DEMO": (str(repo), "Demo")})
    monkeypatch.chdir(repo)

    resolution = resolve_scope_tier(registry_path=registry_path)

    assert resolution.tier is ScopeTier.REPO
    assert resolution.repo_entry is not None
    assert resolution.repo_entry.code == "DEMO"


# ---- Resolution data carrier ------------------------------------------------


def test_scope_resolution_is_frozen() -> None:
    """The dataclass is frozen so callers cannot mutate the result in place."""
    resolution = ScopeResolution(
        tier=ScopeTier.USER,
        repo_entry=None,
        registry_path=Path("/tmp/r"),
        registry=None,
    )
    # ``frozen=True`` raises ``dataclasses.FrozenInstanceError`` on attribute
    # writes; the exception is private under :mod:`dataclasses` so we anchor
    # on the public ``AttributeError`` base class instead.
    with pytest.raises(AttributeError):
        resolution.tier = ScopeTier.REPO  # type: ignore[misc]


def test_scope_tier_values_match_brief() -> None:
    """The three tier names match the brief's §5.3 vocabulary."""
    assert ScopeTier.REPO.value == "repo"
    assert ScopeTier.WORKSPACE.value == "workspace"
    assert ScopeTier.USER.value == "user"
