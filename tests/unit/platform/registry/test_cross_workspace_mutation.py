"""Tests for the cross-workspace mutation refusal on membership edits.

A workspace's ``home_project_code`` is the root a mutation against that
workspace ultimately reaches. Sharing a plain member across workspaces is
ordinary (it is exactly what makes resolution ambiguous, per
``test_workspace_resolution.py``), but letting a second workspace claim
another workspace's home would let a mutation issued against the second
workspace reach a root it does not own. ``update_membership`` refuses
that case before it ever builds a new registry, so a refused call leaves
the input registry's bytes untouched.
"""

from __future__ import annotations

import orjson
import pytest

from eawf.platform.registry import (
    CROSS_WORKSPACE_MUTATION_FORBIDDEN,
    WORKSPACE_NOT_REGISTERED,
    Registry,
    RegistryRepoEntry,
    WorkspaceMutationError,
    WorkspaceRecord,
    update_membership,
)

pytestmark = pytest.mark.unit


def _registry() -> Registry:
    """Two workspaces, each anchored on its own home repo.

    ``SIDE`` also carries ``DEMO`` as a plain (non-home) member, so tests
    can tell "anchors a different workspace" apart from "is merely a
    member of a different workspace" - the latter is ordinary sharing.
    """
    return Registry(
        repos={
            "EAWF": RegistryRepoEntry(code="EAWF", path="/repos/eawf"),
            "SOLO": RegistryRepoEntry(code="SOLO", path="/repos/solo"),
            "DEMO": RegistryRepoEntry(code="DEMO", path="/repos/demo"),
        },
        workspaces={
            "MONO": WorkspaceRecord(
                key="MONO",
                member_project_codes=frozenset({"EAWF"}),
                home_project_code="EAWF",
            ),
            "SIDE": WorkspaceRecord(
                key="SIDE",
                member_project_codes=frozenset({"SOLO", "DEMO"}),
                home_project_code="SOLO",
            ),
        },
    )


def _bytes_of(registry: Registry) -> bytes:
    """Canonical byte form of *registry* for a before/after comparison."""
    return orjson.dumps(registry.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)


def test_update_membership_refuses_a_repository_anchoring_another_workspace() -> None:
    registry = _registry()
    before = _bytes_of(registry)
    with pytest.raises(WorkspaceMutationError) as excinfo:
        update_membership(registry, key="MONO", add=("SOLO",))
    assert excinfo.value.code == CROSS_WORKSPACE_MUTATION_FORBIDDEN
    assert "SOLO" in str(excinfo.value)
    assert "SIDE" in str(excinfo.value)
    assert _bytes_of(registry) == before


def test_update_membership_refuses_when_one_of_several_added_codes_is_foreign() -> None:
    registry = _registry()
    before = _bytes_of(registry)
    with pytest.raises(WorkspaceMutationError) as excinfo:
        update_membership(registry, key="MONO", add=("DEMO", "SOLO"))
    assert excinfo.value.code == CROSS_WORKSPACE_MUTATION_FORBIDDEN
    assert _bytes_of(registry) == before


def test_update_membership_allows_a_plain_member_shared_with_another_workspace() -> None:
    """A repository that is merely a *member* elsewhere is not a foreign root."""
    registry = _registry()
    updated = update_membership(registry, key="MONO", add=("DEMO",))
    assert updated.workspaces["MONO"].member_project_codes == frozenset({"DEMO", "EAWF"})


def test_update_membership_allows_re_adding_its_own_home() -> None:
    """Naming the workspace's own home is a no-op, never a foreign-root refusal."""
    registry = _registry()
    updated = update_membership(registry, key="MONO", add=("EAWF",))
    assert updated.workspaces["MONO"].member_project_codes == frozenset({"EAWF"})


def test_update_membership_refuses_an_unregistered_key_before_foreign_home_check() -> None:
    registry = _registry()
    with pytest.raises(WorkspaceMutationError) as excinfo:
        update_membership(registry, key="GHOST", add=("SOLO",))
    assert excinfo.value.code == WORKSPACE_NOT_REGISTERED
