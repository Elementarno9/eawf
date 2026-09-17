"""An absent version right after a reported success is a retry, not a miss.

Under test: a ``version_absent`` read-back of a leg standing at
``reported_success`` settles nothing while the read-back falls inside
:data:`PROPAGATION_WINDOW` of that report -- it raises
:class:`InconclusiveObservationError` naming when to read again, writes
no status and leaves the record out of ``RECOVERING`` -- while the same
read-back at or after the window's end routes the leg to
``observed_mismatch`` and the record to ``RECOVERING``. The window
covers absence only: a contradicting digest or a match settles at once,
and a leg that never reported success has no window to wait out.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from uuid import UUID

import pytest

from eawf.kernel.spec.publication import PublicationOperation, require_attempt
from eawf.kernel.spec.release import Release, ReleaseStatus, ReleaseTargetStatus
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.adapters import collect_observation
from eawf.workflow.release.observation import ObservationCode, PublicationObservation
from eawf.workflow.release.publication import begin_publication
from eawf.workflow.release.settlement import (
    PROPAGATION_WINDOW,
    InconclusiveObservationError,
    observe_target,
    propagation_window_end,
)
from eawf.workflow.release.target_machine import advance_target_attempt
from eawf.workflow.verify.release_readiness import compute_readiness
from tests._release_helpers import (
    MANIFEST_DIGEST,
    NOW,
    all_passing,
    dev1_config,
    read_back_request,
    recorded_response,
    release_record,
    verifying_publication,
)

pytestmark = pytest.mark.unit

OPERATION_ID = UUID(int=404)
TARGET_IDS = ("pypi", "npm", "github")

#: The recorded answers that expose no version: a registry answering
#: 404, and an npm packument cached before the version was added.
ABSENT_CASES = (("pypi", "missing"), ("npm", "missing"), ("npm", "stale"), ("github", "missing"))


def observation(target_id: str, case: str) -> PublicationObservation:
    """Return the observation the recorded *case* supports for *target_id*."""
    return collect_observation(
        read_back_request(target_id), response=recorded_response(target_id, case), observed_at=NOW
    )


def verifying() -> tuple[Release, PublicationOperation, ReleaseConfig]:
    """Return a record at VERIFYING with every leg at reported_success."""
    return verifying_publication(operation_id=OPERATION_ID)


def reported_at(operation: PublicationOperation, target_id: str) -> datetime:
    """Return the instant *target_id* reported success."""
    settled_at = require_attempt(operation, target_id).settled_at
    assert settled_at is not None
    return settled_at


# --- inside the window ----------------------------------------------------


@pytest.mark.parametrize(("target_id", "case"), ABSENT_CASES)
@pytest.mark.parametrize(
    "elapsed",
    (timedelta(0), timedelta(seconds=1), PROPAGATION_WINDOW - timedelta(microseconds=1)),
)
def test_observe_target_retries_an_absent_version_inside_the_window(
    target_id: str, case: str, elapsed: timedelta
) -> None:
    release, operation, config = verifying()
    read_back = observation(target_id, case)
    assert read_back.code is ObservationCode.VERSION_ABSENT
    now = reported_at(operation, target_id) + elapsed
    with pytest.raises(InconclusiveObservationError) as excinfo:
        observe_target(release, config, operation, observation=read_back, now=now)
    assert excinfo.value.target_id == target_id
    assert excinfo.value.code is ObservationCode.VERSION_ABSENT
    assert excinfo.value.retry_after == reported_at(operation, target_id) + PROPAGATION_WINDOW


def test_observe_target_names_the_retry_instant_in_the_refusal() -> None:
    release, operation, config = verifying()
    retry_after = reported_at(operation, "npm") + PROPAGATION_WINDOW
    with pytest.raises(InconclusiveObservationError) as excinfo:
        observe_target(
            release,
            config,
            operation,
            observation=observation("npm", "stale"),
            now=reported_at(operation, "npm"),
        )
    message = str(excinfo.value)
    assert message.startswith("observation_inconclusive:")
    assert "'version_absent'" in message
    assert "may still be propagating" in message
    assert retry_after.isoformat() in message


def test_observe_target_inside_the_window_writes_nothing() -> None:
    release, operation, config = verifying()
    before_release = release.model_dump(mode="json")
    before_operation = operation.model_dump(mode="json")
    with pytest.raises(InconclusiveObservationError):
        observe_target(
            release,
            config,
            operation,
            observation=observation("pypi", "missing"),
            now=reported_at(operation, "pypi"),
        )
    assert release.model_dump(mode="json") == before_release
    assert operation.model_dump(mode="json") == before_operation
    assert release.status is ReleaseStatus.VERIFYING
    assert require_attempt(operation, "pypi").status is ReleaseTargetStatus.REPORTED_SUCCESS


def test_observe_target_after_a_retry_still_settles_the_leg_that_appeared() -> None:
    release, operation, config = verifying()
    early = reported_at(operation, "pypi")
    with pytest.raises(InconclusiveObservationError):
        observe_target(
            release, config, operation, observation=observation("pypi", "missing"), now=early
        )
    observed, settled = observe_target(
        release,
        config,
        operation,
        observation=observation("pypi", "match"),
        now=early + timedelta(minutes=1),
    )
    assert require_attempt(settled, "pypi").status is ReleaseTargetStatus.OBSERVED_SUCCESS
    assert observed.status is ReleaseStatus.VERIFYING


def test_observe_target_retries_a_read_back_stamped_before_the_report() -> None:
    release, operation, config = verifying()
    with pytest.raises(InconclusiveObservationError):
        observe_target(
            release,
            config,
            operation,
            observation=observation("github", "missing"),
            now=reported_at(operation, "github") - timedelta(seconds=1),
        )


# --- at and after the window ----------------------------------------------


@pytest.mark.parametrize(("target_id", "case"), ABSENT_CASES)
@pytest.mark.parametrize("overrun", (timedelta(0), timedelta(seconds=1), timedelta(days=2)))
def test_observe_target_routes_an_absent_version_after_the_window_to_recovering(
    target_id: str, case: str, overrun: timedelta
) -> None:
    release, operation, config = verifying()
    now = reported_at(operation, target_id) + PROPAGATION_WINDOW + overrun
    recovering, settled = observe_target(
        release, config, operation, observation=observation(target_id, case), now=now
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    row = require_attempt(settled, target_id)
    assert row.status is ReleaseTargetStatus.OBSERVED_MISMATCH
    assert recovering.target_statuses[target_id] is ReleaseTargetStatus.OBSERVED_MISMATCH


# --- what the window does not cover ---------------------------------------


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_observe_target_routes_a_digest_mismatch_inside_the_window_at_once(
    target_id: str,
) -> None:
    release, operation, config = verifying()
    recovering, settled = observe_target(
        release,
        config,
        operation,
        observation=observation(target_id, "mismatch"),
        now=reported_at(operation, target_id),
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_MISMATCH


@pytest.mark.parametrize("target_id", TARGET_IDS)
def test_observe_target_settles_a_match_inside_the_window_at_once(target_id: str) -> None:
    release, operation, config = verifying()
    _observed, settled = observe_target(
        release,
        config,
        operation,
        observation=observation(target_id, "match"),
        now=reported_at(operation, target_id),
    )
    assert require_attempt(settled, target_id).status is ReleaseTargetStatus.OBSERVED_SUCCESS


def test_observe_target_routes_an_absent_version_on_a_timed_out_leg_at_once() -> None:
    config = dev1_config()
    published, operation = begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        config,
        compute_readiness(config, probes=all_passing(), computed_at=NOW),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key="publish-0.7.0.dev1-404",
        proof_digest=f"sha256:{'1' * 64}",
        opened_at=NOW,
    )
    target = next(target for target in config.targets if target.target_id == "pypi")
    row = require_attempt(operation, "pypi")
    operation = advance_target_attempt(
        operation, target=target, to=ReleaseTargetStatus.IN_FLIGHT, now=row.started_at
    )
    operation = advance_target_attempt(
        operation, target=target, to=ReleaseTargetStatus.UNKNOWN, now=row.deadline_at
    )
    recovering, settled = observe_target(
        published,
        config,
        operation,
        observation=observation("pypi", "missing"),
        now=row.deadline_at,
        effect_receipt_ref="receipt://target/effect",
    )
    assert recovering.status is ReleaseStatus.RECOVERING
    assert require_attempt(settled, "pypi").status is ReleaseTargetStatus.OBSERVED_MISMATCH


def test_observe_target_still_refuses_an_unreachable_registry_without_a_retry_instant() -> None:
    release, operation, config = verifying()
    with pytest.raises(InconclusiveObservationError) as excinfo:
        observe_target(
            release, config, operation, observation=observation("npm", "unknown"), now=NOW
        )
    assert excinfo.value.retry_after is None
    assert "propagating" not in str(excinfo.value)


def test_observe_target_refuses_a_naive_instant_before_the_window_is_read() -> None:
    release, operation, config = verifying()
    with pytest.raises(ValueError, match="now must be timezone-aware"):
        observe_target(
            release,
            config,
            operation,
            observation=observation("pypi", "missing"),
            now=datetime(2026, 9, 4, 12, 0),
        )


# --- propagation_window_end -------------------------------------------------


def test_propagation_window_end_is_the_report_plus_the_window() -> None:
    _release, operation, _config = verifying()
    for target_id in TARGET_IDS:
        assert propagation_window_end(operation, target_id) == (
            reported_at(operation, target_id) + PROPAGATION_WINDOW
        )


def test_propagation_window_end_for_a_leg_that_never_reported_is_none() -> None:
    config = dev1_config()
    _published, operation = begin_publication(
        release_record(status=ReleaseStatus.APPROVED, approval_ref="receipt://approval/dev1"),
        config,
        compute_readiness(config, probes=all_passing(), computed_at=NOW),
        operation_id=OPERATION_ID,
        approved_manifest_digest=MANIFEST_DIGEST,
        idempotency_key="publish-0.7.0.dev1-404",
        proof_digest=f"sha256:{'1' * 64}",
        opened_at=NOW,
    )
    assert propagation_window_end(operation, "pypi") is None


def test_propagation_window_end_for_an_unattempted_target_is_none() -> None:
    _release, operation, _config = verifying()
    assert propagation_window_end(operation, "crates") is None


def test_propagation_window_end_for_an_observed_leg_is_none() -> None:
    release, operation, config = verifying()
    _observed, settled = observe_target(
        release,
        config,
        operation,
        observation=observation("pypi", "match"),
        now=reported_at(operation, "pypi"),
    )
    assert propagation_window_end(settled, "pypi") is None


def test_the_propagation_window_outlasts_every_registry_cache_lifetime() -> None:
    longest_cache = timedelta(seconds=900)
    assert 2 * longest_cache <= PROPAGATION_WINDOW
