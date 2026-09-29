"""AUTH-049: the workspace key is its own slot, and the registry resolves it first.

Every persisted identifier carries a workspace key, and the reserved
repository slot ``_`` is never reused as a workspace slot: the two
concepts keep distinct symbols in the identity grammar. The importer's
apply resolves the addressing workspace in the registry before it mints a
single URN; that ordering is proved end to end by
``test_an_unregistered_workspace_refuses_before_the_first_urn_is_minted``
in ``tests/integration/kernel/migration/test_epoch2_apply_guards.py``.
"""

from __future__ import annotations

import pytest

from eawf.kernel.identity import (
    RESERVED_REPOSITORY_SLOT,
    EntityKind,
    IdentityError,
    parse_qualified_urn,
)
from eawf.platform.registry import Registry, WorkspaceMutationError, get_workspace


def test_auth_049_the_reserved_slot_is_not_a_workspace_key() -> None:
    with pytest.raises(IdentityError):
        parse_qualified_urn(f"eawf://{RESERVED_REPOSITORY_SLOT}/PRJ-EAWF/REP-EAWF/task/EAWF-0001")


def test_auth_049_every_persisted_identifier_carries_its_workspace_key() -> None:
    urn = parse_qualified_urn("eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0001")
    assert urn.workspace_key == "WSP-MAIN"
    assert urn.repository_key == "REP-EAWF"


def test_auth_049_a_workspace_level_record_takes_the_reserved_repository_slot() -> None:
    urn = parse_qualified_urn(
        f"eawf://WSP-MAIN/PRJ-EAWF/{RESERVED_REPOSITORY_SLOT}/release/REL-0.7.0rc1"
    )
    assert urn.kind is EntityKind.RELEASE
    assert urn.repository_key is None
    assert urn.workspace_key == "WSP-MAIN"


def test_auth_049_a_repository_level_record_refuses_the_reserved_slot() -> None:
    with pytest.raises(IdentityError, match="needs a repository key"):
        parse_qualified_urn(f"eawf://WSP-MAIN/PRJ-EAWF/{RESERVED_REPOSITORY_SLOT}/task/EAWF-0001")


def test_auth_049_an_unregistered_workspace_does_not_resolve() -> None:
    with pytest.raises(WorkspaceMutationError, match="is not registered"):
        get_workspace(Registry(), "WSP-MAIN")
