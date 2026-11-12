"""Every declared release train resolves from the version being acted on.

The release verbs used to read one hard-wired train, so a version outside
the 0.7.0 ladder could be neither swept, created nor shipped. These tests
pin the lookup, the one-rung 0.7.1 train and that the 0.7.0 ladder still
loads exactly as before.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.release.checkpoint_template import render_checkpoint_config, with_membership_refs
from eawf.kernel.spec.release import ReleaseGateProfile, release_key
from eawf.kernel.spec.release_config import (
    ReleaseConfigError,
    load_release_config,
    parse_release_config,
)
from eawf.workflow.evidence import provider_certification
from eawf.workflow.evidence.canary_export import prior_canary_evidence
from eawf.workflow.evidence.provider_certification import (
    canary_evidence_path,
    load_canary_evidence,
)
from eawf.workflow.release.admission import checkpoint_release_config
from eawf.workflow.release.advance import TrainAdvanceRecord, current_train, derive_train
from eawf.workflow.release.train import (
    CHECKPOINT_CONFIGS,
    RELEASE_TRAINS,
    V07_CONFIG_TEMPLATE,
    V07_TRAIN,
    V071_RELEASE_CONFIG_YAML,
    V071_TRAIN,
    V072_RELEASE_CONFIG_YAML,
    V072_TRAIN,
    V073_RELEASE_CONFIG_YAML,
    V073_TRAIN,
    checkpoint_config_yaml,
    train_for_key,
    train_for_version,
)
from eawf.workflow.verify.checkpoint_succession import (
    assert_predecessor_terminal,
    predecessor_rung,
)
from tests._release_helpers import accepted_canary_export

pytestmark = pytest.mark.unit

_REF = "milestone://eawf/MLS-0107"
_REPO_ROOT = Path(__file__).resolve().parents[4]


def test_train_for_version_resolves_each_train() -> None:
    assert train_for_version("0.7.0.dev1") is V07_TRAIN
    assert train_for_version("0.7.0rc4") is V07_TRAIN
    assert train_for_version("0.7.0") is V07_TRAIN
    assert train_for_version("0.7.1") is V071_TRAIN
    assert train_for_version("0.7.2") is V072_TRAIN
    assert train_for_version("0.7.3") is V073_TRAIN


def test_train_for_key_matches_train_for_version() -> None:
    assert train_for_key("REL-0.7.0") is V07_TRAIN
    assert train_for_key("REL-0.7.1") is V071_TRAIN
    assert train_for_key("REL-0.7.2") is V072_TRAIN
    assert train_for_key("REL-0.7.3") is V073_TRAIN


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
    assert RELEASE_TRAINS[-3:] == (V071_TRAIN, V072_TRAIN, V073_TRAIN)


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


def _stage_membership(monkeypatch: pytest.MonkeyPatch, repo_root: Path, key: str, ref: str) -> None:
    """Map *key* to an evidence directory and commit an export accepting *ref* there."""
    monkeypatch.setitem(
        provider_certification.CANARY_EVIDENCE_DIRS,
        key,
        (".ea", "artifacts", "evidence", f"{key}-stable"),
    )
    document = accepted_canary_export({"reference": ref})
    document["release_key"] = key
    path = canary_evidence_path(repo_root, key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_v072_patch_train(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert V072_TRAIN.train_id == "TRAIN-0.7.2"
    assert V072_TRAIN.target_version == "0.7.2"
    assert V072_TRAIN.current_checkpoint_index == 0
    assert len(V072_TRAIN.checkpoints) == 1
    rung = V072_TRAIN.checkpoints[0]
    assert rung.release_key == "REL-0.7.2"
    assert rung.gate_profile is ReleaseGateProfile.STABLE
    assert rung.authority_epoch == 2
    assert rung.requires_membership
    assert predecessor_rung(V072_TRAIN, "0.7.2") is None

    assert (
        render_checkpoint_config(rung=rung, template=V07_CONFIG_TEMPLATE)
        == V072_RELEASE_CONFIG_YAML
    )
    assert CHECKPOINT_CONFIGS["0.7.2"] == V072_RELEASE_CONFIG_YAML
    assert V071_RELEASE_CONFIG_YAML.replace("0.7.1", "0.7.2") == V072_RELEASE_CONFIG_YAML
    patch, previous = (
        parse_release_config(V072_RELEASE_CONFIG_YAML),
        parse_release_config(V071_RELEASE_CONFIG_YAML),
    )
    assert patch.targets == previous.targets
    assert patch.platform_claims == previous.platform_claims
    assert patch.runtime_claims == previous.runtime_claims

    with pytest.raises(ReleaseConfigError, match=r"'REL-0\.7\.2' requires non-empty"):
        load_release_config(V072_RELEASE_CONFIG_YAML, train=V072_TRAIN)
    loaded = load_release_config(
        with_membership_refs(V072_RELEASE_CONFIG_YAML, membership_refs=(_REF,)),
        train=V072_TRAIN,
    )
    assert loaded.release_key == "REL-0.7.2"

    ref = "milestone://eawf/MLS-0108"
    _stage_membership(monkeypatch, tmp_path, "REL-0.7.2", ref)
    resolved = checkpoint_release_config("0.7.2", repo_root=tmp_path)
    assert resolved.release_key == "REL-0.7.2"
    assert resolved.membership_refs == (ref,)

    _stage_membership(monkeypatch, tmp_path, "REL-0.7.1", _REF)
    assert checkpoint_release_config("0.7.1", repo_root=tmp_path).release_key == "REL-0.7.1"

    with pytest.raises(KeyError, match=r"no release configuration authored for '0\.7\.4'"):
        checkpoint_release_config("0.7.4", repo_root=tmp_path)


def test_v072_canary_row(tmp_path: Path) -> None:
    keys = list(provider_certification.CANARY_EVIDENCE_DIRS)
    assert keys.index("REL-0.7.2") == keys.index("REL-0.7.1") + 1
    assert (
        provider_certification.CANARY_EVIDENCE_DIRS["REL-0.7.2"]
        != provider_certification.CANARY_EVIDENCE_DIRS["REL-0.7.1"]
    )

    with pytest.raises(ValueError, match="canary_export_no_prior"):
        prior_canary_evidence(tmp_path, "REL-0.7.2")

    committed = canary_evidence_path(_REPO_ROOT, "REL-0.7.1")
    staged = canary_evidence_path(tmp_path, "REL-0.7.1")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(committed.read_bytes())
    previous = load_canary_evidence(tmp_path, "REL-0.7.1")
    assert previous is not None
    assert previous.advertised

    assert load_canary_evidence(tmp_path, "REL-0.7.2") is None
    carried = prior_canary_evidence(tmp_path, "REL-0.7.2")
    assert carried.release_key == "REL-0.7.1"
    assert carried.advertised == previous.advertised


def test_current_train_moves_to_the_second_patch_once_it_has_a_record() -> None:
    older = [rung.release_key for rung in V07_TRAIN.checkpoints]
    stays = current_train(RELEASE_TRAINS, recorded_keys=[*older, "REL-0.7.1"], advances=())
    assert stays.train_id == "TRAIN-0.7.1"

    moved = current_train(
        RELEASE_TRAINS, recorded_keys=[*older, "REL-0.7.1", "REL-0.7.2"], advances=()
    )
    assert moved.train_id == "TRAIN-0.7.2"
    assert moved.current_checkpoint_index == 0


def test_v073_patch_train(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    assert V073_TRAIN.train_id == "TRAIN-0.7.3"
    assert V073_TRAIN.target_version == "0.7.3"
    assert V073_TRAIN.current_checkpoint_index == 0
    assert len(V073_TRAIN.checkpoints) == 1
    rung = V073_TRAIN.checkpoints[0]
    assert rung.release_key == "REL-0.7.3"
    assert rung.gate_profile is ReleaseGateProfile.STABLE
    assert rung.authority_epoch == 2
    assert rung.requires_membership
    assert predecessor_rung(V073_TRAIN, "0.7.3") is None
    assert train_for_key("REL-0.7.3") is V073_TRAIN
    assert RELEASE_TRAINS[-1] is V073_TRAIN

    assert (
        render_checkpoint_config(rung=rung, template=V07_CONFIG_TEMPLATE)
        == V073_RELEASE_CONFIG_YAML
    )
    assert CHECKPOINT_CONFIGS["0.7.3"] == V073_RELEASE_CONFIG_YAML
    assert V072_RELEASE_CONFIG_YAML.replace("0.7.2", "0.7.3") == V073_RELEASE_CONFIG_YAML
    patch, previous = (
        parse_release_config(V073_RELEASE_CONFIG_YAML),
        parse_release_config(V072_RELEASE_CONFIG_YAML),
    )
    assert patch.targets == previous.targets
    assert patch.platform_claims == previous.platform_claims
    assert patch.runtime_claims == previous.runtime_claims

    with pytest.raises(ReleaseConfigError, match=r"'REL-0\.7\.3' requires non-empty"):
        load_release_config(V073_RELEASE_CONFIG_YAML, train=V073_TRAIN)
    loaded = load_release_config(
        with_membership_refs(V073_RELEASE_CONFIG_YAML, membership_refs=(_REF,)),
        train=V073_TRAIN,
    )
    assert loaded.release_key == "REL-0.7.3"

    ref = "milestone://eawf/MLS-0109"
    _stage_membership(monkeypatch, tmp_path, "REL-0.7.3", ref)
    resolved = checkpoint_release_config("0.7.3", repo_root=tmp_path)
    assert resolved.release_key == "REL-0.7.3"
    assert resolved.membership_refs == (ref,)


def test_v073_canary_row(tmp_path: Path) -> None:
    keys = list(provider_certification.CANARY_EVIDENCE_DIRS)
    assert keys.index("REL-0.7.3") == keys.index("REL-0.7.2") + 1
    assert keys[-1] == "REL-0.7.3"
    assert provider_certification.CANARY_EVIDENCE_DIRS["REL-0.7.3"] == (
        ".ea",
        "artifacts",
        "evidence",
        "2026-11-13-stable-native-linux",
    )
    assert (
        provider_certification.CANARY_EVIDENCE_DIRS["REL-0.7.3"]
        != provider_certification.CANARY_EVIDENCE_DIRS["REL-0.7.2"]
    )

    with pytest.raises(ValueError, match="canary_export_no_prior"):
        prior_canary_evidence(tmp_path, "REL-0.7.3")

    committed = canary_evidence_path(_REPO_ROOT, "REL-0.7.2")
    staged = canary_evidence_path(tmp_path, "REL-0.7.2")
    staged.parent.mkdir(parents=True)
    staged.write_bytes(committed.read_bytes())
    previous = load_canary_evidence(tmp_path, "REL-0.7.2")
    assert previous is not None
    assert previous.advertised

    assert load_canary_evidence(tmp_path, "REL-0.7.3") is None
    carried = prior_canary_evidence(tmp_path, "REL-0.7.3")
    assert carried.release_key == "REL-0.7.2"
    assert carried.advertised == previous.advertised


def test_current_train_moves_to_the_third_patch_once_it_has_a_record() -> None:
    older = [rung.release_key for rung in V07_TRAIN.checkpoints]
    patches = [*older, "REL-0.7.1", "REL-0.7.2"]
    stays = current_train(RELEASE_TRAINS, recorded_keys=patches, advances=())
    assert stays.train_id == "TRAIN-0.7.2"

    moved = current_train(RELEASE_TRAINS, recorded_keys=[*patches, "REL-0.7.3"], advances=())
    assert moved.train_id == "TRAIN-0.7.3"
    assert moved.current_checkpoint_index == 0
