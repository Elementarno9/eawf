"""The pre-ship multi-agent review artifact stays valid.

Three fresh-context lenses reviewed the phase diff; this suite pins the
committed synthesis artifact (chassis sections + parseable verdict), the
persisted auditor_report row, and the disposition contract (blockers
resolved, remaining findings carried as named followups).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ARTIFACT = _REPO_ROOT / ".ea" / "artifacts" / "reviews" / "2026-07-03-i22-preship-review.md"
_REPORT_STORE = _REPO_ROOT / ".ea" / "store" / "auditor_report.jsonl"


def test_artifact_carries_chassis_and_parseable_verdict() -> None:
    """CR-01: chassis sections + a parseable verdict field."""
    body = _ARTIFACT.read_text(encoding="utf-8")
    for section in ("## Summary", "## References", "## Provenance", "## Scrub"):
        assert section in body, f"missing chassis section {section}"
    match = re.search(r"\*\*verdict: (pass|pass-with-followups|fail|blocked)\*\*", body)
    assert match, "no parseable verdict field"
    assert match.group(1) in ("pass", "pass-with-followups")


def test_auditor_report_row_persisted_with_deduped_id() -> None:
    """CR-01: >= 1 auditor_report row for this wave (W16 id scheme)."""
    rows = [
        json.loads(line)
        for line in _REPORT_STORE.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    w35_rows = [r for r in rows if "P30-I23-W35" in (r.get("scope_id") or "")]
    assert w35_rows, "no auditor_report row for P30-I23-W35"
    assert any(re.match(r"^AR-auditor-P30-I23-W35-\d\d$", r.get("id") or "") for r in w35_rows), (
        "the W16 de-duped id scheme is not exercised"
    )


def test_blockers_are_disposed_not_open() -> None:
    """CR-02: every blocker row in the disposition table reads FIXED."""
    body = _ARTIFACT.read_text(encoding="utf-8")
    blocker_rows = [line for line in body.splitlines() if "| blocker |" in line]
    assert blocker_rows, "the disposition table lost its blocker row"
    for row in blocker_rows:
        assert "FIXED" in row, f"unresolved blocker in the shipped review: {row}"


def test_artifact_carries_no_machine_paths() -> None:
    body = _ARTIFACT.read_text(encoding="utf-8")
    assert "/" + "Users/" not in body
