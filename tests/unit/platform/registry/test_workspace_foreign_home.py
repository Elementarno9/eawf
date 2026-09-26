"""Tests for the foreign-home refusal when a workspace is created.

A membership edit already refuses a repository that anchors a different
workspace; creating a workspace must refuse the same member, or a new
workspace could claim another's home root on the way in and let a
mutation against it reach a root it does not own.
"""

from __future__ import annotations

import orjson
import pytest

from eawf.platform.registry import (
    CROSS_WORKSPACE_MUTATION_FORBIDDEN,
    Registry,
    RegistryRepoEntry,
    WorkspaceMutationError,
    WorkspaceRecord,
    create_workspace,
)

pytestmark = pytest.mark.unit


def _registry() -> Registry:
    """One workspace anchored on ``SOLO`` that also shares ``DEMO`` as a plain member."""
    return Registry(
        repos={
            code: RegistryRepoEntry(code=code, path=f"/repos/{code.lower()}")
            for code in ("EAWF", "SOLO", "DEMO")
        },
        workspaces={
            "SIDE": WorkspaceRecord(
                key="SIDE",
                member_project_codes=frozenset({"SOLO", "DEMO"}),
                home_project_code="SOLO",
            ),
        },
    )


def _bytes_of(registry: Registry) -> bytes:
    return orjson.dumps(registry.model_dump(mode="json"), option=orjson.OPT_SORT_KEYS)


@pytest.mark.parametrize(
    ("members", "home"),
    [({"EAWF", "SOLO"}, "EAWF"), ({"SOLO"}, "SOLO")],
    ids=["foreign-member", "foreign-home"],
)
def test_create_workspace_refuses_a_member_anchoring_another_workspace(
    members: set[str], home: str
) -> None:
    registry = _registry()
    before = _bytes_of(registry)
    record = WorkspaceRecord(
        key="MONO", member_project_codes=frozenset(members), home_project_code=home
    )
    with pytest.raises(WorkspaceMutationError) as excinfo:
        create_workspace(registry, record=record)
    assert excinfo.value.code == CROSS_WORKSPACE_MUTATION_FORBIDDEN
    assert "'SOLO' anchors workspace 'SIDE'" in str(excinfo.value)
    assert _bytes_of(registry) == before


def test_create_workspace_allows_a_plain_member_shared_with_another_workspace() -> None:
    record = WorkspaceRecord(
        key="MONO", member_project_codes=frozenset({"EAWF", "DEMO"}), home_project_code="EAWF"
    )
    created = create_workspace(_registry(), record=record)
    assert created.workspaces["MONO"] == record
    assert set(created.workspaces) == {"SIDE", "MONO"}
