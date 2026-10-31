"""The v0.7.0 train as source declares it carries the two authority rulings it rests on.

Requirement rows proved here, by id:

- ``AUTH-001``: stabilization and epoch 2 belong to one release train. The rungs that
  stabilize on epoch-1 authority and the rungs that run epoch 2 are checkpoints of the
  same :data:`~eawf.workflow.release.train.V07_TRAIN`, walking to one target version,
  with every epoch-1 rung ahead of every epoch-2 rung.
- ``AUTH-050``: the checkpoint after ``dev1`` gates on the eight ``dev1`` gates plus
  ``migration``, ``hosted_gate_runner``, ``schema_strictness`` and ``waiver_count``, read
  off the rung the train declares rather than off a profile constant.
"""

from __future__ import annotations

from uuid import UUID

import pytest

from eawf.kernel.release.gate_binding import DEV1_GATES, profile_gates
from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
    ReleaseTrain,
    release_key,
)
from eawf.kernel.spec.release_config import ReleaseGateName
from eawf.workflow.release.train import V07_TARGET_VERSION, V07_TRAIN

pytestmark = pytest.mark.unit


def _epochs(train: ReleaseTrain) -> list[int]:
    """Return each rung's authority epoch, in train order."""
    return [checkpoint.authority_epoch for checkpoint in train.checkpoints]


def test_auth_001_one_train_carries_both_stabilization_and_epoch_two() -> None:
    epochs = _epochs(V07_TRAIN)

    assert set(epochs) == {1, 2}
    assert V07_TRAIN.target_version == V07_TARGET_VERSION


def test_auth_001_every_stabilization_rung_precedes_every_epoch_two_rung() -> None:
    epochs = _epochs(V07_TRAIN)

    assert epochs == sorted(epochs)


def test_auth_001_the_train_ends_at_its_stable_target_on_epoch_two() -> None:
    last = V07_TRAIN.checkpoints[-1]

    assert V07_TRAIN.checkpoint_for_version(V07_TARGET_VERSION) == last
    assert last.authority_epoch == 2


def test_auth_050_the_rung_after_dev1_gates_on_dev1_plus_four() -> None:
    first, second = V07_TRAIN.checkpoints[:2]

    assert profile_gates(first.gate_profile) == DEV1_GATES
    assert profile_gates(second.gate_profile) == (
        *DEV1_GATES,
        ReleaseGateName.MIGRATION,
        ReleaseGateName.HOSTED_GATE_RUNNER,
        ReleaseGateName.SCHEMA_STRICTNESS,
        ReleaseGateName.WAIVER_COUNT,
    )


def test_auth_050_dev1_admits_exactly_eight_gates() -> None:
    assert len(DEV1_GATES) == 8


# --- AUTH-009: a Release packages accepted Milestones and names no Track -------

_BUNDLE = "eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/{0}#MAB-0001-{0}"


def test_auth_009_a_release_carries_no_track_field() -> None:
    assert [name for name in Release.model_fields if "track" in name] == []


def test_auth_009_one_release_packages_milestones_of_several_tracks() -> None:
    refs = (_BUNDLE.format("MLS-0001"), _BUNDLE.format("MLS-0002"))

    release = Release(
        uid=UUID(int=9),
        key=release_key("0.7.0.dev3"),
        version="0.7.0.dev3",
        channel=ReleaseChannel.DEV,
        authority_epoch=2,
        status=ReleaseStatus.DRAFT,
        membership_refs=refs,
    )

    assert release.membership_refs == refs
