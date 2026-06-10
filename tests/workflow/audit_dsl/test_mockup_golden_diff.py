"""Tests for the ``mockup_golden_diff`` audit-DSL kind."""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.spec.common import OracleTier, _tier_for_gate_kind
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckResult, CheckSpec

_GOLDEN_REL = "tests/snapshots/tui/golden/mockup_P30-I04-W08.txt"


def _write_golden(root: Path, body: str) -> None:
    target = root / _GOLDEN_REL
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body + "\n", encoding="utf-8")


def _run_mockup_check(args: dict[str, object], cwd: Path) -> CheckResult:
    spec = CheckSpec(kind="mockup_golden_diff", name="mockup_golden_diff", args=args)
    return CHECK_REGISTRY["mockup_golden_diff"](spec, cwd)


def test_mockup_golden_diff_registered_at_t5() -> None:
    assert "mockup_golden_diff" in CHECK_REGISTRY
    assert _tier_for_gate_kind("mockup_golden_diff") is OracleTier.T5_GOLDEN


def test_mockup_golden_diff_passes_when_capture_matches(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    expected = "+---+\n| built screen |\n+---+"
    _write_golden(tmp_path, expected)
    seen: dict[str, object] = {}

    def _capture(**kwargs: object) -> str:
        seen.update(kwargs)
        return expected

    monkeypatch.setattr(
        "eawf.surfaces.tui.snapshot.pilot_harness.capture_mockup_golden_screen_text_sync",
        _capture,
    )

    result = _run_mockup_check(
        {
            "golden_path": _GOLDEN_REL,
            "scope": "repo",
            "mode": "home",
            "key_sequence": ["question_mark", "escape"],
            "size": [100, 30],
        },
        tmp_path,
    )

    assert result.status == "pass"
    assert result.passed is True
    assert result.details is not None
    assert "screen matches mockup golden" in result.details
    assert seen["scope"] == "repo"
    assert seen["mode"] == "home"
    assert seen["key_sequence"] == ["question_mark", "escape"]
    assert seen["size"] == (100, 30)


def test_mockup_golden_diff_fails_with_region_diff(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _write_golden(tmp_path, "+---+\n| expected |\n+---+")
    monkeypatch.setattr(
        "eawf.surfaces.tui.snapshot.pilot_harness.capture_mockup_golden_screen_text_sync",
        lambda **_kwargs: "+---+\n| actual |\n+---+",
    )

    result = _run_mockup_check({"golden_path": _GOLDEN_REL}, tmp_path)

    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "mockup golden mismatch" in result.details
    assert "region=@@" in result.details
    assert "-| expected |" in result.details
    assert "+| actual |" in result.details


def test_mockup_golden_diff_invalid_args_fail_not_raise(tmp_path: Path) -> None:
    result = _run_mockup_check({"golden_path": "", "surprise": True}, tmp_path)
    assert result.status == "fail"
    assert "invalid args" in (result.details or "")
