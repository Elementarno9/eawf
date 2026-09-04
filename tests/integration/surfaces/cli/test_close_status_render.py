"""Human close-status rendering for exhausted bounded repair."""

from __future__ import annotations

from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.close import render_close_status

runner = CliRunner()


def _blocked_result() -> dict[str, Any]:
    return {
        "attempt": {
            "id": "close-repair",
            "wave_id": "P01-I01-W01",
            "status": "blocked",
            "integration_id": "integration-1",
            "integrated_sha": "a" * 40,
            "gate_receipt_ids": ["receipt-1"],
            "required_gate_ids": ["gate-1"],
            "failure_kind": "verification_blocked",
            "failure_detail_ref": ".ea/local/close-logs/close-repair.log",
            "required_operator_actions": ["split", "defer", "abort"],
        },
        "backgrounded": False,
    }


def test_render_close_status_shows_required_operator_actions() -> None:
    text = render_close_status(_blocked_result())

    assert "status=blocked" in text
    assert "operator action required: split / defer / abort" in text


@pytest.mark.parametrize(
    ("args", "expected_exit"),
    [
        (["close", "status", "P01-I01-W01"], 0),
        (["close", "follow", "P01-I01-W01", "--interval", "0.05"], 3),
    ],
)
def test_close_status_and_follow_show_required_operator_actions(
    args: list[str],
    expected_exit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _fake_call_close_rpc(**kwargs: Any) -> dict[str, Any]:
        return _blocked_result()

    monkeypatch.setattr(
        "eawf.surfaces.cli.commands.close.call_close_rpc",
        _fake_call_close_rpc,
    )

    result = runner.invoke(app, args)

    assert result.exit_code == expected_exit, result.output
    assert "operator action required: split / defer / abort" in result.output
