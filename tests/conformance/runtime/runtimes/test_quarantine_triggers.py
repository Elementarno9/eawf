"""Nine triggers, nine quarantines, and the dispatch each one takes down.

The subject is what happens in the call the trigger fires in. Every member
of :class:`QuarantineTrigger` is exercised as its own negative fixture and
has to produce the same three facts at once: a failed rollback stage row
under the tuple, a certification of quarantined install trust that refuses
unattended dispatch, and disabled copies of the profiles bound to that
driver.

Route disablement is graded separately from profile disablement because
the two follow different rules. A profile goes because it is bound to the
quarantined driver; a route goes only when the quarantine leaves it with
no allowed profile at all, so a route holding one surviving profile is
deliberately left running.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import (
    CertificationFailureCode,
    ConformanceStageRecord,
    DriverCertification,
    QuarantineTrigger,
)
from eawf.kernel.runtime.provider import AgentProviderProfile, RoutePolicy
from eawf.runtime.runtimes.conformance import (
    ConformanceRunner,
    QuarantineRequest,
    RuntimeTuple,
)
from eawf.runtime.runtimes.quarantine import (
    TRIGGER_FAILURE_CODE,
    disable_dependents,
    failure_code_of,
    is_quarantined,
    reissue_profile,
)
from tests import _provider_helpers as fx

pytestmark = pytest.mark.conformance

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
EVIDENCE_REF = "artifact://quarantine/2026-09-17"
BUNDLE_REF = "artifact://conformance/bundle-2026-09-17"


class MemoryJournal:
    """Stage journal held in memory, one list per tuple digest."""

    def __init__(self) -> None:
        """Start with no record for any tuple."""
        self.rows: dict[str, list[ConformanceStageRecord]] = {}

    def append(self, *, tuple_digest: str, record: ConformanceStageRecord) -> None:
        """Append one stage record for one tuple."""
        self.rows.setdefault(tuple_digest, []).append(record)

    def records(self, *, tuple_digest: str) -> tuple[ConformanceStageRecord, ...]:
        """Return every stage record of one tuple, in append order."""
        return tuple(self.rows.get(tuple_digest, ()))


def runtime_tuple(**overrides: Any) -> RuntimeTuple:
    """Return the fixture runtime tuple, bound to the fixture driver."""
    document: dict[str, Any] = {
        "manifest_ref": fx.DRIVER_REF,
        "manifest_digest": fx.digest("a"),
        "distribution_version": "0.44.0",
        "sdk_or_server_version": "1.9.0",
        "auth_kind": "subscription",
        "model_family": "gpt-5",
        "os_class": "macos",
        "architecture": "aarch64",
        "managed_profile_digest": fx.digest("e"),
        "conformance_suite_version": "1.0.0",
    }
    document.update(overrides)
    return RuntimeTuple.model_validate(document)


def stage_row(stage: str, **overrides: Any) -> ConformanceStageRecord:
    """Return one passed stage record of *stage*."""
    document: dict[str, Any] = {
        "stage": stage,
        "outcome": "passed",
        "evidence_ref": EVIDENCE_REF,
        "started_at": NOW,
        "completed_at": NOW,
    }
    document.update(overrides)
    return ConformanceStageRecord.model_validate(document)


def certification(**overrides: Any) -> DriverCertification:
    """Return the verified certification in force for the fixture tuple."""
    covered = runtime_tuple()
    document: dict[str, Any] = {
        "schema_version": "driver-certification/v1",
        "certification_id": "codex-app-server-2026-09",
        "manifest_ref": covered.manifest_ref,
        "manifest_digest": covered.manifest_digest,
        "distribution_version": covered.distribution_version,
        "sdk_or_server_version": covered.sdk_or_server_version,
        "auth_kind": covered.auth_kind,
        "model_family": covered.model_family,
        "os_class": covered.os_class,
        "architecture": covered.architecture,
        "managed_profile_digest": covered.managed_profile_digest,
        "worker_protocol_version": "2.1.0",
        "semantic_protocol_version": "1.0.0",
        "event_codec_version": "1.0.0",
        "conformance_suite_version": covered.conformance_suite_version,
        "capabilities": [
            {
                "capability_id": "semantic_tools",
                "level": True,
                "status": "verified",
                "basis": "native",
                "evidence_ref": EVIDENCE_REF,
                "verified_at": NOW,
                "expires_at": NOW + timedelta(days=3650),
            }
        ],
        "overall_status": "verified",
        "install_trust": "managed",
        "runtime_facts": {
            "context_window_tokens": 200_000,
            "auto_compaction_threshold_tokens": 150_000,
            "project_document_cap_bytes": 32_768,
            "tool_output_cap_tokens": 25_000,
            "measured_at": NOW,
            "measurement_method": "observed",
        },
        "stage_history": [stage_row("probe"), stage_row("canary"), stage_row("certify")],
        "evidence_bundle_ref": BUNDLE_REF,
        "verified_at": NOW,
        "expires_at": NOW + timedelta(days=3650),
    }
    document.update(overrides)
    return DriverCertification.model_validate(document)


def profile(**overrides: Any) -> AgentProviderProfile:
    """Return the fixture profile, bound to the fixture driver."""
    return AgentProviderProfile.model_validate(fx.profile_document(**overrides))


def route(**overrides: Any) -> RoutePolicy:
    """Return the fixture mutating-task route."""
    return RoutePolicy.model_validate(fx.task_route_document(**overrides))


def runner() -> ConformanceRunner:
    """Return a runner over a memory journal and a frozen clock."""
    return ConformanceRunner(journal=MemoryJournal(), now=lambda: NOW)


def quarantine_request(**overrides: Any) -> QuarantineRequest:
    """Return a quarantine request over the fixture profile and route."""
    document: dict[str, Any] = {
        "runtime_tuple": runtime_tuple(),
        "trigger": QuarantineTrigger.CANARY_FAILURE,
        "certification": certification(),
        "profiles": (profile(),),
        "routes": (route(),),
        "evidence_ref": EVIDENCE_REF,
    }
    document.update(overrides)
    return QuarantineRequest.model_validate(document)


# ---- one negative fixture per trigger ---------------------------------------


@pytest.mark.parametrize("trigger", list(QuarantineTrigger))
def test_quarantine_moves_the_tuple_to_quarantined_on_every_trigger(
    trigger: QuarantineTrigger,
) -> None:
    """Each of the nine triggers quarantines the tuple in the same call."""
    result = runner().quarantine(quarantine_request(trigger=trigger))

    assert result.certification.install_trust == "quarantined"
    assert result.certification.quarantine_trigger is trigger
    assert result.certification.overall_status == "revoked"


@pytest.mark.parametrize("trigger", list(QuarantineTrigger))
def test_quarantine_records_the_failed_rollback_stage_at_once(
    trigger: QuarantineTrigger,
) -> None:
    """The stage row lands under the tuple before the call returns."""
    live = runner()

    result = live.quarantine(quarantine_request(trigger=trigger))

    recorded = live.stage_history(runtime_tuple())
    assert result.record.stage == "rollback"
    assert result.record.outcome == "failed"
    assert result.record.reason_code is TRIGGER_FAILURE_CODE[trigger]
    assert recorded == (result.record,)
    assert is_quarantined(recorded)


@pytest.mark.parametrize("trigger", list(QuarantineTrigger))
def test_quarantine_disables_dispatch_on_the_dependent_profile(
    trigger: QuarantineTrigger,
) -> None:
    """The profile bound to the quarantined driver comes back disabled."""
    result = runner().quarantine(quarantine_request(trigger=trigger))

    assert result.disabled.profile_ids == ("fixture_codex",)
    assert all(row.disabled for row in result.disabled.profiles)
    assert result.disabled.profiles[0].revision == profile().revision + 1


@pytest.mark.parametrize("trigger", list(QuarantineTrigger))
def test_quarantine_disables_every_route_that_requires_the_tuple(
    trigger: QuarantineTrigger,
) -> None:
    """A route whose only allowed profile is gone comes back disabled."""
    result = runner().quarantine(quarantine_request(trigger=trigger))

    assert result.disabled.route_ids == ("mutating-task",)
    assert all(row.disabled for row in result.disabled.routes)


@pytest.mark.parametrize("trigger", list(QuarantineTrigger))
def test_quarantined_certification_refuses_unattended_dispatch(
    trigger: QuarantineTrigger,
) -> None:
    """Dispatch is refused on the install-trust axis, whatever the trigger."""
    result = runner().quarantine(quarantine_request(trigger=trigger))

    decision = result.certification.decide_unattended_dispatch(
        required_capabilities=("semantic_tools",)
    )
    assert decision.admitted is False
    assert decision.refused_axis == "install_trust"


def test_every_trigger_maps_to_exactly_one_failure_code() -> None:
    """The trigger-to-code map is total over the closed trigger set."""
    assert set(TRIGGER_FAILURE_CODE) == set(QuarantineTrigger)
    assert all(isinstance(code, CertificationFailureCode) for code in TRIGGER_FAILURE_CODE.values())


def test_failure_code_of_rejects_a_trigger_outside_the_closed_set() -> None:
    """A trigger nobody enumerated has no code and raises."""
    with pytest.raises(KeyError):
        failure_code_of("invented_trigger")  # type: ignore[arg-type]


# ---- what a quarantine leaves running ---------------------------------------


def test_quarantine_leaves_a_profile_on_another_driver_running() -> None:
    """Only the profiles bound to the quarantined driver are disabled."""
    other = profile(profile_id="fixture_claude", driver_ref=fx.OTHER_DRIVER_REF)

    result = runner().quarantine(quarantine_request(profiles=(profile(), other)))

    assert result.disabled.profile_ids == ("fixture_codex",)


def test_quarantine_leaves_a_route_with_a_surviving_profile_running() -> None:
    """A route keeping one eligible profile never required the tuple."""
    other = profile(profile_id="fixture_claude", driver_ref=fx.OTHER_DRIVER_REF)
    shared = route(allowed_profiles=["fixture_codex", "fixture_claude"])

    result = runner().quarantine(quarantine_request(profiles=(profile(), other), routes=(shared,)))

    assert result.disabled.route_ids == ()


def test_quarantine_with_no_declared_dependents_disables_nothing() -> None:
    """Boundary: the empty profile and route sets disable nothing."""
    result = runner().quarantine(quarantine_request(profiles=(), routes=()))

    assert result.disabled.profiles == ()
    assert result.disabled.routes == ()
    assert result.certification.install_trust == "quarantined"


def test_disable_dependents_counts_an_already_disabled_profile_as_gone() -> None:
    """A route whose surviving profile was already disabled goes too."""
    dormant = profile(profile_id="fixture_claude", driver_ref=fx.OTHER_DRIVER_REF, disabled=True)
    shared = route(allowed_profiles=["fixture_codex", "fixture_claude"])

    disabled = disable_dependents(
        manifest_ref=fx.DRIVER_REF, profiles=(profile(), dormant), routes=(shared,)
    )

    assert disabled.route_ids == ("mutating-task",)
    assert disabled.profile_ids == ("fixture_codex",)


def test_disable_dependents_leaves_an_already_disabled_route_alone() -> None:
    """A route that was already out of service is not reissued."""
    disabled = disable_dependents(
        manifest_ref=fx.DRIVER_REF,
        profiles=(profile(),),
        routes=(route(disabled=True, revision=9),),
    )

    assert disabled.routes == ()


def test_reissue_profile_moves_the_revision_in_both_directions() -> None:
    """A reissued profile is always distinguishable from its original."""
    original = profile()

    off = reissue_profile(original, disabled=True)
    on = reissue_profile(off, disabled=False)

    assert (off.disabled, off.revision) == (True, original.revision + 1)
    assert (on.disabled, on.revision) == (False, original.revision + 2)


# ---- what the quarantine verb refuses ---------------------------------------


def test_quarantine_request_refuses_a_certification_of_another_driver() -> None:
    """A certification of another tuple cannot quarantine this one."""
    with pytest.raises(ValidationError, match="certification covers"):
        quarantine_request(certification=certification(manifest_ref=fx.OTHER_DRIVER_REF))


def test_quarantine_request_refuses_a_certification_of_another_digest() -> None:
    """The manifest digest is part of the tuple the certification covers."""
    with pytest.raises(ValidationError, match="manifest_digest"):
        quarantine_request(certification=certification(manifest_digest=fx.digest("f")))


def test_quarantine_request_refuses_an_unknown_trigger() -> None:
    """The trigger set is closed at the wire boundary."""
    with pytest.raises(ValidationError):
        quarantine_request(trigger="disk_full")


# ---- reading the quarantine back off the history ----------------------------


def test_is_quarantined_is_false_for_a_tuple_with_no_history() -> None:
    """Boundary: an empty history quarantines nothing."""
    assert is_quarantined(()) is False


def test_is_quarantined_is_false_while_only_refusals_were_recorded() -> None:
    """A refusal records that nothing ran, so it decides nothing."""
    refused = stage_row(
        "canary", outcome="refused", reason_code=CertificationFailureCode.SCHEMA_MISMATCH
    )

    assert is_quarantined((refused,)) is False


def test_is_quarantined_is_false_once_the_tuple_is_certified_again() -> None:
    """A later passed certify walks the tuple back into service."""
    live = runner()
    live.quarantine(quarantine_request())
    history = (*live.stage_history(runtime_tuple()), stage_row("certify"))

    assert is_quarantined(history) is False


def test_is_quarantined_ignores_a_refusal_appended_after_the_quarantine() -> None:
    """Off-by-one: the newest effective row decides, not the newest row."""
    live = runner()
    live.quarantine(quarantine_request())
    refused = stage_row(
        "certify", outcome="refused", reason_code=CertificationFailureCode.CANARY_IN_PROGRESS
    )

    assert is_quarantined((*live.stage_history(runtime_tuple()), refused)) is True


def test_a_second_trigger_keeps_the_tuple_quarantined() -> None:
    """Quarantining an already quarantined tuple stays consistent."""
    live = runner()
    first = live.quarantine(quarantine_request(trigger=QuarantineTrigger.SCHEMA_DRIFT))

    second = live.quarantine(
        quarantine_request(trigger=QuarantineTrigger.WRONG_AUTH, certification=first.certification)
    )

    assert second.certification.quarantine_trigger is QuarantineTrigger.WRONG_AUTH
    assert len(second.certification.stage_history) == 5
    assert is_quarantined(live.stage_history(runtime_tuple()))
