"""Versioned mapping rules and the digest that pins each one.

The manifest contract requires every mapping rule to publish an
implementation version and a digest. The digest is taken over the rule's
own table -- the status map, the vocabulary, the arm order -- so editing
a table changes the digest and a manifest produced under the old table
can never be mistaken for one produced under the new.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# Bumped whenever the behaviour of any rule in this package changes in a
# way that is not already visible in a rule payload (a new arm ordering,
# a changed tie-break). A payload edit alone moves only that rule digest.
RULE_IMPLEMENTATION_VERSION = "epoch2-1"


class StrictMigrationModel(BaseModel):
    """Base model for every importer-rule model in this package."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def canonical_json(payload: Any) -> bytes:
    """Serialize ``payload`` to the one byte string the digest is taken over.

    Args:
        payload: Any JSON-serializable rule table.

    Returns:
        UTF-8 bytes with sorted keys and no insignificant whitespace, so
        two structurally equal tables always digest identically.

    Raises:
        TypeError: When ``payload`` holds a value ``json`` cannot encode.
    """
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def rule_digest(payload: Any) -> str:
    """Return the sha256 hex digest of ``payload`` in canonical form.

    Args:
        payload: Any JSON-serializable rule table.

    Returns:
        A 64-character lowercase hex digest.

    Raises:
        TypeError: When ``payload`` holds a value ``json`` cannot encode.
    """
    return hashlib.sha256(canonical_json(payload)).hexdigest()


class MappingRuleVersion(StrictMigrationModel):
    """One importer rule, pinned to the table revision that defines it."""

    rule_id: Annotated[str, Field(pattern=r"^[A-Z]{2,8}-\d{2,}$")]
    source_kind: Annotated[str, Field(min_length=1, max_length=64)]
    title: Annotated[str, Field(min_length=1, max_length=200)]
    implementation_version: Annotated[str, Field(min_length=1, max_length=32)]
    rule_digest: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


def build_rule_version(
    *,
    rule_id: str,
    source_kind: str,
    title: str,
    payload: Any,
) -> MappingRuleVersion:
    """Build a :class:`MappingRuleVersion` whose digest covers ``payload``.

    Args:
        rule_id: The packet identifier of the rule, such as ``DOM-044``.
        source_kind: The epoch-1 collection or model the rule reads.
        title: A one-line statement of what the rule decides.
        payload: The rule's own table, digested to pin the revision.

    Returns:
        The pinned rule version.

    Raises:
        TypeError: When ``payload`` holds a value ``json`` cannot encode.
        ValidationError: When a field violates the model contract.
    """
    return MappingRuleVersion(
        rule_id=rule_id,
        source_kind=source_kind,
        title=title,
        implementation_version=RULE_IMPLEMENTATION_VERSION,
        rule_digest=rule_digest(payload),
    )


def index_rule_versions(versions: tuple[MappingRuleVersion, ...]) -> dict[str, MappingRuleVersion]:
    """Index ``versions`` by rule id, refusing a duplicate id.

    Args:
        versions: The rule versions to index.

    Returns:
        A mapping from rule id to rule version.

    Raises:
        ValueError: When two versions share a ``rule_id``, which would
            make the manifest's rule list ambiguous.
    """
    index: dict[str, MappingRuleVersion] = {}
    for version in versions:
        if version.rule_id in index:
            raise ValueError(f"duplicate mapping rule id {version.rule_id!r}")
        index[version.rule_id] = version
    return index
