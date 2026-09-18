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

What is asserted here is therefore an absence and its reason: no dev3 record,
an empty milestones array behind that absence, and a train still standing on
dev2. The cut belongs to the phase that can first produce an accepted Milestone.
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


def test_canary_evidence_accepted_no_milestone() -> None:
    """The empty milestones array is the reason dev3 cannot be opened."""
    evidence = json.loads(CANARY_EVIDENCE.read_text())

    assert evidence["milestones"] == [], (
        "the canary accepted a Milestone after all, so a membership_refs bundle now exists "
        "and the dev3 cut is no longer blocked on this"
    )
    assert evidence["release_key"] == DEV3_KEY


def test_train_still_stands_on_dev2() -> None:
    """No advance was recorded, so dev2 remains the open rung."""
    assert read_train_advances(STATE_PATH) == ()
