"""OperatorInputPayload — payload model for StoreKind.OPERATOR_INPUT records.

An operator-input store record persists one mid-run operator input on the
campaign control-plane blackboard (the hub-and-spoke channel the orchestrator
folds — AGENTS rule 4 + the a2a verdict). Every operator-initiated input is a
single typed, append-only :class:`~eawf.kernel.spec.operator_input.OperatorInput`
row the round loop reads back via
:func:`~eawf.kernel.spec.operator_input.OperatorInputChannel.fold`.

The canonical model lives in :mod:`eawf.kernel.spec.operator_input`; this kind
module aliases it under the store-kind name so the
:data:`~eawf.kernel.store.kinds.PAYLOAD_MODELS` registry maps the kind to its
payload model the same way every other kind does.
"""

from __future__ import annotations

from eawf.kernel.spec.operator_input import OperatorInput

#: The payload model the ``operator_input`` store kind persists. Aliased from the
#: kernel spec so the registry has one canonical model per kind without a second
#: definition (DRY).
OperatorInputPayload = OperatorInput

__all__ = ["OperatorInputPayload"]
