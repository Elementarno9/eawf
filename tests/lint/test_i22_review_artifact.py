"""The pre-ship multi-agent review left its auditor report on record.

Three fresh-context lenses reviewed the phase diff; this suite pins the
persisted auditor_report row and its de-duplicated id scheme.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPORT_STORE = (
    _REPO_ROOT / "tests" / "fixtures" / "epoch1-snapshot" / "store" / "auditor_report.jsonl"
)


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
