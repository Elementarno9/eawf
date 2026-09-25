"""``eawf research campaign new`` runs on a fresh repo off the shipped profile data.

Unlike :mod:`tests.integration.test_cli_research_campaign`, nothing here
monkeypatches ``resolve_research_block``: the ``research:`` block is composed
from the built-in profile YAML exactly as a freshly initialised repository
would compose it, so a profile data regression that drops the default block
reds this suite.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from typer.testing import CliRunner

import eawf.kernel.config.layered as layered
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.platform.profiles.loader import load_profile
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

runner = CliRunner()

_DEFAULT_DOMAINS = ["codebase", "prior-art", "risks"]


class _UnreachableClient:
    """DaemonClient stand-in that fails on enter, forcing the offline append."""

    def __enter__(self) -> _UnreachableClient:
        raise OSError("daemon socket not found")

    def __exit__(self, *_args: object) -> None:
        return None


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty global config layer, no EA_STATE, and no live daemon."""
    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global-config.yaml")
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._daemon_client.DaemonClient", _UnreachableClient)


def _fresh_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, profiles: list[str]) -> Path:
    """Create a repo whose ``.ea/config.yaml`` enables *profiles* and nothing else."""
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "config.yaml").write_text(
        yaml.safe_dump({"profiles": {"enabled": profiles}}), encoding="utf-8"
    )
    monkeypatch.chdir(repo)
    return repo


def _read_rows(state_path: Path) -> list[ResearchCampaignPayload]:
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return []
    return [
        ResearchCampaignPayload.model_validate(Envelope.model_validate_json(line).payload)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_research_profile_ships_default_research_block() -> None:
    """The built-in research profile carries a non-empty ``research:`` block."""
    block = load_profile("research").research
    assert block is not None
    assert sorted(block.domains) == _DEFAULT_DOMAINS
    assert all(domain.focus for domain in block.domains.values())


def test_campaign_new_succeeds_on_fresh_repo_with_research_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No hand-written block: the shipped default stages and persists a campaign."""
    repo = _fresh_repo(tmp_path, monkeypatch, ["core", "research"])

    result = runner.invoke(
        app, ["--json", "-w", str(repo), "research", "campaign", "new", "Survey caching options"]
    )

    assert result.exit_code == 0, result.output
    body = json.loads(result.stdout)
    assert body["domain_count"] == len(_DEFAULT_DOMAINS)
    rows = _read_rows(repo / ".ea" / "state.json")
    assert len(rows) == 1
    assert rows[0].campaign_id == body["campaign_id"]
    assert [d.domain for d in rows[0].campaign.dispatches] == _DEFAULT_DOMAINS


def test_campaign_new_dry_run_on_fresh_repo_persists_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The resolve-check reports the default domain count and writes no row."""
    repo = _fresh_repo(tmp_path, monkeypatch, ["core", "research"])

    result = runner.invoke(
        app, ["--json", "-w", str(repo), "research", "campaign", "new", "t", "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["domain_count"] == len(_DEFAULT_DOMAINS)
    assert _read_rows(repo / ".ea" / "state.json") == []


def test_campaign_new_without_research_profile_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default block rides the research profile only; core alone has none."""
    repo = _fresh_repo(tmp_path, monkeypatch, ["core"])

    result = runner.invoke(app, ["-w", str(repo), "research", "campaign", "new", "Topic"])

    assert result.exit_code == 1, result.output
    assert "no research: block configured for this scope" in result.output
    assert _read_rows(repo / ".ea" / "state.json") == []
