"""Epoch-2 identity: the URN grammar, the public keys, and the alias index.

Nothing in the epoch-2 model can be addressed until three questions have
one answer each: how a record is named (:mod:`eawf.kernel.identity.keys`),
how it is located (:mod:`eawf.kernel.identity.urn`), and how an epoch-1
identifier still reaches it after the cutover
(:mod:`eawf.kernel.identity.alias`). Rejections are typed in
:mod:`eawf.kernel.identity.errors` so a caller branches on a code rather
than on a message.

Cross-entity ordering is deliberately not here: it belongs to the
workspace, not to any entity's identity, and lives in
:mod:`eawf.kernel.state.canonical_sequence`.
"""

from __future__ import annotations

from eawf.kernel.identity.alias import (
    LegacyAliasEntry,
    LegacyAliasIndex,
    LegacyAliasKey,
    check_alias_invariants,
)
from eawf.kernel.identity.errors import IdentityError, IdentityRejection
from eawf.kernel.identity.keys import (
    ENTITY_SCOPES,
    KEY_ORDINAL_FAMILIES,
    RESERVED_REPOSITORY_SLOT,
    EntityKeyAllocator,
    EntityKeyTransaction,
    EntityKind,
    EntityScope,
    format_entity_key,
    project_code_of,
    validate_entity_key,
    validate_symbol_key,
)
from eawf.kernel.identity.urn import (
    RUNG_MAX,
    RUNG_MIN,
    URN_SCHEME,
    QualifiedUrn,
    format_qualified_urn,
    parse_qualified_urn,
)

__all__ = [
    "ENTITY_SCOPES",
    "KEY_ORDINAL_FAMILIES",
    "RESERVED_REPOSITORY_SLOT",
    "RUNG_MAX",
    "RUNG_MIN",
    "URN_SCHEME",
    "EntityKeyAllocator",
    "EntityKeyTransaction",
    "EntityKind",
    "EntityScope",
    "IdentityError",
    "IdentityRejection",
    "LegacyAliasEntry",
    "LegacyAliasIndex",
    "LegacyAliasKey",
    "QualifiedUrn",
    "check_alias_invariants",
    "format_entity_key",
    "format_qualified_urn",
    "parse_qualified_urn",
    "project_code_of",
    "validate_entity_key",
    "validate_symbol_key",
]
