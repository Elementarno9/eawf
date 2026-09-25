"""``0.7.0.dev3`` has no record, and the store says why.

The dev3 rung is the first checkpoint on epoch-2 authority, and the train
declares it ``requires_membership``. Opening it means ``eawf release create``
with ``membership_refs`` naming an acceptance bundle of a Milestone that
actually reached acceptance. The canary rehearsal that was supposed to produce
one did not: it recorded its run and stopped short of accepting a Milestone, so
its evidence file carries an empty ``milestones`` array and there is no bundle
to name.

The trap this module pins is that the refusal arrives late. ``release create``
validates only that ``membership_refs`` is non-empty, so a plausible-looking but
invented reference is accepted at the cut and the checkpoint then fails its
membership gate at preflight instead, with a record already on file claiming a
membership that was never accepted. Cutting dev3 with a placeholder would have
looked like progress and left a false record behind, so the cut was deferred
rather than forced.

A later canary walk did accept one: the evidence export now records a
COMPLETED Milestone whose bundle a ``membership_refs`` entry can name. What is
asserted here is therefore an absence that is no longer forced: no dev3 record
yet, an accepted Milestone ready to be named by the cut, and a train still
standing on dev2 until that cut is taken.
"""

from __future__ import annotations

import json
from pathlib import Path

from eawf.kernel.spec.release import ReleaseStatus
from eawf.workflow.release.records import read_release_records
from eawf.workflow.release.train_store import read_train_advances

REPO_ROOT = Path(__file__).resolve().parents[4]
STATE_PATH = REPO_ROOT / ".ea" / "state.json"
CANARY_EVIDENCE = (
    REPO_ROOT
    / ".ea"
    / "artifacts"
    / "evidence"
    / "2026-09-18-dev3-conformance"
    / "native-canary-evidence.json"
)
DEV3_KEY = "REL-0.7.0.dev3"
DEV2_KEY = "REL-0.7.0.dev2"


def test_dev3_has_no_release_record_on_file() -> None:
    """The collection holds dev1 and dev2 and stops there."""
    records = read_release_records(STATE_PATH)

    assert DEV3_KEY not in records, (
        f"{DEV3_KEY} has a record, but no accepted Milestone acceptance bundle exists to "
        f"name in its membership_refs, so any record here was cut against an invented reference"
    )
    assert DEV2_KEY in records
    assert records[DEV2_KEY].status is ReleaseStatus.BAKED


def test_canary_evidence_records_an_accepted_milestone_ahead_of_the_cut() -> None:
    """A COMPLETED Milestone is on file, so the cut has a bundle to name."""
    evidence = json.loads(CANARY_EVIDENCE.read_text())

    assert [row["status"] for row in evidence["milestones"]] == ["COMPLETED"]
    assert evidence["release_key"] == DEV3_KEY


def test_train_still_stands_on_dev2() -> None:
    """No advance was recorded, so dev2 remains the open rung."""
    assert read_train_advances(STATE_PATH) == ()
