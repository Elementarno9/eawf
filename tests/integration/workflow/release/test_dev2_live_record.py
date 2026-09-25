"""The committed ``0.7.0.dev2`` record, read back through the release stores.

One claim over the record this repository actually ships, not over a fixture.

**The record is baked on three observed targets.** ``REL-0.7.0.dev2`` was walked
publish then reconcile then observe, and observe is the only verb that can write
``observed_success``: it reads the registry back and settles the leg against the
frozen manifest. So a baked record with three observed legs is a statement that
PyPI, npm and GitHub each served the artifact set the approval signed, and the
digest pinned here is the one the approval bound.

The gate receipts and the train advance past dev2 are pinned by
``tests/unit/workflow/release/test_dev2_advance_record.py``.
"""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.spec.release import ReleaseStatus
from eawf.workflow.release.records import read_release_record

RELEASE_KEY = "REL-0.7.0.dev2"
STATE_PATH = Path(__file__).resolve().parents[4] / ".ea" / "state.json"
APPROVED_MANIFEST_DIGEST = "sha256:" + (
    "3ea0e1250281a90d1840583cbd92c2751fc1c99b9d9f1824e69f740ea9c9e148"  # pragma: allowlist secret
)
PINNED_SOURCE_SHA = "d4f71dcffc5833e4d0498289d6d8db7cdafd54ac"  # pragma: allowlist secret
OBSERVED_TARGETS = ("github", "npm", "pypi")


def test_dev2_record_is_baked_on_three_observed_targets() -> None:
    """The committed record is baked on pypi, npm and github at the pinned source."""
    record = read_release_record(STATE_PATH, RELEASE_KEY)

    assert record is not None, f"{RELEASE_KEY} is absent from the release record collection"
    assert record.status is ReleaseStatus.BAKED
    assert record.manifest_digest == APPROVED_MANIFEST_DIGEST
    assert record.source_sha == PINNED_SOURCE_SHA

    statuses = record.target_statuses or {}
    assert tuple(sorted(statuses)) == OBSERVED_TARGETS
    for target in OBSERVED_TARGETS:
        assert statuses[target] == "observed_success", (
            f"target {target} settled at {statuses[target]!r}, so the record is not "
            f"baked on a read-back of every declared leg"
        )
