"""P30-I21-W34 (G7+G8): campaign ``new --dry-run`` + ``campaign cancel`` CLI.

``campaign new`` persisted a durable active campaign even for a resolve-check, so
a sanity check leaked a campaign into the store; there was also no CLI verb to
cancel an active campaign despite the daemon method existing. This wave adds a
``--dry-run`` flag (report the domain count without persisting) and a
``campaign cancel`` verb over the ``research.cancel_campaign`` RPC.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, ClassVar

import pytest
from typer.testing import CliRunner

import eawf.kernel.config.layered as layered
import eawf.surfaces.cli.commands.research as research_cli
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import ResearchDomainConfig, ResearchProfileBlock
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _isolate_global_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the global config layer at an empty tmp file + clear EA_STATE."""
    fake_global = tmp_path / "global-config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: fake_global)
    monkeypatch.delenv("EA_STATE", raising=False)


def _make_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    return workspace


def _block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _patch_block(monkeypatch: pytest.MonkeyPatch, block: ResearchProfileBlock | None) -> None:
    monkeypatch.setattr(research_cli, "resolve_research_block", lambda flags: block)


def _read_rows(state_path: Path) -> list[ResearchCampaignPayload]:
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return []
    rows: list[ResearchCampaignPayload] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(
                ResearchCampaignPayload.model_validate(Envelope.model_validate_json(line).payload)
            )
    return rows


class _FakeCancelClient:
    """DaemonClient stand-in returning a cancel tombstone result."""

    captured: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> _FakeCancelClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        forwarded = params or {}
        _FakeCancelClient.captured = {"method": method, "params": forwarded}
        return {
            "id": forwarded["campaign_id"],
            "status": "cancelled",
            "cancelled_at": "2026-06-14T00:00:00+00:00",
        }


class _FakeUnreachableClient:
    """DaemonClient stand-in that fails on enter (daemon unreachable)."""

    def __enter__(self) -> _FakeUnreachableClient:
        raise OSError("daemon socket not found")

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakeRejectClient:
    """DaemonClient stand-in whose ``call`` rejects with a typed RPC error."""

    def __enter__(self) -> _FakeRejectClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        from eawf.surfaces.cli._daemon_client import DaemonRpcError

        raise DaemonRpcError(-32602, "unknown campaign: 'unknown-campaign'")


# --------------------------------------------------------------------------
# G7 -- campaign new --dry-run
# --------------------------------------------------------------------------


def test_campaign_new_dry_run_reports_count_and_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """--dry-run reports the domain count and writes NO campaign to the store."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    # Unreachable client would fall back to a direct store append IF persist ran,
    # so an empty store proves the dry-run short-circuited before persistence.
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "new", "--dry-run", "Sanity resolve check"],
    )
    assert result.exit_code == 0, result.output
    assert "dry-run" in result.stdout
    assert "2 domain(s)" in result.stdout
    assert _read_rows(workspace / ".ea" / "state.json") == []


def test_campaign_new_dry_run_json_body(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """--dry-run --json emits dry_run=True + the domain count."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "research", "campaign", "new", "--dry-run", "Topic"],
    )
    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["dry_run"] is True
    assert body["domain_count"] == 2
    assert "id" not in body


def test_campaign_new_without_dry_run_still_persists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression guard: the default (no --dry-run) path still persists a row."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "new", "Persisted topic"],
    )
    assert result.exit_code == 0, result.output
    assert len(_read_rows(workspace / ".ea" / "state.json")) == 1


# --------------------------------------------------------------------------
# G8 -- campaign cancel
# --------------------------------------------------------------------------


def test_campaign_cancel_forwards_to_daemon(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel forwards campaign_id + reason to the research.cancel_campaign RPC."""
    workspace = _make_workspace(tmp_path)
    _FakeCancelClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeCancelClient)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "research",
            "campaign",
            "cancel",
            "campaign-abc123",
            "--reason",
            "sanity leak cleanup",
        ],
    )
    assert result.exit_code == 0, result.output
    assert _FakeCancelClient.captured["method"] == "research.cancel_campaign"
    params = _FakeCancelClient.captured["params"]
    assert params["campaign_id"] == "campaign-abc123"
    assert params["reason"] == "sanity leak cleanup"
    assert "cancelled campaign campaign-abc123" in result.stdout


def test_campaign_cancel_reason_is_optional(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cancel without --reason omits the reason key from the RPC params."""
    workspace = _make_workspace(tmp_path)
    _FakeCancelClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeCancelClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "cancel", "campaign-xyz"],
    )
    assert result.exit_code == 0, result.output
    assert "reason" not in _FakeCancelClient.captured["params"]


def test_campaign_cancel_daemon_error_is_hard_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown / non-ACTIVE campaign surfaces a hard error, not a no-op."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRejectClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "cancel", "unknown-campaign"],
    )
    assert result.exit_code == 1, result.output
