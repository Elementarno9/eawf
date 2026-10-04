"""This machine's probe rows, and the control gate reading them beside the exports.

A row is one conformance probe of one installed runtime version: a passed probe
certifies the version for ninety days, a failed one quarantines it with what the
probe found missing. The gate reads the rows together with the committed canary
exports, lets the newest record of a version decide, and refuses a version a probe
is running for as in progress rather than as uncertified.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.run import RunRuntimeTuple
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.workflow.evidence.machine_certification import (
    CERTIFICATION_LIFETIME,
    append_machine_certification,
    machine_certification_path,
    read_machine_certifications,
)
from eawf.workflow.evidence.provider_certification import CertificationRecord
from eawf.workflow.evidence.run_certification import (
    ControlGate,
    ControlGateCode,
    decide_run_control,
    runtime_certifications,
)
from tests.conftest import REPO_ROOT

PROBED_AT: Final = datetime(2026, 10, 2, 9, 0, tzinfo=UTC)
NOW: Final = datetime(2026, 10, 3, tzinfo=UTC)
DIGEST: Final = f"sha256:{'a' * 64}"
INSTALLED: Final = RunRuntimeTuple(harness="claude-code", harness_version="2.1.288")
EXPORTED: Final = RunRuntimeTuple(harness="claude-code", harness_version="2.1.274")
FINDING: Final = "tool_use: declared=supported but probe shows none of ['mcp'] in observed_flags"


def _capability(capability_id: str, status: str, at: datetime) -> dict[str, Any]:
    return {
        "capability_id": capability_id,
        "level": status == "verified",
        "status": status,
        "basis": "native",
        "evidence_ref": "artifact://runtime-certification/claude-code/2.1.288",
        "verified_at": at,
        "expires_at": at + CERTIFICATION_LIFETIME,
    }


def _probe(outcome: str, at: datetime) -> dict[str, Any]:
    return {
        "stage": "probe",
        "outcome": outcome,
        "reason_code": None if outcome == "passed" else "capability_not_observed",
        "evidence_ref": "artifact://runtime-certification/claude-code/2.1.288",
        "started_at": at,
        "completed_at": at,
    }


def certified(
    *, version: str = "2.1.288", at: datetime = PROBED_AT, **overrides: Any
) -> MachineCertification:
    """Return a certified row of claude-code *version* probed at *at*."""
    document: dict[str, Any] = {
        "certification_urn": f"certification://claude-code/{version}/{at:%Y-%m-%d}",
        "runtime_id": "claude-code",
        "harness_version": version,
        "tuple_digest": DIGEST,
        "outcome": "certified",
        "capabilities": [
            _capability("tool_use", "verified", at),
            _capability("streaming", "verified", at),
            _capability("session_resume", "unsupported", at),
        ],
        "reason": f"claude-code {version} passed the conformance probe",
        "probe": _probe("passed", at),
        "verified_at": at,
        "expires_at": at + CERTIFICATION_LIFETIME,
    }
    document.update(overrides)
    return MachineCertification.model_validate(document)


def quarantined(
    *, version: str = "2.1.288", at: datetime = PROBED_AT, **overrides: Any
) -> MachineCertification:
    """Return a quarantined row of claude-code *version* probed at *at*."""
    document: dict[str, Any] = {
        "certification_urn": f"certification://claude-code/{version}/{at:%Y-%m-%d}",
        "runtime_id": "claude-code",
        "harness_version": version,
        "tuple_digest": DIGEST,
        "outcome": "quarantined",
        "reason_code": "capability_not_observed",
        "findings": [FINDING],
        "reason": f"claude-code {version} failed the conformance probe: tool_use",
        "probe": _probe("failed", at),
        "verified_at": at,
    }
    document.update(overrides)
    return MachineCertification.model_validate(document)


def _decide(
    runtime: RunRuntimeTuple,
    control: ControlKind,
    *,
    machine: tuple[MachineCertification, ...] = (),
    exports: tuple[CertificationRecord, ...] = (),
    probing: bool = False,
    now: datetime = NOW,
) -> ControlGate:
    return decide_run_control(
        runtime,
        control,
        certifications=exports,
        machine=machine,
        quarantined=lambda _digest: False,
        certifying=lambda _runtime, _version: probing,
        now=now,
    )


@pytest.fixture(scope="module")
def exports() -> tuple[CertificationRecord, ...]:
    return runtime_certifications(REPO_ROOT)


# ---------- the row ----------


def test_a_certified_row_round_trips() -> None:
    row = certified()
    assert MachineCertification.model_validate(row.model_dump(mode="json")) == row
    assert row.expires_at == PROBED_AT + timedelta(days=90)


def test_a_quarantined_row_round_trips_with_its_findings() -> None:
    row = quarantined()
    assert MachineCertification.model_validate(row.model_dump(mode="json")) == row
    assert row.findings == (FINDING,)


@pytest.mark.parametrize(
    ("build", "overrides"),
    [
        (certified, {"capabilities": []}),
        (certified, {"expires_at": None}),
        (certified, {"expires_at": PROBED_AT}),
        (certified, {"findings": [FINDING]}),
        (certified, {"reason_code": "capability_not_observed"}),
        (certified, {"probe": _probe("failed", PROBED_AT)}),
        (quarantined, {"findings": []}),
        (quarantined, {"reason_code": None}),
        (quarantined, {"expires_at": PROBED_AT + timedelta(days=1)}),
        (quarantined, {"capabilities": [_capability("tool_use", "verified", PROBED_AT)]}),
        (quarantined, {"probe": _probe("passed", PROBED_AT)}),
        (certified, {"outcome": "pending"}),
        (certified, {"harness_version": ""}),
        (certified, {"harness_version": "/2.1"}),
        (certified, {"runtime_id": "c" * 65}),
        (certified, {"reason": ""}),
        (certified, {"reason": "r" * 501}),
        (certified, {"certification_urn": "certification://Claude"}),
        (certified, {"vendor": "anthropic"}),
    ],
)
def test_a_row_refuses_facts_its_outcome_does_not_carry(build: Any, overrides: Any) -> None:
    with pytest.raises(ValidationError):
        build(**overrides)


def test_a_row_accepts_a_reason_at_its_longest() -> None:
    assert len(certified(reason="r" * 500).reason) == 500


def test_the_rows_live_in_the_machine_local_tier(tmp_path: Path) -> None:
    path = machine_certification_path(tmp_path / ".ea" / "state.json")
    assert path == tmp_path / ".ea" / "local" / "runtime_certification.jsonl"


def test_no_store_reads_as_no_rows(tmp_path: Path) -> None:
    assert read_machine_certifications(tmp_path / ".ea" / "state.json") == ()


def test_rows_read_back_in_append_order(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    first = quarantined(at=PROBED_AT)
    second = certified(at=PROBED_AT + timedelta(hours=1))
    append_machine_certification(state_path, first)
    append_machine_certification(state_path, second)
    assert read_machine_certifications(state_path) == (first, second)


def test_a_store_line_that_is_not_a_row_is_refused(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    path = machine_certification_path(state_path)
    path.parent.mkdir(parents=True)
    path.write_text('{"not": "an envelope"}\n', encoding="utf-8")
    with pytest.raises(ValidationError):
        read_machine_certifications(state_path)


# ---------- the gate ----------


@pytest.mark.parametrize("control", [ControlKind.CANCEL, ControlKind.INTERRUPT, ControlKind.STEER])
def test_a_machine_certification_admits_the_controls_its_capabilities_cover(
    control: ControlKind,
) -> None:
    gate = _decide(INSTALLED, control, machine=(certified(),))
    assert (gate.admitted, gate.code) == (True, ControlGateCode.CERTIFIED)
    assert gate.certification_ref == "certification://claude-code/2.1.288/2026-10-02"


def test_a_machine_certification_refuses_a_control_it_records_unsupported() -> None:
    gate = _decide(INSTALLED, ControlKind.RESUME, machine=(certified(),))
    assert gate.code is ControlGateCode.CAPABILITY_UNCERTIFIED
    assert "'session_resume'" in gate.reason and "unsupported" in gate.reason


def test_a_quarantined_version_is_refused_naming_what_the_probe_found() -> None:
    gate = _decide(INSTALLED, ControlKind.STEER, machine=(quarantined(),))
    assert (gate.admitted, gate.code) == (False, ControlGateCode.QUARANTINED)
    assert gate.reason == (
        "claude-code 2.1.288 is quarantined: claude-code 2.1.288 failed the conformance "
        "probe: tool_use"
    )


def test_an_expired_machine_certification_is_refused_by_its_expiry() -> None:
    gate = _decide(
        INSTALLED, ControlKind.CANCEL, machine=(certified(),), now=PROBED_AT + timedelta(days=91)
    )
    assert gate.code is ControlGateCode.EXPIRED
    assert "expired at 2026-12-31" in gate.reason


def test_a_machine_certification_admits_until_the_instant_before_it_expires() -> None:
    expiry = PROBED_AT + CERTIFICATION_LIFETIME
    before = _decide(
        INSTALLED, ControlKind.CANCEL, machine=(certified(),), now=expiry - timedelta(seconds=1)
    )
    at = _decide(INSTALLED, ControlKind.CANCEL, machine=(certified(),), now=expiry)
    assert (before.code, at.code) == (ControlGateCode.CERTIFIED, ControlGateCode.EXPIRED)


def test_a_version_being_probed_is_in_progress_rather_than_uncertified() -> None:
    gate = _decide(INSTALLED, ControlKind.STEER, probing=True)
    assert (gate.admitted, gate.code) == (False, ControlGateCode.IN_PROGRESS)
    assert gate.code.value == "runtime_certification_in_progress"
    assert gate.reason.startswith("claude-code 2.1.288 is being certified")
    assert _decide(INSTALLED, ControlKind.CANCEL).code is ControlGateCode.UNCERTIFIED


def test_a_stop_on_a_version_being_probed_or_quarantined_is_admitted() -> None:
    probing = _decide(INSTALLED, ControlKind.INTERRUPT, probing=True)
    quarantine = _decide(INSTALLED, ControlKind.CANCEL, machine=(quarantined(),))
    assert (probing.admitted, probing.code) == (True, ControlGateCode.IN_PROGRESS)
    assert (quarantine.admitted, quarantine.code) == (True, ControlGateCode.QUARANTINED)
    assert "failed the conformance probe" in quarantine.reason


def test_an_expired_version_being_reprobed_is_in_progress() -> None:
    later = PROBED_AT + timedelta(days=91)
    gate = _decide(INSTALLED, ControlKind.CANCEL, machine=(certified(),), probing=True, now=later)
    assert gate.code is ControlGateCode.IN_PROGRESS


def test_a_probe_running_does_not_hold_a_certified_or_quarantined_version() -> None:
    assert (
        _decide(INSTALLED, ControlKind.CANCEL, machine=(certified(),), probing=True).code
        is ControlGateCode.CERTIFIED
    )
    assert (
        _decide(INSTALLED, ControlKind.CANCEL, machine=(quarantined(),), probing=True).code
        is ControlGateCode.QUARANTINED
    )


def test_a_row_of_another_version_or_runtime_certifies_nothing() -> None:
    codex = certified(runtime_id="codex")
    other = certified(version="2.1.289")
    gate = _decide(INSTALLED, ControlKind.CANCEL, machine=(codex, other))
    assert gate.code is ControlGateCode.UNCERTIFIED


def test_the_gate_reads_machine_rows_and_export_certifications_together(
    exports: tuple[CertificationRecord, ...],
) -> None:
    machine = (certified(),)
    exported = _decide(EXPORTED, ControlKind.CANCEL, machine=machine, exports=exports)
    installed = _decide(INSTALLED, ControlKind.CANCEL, machine=machine, exports=exports)
    assert exported.certification_ref == "certification://claude-code/2026-09-18"
    assert installed.certification_ref == "certification://claude-code/2.1.288/2026-10-02"
    assert exported.admitted and installed.admitted


def test_a_later_failed_probe_quarantines_a_version_an_export_certifies(
    exports: tuple[CertificationRecord, ...],
) -> None:
    gate = _decide(
        EXPORTED, ControlKind.CANCEL, machine=(quarantined(version="2.1.274"),), exports=exports
    )
    assert gate.code is ControlGateCode.QUARANTINED


def test_a_later_passed_probe_lifts_an_earlier_quarantine() -> None:
    machine = (quarantined(), certified(at=PROBED_AT + timedelta(hours=1)))
    assert _decide(INSTALLED, ControlKind.CANCEL, machine=machine).code is ControlGateCode.CERTIFIED
    reversed_order = (machine[1], machine[0])
    assert (
        _decide(INSTALLED, ControlKind.CANCEL, machine=reversed_order).code
        is ControlGateCode.CERTIFIED
    )


def test_an_export_newer_than_a_quarantine_certifies_again(
    exports: tuple[CertificationRecord, ...],
) -> None:
    stale = quarantined(version="2.1.274", at=datetime(2026, 9, 1, tzinfo=UTC))
    gate = _decide(EXPORTED, ControlKind.CANCEL, machine=(stale,), exports=exports)
    assert gate.code is ControlGateCode.CERTIFIED
