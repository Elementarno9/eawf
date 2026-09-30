"""Binding a Milestone or Batch to a delivery regime, and discharging its debts.

``runtime.regime.bind`` admits the regime a unit of delivery is bound
under: the regime table, the fast quota and window, and the refusal of a
deferred safety gate are all decided in
:mod:`eawf.workflow.delivery.regime_bindings`. The debts a fast binding
mints are the ones ``release.approve`` reads, so a stable release cannot
be approved while one is open. ``runtime.regime.discharge_debt`` closes a
debt against the head and evidence of the deferred gate's passing run.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.state.epoch2.base import ShaStr
from eawf.kernel.state.epoch2.regime import DebtKey, RegimeError
from eawf.kernel.state.epoch2.urns import EvidenceUrn
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext, register
from eawf.runtime.daemon.methods.release_context import require_state_path
from eawf.runtime.lock.portalock import LockTimeout
from eawf.workflow.delivery.regime_bindings import RegimeBindRequest, bind_regime, discharge_debt

logger = logging.getLogger(__name__)

#: The verb that binds a Milestone or Batch to a delivery regime.
REGIME_BIND_METHOD: Final = "runtime.regime.bind"

#: The verb that discharges one verification debt.
REGIME_DISCHARGE_DEBT_METHOD: Final = "runtime.regime.discharge_debt"


class DischargeDebtParams(BaseModel):
    """Params of :data:`REGIME_DISCHARGE_DEBT_METHOD`.

    Attributes:
        key: The debt's ``VDT-####`` key.
        head: The exact head the deferred gate passed at.
        evidence_ref: The evidence of that pass.
    """

    model_config = ConfigDict(extra="forbid")

    key: DebtKey
    head: ShaStr
    evidence_ref: EvidenceUrn


def _refusal(error: ValidationError | RegimeError | LockTimeout) -> DaemonValidationError:
    """Return the wire refusal one admission failure is answered with."""
    if isinstance(error, RegimeError):
        return DaemonValidationError(f"validation_failed: {error.code}: {error}")
    if isinstance(error, LockTimeout):
        return DaemonValidationError(f"validation_failed: regime_admission_busy: {error}")
    detail = "; ".join(str(row["msg"]) for row in error.errors())
    return DaemonValidationError(f"validation_failed: regime_binding_invalid: {detail}")


@register(REGIME_BIND_METHOD)
async def bind(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Bind a scope to a regime, minting a debt for each gate a fast binding defers.

    Args:
        ctx: Server context; supplies the root the binding is recorded under.
        params: JSON-RPC params per :class:`RegimeBindRequest`.

    Returns:
        The recorded binding and the debts it minted.

    Raises:
        DaemonValidationError: The daemon has no state root, or the binding
            is refused; the message leads with the refusal code.
    """
    state_path = require_state_path(ctx)
    try:
        request = RegimeBindRequest.model_validate(params)
        binding, debts = await asyncio.to_thread(
            bind_regime, state_path, request, at=datetime.now(UTC)
        )
    except (ValidationError, RegimeError, LockTimeout) as error:
        raise _refusal(error) from error
    return {
        "binding": binding.model_dump(mode="json"),
        "debts": [debt.model_dump(mode="json") for debt in debts],
    }


@register(REGIME_DISCHARGE_DEBT_METHOD)
async def discharge(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Discharge one open verification debt by the passing run of its deferred gate.

    Args:
        ctx: Server context; supplies the root the debt is recorded under.
        params: JSON-RPC params per :class:`DischargeDebtParams`.

    Returns:
        The debt at ``DISCHARGED``.

    Raises:
        DaemonValidationError: The daemon has no state root, the params do
            not validate, or the debt is unknown or already closed.
    """
    state_path = require_state_path(ctx)
    try:
        args = DischargeDebtParams.model_validate(params)
        debt = await asyncio.to_thread(
            discharge_debt,
            state_path,
            args.key,
            head=args.head,
            evidence_ref=args.evidence_ref,
            at=datetime.now(UTC),
        )
    except (ValidationError, RegimeError, LockTimeout) as error:
        raise _refusal(error) from error
    return {"debt": debt.model_dump(mode="json")}


__all__ = [
    "REGIME_BIND_METHOD",
    "REGIME_DISCHARGE_DEBT_METHOD",
    "DischargeDebtParams",
    "bind",
    "discharge",
]
