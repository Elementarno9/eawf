"""Fresh deterministic evidence exists on committed state.

The phase's teeth metric: a close that executed real deterministic gates
leaves an ``evidence_kind=deterministic`` row in the evidence store. Zero
fresh rows would mean every recent close rode waivers — the exact
regression this iter exists to prevent.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_EVIDENCE = _REPO_ROOT / ".ea" / "store" / "evidence.jsonl"


def test_fresh_deterministic_pass_evidence_exists() -> None:
    """CR-02: >= 1 deterministic pass row minted since the I22 open."""
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    i22_open = state["iters"]["P30-I22"]["opened_at"]
    fresh = []
    for line in _EVIDENCE.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        payload = row.get("payload") or {}
        if (
            payload.get("evidence_kind") == "deterministic"
            and payload.get("status") == "pass"
            and (row.get("created_at") or "") >= i22_open
        ):
            fresh.append(row["id"])
    assert fresh, (
        "no deterministic pass evidence row created since the I22 open — "
        "the phase's teeth metric regressed to waiver-only closes"
    )
