"""UI-014: a Run control is admitted only on a runtime certified for it.

The certifications are the repository's own committed canary exports, so these tests
decide against the record the runner wrote for claude-code 2.1.274 -- certified
2026-09-18, expiring 2026-12-17, ``tool_use`` and ``streaming`` verified and
``session_resume`` unsupported -- at instants either side of its expiry.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.run import Run, RunRuntimeTuple
from eawf.workflow.evidence.provider_certification import CertificationRecord
from eawf.workflow.evidence.run_certification import (
    CONTROL_CAPABILITIES,
    ControlGateCode,
    decide_run_control,
    runtime_certifications,
)
from tests.conftest import REPO_ROOT
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed_row

BEFORE_EXPIRY: Final = datetime(2026, 10, 2, tzinfo=UTC)
AFTER_EXPIRY: Final = datetime(2026, 12, 18, tzinfo=UTC)
CERTIFIED: Final = RunRuntimeTuple(harness="claude-code", harness_version="2.1.274")


@pytest.fixture(scope="module")
def certifications() -> tuple[CertificationRecord, ...]:
    return runtime_certifications(REPO_ROOT)


def _decide(
    runtime: RunRuntimeTuple | None,
    control: ControlKind,
    certifications: tuple[CertificationRecord, ...],
    *,
    now: datetime = BEFORE_EXPIRY,
    quarantined: bool = False,
) -> tuple[bool, ControlGateCode, str]:
    gate = decide_run_control(
        runtime,
        control,
        certifications=certifications,
        machine=(),
        quarantined=lambda _digest: quarantined,
        certifying=lambda _runtime, _version: False,
        now=now,
    )
    return gate.admitted, gate.code, gate.reason


# ---------- the model ----------


def test_runtime_tuple_round_trips_and_defaults_to_none_on_a_run() -> None:
    row = seed_row("run", "RUNNING")
    assert Run.model_validate(row).runtime_tuple is None
    full = RunRuntimeTuple(
        harness="claude-code",
        harness_version="2.1.274",
        provider="anthropic",
        model="claude-sonnet-4-5",
    )
    run = Run.model_validate({**row, "runtime_tuple": full.model_dump(mode="json")})
    assert run.runtime_tuple == full
    assert Run.model_validate(run.model_dump(mode="json")).runtime_tuple == full


@pytest.mark.parametrize(
    "bad",
    [
        {},
        {"harness": ""},
        {"harness": "Claude Code"},
        {"harness": "claude-code", "harness_version": ""},
        {"harness": "claude-code", "harness_version": "/2.1"},
        {"harness": "claude-code", "model": " spaced"},
        {"harness": "claude-code", "harness_version": 2},
        {"harness": "claude-code", "vendor": "anthropic"},
    ],
)
def test_runtime_tuple_refuses_a_missing_harness_a_bad_member_or_an_unknown_key(
    bad: dict[str, object],
) -> None:
    with pytest.raises(ValidationError):
        RunRuntimeTuple.model_validate(bad)


def test_runtime_tuple_accepts_a_member_at_its_longest() -> None:
    longest = RunRuntimeTuple(harness="h" * 64, harness_version="1" * 64, model="m" * 128)
    assert longest.harness_version is not None and len(longest.harness_version) == 64
    with pytest.raises(ValidationError):
        RunRuntimeTuple(harness="h" * 65)


def test_filled_from_keeps_stated_members_and_fills_only_the_gaps() -> None:
    stated = RunRuntimeTuple(harness="claude-code", provider="amazon-bedrock")
    observed = RunRuntimeTuple(
        harness="claude-code", harness_version="2.1.274", provider="anthropic", model="m"
    )
    assert stated.filled_from(observed) == RunRuntimeTuple(
        harness="claude-code", harness_version="2.1.274", provider="amazon-bedrock", model="m"
    )
    assert stated.filled_from(None) == stated
    assert stated.filled_from(RunRuntimeTuple(harness="codex", model="gpt")) == stated


# ---------- the gate ----------


def test_the_committed_exports_hold_one_claude_code_certification(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    assert [record.runtime_id for record in certifications] == ["claude-code"]


def test_every_control_names_its_capability_or_none() -> None:
    assert set(CONTROL_CAPABILITIES) == set(ControlKind)


def test_a_run_recording_no_runtime_keeps_its_controls_with_a_note(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    admitted, code, reason = _decide(None, ControlKind.CANCEL, certifications)
    assert (admitted, code) == (True, ControlGateCode.NOT_RECORDED)
    assert "not gated" in reason


@pytest.mark.parametrize("control", [ControlKind.CANCEL, ControlKind.INTERRUPT, ControlKind.STEER])
def test_a_certified_runtime_admits_a_control_its_certification_covers(
    certifications: tuple[CertificationRecord, ...], control: ControlKind
) -> None:
    admitted, code, reason = _decide(CERTIFIED, control, certifications)
    assert (admitted, code) == (True, ControlGateCode.CERTIFIED)
    assert "certification://claude-code/2026-09-18" in reason


def test_an_expired_certification_refuses_and_names_its_expiry(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    admitted, code, reason = _decide(CERTIFIED, ControlKind.RETRY, certifications, now=AFTER_EXPIRY)
    assert (admitted, code) == (False, ControlGateCode.EXPIRED)
    assert "claude-code 2.1.274" in reason and "expired at 2026-12-17" in reason


def test_a_version_no_certification_covers_is_refused_by_name(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    runtime = RunRuntimeTuple(harness="claude-code", harness_version="2.1.275")
    admitted, code, reason = _decide(runtime, ControlKind.RETRY, certifications)
    assert (admitted, code) == (False, ControlGateCode.UNCERTIFIED)
    assert reason == "claude-code 2.1.275 holds no certification"


def test_a_runtime_with_no_certification_at_all_is_refused(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    runtime = RunRuntimeTuple(harness="codex", harness_version="0.1.0")
    assert _decide(runtime, ControlKind.RETRY, certifications)[:2] == (
        False,
        ControlGateCode.UNCERTIFIED,
    )
    assert _decide(runtime, ControlKind.RETRY, ())[:2] == (False, ControlGateCode.UNCERTIFIED)


def test_an_unrecorded_version_is_refused_rather_than_matched(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    runtime = RunRuntimeTuple(harness="claude-code", model="claude-sonnet-4-5")
    admitted, code, reason = _decide(runtime, ControlKind.STEER, certifications)
    assert (admitted, code) == (False, ControlGateCode.VERSION_NOT_RECORDED)
    assert reason.startswith("claude-code ran at a version this Run did not record")
    assert "steer is refused" in reason
    assert "cancel, interrupt and reconcile stay available" in reason


def test_a_control_whose_capability_is_unsupported_is_refused(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    admitted, code, reason = _decide(CERTIFIED, ControlKind.RESUME, certifications)
    assert (admitted, code) == (False, ControlGateCode.CAPABILITY_UNCERTIFIED)
    assert "'session_resume'" in reason and "unsupported" in reason


def test_a_quarantined_tuple_is_refused_even_while_its_certification_is_current(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    admitted, code, _reason = _decide(
        CERTIFIED, ControlKind.RETRY, certifications, quarantined=True
    )
    assert (admitted, code) == (False, ControlGateCode.QUARANTINED)


def test_a_revoked_certification_is_refused(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    record = certifications[0]
    revoked = record.model_copy(
        update={
            "certification": record.certification.model_copy(
                update={"overall_status": "revoked", "revoked_at": BEFORE_EXPIRY}
            )
        }
    )
    admitted, code, reason = _decide(CERTIFIED, ControlKind.RETRY, (revoked,))
    assert (admitted, code) == (False, ControlGateCode.NOT_VERIFIED)
    assert "is revoked, not verified" in reason


STOPS = [ControlKind.CANCEL, ControlKind.INTERRUPT, ControlKind.RECONCILE]


@pytest.mark.parametrize("control", STOPS)
@pytest.mark.parametrize(
    ("runtime", "now", "quarantined", "gap"),
    [
        (CERTIFIED, AFTER_EXPIRY, False, ControlGateCode.EXPIRED),
        (CERTIFIED, BEFORE_EXPIRY, True, ControlGateCode.QUARANTINED),
        (
            RunRuntimeTuple(harness="claude-code", harness_version="2.1.275"),
            BEFORE_EXPIRY,
            False,
            ControlGateCode.UNCERTIFIED,
        ),
        (
            RunRuntimeTuple(harness="codex"),
            BEFORE_EXPIRY,
            False,
            ControlGateCode.VERSION_NOT_RECORDED,
        ),
    ],
    ids=["expired", "quarantined", "uncertified", "version-not-recorded"],
)
def test_a_stop_is_admitted_across_every_gap_and_keeps_the_gap_code(
    certifications: tuple[CertificationRecord, ...],
    control: ControlKind,
    runtime: RunRuntimeTuple,
    now: datetime,
    quarantined: bool,
    gap: ControlGateCode,
) -> None:
    admitted, code, reason = _decide(
        runtime, control, certifications, now=now, quarantined=quarantined
    )
    assert (admitted, code) == (True, gap)
    assert reason.startswith(f"{control.value} was admitted uncertified, as a stop always is: ")


@pytest.mark.parametrize("control", [c for c in ControlKind if c not in STOPS])
def test_every_other_control_is_still_refused_on_a_gap(
    certifications: tuple[CertificationRecord, ...], control: ControlKind
) -> None:
    runtime = RunRuntimeTuple(harness="codex", harness_version="0.1.0")
    admitted, code, _reason = _decide(runtime, control, certifications)
    assert (admitted, code) == (False, ControlGateCode.UNCERTIFIED)


def test_a_stop_on_a_certified_runtime_carries_no_uncertified_note(
    certifications: tuple[CertificationRecord, ...],
) -> None:
    admitted, code, reason = _decide(CERTIFIED, ControlKind.RECONCILE, certifications)
    assert (admitted, code) == (True, ControlGateCode.CERTIFIED)
    assert "uncertified" not in reason


def test_no_exports_mean_no_certifications(tmp_path: Path) -> None:
    assert runtime_certifications(tmp_path) == ()
