"""The Campaign read models the console opens.

``projection.campaign.view`` and ``projection.campaign.artifact`` read a
Campaign back as :class:`~eawf.kernel.projection.campaign.CampaignView` and one
:class:`~eawf.kernel.projection.campaign.ArtifactCardView`. Nothing here writes;
the writers live in :mod:`eawf.runtime.daemon.methods.campaign`.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Annotated, Any

import orjson
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.kernel.projection.campaign import (
    ArtifactCardView,
    CampaignView,
    build_campaign_view,
    promoted_findings,
    promoting_sequences,
    stored_revisions,
)
from eawf.kernel.state.epoch2.authority import RootAuthority
from eawf.kernel.state.epoch2.base import NonEmptyStr
from eawf.kernel.state.epoch2.campaign import Campaign
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.projection import document_path, firehose_path
from eawf.runtime.daemon.native_guard import require_native_call
from eawf.runtime.daemon.read_contract import (
    CAMPAIGN_ARTIFACT_METHOD,
    CAMPAIGN_VIEW_METHOD,
)
from eawf.runtime.daemon.spend import run_usage

logger = logging.getLogger(__name__)


class _ReadParams(BaseModel):
    """A closed request; an unknown field is refused."""

    model_config = ConfigDict(extra="forbid")


class CampaignReadParams(_ReadParams):
    """Params of :data:`CAMPAIGN_VIEW_METHOD`."""

    repo_root: str | None = None
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]


class ArtifactReadParams(_ReadParams):
    """Params of :data:`CAMPAIGN_ARTIFACT_METHOD`."""

    repo_root: str | None = None
    campaign_key: Annotated[str, Field(pattern=r"^CAM-\d{4,}$")]
    artifact_ref: NonEmptyStr


def campaign_view(authority: RootAuthority, key: str) -> CampaignView:
    """Return one Campaign's read model, its artifacts and findings resolved.

    Raises:
        DaemonValidationError: The tree holds no such Campaign.
        ValueError: The Campaign lists an artifact revision no record holds.
    """
    document_file = document_path(authority)
    document = read_document(document_file)
    row = document_rows(document, Epoch2Collection.CAMPAIGN).get(key)
    if row is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree holds no campaign keyed {key!r}"
        )

    def payloads(collection: Epoch2Collection) -> tuple[Mapping[str, Any], ...]:
        path = ledger_path(document_file, collection)
        return tuple(line.payload for line in effective_records(read_ledger_records(path)))

    campaign = Campaign.model_validate(row)
    runs = read_ledger_records(ledger_path(document_file, Epoch2Collection.RUN))
    return build_campaign_view(
        row,
        revisions=stored_revisions(payloads(Epoch2Collection.ARTIFACT)),
        findings=promoted_findings(payloads(Epoch2Collection.CAMPAIGN_FINDING)),
        runners=_runner_runs(document, document_file, campaign),
        usage=run_usage(runs, (ref for step in campaign.plan_steps for ref in step.run_refs)),
        promoted_at=promoting_sequences(_firehose_payloads(firehose_path(authority))),
    )


def _runner_runs(
    document: dict[str, Any], document_file: Path, campaign: Campaign
) -> dict[str, Run]:
    """Return the Runs the Campaign's steps name as runners, by key.

    A finished round's Run is compacted out of the document into the run ledger,
    so a runner the document no longer holds is read from there.
    """
    wanted = {step.run_refs[-1].entity_key for step in campaign.plan_steps if step.run_refs}
    rows = document_rows(document, Epoch2Collection.RUN)
    found = {key: Run.model_validate(rows[key]) for key in wanted if key in rows}
    if wanted - found.keys():
        path = ledger_path(document_file, Epoch2Collection.RUN)
        for line in effective_records(read_ledger_records(path)):
            if line.record_key in wanted - found.keys() and "payload_kind" not in line.payload:
                found[line.record_key] = Run.model_validate(line.payload)
    return found


def _firehose_payloads(path: Path) -> Iterator[Mapping[str, Any]]:
    """Yield the payload of each readable firehose row, in the order it was written.

    A row that does not parse is skipped: it states no sequence a finding could be
    drawn at, and the finding it would have dated reads as promoted before the cursor.
    """
    if not path.exists():
        return
    with path.open("rb") as handle:
        for raw in handle:
            try:
                row = orjson.loads(raw)
            except orjson.JSONDecodeError:
                logger.warning(f"campaign_view skipped an unreadable firehose row path={path}")
                continue
            if isinstance(row, dict) and isinstance(row.get("payload"), dict):
                yield row["payload"]


def _read_params[T: BaseModel](model: type[T], params: dict[str, Any], method: str) -> T:
    try:
        return model.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: schema_validation_failed: {error.error_count()} bad "
            f"parameter(s) for {method}"
        ) from error


@register(CAMPAIGN_VIEW_METHOD)
async def read_campaign(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one Campaign's :class:`CampaignView`. Nothing is written."""
    args = _read_params(CampaignReadParams, params, CAMPAIGN_VIEW_METHOD)
    authority = require_native_call(ctx, params)
    view = await asyncio.to_thread(campaign_view, authority, args.campaign_key)
    return view.model_dump(mode="json")


@register(CAMPAIGN_ARTIFACT_METHOD)
async def read_artifact(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one artifact revision's :class:`ArtifactCardView`. Nothing is written.

    Raises:
        DaemonValidationError: The Campaign keeps no such revision.
    """
    args = _read_params(ArtifactReadParams, params, CAMPAIGN_ARTIFACT_METHOD)
    authority = require_native_call(ctx, params)
    view = await asyncio.to_thread(campaign_view, authority, args.campaign_key)
    card: ArtifactCardView | None = view.artifact(args.artifact_ref)
    if card is None:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: {args.campaign_key} keeps no revision "
            f"{args.artifact_ref!r}"
        )
    return card.model_dump(mode="json")


__all__ = [
    "CAMPAIGN_ARTIFACT_METHOD",
    "CAMPAIGN_VIEW_METHOD",
    "campaign_view",
]
