"""Standalone durable-close CLI default and compatibility tests."""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.surfaces.cli.app import app

runner = CliRunner()


def _attempt(status: str) -> dict[str, Any]:
    return {
        "attempt": {
            "id": "close-test",
            "wave_id": "P30-I23-W99",
            "status": status,
            "integration_id": "integration-test",
            "integrated_sha": "a" * 40,
            "gate_receipt_ids": [],
            "required_gate_ids": [],
        },
        "backgrounded": status != "closed",
    }


class _CloseClient:
    calls: ClassVar[list[str]] = []

    def __enter__(self) -> _CloseClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(
        self,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append(method)
        if method == "daemon.ping":
            return {"protocol_version": PROTOCOL_VERSION}
        if method in {"close.submit", "close.resume"}:
            return _attempt("queued")
        if method == "close.status":
            return _attempt("closed")
        raise AssertionError(f"unexpected close RPC: {method}")


@pytest.fixture(autouse=True)
def _fake_close_client(monkeypatch: pytest.MonkeyPatch) -> None:
    _CloseClient.calls = []
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        _CloseClient,
    )


def test_close_submit_noninteractive_waits_by_default() -> None:
    result = runner.invoke(
        app,
        ["close", "submit", "P30-I23-W99", "--outcome", "done"],
    )

    assert result.exit_code == 0, result.output
    assert _CloseClient.calls == [
        "daemon.ping",
        "close.submit",
        "daemon.ping",
        "close.status",
    ]
    assert "status=closed" in result.output


def test_close_submit_detach_returns_after_acceptance() -> None:
    result = runner.invoke(
        app,
        [
            "close",
            "submit",
            "P30-I23-W99",
            "--outcome",
            "done",
            "--detach",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _CloseClient.calls == ["daemon.ping", "close.submit"]
    assert "status=queued" in result.output


def test_close_submit_wait_and_detach_are_mutually_exclusive() -> None:
    result = runner.invoke(
        app,
        [
            "close",
            "submit",
            "P30-I23-W99",
            "--outcome",
            "done",
            "--wait",
            "--detach",
        ],
    )

    assert result.exit_code == 2
    assert "mutually exclusive" in result.output
    assert _CloseClient.calls == []
