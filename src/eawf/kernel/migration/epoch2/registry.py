"""The versioned rule registry the migration manifest publishes.

Every rule the importer applies appears here exactly once, pinned to a
digest over its own table. The manifest carries this list, so a reader of
an imported record can name the rule revision that produced it without
re-reading the importer source.

Rules whose statement lives here but whose row-level application belongs
to another importer stage still carry a version row: the statement is
what the digest pins, and a stage that changes the statement must move
the digest with it.
"""

from __future__ import annotations

import logging
from typing import Any

from eawf.kernel.migration.epoch2.allowlist import LegacySymbolAllowlist
from eawf.kernel.migration.epoch2.backlog import BACKLOG_CLASSIFIER_RULE, obsolescence_rule
from eawf.kernel.migration.epoch2.criteria import criteria_rule_payload
from eawf.kernel.migration.epoch2.dispositions import disposition_rule_payload
from eawf.kernel.migration.epoch2.rows import row_contract_payload
from eawf.kernel.migration.epoch2.rules import (
    MappingRuleVersion,
    build_rule_version,
    index_rule_versions,
)
from eawf.kernel.migration.epoch2.status_map import status_rule_payload

logger = logging.getLogger(__name__)


def totality_rule_payload() -> dict[str, Any]:
    """Return the digestable form of every table that makes the import total."""
    return {
        "dispositions": disposition_rule_payload(),
        "status": status_rule_payload(),
        "criteria": criteria_rule_payload(),
        "rows": row_contract_payload(),
    }


TOTALITY_RULE: MappingRuleVersion = build_rule_version(
    rule_id="DOM-004",
    source_kind="document",
    title="Migration is total: every collection, status and required field has a declared rule",
    payload=totality_rule_payload(),
)

SESSION_RUN_SPLIT_RULE: MappingRuleVersion = build_rule_version(
    rule_id="DOM-041",
    source_kind="agent_sessions",
    title="A Run is minted only from a resolving claim entry or a wave attempt entry",
    payload={
        "mints_run_from": ["resolving_claimed_wave_id", "wave_attempt_entry"],
        "default_run_status": "TERMINAL_UNCLASSIFIED",
        "succeeded_requires": ["bound_role_report", "exit_status_zero"],
    },
)

CLAIM_SESSION_REF_RULE: MappingRuleVersion = build_rule_version(
    rule_id="DOM-043",
    source_kind="waves",
    title="A legacy claim-session id is a string on the envelope and never a canonical reference",
    payload={
        "legacy_field": "legacy_refs.claim_session_id",
        "resolving_extra_field": "legacy_session_ref",
        "empty_string_imports_as": "absent_field",
        "canonical_reference": False,
    },
)

AUDIT_UNION_RULE: MappingRuleVersion = build_rule_version(
    rule_id="DOM-045",
    source_kind="audits",
    title="The audit population is the union of the audits collection and the audit ledger",
    payload={
        "union_inputs": ["audits_document_collection", "audit_ledger"],
        "unresolved_imports_as": "legacy_refs.audit_id",
        "manifest_fields": [
            "document_rows",
            "ledger_rows",
            "union_rows",
            "store_only_imported",
            "refs_resolving_in_neither",
        ],
        "shrinking_union_fails_plan": True,
    },
)


def mapping_rule_versions(allowlist: LegacySymbolAllowlist) -> tuple[MappingRuleVersion, ...]:
    """Return every importer rule version, in rule-id order.

    Args:
        allowlist: The shared allowed-legacy-symbol allowlist, which the
            obsolescence rule digests so the two cannot drift apart.

    Returns:
        The rule versions, sorted by rule id.

    Raises:
        ValueError: When the allowlist's deleted set is malformed.
    """
    versions = (
        TOTALITY_RULE,
        obsolescence_rule(allowlist),
        SESSION_RUN_SPLIT_RULE,
        CLAIM_SESSION_REF_RULE,
        BACKLOG_CLASSIFIER_RULE,
        AUDIT_UNION_RULE,
    )
    return tuple(sorted(versions, key=lambda version: version.rule_id))


def mapping_rule_index(allowlist: LegacySymbolAllowlist) -> dict[str, MappingRuleVersion]:
    """Return the rule versions indexed by rule id.

    Args:
        allowlist: The shared allowed-legacy-symbol allowlist.

    Returns:
        A mapping from rule id to rule version.

    Raises:
        ValueError: When two rules share a rule id.
    """
    return index_rule_versions(mapping_rule_versions(allowlist))
