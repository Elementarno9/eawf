"""Typed certification records for one runtime tuple.

Three contracts are pinned. ``install_trust`` and ``overall_status`` are
independent axes on :class:`DriverCertification`: every combination of the
two is constructible, and a refusal of unattended dispatch names the axis
that produced it rather than collapsing both into one verdict. Every
certified capability declares a ``basis`` inside ``native | emulated``, so
an emulated degraded capability is never rendered as a natively verified
one. And :class:`CertifiedRuntimeFacts` are measured values a locally
configured override may lower but never raise, with a fact the runtime
never reported staying absent rather than defaulted.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import (
    STAGE_ORDER,
    CapabilityCertification,
    CertificationFailureCode,
    CertifiedRuntimeFacts,
    ConformanceStageRecord,
    DriverCertification,
    LocalFactOverride,
    QuarantineTrigger,
    UnattendedDecision,
)

pytestmark = pytest.mark.unit

REQUIRED = ("semantic_tools", "event_replay")
EVIDENCE_REF = "artifact://evidence/probe-2026-09"
WORKFLOW_REF = "workflow://degraded/session-resume"
CONFIG_REF = "config://workspace/runtime"
VERIFIED_AT = "2026-09-01T00:00:00Z"
EXPIRES_AT = "2026-12-01T00:00:00Z"
WINDOW_TOKENS = 200_000


def digest(character: str) -> str:
    """Return a well-formed digest made of one repeated hex *character*."""
    return f"sha256:{character * 64}"


def capability(capability_id: str, **overrides: Any) -> dict[str, Any]:
    """Return a natively verified capability certification document."""
    document: dict[str, Any] = {
        "capability_id": capability_id,
        "level": True,
        "status": "verified",
        "basis": "native",
        "evidence_ref": f"artifact://evidence/{capability_id}",
        "verified_at": VERIFIED_AT,
        "expires_at": EXPIRES_AT,
    }
    document.update(overrides)
    return document


def stage(stage_name: str, **overrides: Any) -> dict[str, Any]:
    """Return a passed stage record for *stage_name*."""
    document: dict[str, Any] = {
        "stage": stage_name,
        "outcome": "passed",
        "evidence_ref": EVIDENCE_REF,
        "started_at": "2026-09-01T00:00:00Z",
        "completed_at": "2026-09-01T00:05:00Z",
    }
    document.update(overrides)
    return document


def facts(**overrides: Any) -> dict[str, Any]:
    """Return a measured runtime-facts document."""
    document: dict[str, Any] = {
        "context_window_tokens": WINDOW_TOKENS,
        "auto_compaction_threshold_tokens": 160_000,
        "project_document_cap_bytes": 32_768,
        "tool_output_cap_tokens": 25_000,
        "measured_at": VERIFIED_AT,
        "measurement_method": "runtime_reported",
    }
    document.update(overrides)
    return document


def certification(**overrides: Any) -> dict[str, Any]:
    """Return a managed, verified certification document."""
    document: dict[str, Any] = {
        "schema_version": "driver-certification/v1",
        "certification_id": "codex-app-server-2026-09",
        "manifest_ref": "driver://codex-app-server/v1",
        "manifest_digest": digest("a"),
        "distribution_version": "0.44.0",
        "sdk_or_server_version": "2.1.0",
        "auth_kind": "subscription",
        "model_family": "gpt-5.6",
        "os_class": "macos",
        "architecture": "aarch64",
        "managed_profile_digest": digest("b"),
        "worker_protocol_version": "2.1.0",
        "semantic_protocol_version": "1.0.0",
        "event_codec_version": "1.0.0",
        "conformance_suite_version": "1.2.0",
        "capabilities": [capability(name) for name in REQUIRED],
        "overall_status": "verified",
        "install_trust": "managed",
        "runtime_facts": facts(),
        "stage_history": [stage("probe"), stage("canary"), stage("certify")],
        "evidence_bundle_ref": "artifact://evidence/bundle-2026-09",
        "verified_at": VERIFIED_AT,
        "expires_at": EXPIRES_AT,
    }
    document.update(overrides)
    return document


def build(**overrides: Any) -> DriverCertification:
    """Return a validated certification with *overrides* applied."""
    return DriverCertification.model_validate(certification(**overrides))


# ---------------------------------------------------------------------------
# Install trust and overall status are independent axes
# ---------------------------------------------------------------------------


def test_every_trust_and_status_combination_is_constructible() -> None:
    """Neither axis narrows the other, so all sixteen pairs validate."""
    for trust in ("managed", "observed", "quarantined", "unsupported"):
        for status in ("verified", "failed", "revoked", "expired"):
            extra: dict[str, Any] = {"install_trust": trust, "overall_status": status}
            if trust == "quarantined":
                extra["quarantine_trigger"] = QuarantineTrigger.CANARY_FAILURE.value
            record = build(**extra)
            assert record.install_trust == trust
            assert record.overall_status == status


def test_managed_and_verified_admits_unattended_dispatch() -> None:
    """Both axes clear and every required capability verified."""
    decision = build().decide_unattended_dispatch(required_capabilities=REQUIRED)
    assert decision.admitted is True
    assert decision.refused_axis is None


def test_untrusted_install_refuses_on_the_install_trust_axis() -> None:
    """A fresh certification of an observed install still refuses."""
    decision = build(install_trust="observed").decide_unattended_dispatch(
        required_capabilities=REQUIRED
    )
    assert decision.admitted is False
    assert decision.refused_axis == "install_trust"
    assert "observed" in (decision.reason or "")


def test_stale_evidence_refuses_on_the_overall_status_axis() -> None:
    """A managed install whose evidence expired refuses on freshness."""
    decision = build(overall_status="expired").decide_unattended_dispatch(
        required_capabilities=REQUIRED
    )
    assert decision.admitted is False
    assert decision.refused_axis == "overall_status"
    assert "expired" in (decision.reason or "")


def test_quarantined_install_refuses_on_its_own_axis_despite_verified_status() -> None:
    """Quarantine is a trust fact, so a verified status does not rescue it."""
    decision = build(
        install_trust="quarantined",
        quarantine_trigger=QuarantineTrigger.DENIED_TOOL_ESCAPE.value,
    ).decide_unattended_dispatch(required_capabilities=REQUIRED)
    assert decision.refused_axis == "install_trust"


def test_unverified_required_capability_refuses_on_the_capability_axis() -> None:
    """Both record axes clear, and the capability set still refuses."""
    degraded = capability(
        "event_replay",
        status="degraded",
        basis="emulated",
        degradation_workflow_ref=WORKFLOW_REF,
    )
    decision = build(
        capabilities=[capability("semantic_tools"), degraded]
    ).decide_unattended_dispatch(required_capabilities=REQUIRED)
    assert decision.refused_axis == "capability"
    assert "event_replay" in (decision.reason or "")


def test_absent_required_capability_refuses_on_the_capability_axis() -> None:
    """A capability the certification never rows is not verified."""
    decision = build().decide_unattended_dispatch(
        required_capabilities=(*REQUIRED, "context_compaction")
    )
    assert decision.refused_axis == "capability"
    assert "context_compaction" in (decision.reason or "")


def test_empty_required_set_admits_on_a_clean_record() -> None:
    """Boundary: requiring nothing leaves only the two record axes."""
    assert build().decide_unattended_dispatch(required_capabilities=()).admitted is True


def test_single_required_capability_admits_when_verified() -> None:
    """Boundary: a one-element required set resolves against its row."""
    decision = build().decide_unattended_dispatch(required_capabilities=("semantic_tools",))
    assert decision.admitted is True


def test_unattended_decision_rejects_an_admission_that_names_an_axis() -> None:
    """An admitted decision carries no refusal."""
    with pytest.raises(ValidationError, match="carries no refused_axis"):
        UnattendedDecision(admitted=True, refused_axis="install_trust", reason="why")


def test_unattended_decision_rejects_a_refusal_with_no_axis() -> None:
    """A refusal that names no axis is the collapse this record prevents."""
    with pytest.raises(ValidationError, match="requires both refused_axis and reason"):
        UnattendedDecision(admitted=False, reason="why")


# ---------------------------------------------------------------------------
# Capability basis is native or emulated
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("basis", ["native", "emulated"])
def test_capability_certification_accepts_the_two_bases(basis: str) -> None:
    """Both admissible bases validate."""
    status: dict[str, Any] = (
        {}
        if basis == "native"
        else {"status": "degraded", "degradation_workflow_ref": WORKFLOW_REF}
    )
    row = CapabilityCertification.model_validate(
        capability("semantic_tools", basis=basis, **status)
    )
    assert row.basis == basis


@pytest.mark.parametrize("basis", ["shimmed", "NATIVE", "", "unknown"])
def test_capability_certification_rejects_a_basis_outside_the_two(basis: str) -> None:
    """Any other basis is refused at the boundary."""
    with pytest.raises(ValidationError, match="basis"):
        CapabilityCertification.model_validate(capability("semantic_tools", basis=basis))


def test_capability_certification_requires_a_basis() -> None:
    """A capability with no basis cannot be rendered beside a native one."""
    document = capability("semantic_tools")
    del document["basis"]
    with pytest.raises(ValidationError, match="basis"):
        CapabilityCertification.model_validate(document)


def test_degraded_capability_requires_its_reduced_workflow() -> None:
    """An emulated degraded capability names what emulation provides."""
    with pytest.raises(ValidationError, match="degradation_workflow_ref"):
        CapabilityCertification.model_validate(
            capability("event_replay", status="degraded", basis="emulated")
        )


def test_failed_capability_requires_a_failure_code() -> None:
    """A failed capability says which check produced the failure."""
    with pytest.raises(ValidationError, match="failure_code"):
        CapabilityCertification.model_validate(capability("event_replay", status="failed"))


def test_verified_capability_forbids_a_failure_code() -> None:
    """A verified capability carries neither workflow nor failure code."""
    with pytest.raises(ValidationError, match="carries no degradation workflow"):
        CapabilityCertification.model_validate(
            capability(
                "event_replay",
                failure_code=CertificationFailureCode.CAPABILITY_NOT_OBSERVED.value,
            )
        )


def test_capability_expiry_follows_verification() -> None:
    """Evidence cannot expire before it was taken."""
    with pytest.raises(ValidationError, match="expires_at must follow verified_at"):
        CapabilityCertification.model_validate(capability("event_replay", expires_at=VERIFIED_AT))


def test_capability_certification_rejects_an_unknown_key() -> None:
    """``extra='forbid'`` keeps a misspelled field out of the record."""
    with pytest.raises(ValidationError, match=r"(?i)extra"):
        CapabilityCertification.model_validate(capability("event_replay", bassis="native"))


# ---------------------------------------------------------------------------
# Certified runtime facts: an override lowers, never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (WINDOW_TOKENS + 1, WINDOW_TOKENS),
        (WINDOW_TOKENS, WINDOW_TOKENS),
        (WINDOW_TOKENS - 1, WINDOW_TOKENS - 1),
        (1, 1),
    ],
)
def test_local_override_never_raises_a_certified_cap(configured: int, expected: int) -> None:
    """Off-by-one around the certified value: above it, at it, below it."""
    measured = CertifiedRuntimeFacts.model_validate(facts())
    override = LocalFactOverride(
        fact="context_window_tokens", configured_value=configured, source_ref=CONFIG_REF
    )
    assert measured.effective_cap(override) == expected


@pytest.mark.parametrize(
    "fact",
    ["context_window_tokens", "project_document_cap_bytes", "tool_output_cap_tokens"],
)
def test_no_measured_cap_can_be_raised_locally(fact: str) -> None:
    """The rule holds for every cap, not only the context window."""
    measured = CertifiedRuntimeFacts.model_validate(facts())
    certified = measured.certified_cap(fact)  # type: ignore[arg-type]
    assert certified is not None
    override = LocalFactOverride(
        fact=fact,  # type: ignore[arg-type]
        configured_value=certified * 4,
        source_ref=CONFIG_REF,
    )
    assert measured.effective_cap(override) == certified


def test_an_absent_fact_stays_absent_under_an_override() -> None:
    """A cap the runtime never reported is not a cap an override creates."""
    measured = CertifiedRuntimeFacts.model_validate(facts(auto_compaction_threshold_tokens=None))
    assert measured.certified_cap("auto_compaction_threshold_tokens") is None
    override = LocalFactOverride(
        fact="auto_compaction_threshold_tokens",
        configured_value=120_000,
        source_ref=CONFIG_REF,
    )
    assert measured.effective_cap(override) is None


def test_certified_cap_rejects_a_name_that_is_not_a_measured_fact() -> None:
    """An unmeasured name has no value to return."""
    measured = CertifiedRuntimeFacts.model_validate(facts())
    with pytest.raises(KeyError, match="concurrency"):
        measured.certified_cap("concurrency")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "field",
    [
        "context_window_tokens",
        "auto_compaction_threshold_tokens",
        "project_document_cap_bytes",
        "tool_output_cap_tokens",
        "measurement_method",
    ],
)
def test_a_missing_fact_is_refused_rather_than_defaulted(field: str) -> None:
    """Every fact is written deliberately, including the nullable one."""
    document = facts()
    del document[field]
    with pytest.raises(ValidationError, match=field):
        CertifiedRuntimeFacts.model_validate(document)


def test_compaction_threshold_cannot_exceed_the_window() -> None:
    """A threshold above the window describes a compaction that never fires."""
    with pytest.raises(ValidationError, match="exceeds context_window_tokens"):
        CertifiedRuntimeFacts.model_validate(
            facts(auto_compaction_threshold_tokens=WINDOW_TOKENS + 1)
        )


def test_a_zero_cap_is_not_a_cap() -> None:
    """Boundary: caps are counts of at least one."""
    with pytest.raises(ValidationError, match="greater than or equal to 1"):
        CertifiedRuntimeFacts.model_validate(facts(tool_output_cap_tokens=0))


def test_measured_facts_are_frozen() -> None:
    """A measurement is history, so it cannot be edited after the fact."""
    measured = CertifiedRuntimeFacts.model_validate(facts())
    with pytest.raises(ValidationError, match=r"(?i)frozen"):
        measured.context_window_tokens = 1  # type: ignore[misc]


def test_local_override_rejects_a_raw_path_as_its_source() -> None:
    """The diagnostic fact names a config reference, never a host path."""
    with pytest.raises(ValidationError, match="source_ref"):
        LocalFactOverride(
            fact="tool_output_cap_tokens",
            configured_value=1000,
            source_ref="/etc/eawf/runtime.yaml",
        )


# ---------------------------------------------------------------------------
# Record structure: stages, quarantine, revocation
# ---------------------------------------------------------------------------


def test_stage_records_run_in_stage_order() -> None:
    """The history is appended, so its stages never run backwards."""
    with pytest.raises(ValidationError, match="appended in stage order"):
        build(stage_history=[stage("certify"), stage("probe")])


def test_stage_order_covers_the_four_stages() -> None:
    """The closed stage sequence is the one the runner walks."""
    assert STAGE_ORDER == ("probe", "canary", "certify", "rollback")


def test_empty_stage_history_is_refused() -> None:
    """Boundary: a certification with no stage history has no provenance."""
    with pytest.raises(ValidationError, match=r"(?i)at least 1 item"):
        build(stage_history=[])


def test_empty_capability_set_is_refused() -> None:
    """Boundary: a certification that certifies nothing is not one."""
    with pytest.raises(ValidationError, match=r"(?i)at least 1 item"):
        build(capabilities=[])


def test_duplicate_capability_rows_are_refused() -> None:
    """One row per capability, so no two rows can disagree."""
    with pytest.raises(ValidationError, match="appears more than once"):
        build(capabilities=[capability("semantic_tools"), capability("semantic_tools")])


def test_failed_stage_requires_a_reason_code() -> None:
    """A stage that stopped the tuple says why."""
    with pytest.raises(ValidationError, match="requires a reason_code"):
        ConformanceStageRecord.model_validate(stage("probe", outcome="failed"))


def test_passed_stage_forbids_a_reason_code() -> None:
    """A passed stage has no refusal to name."""
    with pytest.raises(ValidationError, match="carries no reason_code"):
        ConformanceStageRecord.model_validate(
            stage("canary", reason_code=CertificationFailureCode.CANARY_IN_PROGRESS.value)
        )


def test_stage_cannot_complete_before_it_started() -> None:
    """Boundary: the stage interval runs forwards."""
    with pytest.raises(ValidationError, match="completed_at precedes started_at"):
        ConformanceStageRecord.model_validate(
            stage("probe", started_at="2026-09-01T00:05:00Z", completed_at=VERIFIED_AT)
        )


def test_refused_stage_records_its_failure_code() -> None:
    """A refusal carries the code the runner surfaced it with."""
    record = ConformanceStageRecord.model_validate(
        stage(
            "certify",
            outcome="refused",
            reason_code=CertificationFailureCode.EVIDENCE_EXPIRED.value,
        )
    )
    assert record.reason_code is CertificationFailureCode.EVIDENCE_EXPIRED


def test_quarantined_trust_requires_its_trigger() -> None:
    """Quarantine is automatic, so the trigger that fired is recorded."""
    with pytest.raises(ValidationError, match="requires quarantine_trigger"):
        build(install_trust="quarantined")


def test_a_trigger_without_quarantine_is_refused() -> None:
    """A trigger on a non-quarantined record would misreport the install."""
    with pytest.raises(ValidationError, match="requires quarantined install_trust"):
        build(quarantine_trigger=QuarantineTrigger.WRONG_AUTH.value)


def test_quarantine_triggers_are_the_closed_nine() -> None:
    """The trigger set is closed: an undetectable trigger cannot exist."""
    assert {trigger.value for trigger in QuarantineTrigger} == {
        "protocol_drift",
        "schema_drift",
        "wrong_auth",
        "ambient_source_leakage",
        "denied_tool_escape",
        "terminal_outcome_anomaly",
        "corrupt_resume",
        "unexplained_usage",
        "canary_failure",
    }


def test_revocation_fields_appear_together() -> None:
    """A revoked record says when and why, or says neither."""
    with pytest.raises(ValidationError, match="appear together"):
        build(revoked_at=EXPIRES_AT)


def test_certification_expiry_follows_verification() -> None:
    """A certification cannot expire before it was taken."""
    with pytest.raises(ValidationError, match="expires_at must follow verified_at"):
        build(expires_at=VERIFIED_AT)


def test_certification_rejects_an_unknown_key() -> None:
    """``extra='forbid'`` keeps an unwritten authority out of the record."""
    with pytest.raises(ValidationError, match=r"(?i)extra"):
        build(install_trust_level="managed")


def test_certification_round_trips_through_json() -> None:
    """The record survives the store it is written to unchanged."""
    record = build()
    assert DriverCertification.model_validate(record.model_dump(mode="json")) == record
