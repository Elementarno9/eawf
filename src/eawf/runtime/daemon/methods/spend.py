"""The spend reads: the governor ceiling's spend, and one Run's spend against its caps.

Both read the selected generation's document and run ledger without a lock, as every
projection read does, and write nothing. The ceiling is read from the repository's
``economics`` table on each call, so an edited governor is what the next read shows.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon.admission import EconomicsPolicyError, load_economics
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.projection import PROJECTION_UNREADABLE, document_path
from eawf.runtime.daemon.native_guard import require_native_call
from eawf.runtime.daemon.spend import cost_ceiling_view, run_usage_view

logger = logging.getLogger(__name__)

#: The governor ceiling beside what the live Runs spend and hold against it.
SPEND_CEILING_READ_METHOD: Final = "runtime.spend.ceiling.read"

#: One Run's spend beside the caps its binding sealed.
RUN_USAGE_READ_METHOD: Final = "runtime.run.usage.read"


class CeilingReadParams(BaseModel):
    """Params of :data:`SPEND_CEILING_READ_METHOD`: the tree alone."""

    model_config = ConfigDict(extra="forbid")

    repo_root: str | None = None


class RunUsageReadParams(CeilingReadParams):
    """Params of :data:`RUN_USAGE_READ_METHOD`: the tree and the Run."""

    urn: RunUrn


def _params[T: BaseModel](model: type[T], params: dict[str, Any], method: str) -> T:
    """Validate *params*, refusing a bad request with the projection's code."""
    try:
        return model.model_validate(params)
    except ValidationError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error.error_count()} bad "
            f"parameter(s) for {method}"
        ) from error


@register(SPEND_CEILING_READ_METHOD)
async def read_spend_ceiling(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return the governor ceiling, the live Runs' spend, the stops and spend by provider.

    Args:
        ctx: Server context, whose bound state path is the tree fallback.
        params: The request parameters, validated as :class:`CeilingReadParams`.

    Returns:
        The :class:`~eawf.runtime.daemon.spend.CostCeilingView` as a JSON-mode mapping.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The parameters are malformed, or the ``economics``
            table does not validate.
    """
    _params(CeilingReadParams, params, SPEND_CEILING_READ_METHOD)
    authority = require_native_call(ctx, params)
    try:
        governor = (await asyncio.to_thread(load_economics, authority.root.parent)).governor
    except EconomicsPolicyError as error:
        raise DaemonValidationError(
            f"validation_failed: {PROJECTION_UNREADABLE}: {error}"
        ) from error
    path = document_path(authority)
    document = await asyncio.to_thread(read_document, path)
    records = await asyncio.to_thread(read_ledger_records, ledger_path(path, Epoch2Collection.RUN))
    view = cost_ceiling_view(document, records, governor=governor)
    return view.model_dump(mode="json")


@register(RUN_USAGE_READ_METHOD)
async def read_run_usage(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Return one Run's spend, its sealed caps and the typical duration of its kind.

    Args:
        ctx: Server context, whose bound state path is the tree fallback.
        params: The request parameters, validated as :class:`RunUsageReadParams`.

    Returns:
        The :class:`~eawf.runtime.daemon.spend.RunUsageView` as a JSON-mode mapping.

    Raises:
        NativeAuthorityRefusedError: The request addresses no epoch-2 tree.
        DaemonValidationError: The parameters are malformed, or the tree holds no
            such Run.
    """
    args = _params(RunUsageReadParams, params, RUN_USAGE_READ_METHOD)
    authority = require_native_call(ctx, params)
    path = document_path(authority)
    document = await asyncio.to_thread(read_document, path)
    records = await asyncio.to_thread(read_ledger_records, ledger_path(path, Epoch2Collection.RUN))
    try:
        view = run_usage_view(document, records, urn=args.urn)
    except KeyError as error:
        raise DaemonValidationError(
            f"validation_failed: identity_not_found: the tree holds no {args.urn.entity_key}"
        ) from error
    return view.model_dump(mode="json")


__all__ = [
    "RUN_USAGE_READ_METHOD",
    "SPEND_CEILING_READ_METHOD",
    "CeilingReadParams",
    "RunUsageReadParams",
    "read_run_usage",
    "read_spend_ceiling",
]
