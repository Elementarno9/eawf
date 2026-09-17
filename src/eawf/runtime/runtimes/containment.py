"""The containment probe set the canary stage runs, and its receipts.

A canary is evidence of containment only when every axis was attempted.
The four axes and the attempts under each are closed sets, so a run that
exercised three of them is detected as incomplete rather than read as a
pass: :data:`AXIS_ATTEMPTS` is the whole obligation and
:func:`run_containment_probes` grades a reported run against it.

Nothing here executes a call. The attempts happen inside the canary Run,
which is the only place they are allowed to happen, and this module
grades what that Run reported. That keeps the probe set structurally
incapable of running against a production Run.

:class:`DeniedCallReceipt` is the artifact a passed probe leaves behind.
It has no field that can hold a returned value and no free-text field at
all: every member is a closed enum, a typed reference, or a timestamp.
"No secret material observable" is therefore a property of the record
shape rather than of a scanner that has to recognise a secret, and an
attempt that did return bytes yields no receipt at all.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Self

from pydantic import Field, StrictBool, StrictInt, model_validator

from eawf.kernel.runtime.certification import CertificationFailureCode
from eawf.kernel.runtime.provider import ArtifactUrn, RuntimeRecord
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


class ContainmentAxis(StrEnum):
    """An axis containment is probed along."""

    ENVIRONMENT = "environment"
    FILESYSTEM = "filesystem"
    PROCESS = "process"
    NETWORK = "network"


class ContainmentAttempt(StrEnum):
    """One call the probe set makes, named by what it reaches for.

    Every member names a target symbolically. No attempt carries a host
    path, a variable value, or an address, so the enumeration itself
    cannot leak anything about the machine the canary ran on.
    """

    VARIABLE_OUTSIDE_ALLOWLIST = "variable_outside_allowlist"
    AMBIENT_ENVIRONMENT = "ambient_environment"
    CANONICAL_GIT = "canonical_git"
    DAEMON_STORAGE = "daemon_storage"
    WRITE_AHEAD_LOG = "write_ahead_log"
    OUTSIDE_WRITABLE_ROOTS = "outside_writable_roots"
    UNREGISTERED_EXECUTABLE = "unregistered_executable"
    HOST_PROCESS_METADATA = "host_process_metadata"
    DIRECT_EGRESS = "direct_egress"
    CREDENTIAL_VALUE = "credential_value"


class DenialReason(StrEnum):
    """Why a call was denied. Closed, so a receipt carries no prose."""

    OUTSIDE_ALLOWLIST = "outside_allowlist"
    AMBIENT_INHERITANCE_REFUSED = "ambient_inheritance_refused"
    SCOPE_ESCAPE = "scope_escape"
    NOT_IN_COMPONENT_REGISTRY = "not_in_component_registry"
    EGRESS_OUTSIDE_POLICY = "egress_outside_policy"
    CREDENTIAL_REFERENCE_ONLY = "credential_reference_only"


class CredentialFact(StrEnum):
    """The only facts a credential may ever disclose."""

    KIND = "kind"
    HEALTH = "health"
    EXPIRY = "expiry"
    PROVENANCE = "provenance"


#: The axes, in the order the probe set reports them.
CONTAINMENT_AXES: Final[tuple[ContainmentAxis, ...]] = tuple(ContainmentAxis)

#: Every axis and the attempts it obliges. A canary owes one outcome per
#: attempt; the mapping is the whole obligation.
AXIS_ATTEMPTS: Final[Mapping[ContainmentAxis, tuple[ContainmentAttempt, ...]]] = {
    ContainmentAxis.ENVIRONMENT: (
        ContainmentAttempt.VARIABLE_OUTSIDE_ALLOWLIST,
        ContainmentAttempt.AMBIENT_ENVIRONMENT,
    ),
    ContainmentAxis.FILESYSTEM: (
        ContainmentAttempt.CANONICAL_GIT,
        ContainmentAttempt.DAEMON_STORAGE,
        ContainmentAttempt.WRITE_AHEAD_LOG,
        ContainmentAttempt.OUTSIDE_WRITABLE_ROOTS,
    ),
    ContainmentAxis.PROCESS: (
        ContainmentAttempt.UNREGISTERED_EXECUTABLE,
        ContainmentAttempt.HOST_PROCESS_METADATA,
    ),
    ContainmentAxis.NETWORK: (
        ContainmentAttempt.DIRECT_EGRESS,
        ContainmentAttempt.CREDENTIAL_VALUE,
    ),
}

#: Every attempt the probe set owes, in axis order.
CONTAINMENT_ATTEMPTS: Final[tuple[ContainmentAttempt, ...]] = tuple(
    attempt for axis in CONTAINMENT_AXES for attempt in AXIS_ATTEMPTS[axis]
)

_AXIS_OF_ATTEMPT: Final[Mapping[ContainmentAttempt, ContainmentAxis]] = {
    attempt: axis for axis, attempts in AXIS_ATTEMPTS.items() for attempt in attempts
}


def axis_of(attempt: ContainmentAttempt) -> ContainmentAxis:
    """Return the axis *attempt* belongs to.

    Args:
        attempt: The attempted call.

    Returns:
        The axis :data:`AXIS_ATTEMPTS` files it under.

    Raises:
        KeyError: *attempt* is not a member of the closed attempt set.
    """
    return _AXIS_OF_ATTEMPT[attempt]


class ContainmentCallOutcome(RuntimeRecord):
    """What the canary Run reported about one attempted call.

    ``returned_bytes`` is the count of payload bytes the call handed
    back, never the bytes themselves: a count is enough to decide whether
    anything was observable and cannot carry what was observed.
    """

    attempt: ContainmentAttempt
    denied: StrictBool
    denial_reason: DenialReason | None = None
    returned_bytes: Annotated[StrictInt, Field(ge=0)] = 0
    disclosed_facts: tuple[CredentialFact, ...] = ()

    @model_validator(mode="after")
    def _outcome_carries_its_reason(self) -> Self:
        """Bind the denial reason and the disclosure to the outcome.

        Raises:
            ValueError: A denied call names no reason, an allowed call
                names one, a disclosure is reported for an attempt that
                asks for no credential, or a fact is disclosed twice.
        """
        if self.denied and self.denial_reason is None:
            raise ValueError("a denied call requires denial_reason")
        if not self.denied and self.denial_reason is not None:
            raise ValueError("an allowed call carries no denial_reason")
        if self.disclosed_facts and self.attempt is not ContainmentAttempt.CREDENTIAL_VALUE:
            raise ValueError(
                f"{self.attempt.value!r} asks for no credential, so it discloses no facts"
            )
        if len(set(self.disclosed_facts)) != len(self.disclosed_facts):
            raise ValueError("disclosed_facts names a fact more than once")
        return self

    @property
    def observable(self) -> bool:
        """Whether the call let anything through that a caller could read."""
        return self.returned_bytes > 0


class DeniedCallReceipt(RuntimeRecord):
    """The artifact one passed containment probe leaves behind.

    Every field is a closed enum, a typed reference, or a timestamp, so
    the receipt has nowhere to put a value, a path, or a message.
    """

    axis: ContainmentAxis
    attempt: ContainmentAttempt
    denial_reason: DenialReason
    disclosed_facts: tuple[CredentialFact, ...] = ()
    observed_at: UtcDatetime
    evidence_ref: ArtifactUrn


class ContainmentProbeResult(RuntimeRecord):
    """How one canary's containment run graded, and what it produced."""

    receipts: tuple[DeniedCallReceipt, ...]
    passed: StrictBool
    failure_code: CertificationFailureCode | None = None
    escaped: tuple[ContainmentAttempt, ...] = ()
    observed: tuple[ContainmentAttempt, ...] = ()
    skipped: tuple[ContainmentAttempt, ...] = ()

    @model_validator(mode="after")
    def _grade_matches_its_evidence(self) -> Self:
        """Bind the grade to the receipts and the failure code.

        Raises:
            ValueError: A pass does not carry one receipt per attempt or
                carries a failure, or a failure names no code and no
                failing attempt.
        """
        covered = tuple(row.attempt for row in self.receipts)
        if len(set(covered)) != len(covered):
            raise ValueError("receipts name an attempt more than once")
        failures = (*self.escaped, *self.observed, *self.skipped)
        if self.passed:
            if set(covered) != set(CONTAINMENT_ATTEMPTS):
                raise ValueError("a passed probe set carries one receipt per attempt")
            if self.failure_code is not None or failures:
                raise ValueError("a passed probe set carries no failure")
            return self
        if self.failure_code is None or not failures:
            raise ValueError("a failed probe set requires a failure code and a failing attempt")
        return self

    def covered_axes(self) -> tuple[ContainmentAxis, ...]:
        """Return the axes that produced at least one denied-call receipt.

        Returns:
            The covered axes in :data:`CONTAINMENT_AXES` order. A canary
            that skipped an axis is missing from this tuple, which is
            what makes the skip visible instead of silent.
        """
        seen = {axis_of(row.attempt) for row in self.receipts}
        return tuple(axis for axis in CONTAINMENT_AXES if axis in seen)


def _graded(
    outcomes: Mapping[ContainmentAttempt, ContainmentCallOutcome],
) -> tuple[tuple[ContainmentAttempt, ...], ...]:
    """Return the escaped, observable and skipped attempts of *outcomes*."""
    escaped = tuple(
        attempt
        for attempt in CONTAINMENT_ATTEMPTS
        if attempt in outcomes and not outcomes[attempt].denied
    )
    observed = tuple(
        attempt
        for attempt in CONTAINMENT_ATTEMPTS
        if attempt in outcomes and outcomes[attempt].denied and outcomes[attempt].observable
    )
    skipped = tuple(attempt for attempt in CONTAINMENT_ATTEMPTS if attempt not in outcomes)
    return escaped, observed, skipped


def _failure_code(
    *,
    escaped: tuple[ContainmentAttempt, ...],
    observed: tuple[ContainmentAttempt, ...],
    skipped: tuple[ContainmentAttempt, ...],
) -> CertificationFailureCode | None:
    """Return the gravest failure of one probe run, or ``None``.

    An escape is graded ahead of an observation because containment
    itself gave way; an observation through a denial is the narrower
    fault. A skipped axis is last: it is missing evidence rather than
    evidence of a fault, and it is reported as uncovered for that reason.
    """
    if escaped:
        return CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE
    if observed:
        return CertificationFailureCode.SECRET_MATERIAL_OBSERVABLE
    if skipped:
        return CertificationFailureCode.CAPABILITY_EVIDENCE_UNCOVERED
    return None


def run_containment_probes(
    *,
    outcomes: Mapping[ContainmentAttempt, ContainmentCallOutcome],
    evidence_ref: ArtifactUrn,
    observed_at: UtcDatetime,
) -> ContainmentProbeResult:
    """Grade what one canary reported about the containment probe set.

    Args:
        outcomes: What the canary Run reported per attempted call. An
            attempt missing from the mapping was not exercised.
        evidence_ref: The artifact the canary's receipts are filed under.
        observed_at: When the canary reported the run.

    Returns:
        The graded result. It passes only when every attempt in
        :data:`CONTAINMENT_ATTEMPTS` was denied and returned nothing, and
        it then carries one :class:`DeniedCallReceipt` per attempt.

    Raises:
        ValueError: An outcome is filed under an attempt other than its
            own, which would grade one call under another's obligation.
    """
    for attempt, outcome in outcomes.items():
        if outcome.attempt is not attempt:
            raise ValueError(
                f"outcome for {outcome.attempt.value!r} is filed under {attempt.value!r}"
            )
    escaped, observed, skipped = _graded(outcomes)
    failed = {*escaped, *observed, *skipped}
    receipts = tuple(
        DeniedCallReceipt(
            axis=axis_of(attempt),
            attempt=attempt,
            denial_reason=_reason_of(outcomes[attempt]),
            disclosed_facts=outcomes[attempt].disclosed_facts,
            observed_at=observed_at,
            evidence_ref=evidence_ref,
        )
        for attempt in CONTAINMENT_ATTEMPTS
        if attempt not in failed
    )
    code = _failure_code(escaped=escaped, observed=observed, skipped=skipped)
    logger.info(
        f"run_containment_probes receipts={len(receipts)} escaped={len(escaped)} "
        f"observed={len(observed)} skipped={len(skipped)}"
    )
    return ContainmentProbeResult(
        receipts=receipts,
        passed=code is None,
        failure_code=code,
        escaped=escaped,
        observed=observed,
        skipped=skipped,
    )


def _reason_of(outcome: ContainmentCallOutcome) -> DenialReason:
    """Return the denial reason of a denied *outcome*.

    Args:
        outcome: A denied call outcome.

    Returns:
        The reason it was denied.

    Raises:
        ValueError: *outcome* was not denied, so it has no reason and no
            receipt may be written for it.
    """
    if outcome.denial_reason is None:
        raise ValueError(f"{outcome.attempt.value!r} was not denied, so it leaves no receipt")
    return outcome.denial_reason


__all__ = [
    "AXIS_ATTEMPTS",
    "CONTAINMENT_ATTEMPTS",
    "CONTAINMENT_AXES",
    "ContainmentAttempt",
    "ContainmentAxis",
    "ContainmentCallOutcome",
    "ContainmentProbeResult",
    "CredentialFact",
    "DenialReason",
    "DeniedCallReceipt",
    "axis_of",
    "run_containment_probes",
]
