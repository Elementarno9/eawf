"""The inputs the ``0.7.0.dev3`` cut reads are ready before the cut is taken.

``REL-0.7.0.dev3`` is cut after the phase branch merges and its tag is
pushed, because the manifest an approval binds is frozen from the receipts
the tag's publish jobs leave behind. What the cut reads from this commit
is asserted here, over the files this repository ships.

**The package is the checkpoint.** The version module reads dev3, so the
source the record will pin says it is dev3, and the changelog carries the
section the tag chokepoint mines, including the migration outcome it
refuses to infer from silence.

**The membership the cut names was accepted.** The train declares dev3
``requires_membership``, so ``release create`` refuses unless every
``membership_refs`` entry names an acceptance bundle of a Milestone the
canary walk completed. The reference is resolved here with that same
resolver over the committed canary export, so the check here and the
check the cut runs cannot drift apart.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf import __version__
from eawf.surfaces.cli.errors import UserError
from eawf.workflow.evidence.provider_certification import load_canary_evidence
from eawf.workflow.release.admission import assert_membership_resolves
from eawf.workflow.release.train import V07_TRAIN

REPO_ROOT = Path(__file__).resolve().parents[4]
CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DEV3_VERSION = "0.7.0.dev3"
DEV3_KEY = f"REL-{DEV3_VERSION}"

#: The acceptance bundle the dev3 cut names in ``--membership-ref``.
MEMBERSHIP_REF = (
    "eawf://WSP-W37CANARY/PRJ-W37CANARY/REP-W37CANARY/milestone/MLS-0001#MAB-0001-MLS-0001"
)


def dev3_section() -> str:
    """Return the dev3 changelog section, which must precede dev2's."""
    text = CHANGELOG.read_text(encoding="utf-8")
    heading = f"## [{DEV3_VERSION}]"
    assert heading in text, f"CHANGELOG.md has no {heading} section"
    start = text.index(heading)
    assert start < text.index("## [0.7.0.dev2]")
    return text[start : text.index("## [0.7.0.dev2]")]


def test_the_package_version_is_dev3() -> None:
    assert __version__ == DEV3_VERSION


def test_the_changelog_section_states_its_migration_and_limitations() -> None:
    section = dev3_section()

    assert "### Migration" in section
    assert "### Limitations" in section
    assert section.count("\n- ") >= 3


def test_dev3_is_the_membership_rung_on_epoch2_authority() -> None:
    rung = V07_TRAIN.checkpoint_for_version(DEV3_VERSION)

    assert rung.release_key == DEV3_KEY
    assert rung.authority_epoch == 2
    assert rung.requires_membership is True


def test_the_membership_ref_resolves_the_way_release_create_resolves_it() -> None:
    """The committed export records the bundle as a COMPLETED Milestone."""
    evidence = load_canary_evidence(REPO_ROOT)
    assert evidence is not None, "no canary evidence export is committed"

    assert_membership_resolves(evidence, [MEMBERSHIP_REF])
    accepted = evidence.milestone_for(MEMBERSHIP_REF)
    assert accepted is not None
    assert accepted.status.value == "COMPLETED"
    assert evidence.release_key == DEV3_KEY


def test_a_ref_the_canary_never_accepted_is_refused_by_the_same_resolver() -> None:
    """Gate-fire: the resolver the cut runs reds on an invented bundle."""
    evidence = load_canary_evidence(REPO_ROOT)
    invented = MEMBERSHIP_REF.replace("MLS-0001", "MLS-9999")

    with pytest.raises(UserError) as caught:
        assert_membership_resolves(evidence, [invented])
    assert caught.value.kind == "membership_unresolved"
