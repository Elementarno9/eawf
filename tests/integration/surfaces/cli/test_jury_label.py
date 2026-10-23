"""``eawf jury label`` — the gold-label writer surface.

The CLI proxies the daemon's ``jury.label`` RPC; the daemon refuses a label
whose wave the bound tree does not hold, or whose reason is too short, and
writes nothing.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf import __version__
from eawf.observability.eval.jury_validation import GoldLabel
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.jury import label as jury_label_rpc
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import jury as jury_cmd
from tests._epoch2_helpers import lay_epoch2_tree

pytestmark = pytest.mark.integration

_WAVE_ID = "P29-I04-W08"


def _read_gold_labels(state_path: Path) -> list[GoldLabel]:
    """Return the gold labels the writer appended beside *state_path*, oldest first."""
    store = state_path.parent / "store" / "gold_label.jsonl"
    if not store.is_file():
        return []
    lines = store.read_text(encoding="utf-8").splitlines()
    return [GoldLabel.model_validate_json(line) for line in lines if line.strip()]


def _ctx(state_path: Path) -> MethodContext:
    return MethodContext(
        started_at="2026-07-02T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        state_path=state_path,
        idempotency_cache={},
    )


# ---- daemon RPC --------------------------------------------------------------


def test_jury_label_unknown_wave_refused_no_write(tmp_path: Path) -> None:
    state_path = lay_epoch2_tree(tmp_path)
    ctx = _ctx(state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="unknown wave"):
            await jury_label_rpc(
                ctx,
                {
                    "wave_id": "P99-I99-W99",
                    "ground_truth": True,
                    "reason": "this label must never be written anywhere",
                },
            )

    asyncio.run(body())
    assert _read_gold_labels(state_path) == []


def test_jury_label_short_reason_refused(tmp_path: Path) -> None:
    """The >= 20-char reason floor forces a real rationale."""
    state_path = lay_epoch2_tree(tmp_path)
    ctx = _ctx(state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="validation_failed"):
            await jury_label_rpc(
                ctx,
                {"wave_id": _WAVE_ID, "ground_truth": True, "reason": "short"},
            )

    asyncio.run(body())


# ---- CLI proxy ----------------------------------------------------------------


class _FakeClient:
    last_method: str | None = None
    last_params: dict[str, Any] | None = None

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_a: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).last_method = method
        type(self).last_params = dict(params)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def test_cli_jury_label_forwards_params(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(
        jury_cmd,
        "DaemonClient",
        lambda: _FakeClient(
            result={
                "wave_id": "P30-I20-W58",
                "ground_truth": False,
                "labeled_at": "2026-07-02T12:00:00+00:00",
                "envelope": {},
            }
        ),
    )
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "jury",
            "label",
            "P30-I20-W58",
            "--bad",
            "--reason",
            "veto was correct: the wave shipped a broken gate",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _FakeClient.last_method == "jury.label"
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["wave_id"] == "P30-I20-W58"
    assert _FakeClient.last_params["ground_truth"] is False
    assert _FakeClient.last_params["repo_root"] == str(tmp_path.resolve())


def test_cli_jury_label_requires_exactly_one_polarity(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "jury", "label", "W-X", "--reason", "x" * 25],
    )
    assert result.exit_code != 0
    assert "exactly one of --good / --bad" in result.output


def test_cli_jury_label_daemon_reject_surfaces(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    error = DaemonRpcError(code=-32002, message="validation_failed: unknown wave")
    monkeypatch.setattr(jury_cmd, "DaemonClient", lambda: _FakeClient(error=error))
    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "jury",
            "label",
            "P99-I99-W99",
            "--good",
            "--reason",
            "x" * 25,
        ],
    )
    assert result.exit_code != 0
    assert "validation_failed" in str(result.exception)
