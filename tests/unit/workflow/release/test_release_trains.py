"""Every declared release train resolves from the version being acted on.

The release verbs used to read one hard-wired train, so a version outside
the 0.7.0 ladder could be neither swept, created nor shipped. These tests
pin the lookup, the one-rung 0.7.1 train and that the 0.7.0 ladder still
loads exactly as before.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from eawf.kernel.release.checkpoint_template import with_membership_refs
from eawf.kernel.spec.release import ReleaseGateProfile, release_key
from eawf.kernel.spec.release_config import load_release_config
from eawf.workflow.release.advance import TrainAdvanceRecord, current_train, derive_train
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    RELEASE_TRAINS,
    V07_TRAIN,
    V071_TRAIN,
    checkpoint_config_yaml,
    train_for_key,
    train_for_version,
)
from eawf.workflow.verify.checkpoint_succession import (
    assert_predecessor_terminal,
    predecessor_rung,
)

pytestmark = pytest.mark.unit

_REF = "milestone://eawf/MLS-0107"


def test_train_for_version_resolves_each_train() -> None:
    assert train_for_version("0.7.0.dev1") is V07_TRAIN
    assert train_for_version("0.7.0rc4") is V07_TRAIN
    assert train_for_version("0.7.0") is V07_TRAIN
    assert train_for_version("0.7.1") is V071_TRAIN


def test_train_for_key_matches_train_for_version() -> None:
    assert train_for_key("REL-0.7.0") is V07_TRAIN
    assert train_for_key("REL-0.7.1") is V071_TRAIN


def test_train_for_version_refuses_an_undeclared_version() -> None:
    with pytest.raises(KeyError, match=r"no release train declares a checkpoint 'REL-9\.9\.9'"):
        train_for_version("9.9.9")


def test_train_for_key_refuses_an_empty_key() -> None:
    with pytest.raises(KeyError, match="no release train declares"):
        train_for_key("")


def test_train_for_version_refuses_a_malformed_version() -> None:
    with pytest.raises(ValueError):
        train_for_version("not-a-version")


def test_trains_declare_disjoint_ladders_oldest_first() -> None:
    keys = [rung.release_key for train in RELEASE_TRAINS for rung in train.checkpoints]
    assert len(keys) == len(set(keys))
    assert RELEASE_TRAINS[-1] is V071_TRAIN


def test_the_patch_train_is_one_stable_rung() -> None:
    assert V071_TRAIN.train_id == "TRAIN-0.7.1"
    assert len(V071_TRAIN.checkpoints) == 1
    rung = V071_TRAIN.checkpoints[0]
    assert rung.release_key == release_key("0.7.1")
    assert rung.gate_profile is ReleaseGateProfile.STABLE
    assert rung.requires_membership


@pytest.mark.parametrize("version", sorted(CHECKPOINT_CONFIGS))
def test_every_authored_config_loads_against_its_own_train(version: str) -> None:
    train = train_for_version(version)
    source = checkpoint_config_yaml(version)
    document: str | Mapping[str, object] = source
    if train.checkpoint_for_version(version).requires_membership:
        document = with_membership_refs(source, membership_refs=(_REF,))
    config = load_release_config(document, train=train)
    assert config.release_key == release_key(version)


def test_the_patch_rung_succeeds_nothing() -> None:
    assert predecessor_rung(V071_TRAIN, "0.7.1") is None
    assert assert_predecessor_terminal(V071_TRAIN, version="0.7.1", predecessor=None) is None


def test_records_of_the_older_train_leave_the_patch_train_at_its_head() -> None:
    older = [rung.release_key for rung in V07_TRAIN.checkpoints]
    derived = derive_train(V071_TRAIN, recorded_keys=older, advances=())
    assert derived.current_checkpoint_index == 0
    assert derive_train(V07_TRAIN, recorded_keys=older, advances=()).current_checkpoint_index == (
        len(V07_TRAIN.checkpoints) - 1
    )


def _advance(train_id: str, closed: str, opened: str) -> TrainAdvanceRecord:
    return TrainAdvanceRecord(
        train_id=train_id,
        closed_key=closed,
        closed_revision=1,
        opened_key=opened,
        receipt_refs=("checkpoint-receipt://seed",),
        advanced_at=datetime.now(UTC),
        train_revision=1,
    )


def test_current_train_with_nothing_recorded_is_the_first_declared() -> None:
    assert current_train(RELEASE_TRAINS, recorded_keys=(), advances=()).train_id == "TRAIN-0.7.0"


def test_current_train_stays_on_the_older_train_until_the_patch_has_a_record() -> None:
    older = [rung.release_key for rung in V07_TRAIN.checkpoints]
    stays = current_train(RELEASE_TRAINS, recorded_keys=older, advances=())
    assert stays.train_id == "TRAIN-0.7.0"
    assert stays.current_checkpoint_index == len(V07_TRAIN.checkpoints) - 1

    moved = current_train(RELEASE_TRAINS, recorded_keys=[*older, "REL-0.7.1"], advances=())
    assert moved.train_id == "TRAIN-0.7.1"
    assert moved.current_checkpoint_index == 0


def test_current_train_counts_an_advance_as_touching_its_train() -> None:
    advance = _advance("TRAIN-0.7.0", "REL-0.7.0.dev1", "REL-0.7.0.dev2")
    derived = current_train((V07_TRAIN,), recorded_keys=(), advances=(advance,))
    assert derived.current_checkpoint_index == 1


def test_current_train_refuses_an_advance_onto_an_undeclared_rung() -> None:
    advance = _advance("TRAIN-0.7.1", "REL-0.7.1", "REL-0.7.2")
    with pytest.raises(ValueError, match="ladder does not declare"):
        current_train(RELEASE_TRAINS, recorded_keys=(), advances=(advance,))
