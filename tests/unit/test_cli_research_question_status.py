"""CLI dispatch tests for ``eawf research question`` + ``research status`` (W07).

Drives the Typer app via :class:`CliRunner` against a seeded temp workspace:

- ``research question add <title>`` proxies the daemon ``research.add_question``
  RPC, forwarding the title + blocking flag.
- the offline fallback (daemon unreachable) writes the OpenQuestion row directly
  via ``state_transaction``.
- ``research question list`` reads the ledger and exits 0 (empty + populated).
- ``research status`` folds the campaign + round + checkpoint state, exiting 0
  with an honest "no campaign" line when none is staged.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

import eawf.kernel.config.layered as layered
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.runtime.daemon.methods.research import (
    ResearchRoundPayload,
    persist_campaign,
    persist_round,
)
from eawf.surfaces.cli.app import app

runner = CliRunner()

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EA_STATE", raising=False)


def _state_payload() -> dict[str, Any]:
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-06-11T12:00:00+00:00",
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    (workspace / ".ea" / "state.json").write_bytes(orjson.dumps(_state_payload()))
    return workspace


class _FakeOkClient:
    """A DaemonClient whose ``call`` records params + returns an add_question result."""

    captured: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> _FakeOkClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        _FakeOkClient.captured = {"method": method, "params": params or {}}
        return {"question_id": "OQ-abc", "status": "open", "scope_id": "QR"}


class _FakeUnreachableClient:
    def __enter__(self) -> _FakeUnreachableClient:
        raise OSError("daemon socket not found")

    def __exit__(self, *_args: Any) -> None:
        return None


# --------------------------------------------------------------------------
# question add -- daemon proxy + offline fallback
# --------------------------------------------------------------------------


def test_question_add_daemon_proxy_forwards_title(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb proxies research.add_question with the title + blocking flag."""
    workspace = _make_workspace(tmp_path)
    _FakeOkClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeOkClient)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "question", "add", "which model fits", "--blocking"],
    )
    assert result.exit_code == 0, result.output
    assert _FakeOkClient.captured["method"] == "research.add_question"
    params = _FakeOkClient.captured["params"]
    assert params["title"] == "which model fits"
    assert params["blocking"] is True
    assert "added question OQ-abc" in result.stdout


def test_question_add_offline_fallback_writes_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon-unreachable proxy falls back to a direct state write."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)
    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "question", "add", "offline question"],
    )
    assert result.exit_code == 0, result.output
    from eawf.kernel.state.models import State

    state = State.model_validate(orjson.loads((workspace / ".ea" / "state.json").read_bytes()))
    assert state.open_questions is not None
    titles = [q.title for q in state.open_questions.values()]
    assert "offline question" in titles


def test_question_add_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` emits the typed add-question result."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeOkClient)
    result = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "research", "question", "add", "json question"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["question_id"] == "OQ-abc"
    assert payload["status"] == "open"


# --------------------------------------------------------------------------
# question list -- empty + populated, exit 0
# --------------------------------------------------------------------------


def test_question_list_empty_exits_zero(tmp_path: Path) -> None:
    """A scope with no question exits 0 with the honest empty line."""
    workspace = _make_workspace(tmp_path)
    result = runner.invoke(app, ["-w", str(workspace), "research", "question", "list"])
    assert result.exit_code == 0, result.output
    assert "no open questions" in result.stdout


def test_question_list_renders_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A populated ledger renders one row per question with status + blocking."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)
    runner.invoke(app, ["-w", str(workspace), "research", "question", "add", "open one"])
    runner.invoke(
        app, ["-w", str(workspace), "research", "question", "add", "blocked one", "--blocking"]
    )
    result = runner.invoke(app, ["--json", "-w", str(workspace), "research", "question", "list"])
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)["questions"]
    assert {r["title"] for r in rows} == {"open one", "blocked one"}
    blocked = next(r for r in rows if r["title"] == "blocked one")
    assert blocked["status"] == "blocked"
    assert blocked["blocking"] is True


# --------------------------------------------------------------------------
# research status -- campaign + round + checkpoint fold
# --------------------------------------------------------------------------


def test_research_status_no_campaign_exits_zero(tmp_path: Path) -> None:
    """A scope with no staged campaign exits 0 with the honest no-campaign line."""
    workspace = _make_workspace(tmp_path)
    result = runner.invoke(app, ["-w", str(workspace), "research", "status"])
    assert result.exit_code == 0, result.output
    assert "no research campaign staged" in result.stdout


def _seed_campaign_and_round(workspace: Path) -> None:
    state_path = workspace / ".ea" / "state.json"
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={"market-structure": ResearchDomainConfig(focus="venues")},
    )
    persist_campaign(
        state_path,
        ResearchCampaignPayload(
            campaign_id="campaign-s",
            config=block,
            campaign=stage_campaign("options-pricing landscape", block),
        ),
    )
    persist_round(
        state_path,
        ResearchRoundPayload(
            campaign_id="campaign-s",
            round_number=1,
            domains=["market-structure"],
            finding_lines=["a claim"],
            claim_ids=["CLM-r1-market-structure-0"],
            saturated=False,
            checkpoint=True,
            recorded_at=datetime(2026, 6, 11, 12, tzinfo=UTC),
        ),
    )


def test_research_status_renders_campaign_round_checkpoint(tmp_path: Path) -> None:
    """research status folds the campaign + round + checkpoint state."""
    workspace = _make_workspace(tmp_path)
    _seed_campaign_and_round(workspace)
    result = runner.invoke(app, ["--json", "-w", str(workspace), "research", "status"])
    assert result.exit_code == 0, result.output
    campaign = json.loads(result.stdout)["campaign"]
    assert campaign["campaigns"] == 1
    assert campaign["rounds_run"] == 1
    assert campaign["checkpoints"] == 1
    assert campaign["kind"] == "runnable"
    text = runner.invoke(app, ["-w", str(workspace), "research", "status"])
    assert "campaign: runnable" in text.stdout
    assert "rounds=1" in text.stdout
    assert "checkpoints=1" in text.stdout
