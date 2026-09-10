"""The legacy origin every imported epoch-2 record carries.

Four importer stages -- the lifecycle mappers, the measurement re-point,
the legacy envelopes and the ledger imports -- each write a record that
has to name the source row it came from. They build the same origin from
the same four facts, so the construction lives here once: a stage that
forgets the schema version or digests a different value than its
neighbour produces records whose provenance cannot be compared.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from eawf.kernel.migration.epoch2.rules import rule_digest
from eawf.kernel.state.epoch2.values import EntityOrigin, MappingBasis, OriginConfidence

logger = logging.getLogger(__name__)

#: The mapping basis every imported record carries. Every conversion in
#: this package is a table lookup, never an observation or a judgement.
MECHANICAL_BASIS: MappingBasis = "mechanical"


def source_digest(row: Mapping[str, Any]) -> str:
    """Return the prefixed sha256 digest of one source row.

    Args:
        row: The source row, read only.

    Returns:
        The digest in ``sha256:<hex>`` form, which is what an epoch-2
        origin's ``source_digest`` field admits.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
    """
    return f"sha256:{rule_digest(row)}"


def build_legacy_origin(
    *,
    source_kind: str,
    source_id: str,
    row: Mapping[str, Any],
    source_schema_version: str,
    confidence: OriginConfidence,
) -> EntityOrigin:
    """Build the legacy origin one imported record carries.

    Args:
        source_kind: The epoch-1 collection the row came from.
        source_id: The row's own key in that collection.
        row: The source row, digested so the import is reproducible.
        source_schema_version: The epoch-1 schema version of the document
            the row was read from.
        confidence: How much the mapping of this row is trusted.

    Returns:
        The origin, always ``kind='legacy'``.

    Raises:
        TypeError: When the row holds a value ``json`` cannot encode.
        ValidationError: When a field violates the origin contract.
    """
    return EntityOrigin(
        kind="legacy",
        source_schema_version=source_schema_version,
        source_kind=source_kind,
        source_id=source_id,
        source_digest=source_digest(row),
        mapping_basis=MECHANICAL_BASIS,
        confidence=confidence,
    )
