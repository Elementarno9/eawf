"""Standing visual + tui_flow gate census.

The rendered-reskin false-pass (INC-P30-03) and the too-blunt image
oracle (INC-P30-08) both survived because no BLOCKING visual gate stood
on any wave. W31 seeds the standing set: two live-mode
``mockup_golden_diff`` rows (Home roadmap board + Evidence mode, bound
to a committed fixture state so they are machine-independent), the
init-wizard fixture-pair row, and one ``tui_flow`` journey — all
``policy: block``. This census pins their presence in committed state
and the incident's close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_FIXTURES = _REPO_ROOT / "tests" / "fixtures" / "mockup_image_diff"

_LIVE_SENTINEL = "<live>"


def _all_gates() -> list[dict]:
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    gates: list[dict] = []
    for wave in (state.get("waves") or {}).values():
        gates.extend(wave.get("gates") or [])
    return gates


def test_state_carries_two_live_mockup_rows_and_one_tui_flow() -> None:
    """CR-01: >= 2 live-mode mockup_golden_diff rows + >= 1 tui_flow, all block."""
    gates = _all_gates()
    live_mockup = [
        gate
        for gate in gates
        if gate.get("kind") == "mockup_golden_diff"
        and (gate.get("args") or {}).get("tui_png") == _LIVE_SENTINEL
    ]
    tui_flows = [gate for gate in gates if gate.get("kind") == "tui_flow"]
    assert len(live_mockup) >= 2, f"only {len(live_mockup)} live-mode mockup rows"
    assert len(tui_flows) >= 1, "no tui_flow gate row in committed state"
    for gate in live_mockup + tui_flows:
        assert gate.get("policy") == "block", f"gate {gate.get('id')} is not blocking"


def test_standing_fixture_pairs_exist_and_are_committed() -> None:
    """CR-02: every referenced fixture resolves under the standing template dir."""
    gates = [
        gate
        for gate in _all_gates()
        if gate.get("kind") == "mockup_golden_diff" and (gate.get("args") or {}).get("mockup_png")
    ]
    assert gates, "no image-mode mockup_golden_diff rows found"
    for gate in gates:
        args = gate.get("args") or {}
        for key in ("golden_path", "mockup_png"):
            rel = args.get(key)
            assert rel, f"gate {gate.get('id')} missing {key}"
            assert (_REPO_ROOT / rel).is_file(), f"gate {gate.get('id')} {key} missing: {rel}"
        tui_png = args.get("tui_png")
        if tui_png and tui_png != _LIVE_SENTINEL:
            assert (_REPO_ROOT / tui_png).is_file()
        state_fixture = args.get("state_path")
        if state_fixture:
            assert (_REPO_ROOT / state_fixture).is_file(), (
                f"gate {gate.get('id')} binds a missing state fixture: {state_fixture}"
            )


def test_live_rows_bind_a_fixture_state_never_the_live_repo() -> None:
    """CR-02: machine-independence — live rows must not read the real .ea/."""
    for gate in _all_gates():
        if gate.get("kind") != "mockup_golden_diff":
            continue
        args = gate.get("args") or {}
        if args.get("tui_png") != _LIVE_SENTINEL:
            continue
        state_fixture = args.get("state_path") or ""
        assert state_fixture.startswith("tests/"), (
            f"live gate {gate.get('id')} binds {state_fixture!r}, not a committed fixture"
        )


def test_inc_p30_08_closed_with_store_row() -> None:
    """CR-03: the too-blunt-oracle incident is closed in state + store."""
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    incident = (state.get("incidents") or {}).get("INC-P30-08")
    assert incident is not None, "INC-P30-08 missing from state.incidents"
    assert incident["status"] == "resolved"
    assert "P30-I23-W30" in incident["corrective_action_ids"]
    assert "P30-I23-W31" in incident["corrective_action_ids"]
    store = (_REPO_ROOT / ".ea" / "store" / "incident.jsonl").read_text(encoding="utf-8")
    assert any(
        json.loads(line).get("id") == "INC-P30-08-CLOSE" for line in store.splitlines() if line
    ), "no INC-P30-08-CLOSE row in the incident store"
