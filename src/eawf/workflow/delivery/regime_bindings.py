"""Binding a unit of delivery to a regime, and settling the debt a fast binding owes.

A Milestone or Batch is delivered under one
:class:`~eawf.kernel.state.epoch2.regime.DeliveryRegime`, declared at plan
time. :func:`bind_regime` is where that declaration is admitted: the
binding is validated against the regime table, a fast binding is held to
its quota and to a bounded window, and every gate a fast binding defers is
minted as a :class:`~eawf.kernel.state.epoch2.regime.VerificationDebt`,
which refuses a gate the fast regime keeps. Stable release approval reads
those debts and refuses while one is open; :func:`discharge_debt` is the
only way one closes, by naming the head and the evidence of the deferred
gate's passing run.

A scope carries one binding at a time. Changing its regime is a successor
binding at a later policy revision that names the binding it replaces, so
loosening active work, or promoting experimental work without the audit of
its verdict, is refused by the table rather than by this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.epoch2.base import StrictPositiveInt
from eawf.kernel.state.epoch2.regime import (
    DebtKey,
    DeliveryRegime,
    GateClass,
    RegimeBinding,
    RegimeError,
    RegimeSuccession,
    VerificationDebt,
    admit_fast_binding,
)
from eawf.kernel.state.epoch2.urns import BatchUrn, EvidenceUrn, MilestoneUrn
from eawf.kernel.state.models import HypothesisIdStr, IdStr
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.lock import portalock
from eawf.workflow.release.verification_debts import read_verification_debts

logger = logging.getLogger(__name__)

#: How many fast bindings may be in force in one repository at once. A
#: hotfix path that several scopes hold open together is steady delivery
#: with its ceremony removed.
FAST_BINDING_QUOTA: Final = 2

#: The longest a fast binding may stay in force before it lapses.
MAX_FAST_WINDOW: Final = timedelta(days=7)

#: The lock a binding's admission holds, so two admissions cannot both
#: count the same free quota slot.
_ADMISSION_LOCK: Final = "regime_binding.admission"


class RegimeBindRequest(BaseModel):
    """What an operator declares when binding a scope to a regime.

    Attributes:
        regime: The assurance policy.
        scope_ref: The Milestone or Batch the binding governs.
        incident_ref: The Incident a fast binding answers.
        hypothesis_ref: The Hypothesis experimental work tests.
        policy_revision: The policy revision the binding is made under.
        expires_at: When a fast binding lapses.
        deferred_gates: The gates a fast binding defers; each becomes a debt.
        succeeds: The binding this one replaces, when the scope already has one.
    """

    model_config = ConfigDict(extra="forbid")

    regime: DeliveryRegime
    scope_ref: MilestoneUrn | BatchUrn
    incident_ref: IdStr | None = None
    hypothesis_ref: HypothesisIdStr | None = None
    policy_revision: StrictPositiveInt
    expires_at: UtcDatetime | None = None
    deferred_gates: tuple[GateClass, ...] = ()
    succeeds: RegimeSuccession | None = None


def read_regime_bindings(state_path: Path) -> tuple[RegimeBinding, ...]:
    """Return every recorded regime binding, oldest first.

    Args:
        state_path: Path to ``state.json``.

    Returns:
        The bindings in the order they were admitted; empty when none was.

    Raises:
        ValueError: A line is not a binding. A corrupt collection refuses
            rather than skips, because a skipped fast binding frees a quota
            slot it still holds.
    """
    path = store_path(state_path, StoreKind.REGIME_BINDING)
    if not path.exists():
        return ()
    bindings: list[RegimeBinding] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            envelope = Envelope.model_validate_json(line)
            if envelope.kind is not StoreKind.REGIME_BINDING:
                raise ValueError(f"filed under {envelope.kind.value!r}")
            bindings.append(RegimeBinding.model_validate(envelope.payload))
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"regime binding collection {path} line {number}: {exc}") from exc
    return tuple(bindings)


def _require_succession(request: RegimeBindRequest, held: tuple[RegimeBinding, ...]) -> None:
    """Refuse a binding that ignores, or misnames, the scope's current binding.

    Raises:
        RegimeError: ``regime_binding_exists`` when the scope is bound and
            the request names no predecessor; ``regime_succession_mismatch``
            when the named predecessor is not the scope's current binding.
    """
    current = next((b for b in reversed(held) if b.scope_ref == request.scope_ref), None)
    named = request.succeeds
    if named is None:
        if current is None:
            return
        raise RegimeError(
            "regime_binding_exists",
            f"{request.scope_ref} is already bound {current.regime.value}; "
            "a change is a successor binding that names it",
        )
    if current is None or (current.regime, current.policy_revision) != (
        named.prior_regime,
        named.prior_policy_revision,
    ):
        raise RegimeError(
            "regime_succession_mismatch",
            f"{request.scope_ref} is not bound {named.prior_regime.value} at policy revision "
            f"{named.prior_policy_revision}",
        )


def _next_debt_keys(debts: tuple[VerificationDebt, ...], count: int) -> list[str]:
    """Return *count* fresh ``VDT-####`` keys after every key already recorded."""
    last = max((int(d.key.removeprefix("VDT-")) for d in debts), default=0)
    return [f"VDT-{last + offset:04d}" for offset in range(1, count + 1)]


def _debt_envelope(debt: VerificationDebt, *, at: datetime) -> Envelope:
    return Envelope(
        id=f"{debt.key}-{debt.status.value}",
        kind=StoreKind.VERIFICATION_DEBT,
        scope_id=str(debt.scope_ref),
        created_at=at,
        summary=f"{debt.key} {debt.status.value}: {debt.deferred_gate.value} gate",
        payload=debt.model_dump(mode="json"),
    )


def bind_regime(
    state_path: Path, request: RegimeBindRequest, *, at: datetime
) -> tuple[RegimeBinding, tuple[VerificationDebt, ...]]:
    """Admit *request* as the scope's binding, minting a debt per deferred gate.

    Args:
        state_path: Path to ``state.json``.
        request: The declared binding.
        at: When it takes effect.

    Returns:
        The recorded binding and the debts it minted, in gate order.

    Raises:
        RegimeError: ``regime_binding_exists`` or
            ``regime_succession_mismatch`` (see :func:`_require_succession`);
            ``fast_regime_window_unbounded`` when a fast window runs past
            :data:`MAX_FAST_WINDOW`; the codes of
            :func:`~eawf.kernel.state.epoch2.regime.admit_fast_binding`.
        pydantic.ValidationError: The binding breaks the regime table -- an
            unknown regime, a missing or stray reference, a gate deferred by
            a regime that defers none, a successor the table does not draw --
            or a debt names a gate the fast regime keeps.
        StateConflict: The admission lock stayed held.
    """
    anchor = store_path(state_path, StoreKind.REGIME_BINDING).with_name(_ADMISSION_LOCK)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(anchor, timeout=5.0):
        held = read_regime_bindings(state_path)
        _require_succession(request, held)
        recorded = read_verification_debts(state_path)
        keys = _next_debt_keys(recorded, len(request.deferred_gates))
        binding = RegimeBinding.model_validate(
            {
                **request.model_dump(exclude={"deferred_gates"}),
                "effective_at": at,
                "verification_debt_refs": keys,
            }
        )
        if binding.expires_at is not None and binding.expires_at - at > MAX_FAST_WINDOW:
            raise RegimeError(
                "fast_regime_window_unbounded",
                f"a fast window closes within {MAX_FAST_WINDOW.days} days of taking effect",
            )
        if binding.regime is DeliveryRegime.FAST:
            admit_fast_binding(binding, in_force=held, quota=FAST_BINDING_QUOTA, at=at)
        debts = tuple(
            VerificationDebt.model_validate(
                {
                    "key": key,
                    "scope_ref": binding.scope_ref,
                    "incident_ref": binding.incident_ref,
                    "deferred_gate": gate,
                    "opened_at": at,
                }
            )
            for key, gate in zip(keys, request.deferred_gates, strict=True)
        )
        # Debts land before the binding that cites them: a crash between the
        # two leaves an open debt that blocks approval, never a binding that
        # deferred a gate with nothing owed for it.
        for debt in debts:
            append_envelope(
                store_path(state_path, StoreKind.VERIFICATION_DEBT), _debt_envelope(debt, at=at)
            )
        append_envelope(
            store_path(state_path, StoreKind.REGIME_BINDING),
            Envelope(
                id=f"regime-{binding.scope_ref}@{binding.policy_revision}",
                kind=StoreKind.REGIME_BINDING,
                scope_id=str(binding.scope_ref),
                created_at=at,
                summary=f"{binding.scope_ref} bound {binding.regime.value}",
                payload=binding.model_dump(mode="json"),
            ),
        )
    logger.info(
        f"bind_regime scope={binding.scope_ref} regime={binding.regime.value} debts={len(debts)}"
    )
    return binding, debts


def discharge_debt(
    state_path: Path, key: DebtKey, *, head: str, evidence_ref: EvidenceUrn, at: datetime
) -> VerificationDebt:
    """Record that the gate debt *key* deferred ran and passed at *head*.

    Args:
        state_path: Path to ``state.json``.
        key: The debt's ``VDT-####`` key.
        head: The exact head the gate passed at.
        evidence_ref: The evidence of that pass.
        at: When it passed.

    Returns:
        The debt at ``DISCHARGED``.

    Raises:
        RegimeError: ``verification_debt_unknown`` when nothing records
            *key*; ``verification_debt_closed`` when it is not open.
        pydantic.ValidationError: *head* is not a commit sha.
        StateConflict: The admission lock stayed held.
    """
    anchor = store_path(state_path, StoreKind.REGIME_BINDING).with_name(_ADMISSION_LOCK)
    anchor.parent.mkdir(parents=True, exist_ok=True)
    with portalock.acquire(anchor, timeout=5.0):
        debt = next((d for d in read_verification_debts(state_path) if d.key == key), None)
        if debt is None:
            raise RegimeError("verification_debt_unknown", f"no verification debt {key}")
        discharged = debt.discharge(head=head, evidence_ref=evidence_ref, at=at)
        append_envelope(
            store_path(state_path, StoreKind.VERIFICATION_DEBT), _debt_envelope(discharged, at=at)
        )
    logger.info(f"discharge_debt key={key} head={head}")
    return discharged


__all__ = [
    "FAST_BINDING_QUOTA",
    "MAX_FAST_WINDOW",
    "RegimeBindRequest",
    "bind_regime",
    "discharge_debt",
    "read_regime_bindings",
]
