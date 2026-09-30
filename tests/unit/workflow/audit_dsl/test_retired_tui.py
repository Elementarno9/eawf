"""Tests for the TUI gate kinds retired with the epoch-1 application."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.common import GateSpec, _tier_for_gate_kind
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckSpec
from eawf.workflow.audit_dsl.kinds.retired_tui import RETIRED_TUI_DETAIL, check_retired_tui


@pytest.mark.parametrize("kind", ["affordance_parity", "tui_flow"])
def test_cr_026_a_retired_tui_kind_stays_registered_and_tiered(kind: str) -> None:
    assert CHECK_REGISTRY[kind] is check_retired_tui
    assert _tier_for_gate_kind(kind) is not None


@pytest.mark.parametrize(
    ("kind", "args"),
    [
        ("affordance_parity", {}),
        ("affordance_parity", {"mode": "home", "state_path": "x.json", "size": [120, 40]}),
        ("tui_flow", {"flow": "f", "key_sequence": ["q"], "terminal_state": {"mode": "home"}}),
    ],
)
def test_cr_026_a_retired_tui_gate_reports_blocked_whatever_its_args(
    kind: str, args: dict[str, object], tmp_path: Path
) -> None:
    result = CHECK_REGISTRY[kind](CheckSpec(kind=kind, name="g", args=args), tmp_path)
    assert (result.name, result.kind) == ("g", kind)
    assert (result.status, result.passed) == ("blocked", False)
    assert result.details == RETIRED_TUI_DETAIL


#: One gate row of each retired kind, as ``state.json`` persists them.
_HISTORIC_ROWS = (
    {
        "args": {"mode": "evidence"},
        "cadence": "every-wave",
        "criterion_id": "CR-02",
        "id": "G-02",
        "kind": "affordance_parity",
        "policy": "block",
        "required": True,
        "timeout_s": None,
    },
    {
        "args": {
            "flow": "open-evidence-mode",
            "key_sequence": ["6"],
            "scope": "repo",
            "size": [120, 40],
            "state_path": "tests/fixtures/states/valid/03-phase-iter-wave-active.json",
            "terminal_state": {"current_mode": "evidence"},
        },
        "cadence": "every-wave",
        "criterion_id": "CR-01",
        "id": "G-07",
        "kind": "tui_flow",
        "policy": "block",
        "required": True,
        "timeout_s": None,
    },
    {
        "args": {
            "golden_path": "tests/fixtures/mockup_image_diff/roadmap_board_reference.png",
            "mockup_png": "tests/fixtures/mockup_image_diff/roadmap_board_reference.png",
            "scope": "repo",
            "size": [120, 40],
            "state_path": "tests/fixtures/states/valid/03-phase-iter-wave-active.json",
            "tui_png": "<live>",
        },
        "cadence": "every-wave",
        "criterion_id": "CR-02",
        "id": "G-04",
        "kind": "mockup_golden_diff",
        "policy": "block",
        "required": True,
        "timeout_s": None,
    },
)

_REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.mark.parametrize("row", _HISTORIC_ROWS, ids=lambda row: str(row["kind"]))
def test_cr_026_a_historic_gate_row_loads_and_reports_blocked(row: dict[str, object]) -> None:
    gate = GateSpec.model_validate(row)
    spec = CheckSpec(kind=gate.kind, name=gate.id, args=gate.args)
    result = CHECK_REGISTRY[gate.kind](spec, _REPO_ROOT)
    assert (result.status, result.passed) == ("blocked", False)
    assert result.details == RETIRED_TUI_DETAIL
    assert "tests/snapshots/tui/console" in result.details
