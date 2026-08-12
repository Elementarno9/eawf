"""EU capture is real: committed state carries a non-zero actual.

Every close before this wave rode the ``--no-runtime`` waiver because the
capture chain (statusline sidecar -> claim baseline -> SessionEnd hook ->
``runtime.capture`` -> close delta) had never fired end-to-end in a live
repo. W28 fired it: the claim stamped a sidecar baseline, the hook fed
``runtime.capture``, and the close recorded the delta. This suite pins the
result on committed state — the assertion holds forever after.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"


def _actuals() -> dict[str, dict]:
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    return state.get("actuals") or {}


def test_at_least_one_actual_carries_captured_runtime() -> None:
    """CR-01: >= 1 ActualSummary row with elapsed_eu > 0 and provenance."""
    captured = [
        row
        for row in _actuals().values()
        if row.get("elapsed_eu", 0) > 0 and row.get("harness") and row.get("model")
    ]
    assert len(captured) >= 3, (
        f"only {len(captured)} ActualSummary row(s) carry elapsed_eu > 0 with "
        "harness+model provenance — the I23 capture cohort (W28/W45/W46) "
        "regressed"
    )


def test_captured_actual_carries_cost_and_tokens() -> None:
    """The captured row records the billed side too, not only elapsed EU."""
    captured = [
        row for row in _actuals().values() if row.get("elapsed_eu", 0) > 0 and row.get("harness")
    ]
    assert captured
    assert any(
        row.get("actual_tokens", 0) > 0 or row.get("actual_cost_usd", 0) > 0 for row in captured
    )
