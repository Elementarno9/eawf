"""A hook dispatch names every handler that ran, so a handler is observable.

SURF-055: ``eawf hook run subagent_start`` answers with one result row per handler it
ran, and the subagent start reaches the adoption verb through the daemon client.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration


class _Client:
    """Daemon-client stand-in recording each call."""

    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._sink.append(method)
        return {"run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"}


def test_surf_055_a_dispatch_names_the_handler_that_ran(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sink: list[str] = []
    monkeypatch.setattr(
        "eawf.runtime.hooks.runner._default_daemon_client_factory", lambda: _Client(sink)
    )
    payload = {"hook_event_name": "SubagentStart", "agent_id": "agent-1", "session_id": "s"}

    result = CliRunner().invoke(
        app,
        ["--workspace", str(tmp_path), "hook", "run", "subagent_start", "--runtime", "claude"],
        input=json.dumps(payload),
    )

    assert result.exit_code == 0, result.stdout
    rows = json.loads(result.stdout)["body"]["results"]
    adoption = [row for row in rows if row["name"] == "runtime.host_subagent"]
    assert [row["output"] for row in adoption] == [
        "runtime.host_subagent ok run=eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
    ]
    assert sink == ["runtime.host.subagent.start"]
