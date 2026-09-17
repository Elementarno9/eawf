"""The containment probe set: four axes, ten attempts, one receipt each.

The subject is what a canary owes and what it leaves behind. A run passes
only when every attempt on every axis was denied and returned nothing,
and then each attempt yields a :class:`DeniedCallReceipt` whose every
field is a closed enum, a typed reference, or a timestamp -- so a receipt
has nowhere to put a secret rather than being scanned for one.

The three ways a run fails are pinned apart: a call that was allowed is
an escape, a denied call that still handed bytes back is observable
material, and an attempt nobody made is uncovered evidence. A canary that
skipped an axis is graded as missing evidence, never as a pass.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import CertificationFailureCode
from eawf.runtime.runtimes.containment import (
    AXIS_ATTEMPTS,
    CONTAINMENT_ATTEMPTS,
    CONTAINMENT_AXES,
    ContainmentAttempt,
    ContainmentAxis,
    ContainmentCallOutcome,
    ContainmentProbeResult,
    CredentialFact,
    DenialReason,
    DeniedCallReceipt,
    axis_of,
    run_containment_probes,
)

pytestmark = pytest.mark.conformance

OBSERVED_AT = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
EVIDENCE_REF = "artifact://canary/containment-2026-09-17"

#: The reason each attempt is denied for, so a fixture never has to pick.
DENIAL_REASONS: dict[ContainmentAttempt, DenialReason] = {
    ContainmentAttempt.VARIABLE_OUTSIDE_ALLOWLIST: DenialReason.OUTSIDE_ALLOWLIST,
    ContainmentAttempt.AMBIENT_ENVIRONMENT: DenialReason.AMBIENT_INHERITANCE_REFUSED,
    ContainmentAttempt.CANONICAL_GIT: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.DAEMON_STORAGE: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.WRITE_AHEAD_LOG: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.OUTSIDE_WRITABLE_ROOTS: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.UNREGISTERED_EXECUTABLE: DenialReason.NOT_IN_COMPONENT_REGISTRY,
    ContainmentAttempt.HOST_PROCESS_METADATA: DenialReason.SCOPE_ESCAPE,
    ContainmentAttempt.DIRECT_EGRESS: DenialReason.EGRESS_OUTSIDE_POLICY,
    ContainmentAttempt.CREDENTIAL_VALUE: DenialReason.CREDENTIAL_REFERENCE_ONLY,
}


def denied(attempt: ContainmentAttempt, **overrides: Any) -> ContainmentCallOutcome:
    """Return a denied outcome for *attempt* with *overrides* applied."""
    document: dict[str, Any] = {
        "attempt": attempt,
        "denied": True,
        "denial_reason": DENIAL_REASONS[attempt],
        "returned_bytes": 0,
    }
    document.update(overrides)
    return ContainmentCallOutcome.model_validate(document)


def contained() -> dict[ContainmentAttempt, ContainmentCallOutcome]:
    """Return a fully denied run: every attempt, nothing returned."""
    return {attempt: denied(attempt) for attempt in CONTAINMENT_ATTEMPTS}


def graded(
    outcomes: dict[ContainmentAttempt, ContainmentCallOutcome],
) -> ContainmentProbeResult:
    """Grade *outcomes* under the fixed evidence reference and instant."""
    return run_containment_probes(
        outcomes=outcomes, evidence_ref=EVIDENCE_REF, observed_at=OBSERVED_AT
    )


# ---- the probe set, axis by axis --------------------------------------------


@pytest.mark.parametrize("axis", CONTAINMENT_AXES)
def test_run_containment_probes_yields_a_receipt_on_every_axis(axis: ContainmentAxis) -> None:
    """Each axis produces a denied-call receipt for each of its attempts."""
    result = graded(contained())

    receipts = {row.attempt: row for row in result.receipts if row.axis is axis}

    assert set(receipts) == set(AXIS_ATTEMPTS[axis])
    assert all(row.denial_reason is DENIAL_REASONS[row.attempt] for row in receipts.values())
    assert all(row.evidence_ref == EVIDENCE_REF for row in receipts.values())


def test_run_containment_probes_passes_a_fully_contained_run() -> None:
    """A run denied on every attempt passes and covers all four axes."""
    result = graded(contained())

    assert result.passed
    assert result.failure_code is None
    assert len(result.receipts) == len(CONTAINMENT_ATTEMPTS)
    assert result.covered_axes() == CONTAINMENT_AXES


def test_denied_call_receipt_has_no_field_that_can_carry_a_value() -> None:
    """The receipt's shape is what makes secret material unobservable."""
    assert set(DeniedCallReceipt.model_fields) == {
        "axis",
        "attempt",
        "denial_reason",
        "disclosed_facts",
        "observed_at",
        "evidence_ref",
    }


def test_run_containment_probes_receipts_carry_only_closed_vocabulary() -> None:
    """Every string a receipt serialises is drawn from a closed set."""
    allowed = {
        *(axis.value for axis in ContainmentAxis),
        *(attempt.value for attempt in ContainmentAttempt),
        *(reason.value for reason in DenialReason),
        *(fact.value for fact in CredentialFact),
        EVIDENCE_REF,
        OBSERVED_AT.isoformat().replace("+00:00", "Z"),
    }

    for receipt in graded(contained()).receipts:
        document = receipt.model_dump(mode="json")
        strings = [value for value in document.values() if isinstance(value, str)]
        strings.extend(document["disclosed_facts"])
        assert set(strings) <= allowed


def test_run_containment_probes_records_the_four_credential_facts() -> None:
    """A credential discloses its kind, health, expiry and provenance only."""
    facts = tuple(CredentialFact)
    outcomes = contained()
    outcomes[ContainmentAttempt.CREDENTIAL_VALUE] = denied(
        ContainmentAttempt.CREDENTIAL_VALUE, disclosed_facts=facts
    )

    result = graded(outcomes)

    receipt = next(
        row for row in result.receipts if row.attempt is ContainmentAttempt.CREDENTIAL_VALUE
    )
    assert result.passed
    assert receipt.disclosed_facts == facts


# ---- the three ways a run fails ---------------------------------------------


def test_run_containment_probes_reports_an_allowed_call_as_an_escape() -> None:
    """A call that was not denied fails the set and leaves no receipt."""
    outcomes = contained()
    outcomes[ContainmentAttempt.CANONICAL_GIT] = ContainmentCallOutcome(
        attempt=ContainmentAttempt.CANONICAL_GIT, denied=False
    )

    result = graded(outcomes)

    assert not result.passed
    assert result.failure_code is CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE
    assert result.escaped == (ContainmentAttempt.CANONICAL_GIT,)
    assert ContainmentAttempt.CANONICAL_GIT not in {row.attempt for row in result.receipts}


def test_run_containment_probes_reports_returned_bytes_as_observable() -> None:
    """A denial that still handed bytes back is not a passed probe."""
    outcomes = contained()
    outcomes[ContainmentAttempt.DIRECT_EGRESS] = denied(
        ContainmentAttempt.DIRECT_EGRESS, returned_bytes=1
    )

    result = graded(outcomes)

    assert not result.passed
    assert result.failure_code is CertificationFailureCode.SECRET_MATERIAL_OBSERVABLE
    assert result.observed == (ContainmentAttempt.DIRECT_EGRESS,)
    assert ContainmentAttempt.DIRECT_EGRESS not in {row.attempt for row in result.receipts}


def test_run_containment_probes_reports_a_skipped_axis_as_uncovered() -> None:
    """A canary that skipped an axis is missing evidence, not passing."""
    outcomes = contained()
    for attempt in AXIS_ATTEMPTS[ContainmentAxis.NETWORK]:
        del outcomes[attempt]

    result = graded(outcomes)

    assert not result.passed
    assert result.failure_code is CertificationFailureCode.CAPABILITY_EVIDENCE_UNCOVERED
    assert result.skipped == AXIS_ATTEMPTS[ContainmentAxis.NETWORK]
    assert ContainmentAxis.NETWORK not in result.covered_axes()


def test_run_containment_probes_grades_an_escape_above_an_observation() -> None:
    """Containment giving way outranks material observed through a denial."""
    outcomes = contained()
    outcomes[ContainmentAttempt.CANONICAL_GIT] = ContainmentCallOutcome(
        attempt=ContainmentAttempt.CANONICAL_GIT, denied=False
    )
    outcomes[ContainmentAttempt.DIRECT_EGRESS] = denied(
        ContainmentAttempt.DIRECT_EGRESS, returned_bytes=4096
    )
    del outcomes[ContainmentAttempt.AMBIENT_ENVIRONMENT]

    result = graded(outcomes)

    assert result.failure_code is CertificationFailureCode.CONTAINMENT_PROBE_ESCAPE
    assert result.escaped and result.observed and result.skipped


# ---- boundaries -------------------------------------------------------------


def test_run_containment_probes_reports_an_empty_run_as_wholly_uncovered() -> None:
    """A canary that attempted nothing produces no receipt at all."""
    result = graded({})

    assert not result.passed
    assert result.receipts == ()
    assert result.skipped == CONTAINMENT_ATTEMPTS
    assert result.covered_axes() == ()


def test_run_containment_probes_reports_a_single_attempt_run() -> None:
    """One denied attempt is one receipt and nine uncovered obligations."""
    only = ContainmentAttempt.WRITE_AHEAD_LOG
    result = graded({only: denied(only)})

    assert [row.attempt for row in result.receipts] == [only]
    assert len(result.skipped) == len(CONTAINMENT_ATTEMPTS) - 1
    assert result.covered_axes() == (ContainmentAxis.FILESYSTEM,)


def test_run_containment_probes_reports_one_missing_attempt() -> None:
    """Nine of ten attempts is uncovered evidence, not a rounding error."""
    outcomes = contained()
    del outcomes[ContainmentAttempt.HOST_PROCESS_METADATA]

    result = graded(outcomes)

    assert not result.passed
    assert result.skipped == (ContainmentAttempt.HOST_PROCESS_METADATA,)
    assert len(result.receipts) == len(CONTAINMENT_ATTEMPTS) - 1


def test_run_containment_probes_accepts_the_maximum_disclosure() -> None:
    """All four credential facts at once is the ceiling, and it passes."""
    outcomes = contained()
    outcomes[ContainmentAttempt.CREDENTIAL_VALUE] = denied(
        ContainmentAttempt.CREDENTIAL_VALUE, disclosed_facts=tuple(CredentialFact)
    )

    assert graded(outcomes).passed


# ---- error paths ------------------------------------------------------------


def test_run_containment_probes_refuses_an_outcome_filed_under_another_attempt() -> None:
    """Grading one call under another's obligation is refused."""
    outcomes = contained()
    outcomes[ContainmentAttempt.DIRECT_EGRESS] = denied(ContainmentAttempt.CANONICAL_GIT)

    with pytest.raises(ValueError, match="is filed under"):
        graded(outcomes)


def test_axis_of_raises_key_error_for_a_name_that_is_not_an_attempt() -> None:
    """A string that is not a member addresses no axis."""
    with pytest.raises(KeyError):
        axis_of("read_the_disk")  # type: ignore[arg-type]


def test_containment_call_outcome_requires_a_reason_when_denied() -> None:
    """A denial with no reason could not be written into a receipt."""
    with pytest.raises(ValidationError, match="denial_reason"):
        ContainmentCallOutcome(attempt=ContainmentAttempt.DIRECT_EGRESS, denied=True)


def test_containment_call_outcome_refuses_a_reason_when_allowed() -> None:
    """An allowed call was not denied, so it has no denial reason."""
    with pytest.raises(ValidationError, match="no denial_reason"):
        ContainmentCallOutcome(
            attempt=ContainmentAttempt.DIRECT_EGRESS,
            denied=False,
            denial_reason=DenialReason.EGRESS_OUTSIDE_POLICY,
        )


def test_containment_call_outcome_refuses_disclosure_off_the_credential_axis() -> None:
    """Only the credential attempt asks for a credential, so only it discloses."""
    with pytest.raises(ValidationError, match="asks for no credential"):
        denied(ContainmentAttempt.CANONICAL_GIT, disclosed_facts=(CredentialFact.KIND,))


def test_containment_call_outcome_refuses_a_repeated_disclosure() -> None:
    """A fact disclosed twice is an author error, not a set."""
    with pytest.raises(ValidationError, match="more than once"):
        denied(
            ContainmentAttempt.CREDENTIAL_VALUE,
            disclosed_facts=(CredentialFact.KIND, CredentialFact.KIND),
        )


def test_containment_call_outcome_refuses_negative_returned_bytes() -> None:
    """A byte count below zero describes no call."""
    with pytest.raises(ValidationError):
        denied(ContainmentAttempt.DIRECT_EGRESS, returned_bytes=-1)


def test_containment_call_outcome_refuses_an_unknown_field() -> None:
    """An outcome record carries no field nobody declared."""
    with pytest.raises(ValidationError):
        ContainmentCallOutcome.model_validate(
            {
                "attempt": ContainmentAttempt.DIRECT_EGRESS,
                "denied": True,
                "denial_reason": DenialReason.EGRESS_OUTSIDE_POLICY,
                "observed_value": "token-abc",  # pragma: allowlist secret
            }
        )


def test_containment_probe_result_refuses_a_pass_that_skipped_an_attempt() -> None:
    """A pass claims one receipt per attempt, and the record enforces it."""
    receipts = tuple(graded(contained()).receipts[:-1])

    with pytest.raises(ValidationError, match="one receipt per attempt"):
        ContainmentProbeResult(receipts=receipts, passed=True)


def test_containment_probe_result_refuses_a_failure_with_no_code() -> None:
    """A failure names the code that produced it."""
    with pytest.raises(ValidationError, match="requires a failure code"):
        ContainmentProbeResult(receipts=(), passed=False)


def test_containment_probe_result_refuses_a_repeated_receipt() -> None:
    """One attempt leaves one receipt; two would double-count the evidence."""
    receipt = graded(contained()).receipts[0]

    with pytest.raises(ValidationError, match="more than once"):
        ContainmentProbeResult(receipts=(receipt, receipt), passed=False, escaped=())


# ---- the obligation is closed ------------------------------------------------


def test_axis_attempts_partitions_every_attempt_exactly_once() -> None:
    """The axis map is the whole obligation and names nothing twice."""
    flattened = [attempt for axis in CONTAINMENT_AXES for attempt in AXIS_ATTEMPTS[axis]]

    assert sorted(flattened) == sorted(ContainmentAttempt)
    assert len(flattened) == len(set(flattened))
    assert tuple(AXIS_ATTEMPTS) == CONTAINMENT_AXES


def test_axis_of_maps_every_attempt_to_its_axis() -> None:
    """Every attempt resolves, so no receipt can be unfiled."""
    assert all(axis_of(attempt) in CONTAINMENT_AXES for attempt in CONTAINMENT_ATTEMPTS)
