"""CLI-side tests for ``eawf close rereceipt``.

Re-running a closed wave's gates writes receipts and an append-only
binding row, so it is a canonical daemon mutation (AGENTS rule 4) and the
CLI is a thin proxy: it forwards ``wave_id`` + ``repo_root`` to
``close.rereceipt`` and renders what comes back. Coverage:

* the verb forwards the right method and params and renders the summary;
* ``--json`` emits the bound receipt ids verbatim so an operator working
  through ninety gates can pipe them straight into triage;
* a daemon refusal (a wave that is not CLOSED) exits non-zero carrying the
  daemon's own message rather than a rewritten one;
* an unreachable daemon exits non-zero rather than pretending the gates
  ran;
* boundary: a run that bound no receipts renders honestly instead of an
  empty list.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.surfaces.cli import _daemon_client as daemon_client_module
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.errors import RPC_VALIDATION_FAILED

pytestmark = pytest.mark.integration

runner = CliRunner()

_WAVE_ID = "P32-I01-W07"
_RECEIPT_IDS = ["GR-" + "a" * 32, "GR-" + "b" * 32]


def _result(*, receipt_ids: list[str]) -> dict[str, Any]:
    """A ``close.rereceipt`` RPC result with *receipt_ids* bound."""
    return {
        "operation": "rereceipt",
        "wave_id": _WAVE_ID,
        "landed_sha": "c" * 40,
        "binding_id": "GRR-0123456789ab",
        "receipt_ids": receipt_ids,
        "gates": [
            {
                "gate_id": f"GATE-{index:02d}",
                "criterion_id": f"CR-{index:02d}",
                "result": "pass",
                "receipt_id": receipt_id,
            }
            for index, receipt_id in enumerate(receipt_ids, start=1)
        ],
        "passed_count": len(receipt_ids),
        "failed_count": 0,
    }


class _FakeClient:
    """A stand-in DaemonClient recording the non-ping ``call`` arguments."""

    last_method: str | None = None
    last_params: dict[str, Any] | None = None

    def __init__(
        self,
        *,
        result: dict[str, Any] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if method == "daemon.ping":
            return {"protocol_version": PROTOCOL_VERSION}
        type(self).last_method = method
        type(self).last_params = dict(params)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    result: dict[str, Any] | None = None,
    error: Exception | None = None,
) -> None:
    """Point the close CLI's daemon client at a recording stand-in."""
    _FakeClient.last_method = None
    _FakeClient.last_params = None
    monkeypatch.setattr(
        daemon_client_module,
        "DaemonClient",
        lambda *a, **k: _FakeClient(result=result, error=error),
    )


def test_close_rereceipt_proxies_to_the_daemon_method(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-03: the verb forwards close.rereceipt with the wave and repo root."""
    _install(monkeypatch, result=_result(receipt_ids=_RECEIPT_IDS))

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_method == "close.rereceipt"
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["wave_id"] == _WAVE_ID
    assert _FakeClient.last_params["repo_root"] == str(tmp_path.resolve())
    assert "rereceipt " + _WAVE_ID in result.output
    assert "passed=2" in result.output


def test_close_rereceipt_json_prints_the_bound_receipt_ids(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-03: --json carries the receipt ids the run bound."""
    _install(monkeypatch, result=_result(receipt_ids=_RECEIPT_IDS))

    result = runner.invoke(
        app,
        ["--json", "--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert result.exit_code == 0, result.output
    payload = orjson.loads(result.output)
    assert payload["receipt_ids"] == _RECEIPT_IDS
    assert payload["binding_id"] == "GRR-0123456789ab"
    assert payload["wave_id"] == _WAVE_ID


def test_close_rereceipt_json_reports_a_run_that_bound_nothing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-03 boundary: zero bound receipts render honestly in both modes."""
    _install(monkeypatch, result=_result(receipt_ids=[]))

    json_result = runner.invoke(
        app,
        ["--json", "--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )
    text_result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert json_result.exit_code == 0, json_result.output
    assert orjson.loads(json_result.output)["receipt_ids"] == []
    assert text_result.exit_code == 0, text_result.output
    assert "receipts=none" in text_result.output


def test_close_rereceipt_surfaces_the_daemon_refusal_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-03: a wave that is not CLOSED exits non-zero with the daemon's text."""
    message = (
        f"validation_failed: wave {_WAVE_ID!r} is not closed (status='in_progress'); "
        "a re-receipt replays a closed wave's record"
    )
    _install(
        monkeypatch,
        error=DaemonRpcError(code=RPC_VALIDATION_FAILED, message=message),
    )

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert result.exit_code != 0
    assert "is not closed" in result.output
    assert _FakeClient.last_method == "close.rereceipt"


def test_close_rereceipt_refuses_when_the_daemon_is_unreachable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """CR-03 error path: no daemon means no re-receipt, and a non-zero exit."""
    _install(monkeypatch, error=OSError("no socket"))

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert result.exit_code != 0
    assert "daemon unavailable" in result.output


def test_close_rereceipt_omits_at_when_not_given(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Without --at the params are unchanged, so the daemon runs at the landed commit."""
    _install(monkeypatch, result=_result(receipt_ids=_RECEIPT_IDS))

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID],
    )

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_params is not None
    assert "at" not in _FakeClient.last_params
    assert " bound " not in result.output


def test_close_rereceipt_forwards_at_and_renders_the_bound_commit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """--at reaches the daemon verbatim and the summary names both commits."""
    rebound = {**_result(receipt_ids=_RECEIPT_IDS), "bound_sha": "d" * 40}
    _install(monkeypatch, result=rebound)

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID, "--at", "HEAD~1"],
    )

    assert result.exit_code == 0, result.output
    assert _FakeClient.last_params is not None
    assert _FakeClient.last_params["at"] == "HEAD~1"
    assert f"at {'c' * 12} bound {'d' * 12}" in result.output


def test_close_rereceipt_surfaces_an_at_refusal_non_zero(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Error path: a refused --at exits non-zero with the daemon's text."""
    message = (
        f"validation_failed: wave {_WAVE_ID!r} re-bind commit {'e' * 40} does not "
        f"descend from its landed commit {'c' * 40}"
    )
    _install(
        monkeypatch,
        error=DaemonRpcError(code=RPC_VALIDATION_FAILED, message=message),
    )

    result = runner.invoke(
        app,
        ["--workspace", str(tmp_path), "close", "rereceipt", _WAVE_ID, "--at", "e" * 40],
    )

    assert result.exit_code != 0
    assert "does not descend from its landed commit" in result.output
