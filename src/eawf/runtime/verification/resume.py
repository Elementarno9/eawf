"""Resume a timed-out gate leg from the residue its progress manifest proves.

A leg that times out has usually finished most of its obligations. Its
:class:`~eawf.runtime.verification.progress.ProgressManifest` names which
ones passed, so a resumed leg only has to run the rest. That is sound only
while the manifest describes the leg being resumed: it must sit at the same
freshness key the leg has now, it must record a timeout, and it must
enumerate its obligations. Anything short of that yields a full-restart
plan, never a guessed residue.

A resumed leg runs under its own freshness key. :func:`with_residue` stamps
the residue digest into the gate's freshness facts, so the resumed leg
claims a new key and its receipt records exactly which residue it proved.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Collection
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, model_validator

from eawf.kernel.delivery.receipts import FreshnessDigestStr
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.runtime.verification.progress import (
    LegOutcome,
    ObligationDisposition,
    ObligationIdStr,
    ObligationRecord,
    ProgressManifest,
    ResidueSeed,
)
from eawf.workflow.audit_dsl.models import CheckSpec, GateFreshnessInput

logger = logging.getLogger(__name__)


class ResumeKind(StrEnum):
    """What a resume plan lets the runner do."""

    RESIDUE = "residue"
    FULL_RESTART = "full_restart"


class ResumeReason(StrEnum):
    """Why a plan got its kind; only a proven residue permits a partial run."""

    RESIDUE_PROVEN = "residue_proven"
    MISSING = "missing"
    STALE = "stale"
    NOT_TIMED_OUT = "not_timed_out"
    OPAQUE = "opaque"
    NO_RESIDUE = "no_residue"


_REASON_KIND: Final = {
    reason: ResumeKind.RESIDUE if reason is ResumeReason.RESIDUE_PROVEN else ResumeKind.FULL_RESTART
    for reason in ResumeReason
}


def residue_digest(
    *,
    source_freshness_key: str,
    collection_digest: str,
    residue: tuple[str, ...],
) -> str:
    """Return the 64-hex digest identifying one residue of one timed-out leg.

    Args:
        source_freshness_key: The timed-out leg's freshness key.
        collection_digest: Digest of the collection the residue came from.
        residue: The obligations to rerun, in collection order.

    Returns:
        A bare SHA-256 hex digest, the shape a gate receipt stores.
    """
    body = json.dumps(
        {
            "collection_digest": collection_digest,
            "residue": list(residue),
            "source_freshness_key": source_freshness_key,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


class ResumePlan(BaseModel):
    """How a timed-out leg continues: its residue, or an explicit full restart."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_freshness_key: FreshnessDigestStr
    kind: ResumeKind
    reason: ResumeReason
    collection_digest: Sha256DigestStr | None = None
    residue: tuple[ObligationIdStr, ...] = ()
    carried: tuple[ObligationRecord, ...] = ()
    residue_digest: FreshnessDigestStr | None = None

    @model_validator(mode="after")
    def _shape_follows_kind(self) -> Self:
        """Require a residue plan to be complete and a restart plan to be empty.

        Raises:
            ValueError: The kind disagrees with the reason; a residue plan
                lacks its collection, residue or matching digest; a restart
                plan carries any of them; or a carried obligation is also
                in the residue.
        """
        if self.kind is not _REASON_KIND[self.reason]:
            raise ValueError(
                f"reason {self.reason.value} implies kind {_REASON_KIND[self.reason].value}"
            )
        if self.kind is ResumeKind.FULL_RESTART:
            if self.collection_digest or self.residue or self.carried or self.residue_digest:
                raise ValueError("a full-restart plan carries no residue")
            return self
        if self.collection_digest is None or not self.residue:
            raise ValueError("a residue plan needs its collection digest and a non-empty residue")
        expected = residue_digest(
            source_freshness_key=self.source_freshness_key,
            collection_digest=self.collection_digest,
            residue=self.residue,
        )
        if self.residue_digest != expected:
            raise ValueError("residue_digest does not match the residue")
        overlap = sorted({record.obligation_id for record in self.carried} & set(self.residue))
        if overlap:
            raise ValueError(f"obligations both carried and rerun: {overlap}")
        return self

    def seed(self) -> ResidueSeed:
        """Return what the resumed leg's publisher inherits.

        Raises:
            ValueError: The plan is a full restart.
        """
        if self.kind is not ResumeKind.RESIDUE or self.collection_digest is None:
            raise ValueError("only a residue plan seeds a resumed leg")
        return ResidueSeed(
            resumed_from=self.source_freshness_key,
            collection_digest=self.collection_digest,
            residue=self.residue,
            carried=self.carried,
        )


def _restart(source_freshness_key: str, reason: ResumeReason) -> ResumePlan:
    logger.info(f"plan_resume kind='full_restart' reason={reason.value!r}")
    return ResumePlan(
        source_freshness_key=source_freshness_key,
        kind=ResumeKind.FULL_RESTART,
        reason=reason,
    )


def plan_resume(
    manifest: ProgressManifest | None,
    *,
    expected_freshness_key: str,
    invalidated: Collection[str] = (),
) -> ResumePlan:
    """Plan how the leg at *expected_freshness_key* continues after a timeout.

    Args:
        manifest: The claimed manifest read for the leg, or ``None`` when
            there is none.
        expected_freshness_key: The key the leg has now. A manifest taken at
            another key described other inputs and is never trusted.
        invalidated: Obligations whose inputs changed since they passed;
            each is rerun even though the manifest records a pass.

    Returns:
        A residue plan when the manifest proves one; otherwise a
        full-restart plan naming why.

    Raises:
        ValueError: An invalidated obligation is not in the manifest's
            collection, which means the caller is describing another leg.
    """
    if manifest is None:
        return _restart(expected_freshness_key, ResumeReason.MISSING)
    if manifest.leg.freshness_key != expected_freshness_key:
        return _restart(expected_freshness_key, ResumeReason.STALE)
    if manifest.outcome is not LegOutcome.TIMED_OUT:
        return _restart(expected_freshness_key, ResumeReason.NOT_TIMED_OUT)
    if manifest.collected is None or manifest.collection_digest is None:
        return _restart(expected_freshness_key, ResumeReason.OPAQUE)
    unknown = sorted(set(invalidated) - set(manifest.collected))
    if unknown:
        raise ValueError(f"invalidated obligations are not in the collection: {unknown}")
    stale = set(invalidated)
    pending = set(manifest.pending())
    residue = tuple(item for item in manifest.collected if item in stale or item in pending)
    if not residue:
        return _restart(expected_freshness_key, ResumeReason.NO_RESIDUE)
    carried = tuple(
        record.model_copy(update={"carried": True})
        for record in manifest.obligations
        if record.disposition is ObligationDisposition.PASS and record.obligation_id not in stale
    )
    logger.info(
        f"plan_resume kind='residue' collected={len(manifest.collected)} "
        f"residue={len(residue)} carried={len(carried)}"
    )
    return ResumePlan(
        source_freshness_key=expected_freshness_key,
        kind=ResumeKind.RESIDUE,
        reason=ResumeReason.RESIDUE_PROVEN,
        collection_digest=manifest.collection_digest,
        residue=residue,
        carried=carried,
        residue_digest=residue_digest(
            source_freshness_key=expected_freshness_key,
            collection_digest=manifest.collection_digest,
            residue=residue,
        ),
    )


def with_residue(spec: CheckSpec, plan: ResumePlan) -> CheckSpec:
    """Return *spec* bound to *plan*'s residue, so it claims its own key.

    Args:
        spec: The timed-out leg's gate spec.
        plan: A residue plan for that leg.

    Returns:
        A copy whose freshness facts carry the residue digest.

    Raises:
        ValueError: *plan* is a full restart.
    """
    if plan.kind is not ResumeKind.RESIDUE or plan.residue_digest is None:
        raise ValueError("only a residue plan can bind a resumed leg")
    freshness = spec.freshness or GateFreshnessInput()
    return spec.model_copy(
        update={
            "freshness": freshness.model_copy(
                update={"residual_manifest_digest": plan.residue_digest}
            )
        }
    )


def residue_proven(manifest: ProgressManifest, plan: ResumePlan) -> bool:
    """Return whether a resumed leg's manifest proves its whole collection passed.

    A resumed command that exits zero proved only what it ran. Together
    with the carried passes that is the whole collection only when the
    collection is the one the plan was made from and nothing is pending.

    Args:
        manifest: The resumed leg's terminal manifest.
        plan: The residue plan the leg ran under.

    Returns:
        ``True`` when every collected obligation is proved passing.
    """
    return (
        plan.kind is ResumeKind.RESIDUE
        and manifest.collection_digest == plan.collection_digest
        and manifest.collected is not None
        and not manifest.pending()
    )


__all__ = [
    "ResumeKind",
    "ResumePlan",
    "ResumeReason",
    "plan_resume",
    "residue_digest",
    "residue_proven",
    "with_residue",
]
