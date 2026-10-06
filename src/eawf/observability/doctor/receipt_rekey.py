"""Plan the superseding rows that re-key filed proof receipts.

A filed proof receipt stores the freshness key it was proved under, and
the receipt is refused on read unless that key is the digest of the
inputs it records. The key derivation and the URN spelling of those
inputs can change after a proof is filed, so a ledger line can hold a key
its own inputs no longer produce. The ledger is append-only, so the repair
supersedes each such line with the same proof carrying the key its inputs
produce, filed under the record key that key names.
"""

from __future__ import annotations

import copy
import hashlib
from datetime import datetime
from pathlib import Path
from typing import Final

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.delivery.receipts import ProofFreshnessKey
from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.store.ledger import (
    LedgerRecord,
    content_digest,
    line_digest,
    split_ledger_lines,
)
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods.delivery_completion import (
    PROOF_KEY_PREFIX,
    PROOF_PAYLOAD_KIND,
    FiledProof,
)

#: The action that appends a re-keyed row for each filed proof whose stored key is stale.
RECEIPT_REKEY_ACTION: Final = "ledger.receipt-rekey"

_FRESHNESS_KEY: Final = "freshness_key"


class ReceiptRekey(BaseModel):
    """One superseding row planned for one effective filed-proof line.

    Attributes:
        line: The 1-based line the row supersedes.
        supersedes: The digest of that line.
        record_key: The record key the re-keyed row is filed under.
        freshness_key: The digest of the line's recorded freshness inputs.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    line: int = Field(ge=1)
    supersedes: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    record_key: str = Field(pattern=rf"^{PROOF_KEY_PREFIX}[0-9a-f]{{16}}-\S+$")
    freshness_key: str = Field(pattern=r"^[0-9a-f]{64}$")


def selected_receipt_ledger(root: Path) -> Path | None:
    """Return the selected generation's receipt ledger, or ``None`` on an epoch-1 tree.

    Args:
        root: The repository's working directory.

    Returns:
        ``<generation>/ledger/receipt.jsonl``, whether or not it exists.
    """
    authority = resolve_authority(root / ".ea")
    if authority.epoch != 2 or authority.target is None or authority.generation_id is None:
        return None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    return ledger_path(document, Epoch2Collection.RECEIPT)


def _rekeyed_payload(record: LedgerRecord, key: str) -> dict[str, object]:
    """Return *record*'s payload with the receipt's freshness key set to *key*."""
    payload = copy.deepcopy(record.payload)
    payload["receipt"][_FRESHNESS_KEY] = key
    return payload


def plan_receipt_rekeys(ledger: Path) -> tuple[ReceiptRekey, ...]:
    """Plan one re-keyed row per effective filed proof whose stored key is stale.

    Args:
        ledger: The receipt ledger to read; a missing file plans nothing.

    Returns:
        The rows to append, in ledger order.

    Raises:
        LedgerTornTailError: The ledger ends mid-line.
        ValidationError: A line is not a ledger record, or a filed proof
            does not validate even under the key its inputs produce.
    """
    if not ledger.is_file():
        return ()
    lines = split_ledger_lines(ledger.read_bytes())
    records = [LedgerRecord.model_validate_json(text) for text in lines]
    superseded = {record.supersedes for record in records if record.supersedes is not None}
    rows: list[ReceiptRekey] = []
    for number, (text, record) in enumerate(zip(lines, records, strict=True), start=1):
        digest = line_digest(text)
        if digest in superseded or record.payload.get("payload_kind") != PROOF_PAYLOAD_KIND:
            continue
        receipt = record.payload.get("receipt")
        if not isinstance(receipt, dict):
            receipt = {}
        key = ProofFreshnessKey.model_validate(receipt.get("freshness")).digest()
        if receipt.get(_FRESHNESS_KEY) == key:
            continue
        proof = FiledProof.model_validate(_rekeyed_payload(record, key))
        rows.append(
            ReceiptRekey(
                line=number,
                supersedes=digest,
                record_key=f"{PROOF_KEY_PREFIX}{key[:16]}-{proof.task_ref.entity_key}",
                freshness_key=key,
            )
        )
    return tuple(rows)


def receipt_rekey_preview(ledger: Path, rows: tuple[ReceiptRekey, ...]) -> str:
    """Return the preview digest of *rows* over the ledger bytes they were planned from.

    Args:
        ledger: The receipt ledger the rows were planned from.
        rows: The planned rows.

    Returns:
        A ``sha256:`` digest that changes when the ledger or a planned row changes.
    """
    basis = {
        "rows": [row.model_dump(mode="json") for row in rows],
        "ledger": content_digest(ledger.read_bytes()) if ledger.is_file() else None,
    }
    return f"sha256:{hashlib.sha256(orjson.dumps(basis, option=orjson.OPT_SORT_KEYS)).hexdigest()}"


def rekeyed_record(ledger: Path, row: ReceiptRekey, *, recorded_at: datetime) -> LedgerRecord:
    """Build the record that supersedes *row*'s line under its recomputed keys.

    Args:
        ledger: The receipt ledger the row was planned from.
        row: The planned row.
        recorded_at: The UTC instant the new row records.

    Returns:
        The superseded record with the receipt's freshness key and the
        record key recomputed; everything else is kept as filed.

    Raises:
        ValidationError: The line is not a ledger record, or the re-keyed
            proof does not validate strictly.
    """
    text = split_ledger_lines(ledger.read_bytes())[row.line - 1]
    record = LedgerRecord.model_validate_json(text)
    payload = _rekeyed_payload(record, row.freshness_key)
    FiledProof.model_validate(payload)
    return LedgerRecord(
        collection=record.collection,
        record_key=row.record_key,
        status=record.status,
        recorded_at=recorded_at,
        supersedes=row.supersedes,
        payload=payload,
    )


__all__ = [
    "RECEIPT_REKEY_ACTION",
    "ReceiptRekey",
    "plan_receipt_rekeys",
    "receipt_rekey_preview",
    "rekeyed_record",
    "selected_receipt_ledger",
]
