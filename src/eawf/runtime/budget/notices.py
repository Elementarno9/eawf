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
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Final, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.epoch2.base import NonEmptyStr, StrictNonNegativeInt, StrictPositiveInt
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

#: The metered resource dimension. Tokens are the one axis metered today.
BudgetAxis = Literal["tokens"]

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


def notice_key_for(*, scope_id: str, axis: BudgetAxis, basis: NoticeBasis) -> str:
    """Return the logical identity of the notice for one budget condition.

    A changed basis is a changed budget contract, so it is a new identity;
    the band is deliberately absent so escalation stays on one row.

    Args:
        scope_id: The scope the budget belongs to (a wave id today).
        axis: The metered resource dimension.
        basis: What the budget was set on.

    Returns:
        ``sha256:<hex>`` over the unit-separated identity fields.
    """
    digest = hashlib.sha256(f"{scope_id}\x1f{axis}\x1f{basis}".encode()).hexdigest()
    return f"sha256:{digest}"


def severity_for(basis: NoticeBasis, band: NoticeBand) -> NoticeSeverity:
    """Return the severity a notice of *basis* carries at *band*.

    Args:
        basis: What the budget was set on.
        band: The threshold band the crossing reached.

    Returns:
        The notice severity for that basis and band.
    """
    return _SEVERITY[(basis, band)]


class BudgetCrossing(BaseModel):
    """One observation that a scope's consumption reached a threshold band.

    Attributes:
        scope_id: The scope whose budget was crossed.
        axis: The metered resource dimension.
        basis: What the budget was set on.
        band: The highest band the observation reached.
        observed_value: The consumption observed.
        budget_value: The budget it was measured against.
        observed_at: When the producer read the consumption.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: NonEmptyStr
    axis: BudgetAxis = "tokens"
    basis: NoticeBasis
    band: NoticeBand
    observed_value: StrictNonNegativeInt
    budget_value: StrictNonNegativeInt
    observed_at: UtcDatetime

    @property
    def notice_key(self) -> str:
        """The identity of the notice this crossing upserts."""
        return notice_key_for(scope_id=self.scope_id, axis=self.axis, basis=self.basis)


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
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    notice_key: str = Field(pattern=_NOTICE_KEY_PATTERN)
    scope_id: NonEmptyStr
    axis: BudgetAxis
    basis: NoticeBasis
    blocking: Literal[False] = False
    highest_band: NoticeBand
    severity: NoticeSeverity
    observed_value: StrictNonNegativeInt
    budget_value: StrictNonNegativeInt
    status: NoticeStatus = "OPEN"
    revision: StrictPositiveInt = 1
    opened_at: UtcDatetime
    last_observed_at: UtcDatetime
    resolved_at: UtcDatetime | None = None

    @model_validator(mode="after")
    def _check_derived_fields(self) -> Self:
        """Keep the key, severity and timestamps consistent with the facts.

        Raises:
            ValueError: The key does not address this condition, the
                severity is not the one the basis and band derive, the
                last observation precedes the opening, or ``resolved_at``
                disagrees with whether the status is terminal.
        """
        expected_key = notice_key_for(scope_id=self.scope_id, axis=self.axis, basis=self.basis)
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

    ``CREATED`` and ``ESCALATED`` are the only outcomes that write.
    ``UNCHANGED`` is a repeat of the band already on file; ``RETAINED`` is
    a lower band, or any crossing against a terminal notice, which never
    regresses or reopens it.
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
            highest_band=crossing.band,
            severity=severity_for(crossing.basis, crossing.band),
            observed_value=crossing.observed_value,
            budget_value=crossing.budget_value,
            opened_at=crossing.observed_at,
            last_observed_at=crossing.observed_at,
        )
        outcome = UpsertOutcome.CREATED
    elif existing.status != "OPEN" or _BAND_RANK[crossing.band] < _BAND_RANK[existing.highest_band]:
        return ledger, NoticeUpsert(notice=existing, outcome=UpsertOutcome.RETAINED)
    elif crossing.band == existing.highest_band:
        return ledger, NoticeUpsert(notice=existing, outcome=UpsertOutcome.UNCHANGED)
    else:
        notice = existing.model_copy(
            update={
                "highest_band": crossing.band,
                "severity": severity_for(existing.basis, crossing.band),
                "observed_value": crossing.observed_value,
                "budget_value": crossing.budget_value,
                "revision": existing.revision + 1,
                "last_observed_at": max(existing.last_observed_at, crossing.observed_at),
            }
        )
        outcome = UpsertOutcome.ESCALATED
    updated = BudgetNoticeLedger(notices={**ledger.notices, key: notice})
    return updated, NoticeUpsert(notice=notice, outcome=outcome)


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


def upsert_notice(path: Path, crossing: BudgetCrossing) -> NoticeUpsert:
    """Upsert the notice for *crossing* into the ledger at *path*, atomically.

    The load, fold and write run under the ledger's exclusive lock, so
    concurrent producers of the same crossing yield one row at one
    revision, and only a created or escalated notice is written.

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
    with portalock.acquire(path, timeout=_LOCK_TIMEOUT_SECONDS):
        ledger = load_notice_ledger(path)
        updated, result = apply_crossing(ledger, crossing)
        if result.outcome in (UpsertOutcome.CREATED, UpsertOutcome.ESCALATED):
            atomic_write_json_locked(path, updated.model_dump(mode="json"))
    logger.info(
        f"upsert_notice scope={crossing.scope_id} band={crossing.band} "
        f"outcome={result.outcome} revision={result.notice.revision}"
    )
    return result


__all__ = [
    "BudgetAxis",
    "BudgetCrossing",
    "BudgetNoticeLedger",
    "BudgetThresholdNotice",
    "NoticeBand",
    "NoticeBasis",
    "NoticeSeverity",
    "NoticeStatus",
    "NoticeUpsert",
    "UpsertOutcome",
    "apply_crossing",
    "load_notice_ledger",
    "notice_key_for",
    "notices_path",
    "severity_for",
    "upsert_notice",
]
