"""Profile layer order and declared-root discovery.

Resolution runs built-in, then the global (user) overlay, then the
workspace overlay, then the repository overlay, the last layer holding an
id winning. Custom profiles are found only under those declared roots.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.platform.profiles import discovery
from eawf.platform.profiles.loader import list_profiles, load_profile
from eawf.surfaces.cli.errors import UserError, ValidationError


@pytest.fixture(autouse=True)
def _clear_cache() -> None:
    discovery._clear_cache_for_tests()


@pytest.fixture()
def roots(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Return ``(repo, workspace)`` roots under an isolated home."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    repo = tmp_path / "repo"
    workspace = tmp_path / "ws"
    repo.mkdir()
    workspace.mkdir()
    return repo, workspace


def _write(root: Path, profile_id: str, description: str) -> Path:
    path = root / f"{profile_id}.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"name: {profile_id}\ndescription: {description}\n", encoding="utf-8")
    return path


def _layer_dirs(repo: Path, workspace: Path) -> dict[str, Path]:
    return {
        "user": discovery.user_profiles_dir(),
        "workspace": discovery.workspace_profiles_dir(workspace),
        "repo": discovery.workspace_profiles_dir(repo),
    }


@pytest.mark.parametrize(
    ("present", "winner"),
    [
        ((), "builtin"),
        (("user",), "user"),
        (("user", "workspace"), "workspace"),
        (("user", "workspace", "repo"), "repo"),
        (("repo",), "repo"),
        (("workspace", "repo"), "repo"),
        (("user", "repo"), "repo"),
    ],
)
def test_surf_011_later_layer_wins(
    roots: tuple[Path, Path], present: tuple[str, ...], winner: str
) -> None:
    repo, workspace = roots
    dirs = _layer_dirs(repo, workspace)
    for layer in present:
        _write(dirs[layer], "core", description=f"from-{layer}")
    loc = discovery.discover_profile("core", repo=repo, workspace=workspace)
    assert loc.source == winner
    body = load_profile("core", repo=repo, workspace=workspace)
    if winner != "builtin":
        assert body.description == f"from-{winner}"


@pytest.mark.parametrize("layer", ["user", "workspace", "repo"])
def test_surf_011_every_overlay_layer_contributes_new_ids(
    roots: tuple[Path, Path], layer: str
) -> None:
    repo, workspace = roots
    _write(_layer_dirs(repo, workspace)[layer], "only-here", description=layer)
    assert "only-here" in list_profiles(repo=repo, workspace=workspace)
    assert load_profile("only-here", repo=repo, workspace=workspace).description == layer


def test_surf_011_repository_layer_needs_no_workspace(roots: tuple[Path, Path]) -> None:
    repo, _workspace = roots
    _write(discovery.workspace_profiles_dir(repo), "repo-only", description="repo")
    assert discovery.discover_profile("repo-only", repo=repo).source == "repo"


def test_surf_011_absent_anchor_skips_its_layer(roots: tuple[Path, Path]) -> None:
    repo, workspace = roots
    _write(discovery.workspace_profiles_dir(workspace), "ws-only", description="ws")
    assert "ws-only" not in list_profiles(repo=repo)
    with pytest.raises(UserError, match="unknown profile 'ws-only'"):
        discovery.discover_profile("ws-only", repo=repo)


def test_surf_011_empty_overlay_falls_through_to_builtin(roots: tuple[Path, Path]) -> None:
    repo, workspace = roots
    discovery.workspace_profiles_dir(repo).mkdir(parents=True)
    discovery.workspace_profiles_dir(workspace).mkdir(parents=True)
    assert discovery.discover_profile("core", repo=repo, workspace=workspace).source == "builtin"


def test_surf_011_malformed_repo_overlay_is_refused_not_skipped(
    roots: tuple[Path, Path],
) -> None:
    repo, workspace = roots
    _write(discovery.workspace_profiles_dir(workspace), "core", description="ws")
    (discovery.workspace_profiles_dir(repo) / "core.yaml").parent.mkdir(parents=True)
    (discovery.workspace_profiles_dir(repo) / "core.yaml").write_text("name: [", encoding="utf-8")
    with pytest.raises(ValidationError, match="malformed YAML"):
        load_profile("core", repo=repo, workspace=workspace)


def test_surf_013_nested_directories_under_a_root_are_not_walked(
    roots: tuple[Path, Path],
) -> None:
    repo, workspace = roots
    _write(discovery.workspace_profiles_dir(repo) / "nested", "deep", description="nested")
    assert "deep" not in list_profiles(repo=repo, workspace=workspace)
    with pytest.raises(UserError, match="unknown profile 'deep'"):
        load_profile("deep", repo=repo, workspace=workspace)


def test_surf_013_undeclared_directories_are_not_inferred(roots: tuple[Path, Path]) -> None:
    repo, workspace = roots
    # Look-alike layouts next to, above and below the declared roots.
    _write(repo / "profiles", "sibling", description="x")
    _write(repo.parent / ".ea" / "profiles", "parent", description="x")
    _write(repo / "sub" / ".ea" / "profiles", "child", description="x")
    listing = list_profiles(repo=repo, workspace=workspace)
    assert {"sibling", "parent", "child"}.isdisjoint(listing)


def test_surf_013_only_yaml_files_directly_under_a_root_count(roots: tuple[Path, Path]) -> None:
    repo, workspace = roots
    root = discovery.workspace_profiles_dir(repo)
    _write(root, "kept", description="x")
    (root / "notes.md").write_text("name: notes\n", encoding="utf-8")
    (root / "other.yml").write_text("name: other\n", encoding="utf-8")
    (root / "dir.yaml").mkdir()
    overlay_ids = set(list_profiles(repo=repo, workspace=workspace)) - set(list_profiles())
    assert overlay_ids == {"kept"}
