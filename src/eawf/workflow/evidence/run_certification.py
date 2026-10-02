"""Whether a Run's runtime is certified for the control an operator asks of it.

A control reaches a live runtime: an interrupt or a cancel has to be honoured
by the process the Run drives, and a steer has to be delivered into its
session. Asking for one is therefore only admitted when the runtime the Run
records it ran on holds a current certification, and, where the control leans
on one certified capability, that capability is certified ``verified`` too.

The certifications are the conformance runner's own records as the repository
committed them in its native-canary exports, so the gate reads the same
evidence a release readiness sweep does. A certification is matched on the
runtime and its version, because a certification describes exactly one tuple
and a version it never saw is not certified by it.

A Run that records no runtime tuple predates every producer of one. Locking
it out would turn a missing record into a refusal nobody can clear, so it
keeps the controls it had, and the answer says the control was not gated.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final

from pydantic import BaseModel, ConfigDict

from eawf.kernel.runtime.provider import CapabilityId, ControlKind
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    CertificationRecord,
    load_canary_evidence,
)

logger = logging.getLogger(__name__)

#: The certified capability each control leans on beyond the runtime's own
#: certification. A steer or an answer is delivered into the running
#: session's stream; a resume or a fork reopens a session. The stopping
#: controls ask the process to stop, which a certified runtime honours
#: without any one capability, so they need the certification alone.
CONTROL_CAPABILITIES: Final[Mapping[ControlKind, CapabilityId | None]] = MappingProxyType(
    {
        ControlKind.STEER: "streaming",
        ControlKind.ANSWER: "streaming",
        ControlKind.INTERRUPT: None,
        ControlKind.CANCEL: None,
        ControlKind.RESUME: "session_resume",
        ControlKind.RETRY: None,
        ControlKind.FORK: "session_resume",
        ControlKind.RECONCILE: None,
    }
)


class ControlGateCode(StrEnum):
    """Why a control was admitted or refused, one member per reason.

    ``NOT_RECORDED`` admits: the Run predates the runtime tuple. Every other
    member but ``CERTIFIED`` refuses, and names the gap an operator closes.
    """

    CERTIFIED = "runtime_certified"
    NOT_RECORDED = "runtime_not_recorded"
    VERSION_NOT_RECORDED = "runtime_version_not_recorded"
    UNCERTIFIED = "runtime_uncertified"
    QUARANTINED = "runtime_quarantined"
    NOT_VERIFIED = "runtime_certification_not_verified"
    EXPIRED = "runtime_certification_expired"
    CAPABILITY_UNCERTIFIED = "runtime_capability_uncertified"


class ControlGate(BaseModel):
    """What the gate decided about one control on one Run.

    Attributes:
        admitted: Whether the control may be asked for.
        code: Why.
        runtime: The runtime the decision is about, as ``<harness> <version>``;
            ``None`` for a Run that records none.
        certification_ref: The certification the decision rests on, when one
            was found.
        reason: One sentence an operator reads, naming the runtime and the
            certification that is missing, expired or short of the control.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    admitted: bool
    code: ControlGateCode
    runtime: str | None = None
    certification_ref: str | None = None
    reason: str


def runtime_certifications(repo_root: Path) -> tuple[CertificationRecord, ...]:
    """Return every certification the repository's canary exports commit, once each.

    Args:
        repo_root: The repository the exports are committed in.

    Returns:
        The certifications, one per certification URN, in export order.

    Raises:
        ValueError: A committed export is unreadable or does not validate.
    """
    found: dict[str, CertificationRecord] = {}
    for release_key in CANARY_EVIDENCE_DIRS:
        evidence = load_canary_evidence(repo_root, release_key)
        for record in () if evidence is None else evidence.certifications:
            found.setdefault(record.certification_urn, record)
    return tuple(found.values())


def _named(runtime: RunRuntimeTuple) -> str:
    """Return a runtime as an operator reads it: its harness and its version."""
    return f"{runtime.harness} {runtime.harness_version or 'of an unrecorded version'}"


def decide_run_control(
    runtime: RunRuntimeTuple | None,
    control: ControlKind,
    *,
    certifications: Sequence[CertificationRecord],
    quarantined: Callable[[str], bool],
    now: datetime,
) -> ControlGate:
    """Decide whether *control* may be asked of a Run that ran on *runtime*.

    Args:
        runtime: The Run's runtime tuple, or ``None`` when it records none.
        control: The control asked for.
        certifications: Every certification the repository holds.
        quarantined: Whether the tuple a certification addresses by its digest
            is out of service now.
        now: The instant a certification's expiry is judged at.

    Returns:
        The decision; a refusal names the runtime and the certification gap.
    """
    if runtime is None:
        return ControlGate(
            admitted=True,
            code=ControlGateCode.NOT_RECORDED,
            reason=("this Run records no runtime, so the control was not gated on a certification"),
        )
    named = _named(runtime)
    if runtime.harness_version is None:
        return ControlGate(
            admitted=False,
            code=ControlGateCode.VERSION_NOT_RECORDED,
            runtime=named,
            reason=(
                f"{runtime.harness} ran at a version this Run did not record, so no "
                f"certification can be matched to it"
            ),
        )
    matching = [
        record
        for record in certifications
        if record.runtime_id == runtime.harness
        and record.certification.distribution_version == runtime.harness_version
    ]
    if not matching:
        return ControlGate(
            admitted=False,
            code=ControlGateCode.UNCERTIFIED,
            runtime=named,
            reason=f"{named} holds no certification",
        )
    record = max(matching, key=lambda item: item.certification.verified_at)
    return _judged(record, named=named, control=control, quarantined=quarantined, now=now)


def _judged(
    record: CertificationRecord,
    *,
    named: str,
    control: ControlKind,
    quarantined: Callable[[str], bool],
    now: datetime,
) -> ControlGate:
    """Judge the newest certification of a runtime against *control* at *now*."""
    ref = record.certification_urn
    certification = record.certification

    def refused(code: ControlGateCode, reason: str) -> ControlGate:
        return ControlGate(
            admitted=False, code=code, runtime=named, certification_ref=ref, reason=reason
        )

    if quarantined(record.tuple_digest):
        return refused(ControlGateCode.QUARANTINED, f"{named} is quarantined; {ref} is suspended")
    if certification.overall_status != "verified" or certification.revoked_at is not None:
        return refused(
            ControlGateCode.NOT_VERIFIED,
            f"{ref} for {named} is {certification.overall_status}, not verified",
        )
    if certification.expires_at <= now:
        return refused(
            ControlGateCode.EXPIRED,
            f"{ref} for {named} expired at {certification.expires_at.isoformat()}",
        )
    capability = CONTROL_CAPABILITIES[control]
    if capability is not None:
        row = next((c for c in certification.capabilities if c.capability_id == capability), None)
        if row is None:
            observed = "absent"
        elif row.status != "verified":
            observed = row.status
        elif row.expires_at <= now:
            observed = f"expired at {row.expires_at.isoformat()}"
        else:
            observed = None
        if observed is not None:
            return refused(
                ControlGateCode.CAPABILITY_UNCERTIFIED,
                f"{control.value} needs {capability!r}, which {ref} for {named} certifies "
                f"as {observed}",
            )
    logger.debug(f"decide_run_control control={control.value} certification={ref}")
    return ControlGate(
        admitted=True,
        code=ControlGateCode.CERTIFIED,
        runtime=named,
        certification_ref=ref,
        reason=f"{named} is certified by {ref}",
    )


__all__ = [
    "CONTROL_CAPABILITIES",
    "ControlGate",
    "ControlGateCode",
    "decide_run_control",
    "runtime_certifications",
]
