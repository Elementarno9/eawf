"""Each rung's canary evidence is read from its own export, never another rung's.

The ``provider`` and ``membership`` readiness rows, the tag-time membership
resolution and ``release create`` all read one committed canary export. Once a
second rung carries its own export, reading a single fixed location would let
dev4 be judged on dev3's record, or dev3 on dev4's. These checks pin the
per-release lookup against the real committed exports: each key loads its own
export, an unmapped key reads as absent, and an export filed under one release
while naming another is refused rather than lent to the rung it was filed for.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.release.signals import ReleaseSignalName, ReleaseSignalStatus
from eawf.kernel.state.epoch2.milestone import MilestoneStatus
from eawf.workflow.evidence.provider_certification import (
    CANARY_EVIDENCE_DIRS,
    canary_evidence_path,
    load_canary_evidence,
    membership_findings,
    provider_findings,
)
from eawf.workflow.release.admission import (
    checkpoint_release_config,
    committed_membership_refs,
)
from eawf.workflow.release.signal_probes import membership_probe, provider_probe
from eawf.workflow.release.train import CHECKPOINT_CONFIGS, V07_TRAIN
from eawf.workflow.verify.release_readiness import ReleaseSignalContext

pytestmark = pytest.mark.integration

#: This checkout, whose committed exports are the real evidence.
REPO_ROOT: Final = Path(__file__).resolve().parents[4]

DEV3_KEY: Final = "REL-0.7.0.dev3"
DEV4_VERSION: Final = "0.7.0.dev4"
DEV4_KEY: Final = f"REL-{DEV4_VERSION}"

#: A release no export directory is mapped for.
UNMAPPED_KEY: Final = "REL-0.7.0.dev2"

#: The dev4 walk record, whose Milestone reference is the dev4 membership ref.
DEV4_WALK: Final = REPO_ROOT.joinpath(
    *CANARY_EVIDENCE_DIRS[DEV4_KEY], "canary-acceptance-walk.json"
)


def dev4_walk_reference() -> str:
    """Return the membership reference the dev4 walk recorded."""
    walk: dict[str, Any] = json.loads(DEV4_WALK.read_text(encoding="utf-8"))
    reference: str = walk["milestone"]["reference"]
    return reference


def stage(tmp_path: Path, release_key: str, document: dict[str, Any]) -> Path:
    """Write *document* as *release_key*'s export of a checkout at *tmp_path*.

    Args:
        tmp_path: The staged checkout root.
        release_key: The release whose export location is written.
        document: The export payload.

    Returns:
        The staged checkout root.
    """
    path = canary_evidence_path(tmp_path, release_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=2), encoding="utf-8")
    return tmp_path


def committed_document(release_key: str) -> dict[str, Any]:
    """Return *release_key*'s committed export, decoded and mutable."""
    document: dict[str, Any] = json.loads(
        canary_evidence_path(REPO_ROOT, release_key).read_text(encoding="utf-8")
    )
    return document


def test_each_mapped_release_has_its_own_directory() -> None:
    assert CANARY_EVIDENCE_DIRS[DEV3_KEY][-1] == "2026-09-18-dev3-conformance"
    assert CANARY_EVIDENCE_DIRS[DEV4_KEY][-1] == "2026-09-26-dev4-conformance"


def test_the_dev3_key_loads_the_dev3_export() -> None:
    evidence = load_canary_evidence(REPO_ROOT, DEV3_KEY)

    assert evidence is not None
    assert evidence.release_key == DEV3_KEY


def test_the_dev4_key_loads_the_dev4_export() -> None:
    evidence = load_canary_evidence(REPO_ROOT, DEV4_KEY)

    assert evidence is not None
    assert evidence.release_key == DEV4_KEY
    assert canary_evidence_path(REPO_ROOT, DEV4_KEY) != canary_evidence_path(REPO_ROOT, DEV3_KEY)


def test_the_dev4_export_carries_the_dev3_certification_forward() -> None:
    dev3 = load_canary_evidence(REPO_ROOT, DEV3_KEY)
    dev4 = load_canary_evidence(REPO_ROOT, DEV4_KEY)

    assert dev3 is not None and dev4 is not None
    assert dev4.certifications == dev3.certifications
    assert dev4.advertised == dev3.advertised
    assert provider_findings(dev4) == ()


def test_an_unmapped_key_reads_as_no_export() -> None:
    assert load_canary_evidence(REPO_ROOT, UNMAPPED_KEY) is None


def test_an_unmapped_key_has_no_export_path() -> None:
    with pytest.raises(KeyError):
        canary_evidence_path(REPO_ROOT, UNMAPPED_KEY)


def test_every_authored_membership_rung_maps_a_loadable_export() -> None:
    """A rung that is cut and demands membership must have its own export to read.

    A rung with no authored configuration yet is exempt: it cannot be cut, so
    there is nothing to read evidence for.
    """
    rungs = [
        rung
        for rung in V07_TRAIN.checkpoints
        if rung.requires_membership and rung.version in CHECKPOINT_CONFIGS
    ]

    assert rungs
    for rung in rungs:
        assert rung.release_key in CANARY_EVIDENCE_DIRS, rung.release_key
        assert canary_evidence_path(REPO_ROOT, rung.release_key).is_file(), rung.release_key
        evidence = load_canary_evidence(REPO_ROOT, rung.release_key)
        assert evidence is not None, rung.release_key
        assert evidence.release_key == rung.release_key


def test_an_unmapped_key_names_the_missing_map_entry() -> None:
    """An unmapped key is remediated by mapping it, not by committing to a non-path."""
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    unmapped = config.model_copy(update={"version": UNMAPPED_KEY.removeprefix("REL-")})
    expected = f"no canary evidence directory is mapped for {UNMAPPED_KEY} in CANARY_EVIDENCE_DIRS"

    for signal, probe in (
        (ReleaseSignalName.PROVIDER, provider_probe),
        (ReleaseSignalName.MEMBERSHIP, membership_probe),
    ):
        outcome = probe(ReleaseSignalContext(unmapped, signal, None), repo_root=REPO_ROOT)
        assert expected in outcome.remediation, signal
        assert "committed at" not in outcome.remediation, signal


def test_a_mapped_key_without_an_export_names_its_path(tmp_path: Path) -> None:
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    location = "/".join(canary_evidence_path(Path(), DEV4_KEY).parts)

    outcome = provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=tmp_path
    )

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert f"no conformance certification export is committed at {location}," in (
        outcome.remediation
    )


def test_an_unmapped_key_leaves_the_provider_row_unavailable() -> None:
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    context = ReleaseSignalContext(
        config.model_copy(update={"version": UNMAPPED_KEY.removeprefix("REL-")}),
        ReleaseSignalName.PROVIDER,
        None,
    )

    outcome = provider_probe(context, repo_root=REPO_ROOT)

    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "no canary evidence directory is mapped" in outcome.remediation


def test_an_export_naming_another_release_is_refused(tmp_path: Path) -> None:
    root = stage(tmp_path, DEV4_KEY, committed_document(DEV3_KEY))

    with pytest.raises(ValueError, match=re.escape(f"names release {DEV3_KEY!r}")):
        load_canary_evidence(root, DEV4_KEY)


def test_an_export_naming_another_release_fails_both_rows(tmp_path: Path) -> None:
    root = stage(tmp_path, DEV4_KEY, committed_document(DEV3_KEY))
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)

    for signal, probe in (
        (ReleaseSignalName.PROVIDER, provider_probe),
        (ReleaseSignalName.MEMBERSHIP, membership_probe),
    ):
        outcome = probe(ReleaseSignalContext(config, signal, None), repo_root=root)
        assert outcome.status is ReleaseSignalStatus.FAIL, signal
        assert "does not read back" in outcome.remediation


def test_an_export_naming_another_release_is_refused_at_tag_time(tmp_path: Path) -> None:
    root = stage(tmp_path, DEV4_KEY, committed_document(DEV3_KEY))

    with pytest.raises(ValueError, match=re.escape(f"filed for {DEV4_KEY!r}")):
        committed_membership_refs(root, DEV4_KEY)


def test_dev4_membership_refs_are_exactly_the_dev4_walk_reference() -> None:
    reference = dev4_walk_reference()

    assert committed_membership_refs(REPO_ROOT, DEV4_KEY) == (reference,)


def test_the_dev4_membership_ref_resolves_completed() -> None:
    evidence = load_canary_evidence(REPO_ROOT, DEV4_KEY)
    reference = dev4_walk_reference()

    assert evidence is not None
    accepted = evidence.milestone_for(reference)
    assert accepted is not None
    assert accepted.status is MilestoneStatus.COMPLETED
    assert membership_findings(evidence, [reference]) == ()


def test_the_dev4_membership_ref_is_bound_to_the_dev4_walk() -> None:
    """The dev4 export's Milestone carries the dev4 walk's digest, not dev3's."""
    walk: dict[str, Any] = json.loads(DEV4_WALK.read_text(encoding="utf-8"))
    dev3 = load_canary_evidence(REPO_ROOT, DEV3_KEY)
    dev4 = load_canary_evidence(REPO_ROOT, DEV4_KEY)

    assert dev3 is not None and dev4 is not None
    accepted = dev4.milestone_for(dev4_walk_reference())
    assert accepted is not None
    assert accepted.bundle_digest == walk["bundle_digest"]
    earlier = dev3.milestone_for(dev4_walk_reference())
    assert earlier is not None
    assert earlier.bundle_digest != accepted.bundle_digest


def test_the_provider_and_membership_rows_pass_for_dev4() -> None:
    config = checkpoint_release_config(DEV4_VERSION, repo_root=REPO_ROOT)
    assert config.release_key == DEV4_KEY
    assert config.membership_refs == (dev4_walk_reference(),)

    provider = provider_probe(
        ReleaseSignalContext(config, ReleaseSignalName.PROVIDER, None), repo_root=REPO_ROOT
    )
    membership = membership_probe(
        ReleaseSignalContext(config, ReleaseSignalName.MEMBERSHIP, None), repo_root=REPO_ROOT
    )

    assert provider.status is ReleaseSignalStatus.PASS, provider.remediation
    assert membership.status is ReleaseSignalStatus.PASS, membership.remediation
