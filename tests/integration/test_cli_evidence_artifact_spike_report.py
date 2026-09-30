"""CLI test for ``eawf artifact file-spike-report``.

The command is dispatch only (AGENTS rule 1): it sends
``runtime.evidence.spike_report.file`` to the daemon and renders the
answer verbatim, never promoting anything itself. These tests drive the
real Typer app with a stand-in daemon client, mirroring
:mod:`tests.integration.test_cli_domain_lifecycle`'s approach for the
other native-RPC-forwarding commands (``task submit``,
``milestone seal-approval``): one RPC per invocation, the filed
``artifact_ref``/``content_digest`` printed as the daemon answered them,
and a refusal surfaced with the daemon's own code and a stable exit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_RUN_URN = f"{_ROOT}/run/RUN-00000001"
_ARTIFACT_REF = "artifact://spike/run-cli-01"
_REPORT: dict[str, Any] = {"report_id": "RPT-CLI-0001", "verified": True, "contracts": []}


class _FakeClient:
    """A stand-in daemon client recording every call it is handed."""

    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append((method, dict(params)))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _no_escalate(verb: str, *, flags: object = None, runtime_dir: object = None) -> int:
    """Stand in for the mutating-verb escalation gate."""
    return 0


@pytest.fixture(autouse=True)
def _no_daemon_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the escalation gate from spawning a real daemon."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _no_escalate)
    _FakeClient.calls = []


def _install(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    """Point ``_call_native_rpc`` (in ``domain.py``) at a fake client."""
    monkeypatch.setattr(domain_cmd, "DaemonClient", lambda *a, **k: _FakeClient(**kwargs))


def _invoke(tmp_path: Path, *, report_path: Path, artifact_ref: str = _ARTIFACT_REF) -> Any:
    return runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "artifact",
            "file-spike-report",
            str(report_path),
            "--run",
            _RUN_URN,
            "--artifact-ref",
            artifact_ref,
            "--actor",
            "OPERATOR",
        ],
    )


def test_an_unreadable_report_path_is_refused_before_any_rpc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: a missing file is refused locally, never reaching the wire."""
    _install(monkeypatch, result={})

    result = _invoke(tmp_path, report_path=tmp_path / "missing.json")

    assert result.exit_code != exit_codes.OK
    assert _FakeClient.calls == []
