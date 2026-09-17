"""Live quarantine of a runtime tuple, and the rollback that follows it.

A quarantine is not a verdict recorded after the fact: the trigger fires,
the tuple leaves service in the same call, and everything that can only
run on that tuple leaves service with it. The nine triggers are the
closed set on :class:`QuarantineTrigger`, and :data:`TRIGGER_FAILURE_CODE`
maps every one of them to the failure code its stage record is filed
under, so a quarantined tuple always names the check that produced it.

Disablement follows dependency rather than name. A profile is disabled
when it is bound to the quarantined driver; a route is disabled only once
every profile it allows has gone, because a route that still has one
eligible profile never required the quarantined tuple in the first place.

Rollback is deliberately the narrower verb. It selects the newest pin a
profile still has that is not itself quarantined, revoked or expired, and
what it changes is the next compiled Run: a Run already compiled is
sealed under authority the compiler resolved, so re-pointing it in flight
would run it under a contract nobody compiled. A profile with no
admissible pin stays disabled, which is the fail-safe direction -- an
absent pin is never a reason to select a tuple nobody certified.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from datetime import datetime
from typing import Final, Literal, Protocol, Self

from pydantic import model_validator

from eawf.kernel.runtime.certification import (
    CertificationFailureCode,
    ConformanceStageRecord,
    DriverCertification,
    QuarantineTrigger,
)
from eawf.kernel.runtime.provider import (
    AgentProviderProfile,
    Digest,
    DriverManifestUrn,
    ProviderProfileId,
    RoutePolicy,
    RoutePolicyId,
    RuntimeRecord,
)
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The failure code each trigger is recorded under. The map is total over
#: :class:`QuarantineTrigger` so a quarantine can never be filed without a
#: code, and several triggers share one code where they are the same
#: check failing: a canary failure and a denied-tool escape are both the
#: containment probe set giving way.
TRIGGER_FAILURE_CODE: Final[dict[QuarantineTrigger, CertificationFailureCode]] = {
    QuarantineTrigger.PROTOCOL_DRIFT: CertificationFailureCode.PROTOCOL_VERSION_MISMATCH,
    QuarantineTrigger.SCHEMA_DRIFT: CertificationFailureCode.SCHEMA_MISMATCH,
    QuarantineTrigger.WRONG_AUTH: CertificationFailureCode.AUTH_KIND_MISMATCH,
    QuarantineTrigger.AMBIENT_SOURCE_LEAKAGE: CertificationFailureCode.SECRET_MATERIAL_OBSERVABLE,
    QuarantineTrigger.DENIED_TOOL_ESCAPE: CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
    QuarantineTrigger.TERMINAL_OUTCOME_ANOMALY: CertificationFailureCode.CAPABILITY_NOT_OBSERVED,
    QuarantineTrigger.CORRUPT_RESUME: CertificationFailureCode.EVENT_CODEC_MISMATCH,
    QuarantineTrigger.UNEXPLAINED_USAGE: CertificationFailureCode.CAPABILITY_EVIDENCE_UNCOVERED,
    QuarantineTrigger.CANARY_FAILURE: CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE,
}

#: What a rollback changes. A compiled Run is sealed, so the only Run a
#: rollback can reach is one that has not been compiled yet.
RollbackHorizon = Literal["next_compiled_run"]


class DependentDisablement(RuntimeRecord):
    """The profiles and routes one quarantine takes out of service.

    Both tuples carry disabled copies rather than ids alone, so the caller
    applies the daemon's decision instead of re-deriving it.
    """

    profiles: tuple[AgentProviderProfile, ...] = ()
    routes: tuple[RoutePolicy, ...] = ()

    @property
    def profile_ids(self) -> tuple[ProviderProfileId, ...]:
        """Return the ids of the disabled profiles, in input order."""
        return tuple(profile.profile_id for profile in self.profiles)

    @property
    def route_ids(self) -> tuple[RoutePolicyId, ...]:
        """Return the ids of the disabled routes, in input order."""
        return tuple(route.route_id for route in self.routes)


class LastKnownGoodPin(RuntimeRecord):
    """One profile's certification, pinned when its certify stage passed.

    The pin carries the whole certification rather than a reference to
    one, because what rollback has to decide is whether the pinned tuple
    is still good, and a reference cannot answer that on its own.
    """

    schema_version: Literal["last-known-good-pin/v1"] = "last-known-good-pin/v1"
    profile_id: ProviderProfileId
    tuple_digest: Digest
    certification: DriverCertification
    pinned_at: UtcDatetime


class RollbackSelection(RuntimeRecord):
    """Which pin a rollback selected, or why none was admissible."""

    pin: LastKnownGoodPin | None = None
    refusal: CertificationFailureCode | None = None
    rejected: tuple[Digest, ...] = ()

    @model_validator(mode="after")
    def _selection_or_refusal(self) -> Self:
        """Require exactly one of a selected pin and a refusal.

        Raises:
            ValueError: Both are present, or neither is, either of which
                would leave the caller without a decision.
        """
        if (self.pin is None) == (self.refusal is None):
            raise ValueError("a selection carries either a pin or a refusal, never both or neither")
        return self


class PinLedger(Protocol):
    """The per-profile store of last-known-good pins, newest appended last."""

    def pin(self, *, pin: LastKnownGoodPin) -> None:
        """Append one pin for one profile."""

    def pins(self, *, profile_id: ProviderProfileId) -> tuple[LastKnownGoodPin, ...]:
        """Return every pin of one profile, in append order."""


class MemoryPinLedger:
    """Pin ledger held for the life of one process.

    The ledger lives beside the canary lease on the daemon's cached
    runner, so a certify and the rollback that later reads its pin see the
    same rows. Pins do not survive a restart, and that is the fail-safe
    direction: a profile whose pin is gone stays disabled rather than
    binding a tuple nothing in this process certified.
    """

    def __init__(self) -> None:
        """Start with no pin for any profile."""
        self._rows: dict[str, list[LastKnownGoodPin]] = {}

    def pin(self, *, pin: LastKnownGoodPin) -> None:
        """Append one pin for one profile.

        Args:
            pin: The certification to record as that profile's newest
                known-good.
        """
        self._rows.setdefault(pin.profile_id, []).append(pin)

    def pins(self, *, profile_id: ProviderProfileId) -> tuple[LastKnownGoodPin, ...]:
        """Return every pin of one profile, in append order.

        Args:
            profile_id: The profile to read.

        Returns:
            The profile's pins, oldest first; empty when it has none.
        """
        return tuple(self._rows.get(profile_id, ()))


def failure_code_of(trigger: QuarantineTrigger) -> CertificationFailureCode:
    """Return the failure code *trigger* is recorded under.

    Args:
        trigger: The trigger that fired.

    Returns:
        The code its stage record carries.

    Raises:
        KeyError: *trigger* is not a member of the closed trigger set, so
            no check could have produced it.
    """
    return TRIGGER_FAILURE_CODE[trigger]


def is_quarantined(records: Sequence[ConformanceStageRecord]) -> bool:
    """Report whether a tuple's stage history leaves it quarantined.

    Refused stages are skipped because a refusal means nothing ran. What
    decides is the newest stage that did run: a failed rollback stage is
    the quarantine record itself, and any later stage is the tuple being
    walked back into service.

    Args:
        records: Every stage record of one tuple, in append order.

    Returns:
        ``True`` while the newest effective record is a failed rollback.
    """
    effective = [row for row in records if row.outcome != "refused"]
    if not effective:
        return False
    newest = effective[-1]
    return newest.stage == "rollback" and newest.outcome == "failed"


def disable_dependents(
    *,
    manifest_ref: DriverManifestUrn,
    profiles: Sequence[AgentProviderProfile],
    routes: Sequence[RoutePolicy],
) -> DependentDisablement:
    """Return the profiles and routes a quarantine of *manifest_ref* removes.

    Args:
        manifest_ref: The driver of the quarantined tuple.
        profiles: The declared profiles, whatever their driver.
        routes: The declared routes, whatever their profiles.

    Returns:
        Disabled copies of the profiles bound to that driver, and of the
        routes those profiles leave with no allowed profile at all. A
        route keeping one enabled profile is untouched: it never required
        the quarantined tuple.
    """
    bound = tuple(
        profile
        for profile in profiles
        if profile.driver_ref == manifest_ref and not profile.disabled
    )
    gone = {profile.profile_id for profile in bound}
    gone.update(profile.profile_id for profile in profiles if profile.disabled)
    stranded = tuple(
        route
        for route in routes
        if not route.disabled and all(allowed in gone for allowed in route.allowed_profiles)
    )
    logger.info(
        f"disable_dependents driver={manifest_ref!r} profiles={len(bound)} routes={len(stranded)}"
    )
    return DependentDisablement(
        profiles=tuple(reissue_profile(profile, disabled=True) for profile in bound),
        routes=tuple(_disabled_route(route) for route in stranded),
    )


def reissue_profile(profile: AgentProviderProfile, *, disabled: bool) -> AgentProviderProfile:
    """Return *profile* at the next revision, enabled or disabled.

    The revision moves because a reader that cannot tell the reissued copy
    from the original cannot tell whether the decision was applied.

    Args:
        profile: The declared profile to reissue.
        disabled: Whether the reissued copy is out of service.

    Returns:
        The reissued profile.
    """
    document = profile.model_dump(mode="json")
    document.update(disabled=disabled, revision=profile.revision + 1)
    return AgentProviderProfile.model_validate(document)


def select_last_known_good(
    *,
    pins: Sequence[LastKnownGoodPin],
    quarantined: Collection[Digest],
    now: datetime,
) -> RollbackSelection:
    """Return the newest pin a rollback may bind, or why none is left.

    Pins are examined newest first and the first admissible one wins. A
    pin is admissible only while its tuple is out of quarantine and its
    certification is still verified and unexpired, which is what keeps a
    rollback from selecting an uncertified tuple: the refusal is the
    outcome, never a weaker binding.

    Args:
        pins: The profile's pins, oldest first.
        quarantined: Digests of the tuples currently in quarantine.
        now: The instant expiry is judged against.

    Returns:
        The selected pin, or a refusal naming
        :attr:`CertificationFailureCode.EVIDENCE_EXPIRED` when expiry was
        the only thing standing in the way and
        :attr:`CertificationFailureCode.NO_LAST_KNOWN_GOOD_PIN` otherwise.
    """
    rejected: list[Digest] = []
    expired_seen = False
    for candidate in reversed(tuple(pins)):
        reason = _pin_refusal(candidate, quarantined=quarantined, now=now)
        if reason is None:
            return RollbackSelection(pin=candidate, rejected=tuple(rejected))
        expired_seen = expired_seen or reason is CertificationFailureCode.EVIDENCE_EXPIRED
        rejected.append(candidate.tuple_digest)
    refusal = (
        CertificationFailureCode.EVIDENCE_EXPIRED
        if expired_seen
        else CertificationFailureCode.NO_LAST_KNOWN_GOOD_PIN
    )
    logger.info(f"select_last_known_good refused reason={refusal.value!r} pins={len(rejected)}")
    return RollbackSelection(refusal=refusal, rejected=tuple(rejected))


def _pin_refusal(
    pin: LastKnownGoodPin,
    *,
    quarantined: Collection[Digest],
    now: datetime,
) -> CertificationFailureCode | None:
    """Return why *pin* is not good any more, or ``None`` when it still is."""
    certification = pin.certification
    if pin.tuple_digest in quarantined or certification.install_trust == "quarantined":
        return CertificationFailureCode.NO_LAST_KNOWN_GOOD_PIN
    if certification.overall_status != "verified" or certification.revoked_at is not None:
        return CertificationFailureCode.NO_LAST_KNOWN_GOOD_PIN
    if certification.expires_at <= now:
        return CertificationFailureCode.EVIDENCE_EXPIRED
    return None


def _disabled_route(route: RoutePolicy) -> RoutePolicy:
    """Return *route* disabled, at the next revision."""
    document = route.model_dump(mode="json")
    document.update(disabled=True, revision=route.revision + 1)
    return RoutePolicy.model_validate(document)


__all__ = [
    "TRIGGER_FAILURE_CODE",
    "DependentDisablement",
    "LastKnownGoodPin",
    "MemoryPinLedger",
    "PinLedger",
    "RollbackHorizon",
    "RollbackSelection",
    "disable_dependents",
    "failure_code_of",
    "is_quarantined",
    "reissue_profile",
    "select_last_known_good",
]
