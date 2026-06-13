"""CLI dispatch tests for ``eawf research campaign new`` (P29-I09-W07).

Drives the Typer app via :class:`CliRunner` against a temp workspace and
checks the campaign-staging sub-verb:

- ``research campaign new <topic>`` stages + persists when the daemon proxy
  succeeds (params forwarded to the ``research.create_campaign`` RPC).
- The offline fallback (daemon unreachable) appends the campaign row directly
  via the shared ``persist_campaign`` helper so the row lands in the store.
- ``research campaign new`` with no ``research:`` block fails fast as
  ``InvalidInput`` (exit code 1) and writes no row.
- ``--json`` emits the typed campaign envelope.

``resolve_research_block`` is monkeypatched so the test controls the merged
block without standing up a full profile-composition fixture; the daemon
client is monkeypatched so no real daemon is spawned.
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
    """Create a workspace with an empty ``.ea/`` directory."""
    workspace = tmp_path / "ws"
    (workspace / ".ea").mkdir(parents=True)
    return workspace


def _block() -> ResearchProfileBlock:
    """A two-domain research block."""
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _patch_block(monkeypatch: pytest.MonkeyPatch, block: ResearchProfileBlock | None) -> None:
    """Force ``resolve_research_block`` to return *block* (or ``None``)."""
    monkeypatch.setattr(research_cli, "resolve_research_block", lambda flags: block)


class _FakeOkClient:
    """Stand-in DaemonClient whose ``call`` returns a success result."""

    captured: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> _FakeOkClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        forwarded = params or {}
        _FakeOkClient.captured = {"method": method, "params": forwarded}
        return {"id": forwarded["campaign_id"], "appended_at": "2026-06-03T00:00:00+00:00"}


class _FakeUnreachableClient:
    """Stand-in DaemonClient that fails on enter (daemon unreachable)."""

    def __enter__(self) -> _FakeUnreachableClient:
        raise OSError("daemon socket not found")

    def __exit__(self, *_args: Any) -> None:
        return None


class _FakeRunHandleClient:
    """Stand-in DaemonClient whose ``call`` returns a research-run handle."""

    captured: ClassVar[dict[str, Any]] = {}

    def __enter__(self) -> _FakeRunHandleClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        forwarded = params or {}
        _FakeRunHandleClient.captured = {"method": method, "params": forwarded}
        return {
            "handle_id": "research-run-abc123",
            "campaign_id": forwarded["campaign_id"],
            "run_state": "running",
            "backgrounded": True,
        }


class _FakeRejectClient:
    """Stand-in DaemonClient whose ``call`` rejects with a typed RPC error."""

    def __enter__(self) -> _FakeRejectClient:
        return self

    def __exit__(self, *_args: Any) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        from eawf.surfaces.cli._daemon_client import DaemonRpcError

        raise DaemonRpcError(-32602, "unknown campaign: 'unknown-campaign'")


def _read_rows(state_path: Path) -> list[ResearchCampaignPayload]:
    """Return every campaign payload off the on-disk research_campaign store."""
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return []
    rows: list[ResearchCampaignPayload] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        envelope = Envelope.model_validate_json(line)
        rows.append(ResearchCampaignPayload.model_validate(envelope.payload))
    return rows


def test_campaign_new_daemon_proxy_forwards_params(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The CLI stages the campaign and forwards the payload to the daemon RPC."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    _FakeOkClient.captured = {}
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeOkClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "new", "Survey the pricing landscape"],
    )
    assert result.exit_code == 0, result.output
    assert _FakeOkClient.captured["method"] == "research.create_campaign"
    params = _FakeOkClient.captured["params"]
    assert params["campaign"]["topic"] == "Survey the pricing landscape"
    assert params["campaign_id"].startswith("campaign-")
    assert [d["domain"] for d in params["campaign"]["dispatches"]] == [
        "market-structure",
        "pricing-models",
    ]
    assert "staged campaign campaign-" in result.stdout


def test_campaign_new_offline_fallback_appends_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon-unreachable proxy falls back to a direct store append."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "new", "Offline campaign topic"],
    )
    assert result.exit_code == 0, result.output
    state_path = workspace / ".ea" / "state.json"
    rows = _read_rows(state_path)
    assert len(rows) == 1
    payload = rows[0]
    assert payload.campaign.topic == "Offline campaign topic"
    assert payload.campaign_id.startswith("campaign-")
    assert [d.domain for d in payload.campaign.dispatches] == [
        "market-structure",
        "pricing-models",
    ]


def test_campaign_new_json_envelope(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``--json`` emits the typed campaign envelope with id + domain count."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, _block())
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app,
        ["--json", "-w", str(workspace), "research", "campaign", "new", "Json campaign topic"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["topic"] == "Json campaign topic"
    assert payload["domain_count"] == 2
    assert payload["campaign_id"].startswith("campaign-")
    assert payload["id"] == payload["campaign_id"]


def test_campaign_new_no_research_block_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A scope with no ``research:`` block fails fast as InvalidInput, no row."""
    workspace = _make_workspace(tmp_path)
    _patch_block(monkeypatch, None)

    result = runner.invoke(
        app,
        ["-w", str(workspace), "research", "campaign", "new", "No block topic"],
    )
    assert result.exit_code == 1, result.output
    assert "no research: block configured for this scope" in result.output
    assert _read_rows(workspace / ".ea" / "state.json") == []


def test_campaign_new_no_args_is_help() -> None:
    """``research campaign`` with no sub-verb prints help (no_args_is_help)."""
    result = runner.invoke(app, ["research", "campaign"])
    assert result.exit_code in (0, 2)
    assert "new" in result.output


# --------------------------------------------------------------------------
# W27: ``research campaign run`` -- the operator trigger for research.run
# --------------------------------------------------------------------------


def test_campaign_run_forwards_to_research_run_rpc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``campaign run <id>`` proxies the live ``research.run`` RPC."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRunHandleClient)

    result = runner.invoke(
        app, ["-w", str(workspace), "research", "campaign", "run", "campaign-xyz"]
    )

    assert result.exit_code == 0, result.output
    assert _FakeRunHandleClient.captured["method"] == "research.run"
    assert _FakeRunHandleClient.captured["params"] == {"campaign_id": "campaign-xyz"}
    assert "started research run" in result.output


def test_campaign_run_forwards_round_budget(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--round-budget`` reaches the RPC params."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRunHandleClient)

    result = runner.invoke(
        app,
        [
            "-w",
            str(workspace),
            "research",
            "campaign",
            "run",
            "campaign-xyz",
            "--round-budget",
            "3",
        ],
    )

    assert result.exit_code == 0, result.output
    assert _FakeRunHandleClient.captured["params"]["round_budget"] == 3


def test_campaign_run_daemon_unreachable_is_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A live run has no offline fallback: an unreachable daemon fails fast."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeUnreachableClient)

    result = runner.invoke(
        app, ["-w", str(workspace), "research", "campaign", "run", "campaign-xyz"]
    )

    assert result.exit_code == 1
    assert "started research run" not in result.output


def test_campaign_run_daemon_rejection_is_invalid_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The daemon's not-staged / not-ACTIVE guard surfaces as a CLI error."""
    workspace = _make_workspace(tmp_path)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _FakeRejectClient)

    result = runner.invoke(
        app, ["-w", str(workspace), "research", "campaign", "run", "unknown-campaign"]
    )

    assert result.exit_code == 1
    assert "started research run" not in result.output
