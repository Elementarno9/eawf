"""Tests for the workspace resolution ladder and membership algebra.

The three behaviours the wave is defined by:

* one exactly-registered repository root resolves its workspace;
* a root whose membership overlaps two workspaces refuses with
  ``workspace_ambiguous`` and lists the candidates;
* an unregistered root returns ``workspace_not_registered`` and no
  parent directory is consulted - proven by registering the *parent* as
  a workspace member and resolving from the child, which must still
  refuse.

Every registry here is built in memory from ``tmp_path`` roots; nothing
reads or writes the operator's real ``~/.eawf/registry.json``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.platform.registry import (
    WORKSPACE_ALREADY_REGISTERED,
    WORKSPACE_AMBIGUOUS,
    WORKSPACE_NOT_REGISTERED,
    WORKSPACE_REVISION_CONFLICT,
    Registry,
    RegistryRepoEntry,
    WorkspaceMutationError,
    WorkspaceRecord,
    WorkspaceResolutionError,
    WorkspaceSource,
    create_workspace,
    get_workspace,
    list_workspaces,
    project_codes_at_root,
    resolve_workspace,
    update_membership,
)

pytestmark = pytest.mark.unit


def _registry(
    *,
    repos: dict[str, Path],
    workspaces: dict[str, tuple[set[str], str]],
) -> Registry:
    """Build a registry from ``code -> path`` and ``key -> (members, home)``."""
    return Registry(
        repos={code: RegistryRepoEntry(code=code, path=str(path)) for code, path in repos.items()},
        workspaces={
            key: WorkspaceRecord(
                key=key,
                member_project_codes=frozenset(members),
                home_project_code=home,
            )
            for key, (members, home) in workspaces.items()
        },
    )


@pytest.fixture
def roots(tmp_path: Path) -> dict[str, Path]:
    """Create three registered repository roots under ``tmp_path``."""
    made: dict[str, Path] = {}
    for code in ("EAWF", "DEMO", "SOLO"):
        root = tmp_path / code.lower()
        root.mkdir()
        made[code] = root
    return made


# ---- rung 4-5: exact repository-root match ---------------------------------


def test_resolve_workspace_exact_root_resolves_its_workspace(roots: dict[str, Path]) -> None:
    registry = _registry(repos=roots, workspaces={"MONO": ({"EAWF", "DEMO"}, "EAWF")})
    resolution = resolve_workspace(registry, repo_root=roots["EAWF"])
    assert resolution.key == "MONO"
    assert resolution.source is WorkspaceSource.REPO_ROOT
    assert resolution.record.home_project_code == "EAWF"


def test_resolve_workspace_ignores_unrelated_workspace(roots: dict[str, Path]) -> None:
    registry = _registry(
        repos=roots,
        workspaces={"MONO": ({"EAWF"}, "EAWF"), "SIDE": ({"SOLO"}, "SOLO")},
    )
    assert resolve_workspace(registry, repo_root=roots["SOLO"]).key == "SIDE"


def test_resolve_workspace_ambiguous_lists_candidates(roots: dict[str, Path]) -> None:
    registry = _registry(
        repos=roots,
        workspaces={
            "MONO": ({"EAWF", "DEMO"}, "EAWF"),
            "ALT": ({"EAWF"}, "EAWF"),
            "SIDE": ({"SOLO"}, "SOLO"),
        },
    )
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(registry, repo_root=roots["EAWF"])
    assert excinfo.value.code == WORKSPACE_AMBIGUOUS
    assert excinfo.value.candidates == ("ALT", "MONO")
    assert "ALT, MONO" in str(excinfo.value)


def test_resolve_workspace_ambiguity_yields_to_an_explicit_selection(
    roots: dict[str, Path],
) -> None:
    registry = _registry(
        repos=roots,
        workspaces={"MONO": ({"EAWF"}, "EAWF"), "ALT": ({"EAWF"}, "EAWF")},
    )
    resolution = resolve_workspace(registry, session_key="ALT", repo_root=roots["EAWF"])
    assert resolution.key == "ALT"
    assert resolution.source is WorkspaceSource.SESSION


# ---- rung 6: refusals ------------------------------------------------------


def test_resolve_workspace_unregistered_root_refuses(
    roots: dict[str, Path], tmp_path: Path
) -> None:
    registry = _registry(repos=roots, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(registry, repo_root=stranger)
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED
    assert excinfo.value.candidates == ()


def test_resolve_workspace_does_not_scan_parent_directories(tmp_path: Path) -> None:
    """A child of a registered root must not inherit the parent's workspace."""
    parent = tmp_path / "parent"
    child = parent / "child"
    child.mkdir(parents=True)
    registry = _registry(repos={"EAWF": parent}, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(registry, repo_root=child)
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED


def test_resolve_workspace_registered_root_outside_any_workspace_refuses(
    roots: dict[str, Path],
) -> None:
    registry = _registry(repos=roots, workspaces={"MONO": ({"DEMO"}, "DEMO")})
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(registry, repo_root=roots["SOLO"])
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED


def test_resolve_workspace_without_root_or_key_refuses() -> None:
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(Registry())
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED
    assert "--workspace-key" in str(excinfo.value)


def test_resolve_workspace_empty_registry_refuses(roots: dict[str, Path]) -> None:
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(Registry(), repo_root=roots["EAWF"])
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED


# ---- rungs 1-3: declared keys ----------------------------------------------


def test_resolve_workspace_explicit_key_outranks_the_root(roots: dict[str, Path]) -> None:
    registry = _registry(
        repos=roots,
        workspaces={"MONO": ({"EAWF"}, "EAWF"), "SIDE": ({"SOLO"}, "SOLO")},
    )
    resolution = resolve_workspace(registry, explicit_key="SIDE", repo_root=roots["EAWF"])
    assert resolution.key == "SIDE"
    assert resolution.source is WorkspaceSource.EXPLICIT


def test_resolve_workspace_env_outranks_session(roots: dict[str, Path]) -> None:
    registry = _registry(
        repos=roots,
        workspaces={"MONO": ({"EAWF"}, "EAWF"), "SIDE": ({"SOLO"}, "SOLO")},
    )
    resolution = resolve_workspace(registry, env_key="SIDE", session_key="MONO")
    assert resolution.key == "SIDE"
    assert resolution.source is WorkspaceSource.ENVIRONMENT


def test_resolve_workspace_unknown_declared_key_refuses(roots: dict[str, Path]) -> None:
    registry = _registry(repos=roots, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    with pytest.raises(WorkspaceResolutionError) as excinfo:
        resolve_workspace(registry, explicit_key="GHOST", repo_root=roots["EAWF"])
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED
    assert "GHOST" in str(excinfo.value)


# ---- project_codes_at_root -------------------------------------------------


def test_project_codes_at_root_returns_every_code_at_the_path(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    shared.mkdir()
    registry = _registry(repos={"EAWF": shared, "DEMO": shared}, workspaces={})
    assert project_codes_at_root(registry, shared) == frozenset({"DEMO", "EAWF"})


def test_project_codes_at_root_normalises_relative_segments(roots: dict[str, Path]) -> None:
    registry = _registry(repos=roots, workspaces={})
    noisy = roots["EAWF"] / "." / ".." / roots["EAWF"].name
    assert project_codes_at_root(registry, noisy) == frozenset({"EAWF"})


def test_project_codes_at_root_empty_for_unregistered_path(tmp_path: Path) -> None:
    assert project_codes_at_root(Registry(), tmp_path) == frozenset()


# ---- membership algebra ----------------------------------------------------


def test_create_workspace_adds_the_record() -> None:
    record = WorkspaceRecord(
        key="MONO", member_project_codes=frozenset({"EAWF"}), home_project_code="EAWF"
    )
    updated = create_workspace(Registry(), record=record)
    assert updated.workspaces == {"MONO": record}


def test_create_workspace_refuses_a_duplicate_key() -> None:
    record = WorkspaceRecord(
        key="MONO", member_project_codes=frozenset({"EAWF"}), home_project_code="EAWF"
    )
    registry = create_workspace(Registry(), record=record)
    with pytest.raises(WorkspaceMutationError) as excinfo:
        create_workspace(registry, record=record)
    assert excinfo.value.code == WORKSPACE_ALREADY_REGISTERED


def test_update_membership_adds_and_bumps_revision() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    updated = update_membership(registry, key="MONO", add=("DEMO",))
    record = updated.workspaces["MONO"]
    assert record.member_project_codes == frozenset({"DEMO", "EAWF"})
    assert record.revision == 2
    assert registry.workspaces["MONO"].revision == 1


def test_update_membership_removes_a_member() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF", "DEMO"}, "EAWF")})
    updated = update_membership(registry, key="MONO", remove=("DEMO",))
    assert updated.workspaces["MONO"].member_project_codes == frozenset({"EAWF"})


def test_update_membership_refuses_removing_the_home_repo() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF", "DEMO"}, "EAWF")})
    with pytest.raises(ValidationError):
        update_membership(registry, key="MONO", remove=("EAWF",))


def test_update_membership_refuses_emptying_the_membership() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    with pytest.raises(ValidationError):
        update_membership(registry, key="MONO", remove=("EAWF",))


def test_update_membership_refuses_a_stale_revision() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    with pytest.raises(WorkspaceMutationError) as excinfo:
        update_membership(registry, key="MONO", add=("DEMO",), expected_revision=99)
    assert excinfo.value.code == WORKSPACE_REVISION_CONFLICT


def test_update_membership_accepts_the_current_revision() -> None:
    registry = _registry(repos={}, workspaces={"MONO": ({"EAWF"}, "EAWF")})
    updated = update_membership(registry, key="MONO", add=("DEMO",), expected_revision=1)
    assert updated.workspaces["MONO"].revision == 2


def test_update_membership_refuses_an_unregistered_key() -> None:
    with pytest.raises(WorkspaceMutationError) as excinfo:
        update_membership(Registry(), key="GHOST", add=("DEMO",))
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED


def test_get_workspace_refuses_an_unregistered_key() -> None:
    with pytest.raises(WorkspaceMutationError) as excinfo:
        get_workspace(Registry(), "GHOST")
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED


def test_list_workspaces_is_empty_for_a_fresh_registry() -> None:
    assert list_workspaces(Registry()) == []


def test_list_workspaces_is_ordered_by_key() -> None:
    registry = _registry(
        repos={},
        workspaces={
            "ZED": ({"EAWF"}, "EAWF"),
            "ABC": ({"EAWF"}, "EAWF"),
            "MID": ({"EAWF"}, "EAWF"),
        },
    )
    assert [record.key for record in list_workspaces(registry)] == ["ABC", "MID", "ZED"]
