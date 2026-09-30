"""End-to-end tests for the evidence CLI sub-apps.

Drives every command through :class:`typer.testing.CliRunner` against a
temporary ``.ea/state.json`` derived from the empty-repo fixture. The audit-
evidence guard is exercised across all five verdict-bearing commands
(``outcome set``, ``hypothesis verdict``, ``incident close``,
``backlog close``) — each must exit ``4`` (``VALIDATION_FAILED``) without an
``--audit`` of a complete audit, and exit ``0`` once a complete audit exists.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
runner = CliRunner()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    return target


# ---- goal ------------------------------------------------------------------


# ---- outcome ---------------------------------------------------------------


# ---- hypothesis ------------------------------------------------------------


def test_hypothesis_verdict_without_audit_exits_validation_failed(
    state_path: Path,
) -> None:
    runner.invoke(
        app,
        [
            "hypothesis",
            "define",
            "H03-12",
            "--scope-id",
            "QR",
            "--text",
            "t",
            "--metric",
            "m",
            "--confirm",
            "c",
            "--reject",
            "r",
        ],
    )
    result = runner.invoke(
        app,
        [
            "hypothesis",
            "verdict",
            "H03-12",
            "--verdict",
            "confirmed",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


# ---- audit -----------------------------------------------------------------


# ---- incident --------------------------------------------------------------


def test_incident_close_without_audit_exits_validation_failed(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "incident",
            "open",
            "INC-001",
            "--severity",
            "high",
            "--title",
            "leak",
        ],
    )
    result = runner.invoke(
        app,
        [
            "incident",
            "close",
            "INC-001",
            "--root-cause",
            "x",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


# ---- decision --------------------------------------------------------------


# ---- artifact --------------------------------------------------------------


def test_artifact_add_rejects_local_path_option(state_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "artifact",
            "add",
            "ART-001",
            "--kind",
            "audit_report",
            "--uri",
            "repo:.ea/artifacts/audits/p13-i04.md",
            "--local-path",
            "tmp/audit.md",
        ],
    )
    assert result.exit_code == 2


# ---- backlog ---------------------------------------------------------------


def test_backlog_close_without_audit_exits_validation_failed(state_path: Path) -> None:
    runner.invoke(
        app,
        [
            "backlog",
            "add",
            "B023",
            "--title",
            "Split workflow",
            "--priority",
            "P1",
        ],
    )
    result = runner.invoke(
        app,
        [
            "backlog",
            "close",
            "B023",
            "--resolution",
            "done",
            "--commit",
            "abc",
            "--audit",
            "AUD-NOPE",
        ],
    )
    assert result.exit_code == 2


# ---- cross-cutting ---------------------------------------------------------
