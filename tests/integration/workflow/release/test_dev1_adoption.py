"""Adopting the out-of-band ``0.7.0.dev1`` publication into its record.

The version reached four targets while the release machinery held no
record of it, and the machinery could describe none of that: every state
past DRAFT asserts a pin, an approval or a dispatch, and none of the
three happened. What this module pins is the move that closes that --
``release.adopt`` writing the observed per-target facts onto the draft
record, and writing *only* those.

Three claims carry the weight, and each is asserted against the record
the operator verb actually produced rather than against a library
return value:

**observed, not asserted.** The adopted record carries one independent
read-back per target, including the ``plugins-dist`` leg the checkpoint
configuration never declared, and carries no ``approval_ref``, no
``manifest_digest``, no ``source_sha`` and no readiness sweep. Nothing
about the publication is inferred; everything in it was read back.

**not a bypass.** An adoption that leaves a configured leg unobserved is
refused, an observation that merely reports is refused, and a record
that already carries an approval cannot take an adoption at all. The
last of those is a model invariant, not a handler check, so the two
provenances are exclusive wherever a record is built.

**distinguishable.** ``approval_ref`` and ``adoption`` are mutually
exclusive on the record, so a reader telling an adopted checkpoint from
an approved one reads a typed field rather than interpreting the prose
inside a reference string.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.release import (
    AdoptedTargetObservation,
    Release,
    ReleaseAdoption,
    ReleaseStatus,
    ReleaseTargetStatus,
)
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.release_disposition import adopt
from eawf.workflow.evidence._io import atomic_write_state, load_state
from eawf.workflow.release.adoption import adopt_publication, unconfigured_targets
from eawf.workflow.release.records import read_release_record, record_envelope_id
from tests._release_helpers import (
    ADOPTION_INCIDENT_REF,
    NOW,
    SOURCE_SHA,
    TREE_SHA,
    adopted_observation,
    dev1_adoption,
    dev1_config,
    dev1_draft,
)

pytestmark = pytest.mark.integration

DEV1_VERSION = "0.7.0.dev1"
DEV1_KEY = f"REL-{DEV1_VERSION}"

#: The three legs the authored dev1 checkpoint declares.
CONFIGURED_TARGETS = ("pypi", "npm", "github")

#: The leg the publication reached that no configured target names.
UNCONFIGURED_TARGET = "plugins-dist"

_REPO_ROOT = Path(__file__).resolve().parents[4]
_EMPTY_STATE = _REPO_ROOT / "tests" / "fixtures" / "states" / "valid" / "01-empty-repo.json"


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return a throwaway state root the adoption may record into."""
    path = tmp_path / ".ea" / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_state(path, load_state(_EMPTY_STATE))
    return path


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a method context bound to the recording state root."""
    return MethodContext(
        started_at="2026-09-11T00:00:00+00:00",
        pid=7744,
        protocol_version="1",
        version=DEV1_VERSION,
        state_path=state_path,
    )


def adopt_params(record: Release, **overrides: Any) -> dict[str, Any]:
    """Return well-formed ``release.adopt`` params for *record*."""
    params: dict[str, Any] = {
        "release": record.model_dump(mode="json"),
        "expected_revision": record.revision,
        "adoption": dev1_adoption().model_dump(mode="json"),
    }
    params.update(overrides)
    return params


def adopt_via_rpc(ctx: MethodContext, **overrides: Any) -> dict[str, Any]:
    """Adopt a fresh dev1 draft through the daemon verb."""
    return asyncio.run(adopt(ctx, adopt_params(dev1_draft(), **overrides)))


# --- observed, not asserted -------------------------------------------


def test_adoption_records_every_observed_target_state(ctx: MethodContext) -> None:
    """The adopted record projects exactly what was read back."""
    result = adopt_via_rpc(ctx)

    assert result["observed_targets"] == {
        "github": "observed_mismatch",
        "npm": "observed_success",
        "plugins-dist": "observed_mismatch",
        "pypi": "observed_mismatch",
    }
    record = Release.model_validate(result["release"])
    assert dict(record.target_statuses) == {
        "github": ReleaseTargetStatus.OBSERVED_MISMATCH,
        "npm": ReleaseTargetStatus.OBSERVED_SUCCESS,
        "plugins-dist": ReleaseTargetStatus.OBSERVED_MISMATCH,
        "pypi": ReleaseTargetStatus.OBSERVED_MISMATCH,
    }


def test_adoption_asserts_no_approval_and_no_readiness_sweep(ctx: MethodContext) -> None:
    """Nothing the publication never had is written onto the record."""
    record = Release.model_validate(adopt_via_rpc(ctx)["release"])

    assert record.approval_ref is None
    assert record.manifest_ref is None
    assert record.manifest_digest is None
    assert record.source_sha is None
    assert record.source_tree_sha is None
    assert record.status is ReleaseStatus.DRAFT


def test_adoption_carries_its_reason_and_its_incident(ctx: MethodContext) -> None:
    """A record whose publication nobody governed says why it was adopted."""
    record = Release.model_validate(adopt_via_rpc(ctx)["release"])

    assert record.adoption is not None
    assert record.adoption.incident_ref == ADOPTION_INCIDENT_REF
    assert "no release record open" in record.adoption.reason


def test_adoption_names_the_target_the_checkpoint_never_declared(ctx: MethodContext) -> None:
    """A leg outside the configured set is recorded, not dropped."""
    result = adopt_via_rpc(ctx)

    assert result["unconfigured_targets"] == [UNCONFIGURED_TARGET]
    record = Release.model_validate(result["release"])
    assert record.adoption is not None
    observed = record.adoption.observed_target_statuses
    assert observed[UNCONFIGURED_TARGET] is ReleaseTargetStatus.OBSERVED_MISMATCH


def test_adopted_record_is_readable_back_from_the_collection(
    ctx: MethodContext, state_path: Path
) -> None:
    """An adoption only one RPC reply carried could not be found again."""
    result = adopt_via_rpc(ctx)

    settled = read_release_record(state_path, DEV1_KEY)
    assert settled is not None
    assert settled.adoption is not None
    assert record_envelope_id(settled) == result["release_record_id"]
    assert settled.revision == 1


def test_unconfigured_targets_is_empty_when_the_publication_stayed_inside(
    ctx: MethodContext,
) -> None:
    """The configured-only case reports no stray leg."""
    adoption = dev1_adoption(targets=CONFIGURED_TARGETS).model_dump(mode="json")
    result = adopt_via_rpc(ctx, adoption=adoption)

    assert result["unconfigured_targets"] == []


def test_unconfigured_targets_reads_the_configuration_not_the_record() -> None:
    """The helper answers over the authored target set."""
    assert unconfigured_targets(dev1_config(), dev1_adoption()) == (UNCONFIGURED_TARGET,)
    assert unconfigured_targets(dev1_config(), dev1_adoption(targets=("npm",))) == ()


# --- not a bypass ------------------------------------------------------


def test_adoption_refuses_to_leave_a_configured_leg_unobserved(ctx: MethodContext) -> None:
    """Half a publication adopted leaves the record blind to the rest."""
    with pytest.raises(DaemonValidationError) as excinfo:
        adopt_via_rpc(ctx, adoption=dev1_adoption(targets=("pypi",)).model_dump(mode="json"))

    assert "observes no state for configured targets" in str(excinfo.value)
    assert "'github'" in str(excinfo.value)
    assert "'npm'" in str(excinfo.value)


@pytest.mark.parametrize(
    "status",
    [
        ReleaseTargetStatus.REPORTED_SUCCESS,
        ReleaseTargetStatus.REPORTED_FAILURE,
        ReleaseTargetStatus.UNKNOWN,
        ReleaseTargetStatus.QUEUED,
        ReleaseTargetStatus.IN_FLIGHT,
        ReleaseTargetStatus.NOT_STARTED,
    ],
)
def test_adoption_refuses_a_row_that_reports_rather_than_observes(
    status: ReleaseTargetStatus,
) -> None:
    """A publisher's claim is not a read-back, and there was no publisher."""
    with pytest.raises(ValidationError, match="an adoption records an independent read-back"):
        adopted_observation("pypi", observed_status=status)


def test_adoption_refuses_an_empty_observation_set() -> None:
    """An adoption that observed nothing asserts effect it cannot see."""
    with pytest.raises(ValidationError, match="at least 1 item"):
        dev1_adoption(observations=())


def test_adoption_refuses_two_rows_for_one_target() -> None:
    """One leg cannot be recorded as both matched and diverged."""
    with pytest.raises(ValidationError, match="observes targets more than once"):
        ReleaseAdoption(
            adopted_at=NOW,
            reason="two readings of one leg",
            incident_ref=ADOPTION_INCIDENT_REF,
            observations=(adopted_observation("pypi"), adopted_observation("pypi")),
        )


def test_adoption_refuses_a_blank_reason() -> None:
    """A terminal write-up with no stated cause reads as an outcome."""
    with pytest.raises(ValidationError, match="at least 1 character"):
        dev1_adoption(reason="")


def test_adoption_accepts_a_reason_at_its_maximum_length() -> None:
    """The boundary length is admitted, one past it is not."""
    assert len(dev1_adoption(reason="x" * 1000).reason) == 1000
    with pytest.raises(ValidationError, match="at most 1000 characters"):
        dev1_adoption(reason="x" * 1001)


def test_adoption_refuses_a_record_that_already_carries_one(ctx: MethodContext) -> None:
    """Adopting twice would let a second reading replace the first."""
    adopted = Release.model_validate(adopt_via_rpc(ctx)["release"])

    with pytest.raises(DaemonValidationError, match="already carries an adoption"):
        asyncio.run(adopt(ctx, adopt_params(adopted)))


@pytest.mark.parametrize(
    "status",
    [ReleaseStatus.CANDIDATE, ReleaseStatus.PREFLIGHT_FAILED, ReleaseStatus.PARTIALLY_RELEASED],
)
def test_adoption_refuses_a_record_past_draft(status: ReleaseStatus) -> None:
    """An adoption belongs before any status the machinery itself made."""
    pinned = dev1_draft().model_copy(
        update={
            "status": status,
            "source_sha": SOURCE_SHA,
            "source_tree_sha": TREE_SHA,
            "manifest_ref": "artifact://release/manifest/0.7.0.dev1",
            "manifest_digest": f"sha256:{'c' * 64}",
            "approval_ref": "receipt://approval/dev1",
        }
    )

    with pytest.raises(ValueError, match="an adoption is recorded on a draft"):
        adopt_publication(
            Release.model_validate(pinned.model_dump(mode="json")),
            dev1_config(),
            adoption=dev1_adoption(),
        )


def test_adoption_and_approval_cannot_coexist_on_a_record() -> None:
    """The exclusion is a model invariant, not a handler courtesy."""
    with pytest.raises(ValidationError, match="carries an adoption and approval_ref"):
        Release.model_validate(
            dev1_draft()
            .model_copy(
                update={
                    "approval_ref": "receipt://approval/dev1",
                    "adoption": dev1_adoption(),
                    "target_statuses": dict(dev1_adoption().observed_target_statuses),
                }
            )
            .model_dump(mode="json")
        )


def test_adopted_projection_cannot_drift_from_the_observations() -> None:
    """The statuses a reader reads are the facts the adoption carries."""
    drifted = dict(dev1_adoption().observed_target_statuses)
    drifted["pypi"] = ReleaseTargetStatus.OBSERVED_SUCCESS

    with pytest.raises(ValidationError, match="disagree with target_statuses"):
        Release.model_validate(
            dev1_draft()
            .model_copy(update={"adoption": dev1_adoption(), "target_statuses": drifted})
            .model_dump(mode="json")
        )


def test_adoption_refuses_a_stale_revision(ctx: MethodContext) -> None:
    """A caller holding an old record is refused, not silently obeyed."""
    with pytest.raises(DaemonValidationError, match="stale_release_revision"):
        adopt_via_rpc(ctx, expected_revision=9)


def test_adoption_refuses_an_unknown_target_id_shape() -> None:
    """The target vocabulary is the same one the configuration uses."""
    with pytest.raises(ValidationError, match="String should match pattern"):
        AdoptedTargetObservation(
            target_id="PyPI",
            observed_status=ReleaseTargetStatus.OBSERVED_SUCCESS,
            observed_at=NOW,
            evidence_ref="observation://adopted/pypi",
            detail="uppercase is not a target id",
        )


# --- boundaries --------------------------------------------------------


def test_a_single_target_checkpoint_is_adopted_from_one_observation() -> None:
    """One configured leg, one read-back: the smallest complete adoption."""
    config = dev1_config(
        targets=[
            {
                "target_id": "npm",
                "required": True,
                "artifact_kinds": ["codex_plugin"],
                "observe_adapter": "npm_registry",
                "prerelease_dist_tag": "next",
                "stable_dist_tag": "latest",
                "timeout_seconds": 1800,
                "retry_limit": 2,
            }
        ]
    )

    adopted = adopt_publication(dev1_draft(), config, adoption=dev1_adoption(targets=("npm",)))

    assert adopted.adoption is not None
    assert len(adopted.adoption.observations) == 1
    assert adopted.revision == 1


def test_adoption_preserves_a_target_status_it_did_not_observe() -> None:
    """A prior projection entry survives beside the adopted rows."""
    seeded = Release.model_validate(
        dev1_draft()
        .model_copy(update={"target_statuses": {"legacy": ReleaseTargetStatus.NOT_STARTED}})
        .model_dump(mode="json")
    )

    adopted = adopt_publication(seeded, dev1_config(), adoption=dev1_adoption())

    assert adopted.target_statuses["legacy"] is ReleaseTargetStatus.NOT_STARTED
    assert adopted.target_statuses["pypi"] is ReleaseTargetStatus.OBSERVED_MISMATCH


def test_an_observation_may_be_taken_later_than_the_adoption_is_recorded() -> None:
    """The two stamps are independent facts and neither bounds the other."""
    later = adopted_observation("npm", observed_at=NOW + timedelta(days=3))

    adoption = dev1_adoption(observations=(later,))

    assert adoption.observations[0].observed_at > adoption.adopted_at


def test_adopted_record_keeps_the_identity_the_train_opened(ctx: MethodContext) -> None:
    """Adoption changes what the record knows, not which record it is."""
    draft = dev1_draft(UUID(int=901))
    result = asyncio.run(adopt(ctx, adopt_params(draft)))

    adopted = Release.model_validate(result["release"])
    assert adopted.uid == draft.uid
    assert adopted.key == draft.key
    assert adopted.authority_epoch == draft.authority_epoch
