"""``eawf campaign new|run|cancel`` forward to the native Campaign verbs.

Each verb is a dispatch: ``new`` sends the brief to ``runtime.campaign.start``, ``run``
names the Campaign to ``runtime.campaign.run``, and ``cancel`` reads the Campaign's
revision back before closing it through ``runtime.campaign.close``. The daemon is
replaced by a recorder, so nothing here reaches a socket.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain

CAMPAIGN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001"
TRACK: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/track/TRK-RUNTIME"


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    """Record every verb sent, answering each the way its daemon verb does."""
    calls: list[tuple[str, dict[str, Any]]] = []
    answers: dict[str, dict[str, Any]] = {
        "runtime.campaign.start": {
            "record": {"key": "CAM-0001", "plan_steps": [{}, {}]},
            "committed": True,
            "driving": False,
        },
        "runtime.campaign.run": {"campaign_key": "CAM-0001", "driving": True},
        "projection.campaign.view": {"campaign_ref": CAMPAIGN, "revision": 7},
        "runtime.campaign.close": {"record": {"status": "cancelled"}, "committed": True},
    }

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        calls.append((method, params))
        return answers[method]

    monkeypatch.setattr(domain, "_native_answer", answer)
    return calls


def _invoke(tmp_path: Path, *args: str) -> Any:
    return CliRunner().invoke(app, ["--json", "-w", str(tmp_path), "campaign", *args])


def test_campaign_new_sends_the_brief_with_its_budget(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    result = _invoke(
        tmp_path,
        "new",
        "Establish whether replay preserves event order",
        "--actor",
        "OP-0001",
        "--track",
        TRACK,
        "--question",
        "Does replay preserve event order?",
        "--budget-tokens",
        "5000",
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert sent == [
        (
            "runtime.campaign.start",
            {
                "actor": "OP-0001",
                "track_ref": TRACK,
                "title": "Establish whether replay preserves event order",
                "questions": ["Does replay preserve event order?"],
                "runtime": "claude-code",
                "drive": False,
                "budget": {"axes": [{"axis_kind": "tokens", "limit": 5000, "unit": "tokens"}]},
            },
        )
    ]
    assert orjson.loads(result.stdout)["record"]["key"] == "CAM-0001"


def test_campaign_run_names_the_campaign_and_its_width(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    result = _invoke(tmp_path, "run", "CAM-0001", "--actor", "OP-0001", "--agents", "2")
    assert result.exit_code == exit_codes.OK, result.output
    assert sent == [
        (
            "runtime.campaign.run",
            {"actor": "OP-0001", "campaign_key": "CAM-0001", "runtime": "claude-code", "agents": 2},
        )
    ]


def test_campaign_cancel_closes_against_the_revision_it_read(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    result = _invoke(tmp_path, "cancel", "CAM-0001", "--actor", "OP-0001", "--reason", "superseded")
    assert result.exit_code == exit_codes.OK, result.output
    assert [method for method, _params in sent] == [
        "projection.campaign.view",
        "runtime.campaign.close",
    ]
    assert sent[1][1] == {
        "actor": "OP-0001",
        "urn": CAMPAIGN,
        "expected_revision": 7,
        "to_status": "cancelled",
        "reason": "superseded",
    }
