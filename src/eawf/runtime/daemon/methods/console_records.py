"""The records a console route draws beside its projection, read back from their stores.

The console routes draw records no route projection carries, because each is filed as
a ledger line, a store envelope or a machine-local file rather than a document row, or
is no eawf record at all:

``projection.health.verdicts``
    the newest conformance verdict of every runtime tuple, from the conformance store
    the ``conformance.*`` verbs append to; drawn by the health route.
``projection.git.pr.generations``
    one Batch's integration generations, oldest first, from the Batch ledger the
    delivery verbs append to; drawn by the Git surface.
``projection.git.pr.repository``
    the branch the tree has checked out and the pull request open for it, from ``git``
    and the host's ``gh`` CLI; drawn by the Git surface.
``projection.merge.conflict.frames``
    the conflict frames a blocked integration of one Batch left, from the same ledger;
    drawn by the conflict card.
``projection.crash.recovery.boot``
    what the daemon's last start repaired and what that cost, from the record the start
    wrote beside its WAL; drawn by the Recovery frame.
``projection.receipt.proofs``
    the proof receipt filed under one receipt key, from the receipt ledger the proof
    verb appends to; drawn by the receipt card.
``projection.target.resolve``
    whether anything was ever written under a key a route was opened onto and does not
    hold; a key nothing names opens the resolution card as missing.
``projection.history.changes``
    the change feed, newest first and a page at a time: one record's changes, or the
    whole tree's recent ones; drawn by History and its diff.

Every verb reads and writes nothing else, and answers an empty run for a subject nothing
was filed for: an absence the route then states, never an error.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from eawf.kernel.delivery.integration import IntegrationConflict, IntegrationGeneration
from eawf.kernel.delivery.receipts import ProofReceipt
from eawf.kernel.runtime.boot_recovery import BootRecovery
from eawf.kernel.runtime.certification import CertificationFailureCode
from eawf.kernel.state.epoch2.urns import BatchUrn
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.changes import MAX_PAGE, ChangePage, read_change_page
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import LEDGER_COLLECTIONS, Epoch2Collection
from eawf.observability.doctor.models import CheckResult
from eawf.observability.doctor.runtime_health import runtime_tuple_verdicts
from eawf.runtime.daemon.epoch2_recovery import read_boot_recovery
from eawf.runtime.daemon.methods import MethodContext, register
from eawf.runtime.daemon.methods.delivery import read_conflict_frames, read_generation_ledger
from eawf.runtime.daemon.methods.delivery_completion import PROOF_PAYLOAD_KIND, FiledProof
from eawf.runtime.daemon.methods.projection import document_path
from eawf.runtime.daemon.native_guard import native_params, require_native_call
from eawf.runtime.vcs.repository_read import read_repository

logger = logging.getLogger(__name__)

#: Read the newest conformance verdict of every runtime tuple the tree has certified.
HEALTH_VERDICTS_READ_METHOD: Final = "projection.health.verdicts"

#: Read one Batch's integration generations, oldest first.
GENERATIONS_READ_METHOD: Final = "projection.git.pr.generations"

#: Read the branch the tree has checked out and the pull request open for it.
REPOSITORY_READ_METHOD: Final = "projection.git.pr.repository"

#: Read the conflict frames one Batch's blocked integrations left, in record order.
CONFLICT_FRAMES_READ_METHOD: Final = "projection.merge.conflict.frames"

#: Read what the daemon's last start repaired and what that cost.
BOOT_RECOVERY_READ_METHOD: Final = "projection.crash.recovery.boot"

#: Read the proof receipt filed under one receipt key.
PROOF_RECEIPTS_READ_METHOD: Final = "projection.receipt.proofs"

#: Read whether anything was ever written under one key.
TARGET_RESOLVE_METHOD: Final = "projection.target.resolve"

#: Read one page of the change feed, newest first.
HISTORY_CHANGES_READ_METHOD: Final = "projection.history.changes"

#: How many records a page of the change feed holds when the read names no limit.
DEFAULT_HISTORY_PAGE: Final = 50

#: The ending of a key nothing in the tree was ever written under.
MISSING_ENDING: Final = "missing"


class _Closed(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


_Key = Annotated[str, StringConstraints(strict=True, pattern=r"^[A-Z][A-Z0-9]*-[A-Za-z0-9.-]+$")]


class HealthVerdictsRead(_Closed):
    """What the verdicts read is asked for: nothing beyond the tree it addresses."""


class TupleVerdict(_Closed):
    """One runtime tuple's newest verdict on the wire.

    Attributes:
        check: The doctor check repeating the newest stage record.
        reason_code: The failure code that record was filed under; ``None`` for a pass.
        checked_at: When that record's stage completed, which is when the check last ran.
    """

    check: CheckResult
    reason_code: CertificationFailureCode | None = None
    checked_at: UtcDatetime


class HealthVerdictsAnswer(_Closed):
    """The verdicts, ordered by tuple digest; empty for a tree that never certified."""

    verdicts: tuple[TupleVerdict, ...] = ()


class BatchRecordsRead(_Closed):
    """What a Batch's generations or conflict frames are read for.

    Attributes:
        urn: The Batch.
    """

    urn: BatchUrn


class GenerationsAnswer(_Closed):
    """One Batch's generations, oldest first."""

    generations: tuple[IntegrationGeneration, ...] = ()


class ConflictFramesAnswer(_Closed):
    """The conflict frames one Batch's blocked integrations left, in record order."""

    conflicts: tuple[IntegrationConflict, ...] = ()


class RepositoryRead(_Closed):
    """What the repository read is asked for: nothing beyond the tree it addresses."""


class BootRecoveryRead(_Closed):
    """What the recovery read is asked for: nothing beyond the tree it addresses."""


class BootRecoveryAnswer(_Closed):
    """The daemon's last start, or ``None`` when no start on this machine recorded one."""

    last: BootRecovery | None = None


class ProofReceiptsRead(_Closed):
    """What the receipt read is asked for.

    Attributes:
        key: The receipt key the card is opened for.
    """

    key: _Key


class ProofReceiptsAnswer(_Closed):
    """The receipts filed under the key, in record order; empty when none was."""

    receipts: tuple[ProofReceipt, ...] = ()


class TargetResolve(_Closed):
    """What the resolve read is asked for.

    Attributes:
        key: The key a route was opened onto.
    """

    key: _Key


class TargetResolution(_Closed):
    """Whether a key still names something.

    Attributes:
        key: The key asked about.
        ending: ``missing`` when no document row and no ledger line holds the key; ``None``
            when something does, so the route's own frame states where it stands.
    """

    key: str
    ending: Literal["missing"] | None = None


class HistoryChangesRead(_Closed):
    """What a page of the change feed is read for.

    Attributes:
        key: The one record whose changes are read; ``None`` reads the whole tree's.
        cursor: The ``next_cursor`` of the page before; ``None`` reads from the newest.
        limit: The most records the page holds.
    """

    key: _Key | None = None
    cursor: Annotated[int, Field(strict=True, ge=1)] | None = None
    limit: Annotated[int, Field(strict=True, ge=1, le=MAX_PAGE)] = DEFAULT_HISTORY_PAGE


def resolve_target(document_file: Path, key: str) -> TargetResolution:
    """Return whether the tree whose document is *document_file* holds anything under *key*.

    A ledger line that names the key anywhere counts as holding it, so a record filed only
    on a ledger, or named only by another record's line, is never called missing.
    """
    document = read_document(document_file)
    held = any(key in document_rows(document, collection) for collection in Epoch2Collection)
    quoted = f'"{key}"'
    for collection in LEDGER_COLLECTIONS:
        if held:
            break
        path = ledger_path(document_file, collection)
        held = path.is_file() and quoted in path.read_text(encoding="utf-8")
    logger.debug(f"resolve_target key={key} held={held}")
    return TargetResolution(key=key, ending=None if held else MISSING_ENDING)


def read_health_verdicts(tree: Path) -> HealthVerdictsAnswer:
    """Return the newest verdict of every runtime tuple the ``.ea`` tree *tree* certified."""
    verdicts = tuple(
        TupleVerdict(check=check, reason_code=code, checked_at=checked_at)
        for check, code, checked_at in runtime_tuple_verdicts(tree.parent)
    )
    logger.debug(f"read_health_verdicts tuples={len(verdicts)}")
    return HealthVerdictsAnswer(verdicts=verdicts)


def read_proof_receipts(path: Path, key: str) -> tuple[ProofReceipt, ...]:
    """Return every proof receipt the receipt ledger at *path* files under *key*."""
    proofs = (
        FiledProof.model_validate(item.payload)
        for item in read_ledger_records(path)
        if item.payload.get("payload_kind") == PROOF_PAYLOAD_KIND
    )
    return tuple(proof.receipt for proof in proofs if proof.receipt.id == key)


@register(HEALTH_VERDICTS_READ_METHOD)
async def _read_health_verdicts(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the newest conformance verdict of every runtime tuple."""
    authority = require_native_call(ctx, params)
    native_params(HealthVerdictsRead, params)
    answer = await asyncio.to_thread(read_health_verdicts, authority.root)
    return answer.model_dump(mode="json")


@register(GENERATIONS_READ_METHOD)
async def _read_generations(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one Batch's integration generations, oldest first."""
    authority = require_native_call(ctx, params)
    args = native_params(BatchRecordsRead, params)
    path = ledger_path(document_path(authority), Epoch2Collection.BATCH)
    ledger = await asyncio.to_thread(read_generation_ledger, path, args.urn)
    answer = GenerationsAnswer(generations=tuple(ledger.generations))
    logger.debug(f"read_generations batch={args.urn.entity_key} count={len(answer.generations)}")
    return answer.model_dump(mode="json")


@register(REPOSITORY_READ_METHOD)
async def _read_repository(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the tree's branch and its pull request, off the event loop."""
    authority = require_native_call(ctx, params)
    native_params(RepositoryRead, params)
    answer = await asyncio.to_thread(read_repository, authority.root.parent)
    return answer.model_dump(mode="json")


@register(CONFLICT_FRAMES_READ_METHOD)
async def _read_conflict_frames(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the conflict frames one Batch's blocked integrations left."""
    authority = require_native_call(ctx, params)
    args = native_params(BatchRecordsRead, params)
    path = ledger_path(document_path(authority), Epoch2Collection.BATCH)
    frames = await asyncio.to_thread(read_conflict_frames, path, args.urn)
    logger.debug(f"read_conflict_frames batch={args.urn.entity_key} count={len(frames)}")
    return ConflictFramesAnswer(conflicts=frames).model_dump(mode="json")


@register(BOOT_RECOVERY_READ_METHOD)
async def _read_boot_recovery(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return what the daemon's last start repaired and what that cost."""
    require_native_call(ctx, params)
    native_params(BootRecoveryRead, params)
    last = await asyncio.to_thread(read_boot_recovery, Path(ctx.wal_dir))
    return BootRecoveryAnswer(last=last).model_dump(mode="json")


@register(PROOF_RECEIPTS_READ_METHOD)
async def _read_proof_receipts(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the proof receipt filed under one receipt key."""
    authority = require_native_call(ctx, params)
    args = native_params(ProofReceiptsRead, params)
    path = ledger_path(document_path(authority), Epoch2Collection.RECEIPT)
    receipts = await asyncio.to_thread(read_proof_receipts, path, args.key)
    logger.debug(f"read_proof_receipts key={args.key} count={len(receipts)}")
    return ProofReceiptsAnswer(receipts=receipts).model_dump(mode="json")


@register(TARGET_RESOLVE_METHOD)
async def _resolve_target(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return whether anything was ever written under one key."""
    authority = require_native_call(ctx, params)
    args = native_params(TargetResolve, params)
    answer = await asyncio.to_thread(resolve_target, document_path(authority), args.key)
    return answer.model_dump(mode="json")


@register(HISTORY_CHANGES_READ_METHOD)
async def _read_history_changes(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one page of the change feed, newest first."""
    authority = require_native_call(ctx, params)
    args = native_params(HistoryChangesRead, params)
    page: ChangePage = await asyncio.to_thread(
        read_change_page,
        document_path(authority),
        record_key=args.key,
        cursor=args.cursor,
        limit=args.limit,
    )
    logger.debug(f"read_history_changes key={args.key} count={len(page.changes)}")
    return page.model_dump(mode="json")


__all__ = [
    "BOOT_RECOVERY_READ_METHOD",
    "CONFLICT_FRAMES_READ_METHOD",
    "DEFAULT_HISTORY_PAGE",
    "GENERATIONS_READ_METHOD",
    "HEALTH_VERDICTS_READ_METHOD",
    "HISTORY_CHANGES_READ_METHOD",
    "MISSING_ENDING",
    "PROOF_RECEIPTS_READ_METHOD",
    "REPOSITORY_READ_METHOD",
    "TARGET_RESOLVE_METHOD",
    "BatchRecordsRead",
    "BootRecoveryAnswer",
    "BootRecoveryRead",
    "ConflictFramesAnswer",
    "GenerationsAnswer",
    "HealthVerdictsAnswer",
    "HistoryChangesRead",
    "ProofReceiptsAnswer",
    "ProofReceiptsRead",
    "RepositoryRead",
    "TargetResolution",
    "TargetResolve",
    "TupleVerdict",
    "read_conflict_frames",
    "read_health_verdicts",
    "read_proof_receipts",
    "resolve_target",
]
