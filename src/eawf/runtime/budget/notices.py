"""Non-blocking budget notices and their atomic, escalating upsert.

A budget crossing is recorded as one notice per logical condition -- a
scope, a resource axis and the basis the budget was set on -- and never
as a question, a pause, a pending action or a hold: the lifecycle records
refuse a budget event outright (see
:mod:`eawf.kernel.state.budget_signal`). The threshold band is kept out of
the identity on purpose, so the approaching band and the limit band move
one row through two revisions instead of stacking two cards.

The upsert runs as one locked read-modify-write over the notice ledger, so
a restart, a retried dispatch or two concurrent producers crossing the
same band still leave one row at one revision. A repeated crossing and a
late lower band write nothing: the first is already on file, the second
must never regress the band a reader has already seen.

No threshold is defined here. The band a crossing reached is computed by
:mod:`eawf.runtime.budget.policy`; a notice only records the value it
observed and the budget it was measured against.
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.runtime.provider import Digest
from eawf.kernel.state.epoch2.base import (
    NonEmptyStr,
    PrincipalKey,
    StrictNonNegativeInt,
    StrictPositiveInt,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.state.writer import atomic_write_json_locked
from eawf.runtime.lock import portalock

logger = logging.getLogger(__name__)

#: The ordered threshold bands. ``approaching`` precedes ``limit_reached``;
#: a notice only ever moves forward through them.
NoticeBand = Literal["approaching", "limit_reached"]

#: What the budget was set on. An ``estimate`` is a prediction the run may
#: legitimately outgrow; a ``hard_limit`` is an enforced cap.
NoticeBasis = Literal["estimate", "hard_limit"]

NoticeSeverity = Literal["info", "warning", "critical"]

#: Terminal statuses stay terminal: a late crossing never reopens them.
NoticeStatus = Literal["OPEN", "RESOLVED", "CLEARED", "SUPERSEDED", "EXPIRED"]

#: The metered resource dimension: tokens spent, or wall seconds elapsed.
BudgetAxis = Literal["tokens", "wall_seconds"]

#: What a principal, or the source policy, did to a notice.
NoticeAction = Literal["seen", "acknowledged", "snoozed", "resolved", "cleared"]

#: Who an epoch-1 root's notices are for. That root records no principals,
#: so its one operator is the audience of last resort rather than whichever
#: client happens to connect first.
LOCAL_OPERATOR: Final[str] = "OPERATOR"

_BAND_RANK: Final[dict[str, int]] = {"approaching": 0, "limit_reached": 1}

_SEVERITY: Final[dict[tuple[str, str], NoticeSeverity]] = {
    ("estimate", "approaching"): "info",
    ("estimate", "limit_reached"): "warning",
    ("hard_limit", "approaching"): "warning",
    ("hard_limit", "limit_reached"): "critical",
}

_NOTICE_KEY_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"

#: Timeout for the ledger lock; one upsert holds it for a single small write.
_LOCK_TIMEOUT_SECONDS: Final[float] = 5.0


def notice_key_for(
    *, scope_id: str, axis: BudgetAxis, basis: NoticeBasis, contract_digest: str | None = None
) -> str:
    """Return the logical identity of the notice for one budget condition.

    A changed basis is a changed budget contract, so it is a new identity,
    and so is a Run recompiled under another contract: its ceiling is a
    different ceiling. The band is deliberately absent so escalation stays
    on one row.

    Args:
        scope_id: The scope the budget belongs to: a wave id, or a Run key.
        axis: The metered resource dimension.
        basis: What the budget was set on.
        contract_digest: The compiled contract a Run's ceiling came from,
            or ``None`` for a scope that has none.

    Returns:
        ``sha256:<hex>`` over the unit-separated identity fields.
    """
    identity = f"{scope_id}\x1f{axis}\x1f{basis}"
    if contract_digest is not None:
        identity = f"{identity}\x1f{contract_digest}"
    digest = hashlib.sha256(identity.encode()).hexdigest()
    return f"sha256:{digest}"


def budget_window_digest(claim_ref: str) -> str:
    """Return the budget-window reference a claim folds into a notice's identity.

    A wave re-claimed after a release is a new attempt at the same budget,
    so its crossing is a new notice; a restart inside one claim is not, and
    cannot invent a new window because the claim reference is unchanged.

    Args:
        claim_ref: What identifies the claim: its session id, or its
            start stamp where no session was recorded.

    Returns:
        ``sha256:<hex>`` over the claim reference.
    """
    return f"sha256:{hashlib.sha256(f'claim\x1f{claim_ref}'.encode()).hexdigest()}"


def severity_for(basis: NoticeBasis, band: NoticeBand) -> NoticeSeverity:
    """Return the severity a notice of *basis* carries at *band*.

    Args:
        basis: What the budget was set on.
        band: The threshold band the crossing reached.

    Returns:
        The notice severity for that basis and band.
    """
    return _SEVERITY[(basis, band)]


class RecipientState(BaseModel):
    """Where one recipient stands with one notice.

    Delivery is not acknowledgement and opening is not resolution, so each
    is its own revision; a restart reads them and cannot re-deliver a
    revision already delivered.

    Attributes:
        delivered_revision: The revision last delivered to this recipient.
        seen_revision: The revision this recipient last opened.
        acknowledged_revision: The revision this recipient acknowledged.
        snoozed_until: When this recipient's snooze lapses.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    delivered_revision: StrictPositiveInt | None = None
    seen_revision: StrictPositiveInt | None = None
    acknowledged_revision: StrictPositiveInt | None = None
    snoozed_until: UtcDatetime | None = None


class NoticeHistoryEntry(BaseModel):
    """One disposition a notice received, kept in the order it happened.

    Attributes:
        action: What was done.
        revision: The notice revision it was done to.
        at: When.
        principal: Who did it, or ``None`` for an imported legacy fact.
        reason: Why, where the action came from a source policy or a
            legacy import rather than a principal.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: NoticeAction
    revision: StrictPositiveInt
    at: UtcDatetime
    principal: PrincipalKey | None = None
    reason: NonEmptyStr | None = None


class BudgetCrossing(BaseModel):
    """One observation that a scope's consumption reached a threshold band.

    Attributes:
        scope_id: The scope whose budget was crossed.
        axis: The metered resource dimension.
        basis: What the budget was set on.
        band: The highest band the observation reached.
        observed_value: The consumption observed.
        budget_value: The budget it was measured against, or ``None`` when
            the observation recorded none.
        observed_at: When the producer read the consumption.
        contract_digest: The compiled contract a Run's ceiling came from,
            the budget window of a claim, or ``None`` for neither.
        audience: The principals a notice this crossing creates is for.
        provenance: The legacy identifiers the observation came from.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: NonEmptyStr
    axis: BudgetAxis = "tokens"
    basis: NoticeBasis
    band: NoticeBand
    observed_value: StrictNonNegativeInt
    budget_value: StrictNonNegativeInt | None
    observed_at: UtcDatetime
    contract_digest: Digest | None = None
    audience: tuple[PrincipalKey, ...] = ()
    provenance: tuple[NonEmptyStr, ...] = ()

    @property
    def notice_key(self) -> str:
        """The identity of the notice this crossing upserts."""
        return notice_key_for(
            scope_id=self.scope_id,
            axis=self.axis,
            basis=self.basis,
            contract_digest=self.contract_digest,
        )


class BudgetThresholdNotice(BaseModel):
    """The one durable, non-blocking notice for a budget condition.

    Attributes:
        notice_key: Logical identity; see :func:`notice_key_for`.
        scope_id: The scope whose budget was crossed.
        axis: The metered resource dimension.
        basis: What the budget was set on.
        blocking: Always false and unsettable, so no reader can treat an
            open notice as authority to stop work.
        highest_band: The highest band observed; monotonic.
        severity: Derived from ``basis`` and ``highest_band``.
        observed_value: The consumption at the latest escalation.
        budget_value: The budget that consumption was measured against.
        status: Lifecycle position; only ``OPEN`` accepts escalation.
        revision: Starts at one and increments once per escalation.
        opened_at: When the first crossing was recorded.
        last_observed_at: When the latest escalation was recorded.
        resolved_at: When the notice reached a terminal status.
        contract_digest: The compiled contract a Run's ceiling came from.
        audience: The principals the notice is for, resolved at creation.
        recipients: Each recipient's delivery and disposition state.
        history: Every disposition, oldest first.
        provenance: The legacy identifiers the notice was imported from.
        resolved_by: The principal who resolved it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    notice_key: str = Field(pattern=_NOTICE_KEY_PATTERN)
    scope_id: NonEmptyStr
    axis: BudgetAxis
    basis: NoticeBasis
    contract_digest: Digest | None = None
    blocking: Literal[False] = False
    highest_band: NoticeBand
    severity: NoticeSeverity
    observed_value: StrictNonNegativeInt
    budget_value: StrictNonNegativeInt | None
    status: NoticeStatus = "OPEN"
    revision: StrictPositiveInt = 1
    opened_at: UtcDatetime
    last_observed_at: UtcDatetime
    resolved_at: UtcDatetime | None = None
    audience: tuple[PrincipalKey, ...] = ()
    recipients: dict[PrincipalKey, RecipientState] = Field(default_factory=dict)
    history: tuple[NoticeHistoryEntry, ...] = ()
    provenance: tuple[NonEmptyStr, ...] = ()
    resolved_by: PrincipalKey | None = None

    @model_validator(mode="after")
    def _check_derived_fields(self) -> Self:
        """Keep the key, severity and timestamps consistent with the facts.

        Raises:
            ValueError: The key does not address this condition, the
                severity is not the one the basis and band derive, the
                last observation precedes the opening, or ``resolved_at``
                disagrees with whether the status is terminal, or a
                recipient is outside the audience.
        """
        expected_key = notice_key_for(
            scope_id=self.scope_id,
            axis=self.axis,
            basis=self.basis,
            contract_digest=self.contract_digest,
        )
        if self.notice_key != expected_key:
            raise ValueError(f"notice_key {self.notice_key!r} does not address this condition")
        if self.severity != severity_for(self.basis, self.highest_band):
            raise ValueError(
                f"severity {self.severity!r} is not derived from "
                f"basis={self.basis!r} band={self.highest_band!r}"
            )
        if self.last_observed_at < self.opened_at:
            raise ValueError("last_observed_at precedes opened_at")
        if (self.status == "OPEN") != (self.resolved_at is None):
            raise ValueError("resolved_at is set exactly when the status is terminal")
        strangers = sorted(set(self.recipients) - set(self.audience))
        if strangers:
            raise ValueError(f"recipients outside the audience: {strangers}")
        return self


class BudgetNoticeLedger(BaseModel):
    """Every budget notice on file, keyed by notice key."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    notices: dict[str, BudgetThresholdNotice] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _keys_match_rows(self) -> Self:
        """Refuse a ledger whose map key disagrees with the row it holds.

        Raises:
            ValueError: A row is filed under another notice's key.
        """
        for key, notice in self.notices.items():
            if key != notice.notice_key:
                raise ValueError(f"notice filed under {key!r} carries key {notice.notice_key!r}")
        return self


class UpsertOutcome(StrEnum):
    """What one crossing did to the ledger.

    ``CREATED`` and ``ESCALATED`` are the only outcomes that move a notice.
    ``UNCHANGED`` is a repeat of the band already on file; ``RETAINED`` is
    a lower band, or any crossing against a terminal notice, which never
    regresses or reopens it. Either writes only the legacy identifiers it
    brought that the notice did not already hold.
    """

    CREATED = "created"
    ESCALATED = "escalated"
    UNCHANGED = "unchanged"
    RETAINED = "retained"


@dataclass(frozen=True, slots=True)
class NoticeUpsert:
    """The notice on file after an upsert, and how the crossing landed.

    Attributes:
        notice: The notice as it stands after the crossing.
        outcome: What the crossing did; see :class:`UpsertOutcome`.
    """

    notice: BudgetThresholdNotice
    outcome: UpsertOutcome


def apply_crossing(
    ledger: BudgetNoticeLedger, crossing: BudgetCrossing
) -> tuple[BudgetNoticeLedger, NoticeUpsert]:
    """Fold one *crossing* into *ledger* without touching storage.

    Args:
        ledger: The ledger as currently on file.
        crossing: The observation to fold in.

    Returns:
        The ledger after the crossing (the same object when nothing
        changed) and the resulting :class:`NoticeUpsert`.
    """
    key = crossing.notice_key
    existing = ledger.notices.get(key)
    if existing is None:
        notice = BudgetThresholdNotice(
            notice_key=key,
            scope_id=crossing.scope_id,
            axis=crossing.axis,
            basis=crossing.basis,
            contract_digest=crossing.contract_digest,
            highest_band=crossing.band,
            severity=severity_for(crossing.basis, crossing.band),
            observed_value=crossing.observed_value,
            budget_value=crossing.budget_value,
            opened_at=crossing.observed_at,
            last_observed_at=crossing.observed_at,
            audience=crossing.audience,
            provenance=crossing.provenance,
        )
        outcome = UpsertOutcome.CREATED
    else:
        # A lower, late or repeated band never moves the notice, but the
        # legacy identifiers it carries are kept: every source row stays
        # traceable to the one notice it collapsed into.
        added = tuple(ref for ref in crossing.provenance if ref not in existing.provenance)
        if (
            existing.status != "OPEN"
            or _BAND_RANK[crossing.band] <= _BAND_RANK[existing.highest_band]
        ):
            outcome = (
                UpsertOutcome.UNCHANGED
                if existing.status == "OPEN" and crossing.band == existing.highest_band
                else UpsertOutcome.RETAINED
            )
            if not added:
                return ledger, NoticeUpsert(notice=existing, outcome=outcome)
            notice = existing.model_copy(update={"provenance": existing.provenance + added})
        else:
            notice = existing.model_copy(
                update={
                    "highest_band": crossing.band,
                    "severity": severity_for(existing.basis, crossing.band),
                    "observed_value": crossing.observed_value,
                    "budget_value": crossing.budget_value,
                    "revision": existing.revision + 1,
                    "last_observed_at": max(existing.last_observed_at, crossing.observed_at),
                    "provenance": existing.provenance + added,
                }
            )
            outcome = UpsertOutcome.ESCALATED
    updated = BudgetNoticeLedger(notices={**ledger.notices, key: notice})
    return updated, NoticeUpsert(notice=notice, outcome=outcome)


def imported_pause_urns(ledger: BudgetNoticeLedger) -> frozenset[str]:
    """Return every legacy pause a notice on *ledger* was imported from.

    These pauses are notices now, so the pause projection leaves them out:
    nothing appears both as a pause and as a notice.

    Args:
        ledger: The notice ledger.

    Returns:
        The pause URNs carried as notice provenance.
    """
    return frozenset(urn for notice in ledger.notices.values() for urn in notice.provenance)


def notices_path(state_path: Path) -> Path:
    """Return the notice ledger path beside *state_path*.

    The ledger lives under the gitignored local directory: notices are
    runtime observations, not committed project state.

    Args:
        state_path: The repo's ``state.json`` path.

    Returns:
        The ``budget_notices.json`` path under the sibling ``local`` directory.
    """
    return state_path.parent / "local" / "budget_notices.json"


def load_notice_ledger(path: Path) -> BudgetNoticeLedger:
    """Read the notice ledger at *path*; an absent file is an empty ledger.

    Args:
        path: The notice ledger file.

    Returns:
        The validated ledger, empty when the file does not exist.

    Raises:
        pydantic.ValidationError: The file on disk is not a valid ledger.
    """
    if not path.exists():
        return BudgetNoticeLedger()
    return BudgetNoticeLedger.model_validate_json(path.read_bytes())


def rewrite_ledger[T](
    path: Path, change: Callable[[BudgetNoticeLedger], tuple[BudgetNoticeLedger, T]]
) -> T:
    """Apply *change* to the ledger at *path* as one locked read-modify-write.

    Every writer of the ledger goes through here, so a producer's upsert
    and a recipient's disposition serialize on the one lock rather than
    overwriting each other.

    Args:
        path: The notice ledger file (see :func:`notices_path`).
        change: Returns the ledger to keep -- the same object when nothing
            changed, which writes nothing -- and the caller's result.

    Returns:
        What *change* returned beside the ledger.

    Raises:
        eawf.runtime.lock.portalock.LockTimeout: The ledger lock was not
            acquired in time.
        pydantic.ValidationError: The ledger on disk is corrupt.
    """
    with portalock.acquire(path, timeout=_LOCK_TIMEOUT_SECONDS):
        ledger = load_notice_ledger(path)
        updated, result = change(ledger)
        if updated is not ledger:
            atomic_write_json_locked(path, updated.model_dump(mode="json"))
    return result


def upsert_notice(path: Path, crossing: BudgetCrossing) -> NoticeUpsert:
    """Upsert the notice for *crossing* into the ledger at *path*, atomically.

    The load, fold and write run under the ledger's exclusive lock, so
    concurrent producers of the same crossing yield one row at one
    revision, and a crossing that changes nothing writes nothing.

    Args:
        path: The notice ledger file (see :func:`notices_path`).
        crossing: The observation to record.

    Returns:
        The resulting :class:`NoticeUpsert`.

    Raises:
        eawf.runtime.lock.portalock.LockTimeout: The ledger lock was not
            acquired in time.
        pydantic.ValidationError: The ledger on disk is corrupt.
    """
    result = rewrite_ledger(path, lambda ledger: apply_crossing(ledger, crossing))
    logger.info(
        f"upsert_notice scope={crossing.scope_id} band={crossing.band} "
        f"outcome={result.outcome} revision={result.notice.revision}"
    )
    return result


__all__ = [
    "LOCAL_OPERATOR",
    "BudgetAxis",
    "BudgetCrossing",
    "BudgetNoticeLedger",
    "BudgetThresholdNotice",
    "NoticeAction",
    "NoticeBand",
    "NoticeBasis",
    "NoticeHistoryEntry",
    "NoticeSeverity",
    "NoticeStatus",
    "NoticeUpsert",
    "RecipientState",
    "UpsertOutcome",
    "apply_crossing",
    "budget_window_digest",
    "imported_pause_urns",
    "load_notice_ledger",
    "notice_key_for",
    "notices_path",
    "rewrite_ledger",
    "severity_for",
    "upsert_notice",
]
