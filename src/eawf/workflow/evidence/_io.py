"""Internal I/O helpers shared by the evidence-area mutators.

These helpers live behind a leading-underscore module name because they are
part of the implementation contract for ``evidence/<area>.py`` only — the CLI
layer never imports them directly. They centralise:

* Reading the promoted measured contracts out of an epoch-2 tree's
  selected generation into a :class:`ContractView`.
* Computing a stable args-hash for the event envelope.
* Building event envelopes with consistent shape.

The append helper and canonical store-path resolver live in
``eawf.kernel.store.append`` and ``eawf.kernel.store.paths`` respectively. This module
re-exports them so existing callers keep working.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import Artifact
from eawf.kernel.state.urn import build as build_urn
from eawf.kernel.store.append import append_envelope as append_jsonl
from eawf.kernel.store.compaction import document_rows
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import store_paths
from eawf.kernel.store.tiers import Epoch2Collection

logger = logging.getLogger(__name__)

#: The status of the evidence-ledger line that files one spike report's
#: promoted contracts, under ``record_key`` the report id.
MEASURED_CONTRACT_STATUS: Final = "measured_contract"

__all__ = [
    "MEASURED_CONTRACT_STATUS",
    "ContractView",
    "append_jsonl",
    "artifact_urn",
    "event_envelope",
    "generation_contract_view",
    "kind_envelope",
    "store_paths",
]


@dataclass
class ContractView:
    """The measured contracts one repository has promoted, held in memory only.

    Attributes:
        project_code: The repository's project code, or ``None`` when the
            document holds no project row.
        artifacts: Promoted contracts keyed by contract id.
        updated_at: When the view last changed.
    """

    project_code: str | None
    artifacts: dict[str, Artifact]
    updated_at: datetime


def _promoted_contracts(*, artifact_ledger: Path, evidence_ledger: Path) -> dict[str, Artifact]:
    """Return every measured contract a generation holds, keyed by contract id.

    Two sources hold one: the artifact ledger carries the contract rows the
    cutover imported from the v1 document, and the evidence ledger carries
    every promotion filed since, one line per spike report.

    Raises:
        LedgerTornTailError: A ledger ends mid-line.
        pydantic.ValidationError: A line, or a contract row it carries, does
            not satisfy its model.
        KeyError: A promotion line carries no ``contracts``.
    """
    from eawf.workflow.evidence.measured_contract import CONTRACT_BODY_URI

    contracts: dict[str, Artifact] = {}
    for record in effective_records(read_ledger_records(artifact_ledger)):
        imported = record.payload.get("payload")
        if isinstance(imported, Mapping) and imported.get("uri") == CONTRACT_BODY_URI:
            contract = Artifact.model_validate(imported)
            contracts[contract.id] = contract
    for record in effective_records(read_ledger_records(evidence_ledger)):
        if record.status != MEASURED_CONTRACT_STATUS:
            continue
        for row in record.payload["contracts"]:
            contract = Artifact.model_validate(row)
            contracts[contract.id] = contract
    return contracts


def generation_contract_view(
    document: Mapping[str, Any], *, artifact_ledger: Path, evidence_ledger: Path
) -> ContractView:
    """Return a generation's promoted contracts and its project code.

    The code is the key of the document's one project row, which a
    migrated and a native tree spell alike.

    Args:
        document: The selected generation's document.
        artifact_ledger: The generation's ``ledger/artifact.jsonl``.
        evidence_ledger: The generation's ``ledger/evidence.jsonl``.

    Returns:
        An in-memory view that is never written anywhere. Its
        ``project_code`` is ``None`` when the document holds no project row;
        a later promotion of a contract id wins over an earlier one.

    Raises:
        ValueError: The document holds something other than an object under
            the project collection.
        LedgerTornTailError: A ledger ends mid-line.
        pydantic.ValidationError: A ledger line, or a contract row it
            carries, does not satisfy its model.
        KeyError: A promotion line carries no ``contracts``.
    """
    code = min(document_rows(dict(document), Epoch2Collection.PROJECT), default=None)
    return ContractView(
        project_code=code,
        artifacts=_promoted_contracts(
            artifact_ledger=artifact_ledger, evidence_ledger=evidence_ledger
        ),
        updated_at=datetime.now(UTC),
    )


def args_hash(args: dict[str, Any]) -> str:
    """Return a stable SHA-256 hex digest of *args* for event envelopes.

    Keys are sorted before hashing so equivalent calls hash identically. The
    digest goes into :class:`~eawf.kernel.store.kinds.event.EventPayload.args_hash`.
    """
    raw = json.dumps(args, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def event_envelope(
    *,
    event_id: str,
    scope_id: str | None,
    event_type: str,
    actor: str,
    command: str,
    args: dict[str, Any],
    summary: str,
    status: str = "ok",
    message: str = "",
    artifact_ids: list[str] | None = None,
) -> Envelope:
    """Construct an :class:`Envelope` for the events.jsonl stream."""
    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=event_id,
        kind=StoreKind.EVENT,
        scope_id=scope_id,
        created_at=now,
        updated_at=None,
        summary=summary,
        payload={
            "timestamp": now.isoformat(),
            "event_type": event_type,
            "actor": actor,
            "command": command,
            "args_hash": args_hash(args),
            "before_state_version": None,
            "after_state_version": None,
            "status": status,
            "message": message,
        },
        blob_refs=[],
        artifact_ids=artifact_ids or [],
    )


def kind_envelope(
    *,
    record_id: str,
    kind: StoreKind,
    scope_id: str | None,
    summary: str,
    payload: dict[str, Any],
    artifact_ids: list[str] | None = None,
    blob_refs: list[str] | None = None,
) -> Envelope:
    """Construct a payload-bearing :class:`Envelope` (non-event store kinds)."""
    now = datetime.now(UTC)
    return Envelope(
        schema_version="1.0",
        id=record_id,
        kind=kind,
        scope_id=scope_id,
        created_at=now,
        updated_at=now,
        summary=summary,
        payload=payload,
        blob_refs=blob_refs or [],
        artifact_ids=artifact_ids or [],
    )


def artifact_urn(scope_id: str, artifact_id: str) -> str:
    """Build a canonical ``urn:eawf:v1:artifact:<scope_id>/<id>`` URN."""
    return build_urn("artifact", owner=scope_id, id=artifact_id)
