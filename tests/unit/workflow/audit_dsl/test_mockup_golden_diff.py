"""Tests for the ``mockup_golden_diff`` audit-DSL kind."""

from __future__ import annotations

from pathlib import Path

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec
from eawf.workflow.audit_dsl.kinds.mockup_image_diff import LIVE_CAPTURE_SENTINEL
from eawf.workflow.audit_dsl.kinds.retired_tui import RETIRED_TUI_DETAIL

_GOLDEN_REL = "tests/snapshots/tui/golden/mockup_P30-I04-W08.txt"


def _run_mockup_check(args: dict[str, object], cwd: Path) -> CheckResult:
    spec = CheckSpec(kind="mockup_golden_diff", name="mockup_golden_diff", args=args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, cwd)


def test_mockup_golden_diff_registered_at_t5() -> None:
    assert "mockup_golden_diff" in CHECK_REGISTRY
    assert _tier_for_gate_kind("mockup_golden_diff") is OracleTier.T5_GOLDEN


def test_mockup_golden_diff_text_mode_is_blocked_on_the_retired_tui(tmp_path: Path) -> None:
    result = _run_mockup_check(
        {"golden_path": _GOLDEN_REL, "scope": "repo", "mode": "home", "size": [100, 30]},
        tmp_path,
    )
    assert (result.status, result.passed) == ("blocked", False)
    assert result.details == RETIRED_TUI_DETAIL


def test_mockup_golden_diff_live_image_mode_is_blocked_on_the_retired_tui(
    tmp_path: Path,
) -> None:
    mockup = tmp_path / "mockup.png"
    mockup.write_bytes(b"not decoded: the live capture is refused first")
    result = _run_mockup_check(
        {"golden_path": _GOLDEN_REL, "mockup_png": "mockup.png", "tui_png": LIVE_CAPTURE_SENTINEL},
        tmp_path,
    )
    assert (result.status, result.passed) == ("blocked", False)
    assert result.details == RETIRED_TUI_DETAIL


def test_mockup_golden_diff_invalid_args_fail_not_raise(tmp_path: Path) -> None:
    result = _run_mockup_check({"golden_path": "", "surprise": True}, tmp_path)
    assert result.status == "fail"
    assert "invalid args" in (result.details or "")


def test_mockup_golden_diff_missing_mockup_png_fails_before_the_live_capture(
    tmp_path: Path,
) -> None:
    result = _run_mockup_check(
        {"golden_path": _GOLDEN_REL, "mockup_png": "absent.png", "tui_png": LIVE_CAPTURE_SENTINEL},
        tmp_path,
    )
    assert result.status == "fail"
    assert "mockup_png=absent.png not found" in (result.details or "")
