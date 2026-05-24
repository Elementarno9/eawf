from __future__ import annotations

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.artifacts.validation import validate_markdown_artifact
from eawf.cli.app import app
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from tests.integration.test_plan_show_e2e import _VALID_STATE

runner = CliRunner()


def _seed(tmp_path: Path) -> Path:
    state = deepcopy(_VALID_STATE)
    state["artifacts"] = {}
    state["waves"]["P05-I01-W00"]["blocks"] = ["P05-I01-W01"]
    state["waves"]["P05-I01-W01"]["blocks"] = ["P05-I01-W02"]
    state["waves"]["P05-I01-W02"]["blocks"] = []
    state_dir = tmp_path / ".ea"
    state_dir.mkdir()
    (state_dir / "store").mkdir()
    state_path = state_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state))
    return state_path


def test_research_show_md_migrates_legacy_sources(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    envelope = Envelope(
        id="BR-001",
        kind=StoreKind.RESEARCH,
        scope_id="QR",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="brief",
        payload={
            "topic": "artifact chassis",
            "findings": ["Renderer uses typed references [1]."],
            "sources": ["src/eawf/render/research.py:1"],
        },
    )
    store_path(state_path, StoreKind.RESEARCH).write_text(
        envelope.model_dump_json() + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["research", "show", "BR-001", "--md"])
    assert result.exit_code == 0, result.output
    assert "# Research Brief: BR-001" in result.stdout
    assert "[1] src/eawf/render/research.py:1" in result.stdout


def test_research_show_md_uses_all_references_when_findings_lack_markers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    envelope = Envelope(
        id="BR-001",
        kind=StoreKind.RESEARCH,
        scope_id="QR",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="brief",
        payload={
            "topic": "artifact chassis",
            "findings": ["Renderer uses typed references."],
            "sources": ["src/eawf/render/research.py:1"],
        },
    )
    store_path(state_path, StoreKind.RESEARCH).write_text(
        envelope.model_dump_json() + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["research", "show", "BR-001", "--md"])
    assert result.exit_code == 0, result.output
    assert "- References: [1]" in result.stdout
    assert validate_markdown_artifact(result.stdout).ok


def test_research_show_md_rejects_global_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    envelope = Envelope(
        id="BR-001",
        kind=StoreKind.RESEARCH,
        scope_id="QR",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        summary="brief",
        payload={
            "topic": "artifact chassis",
            "findings": ["Renderer uses typed references [1]."],
            "sources": ["src/eawf/render/research.py:1"],
        },
    )
    store_path(state_path, StoreKind.RESEARCH).write_text(
        envelope.model_dump_json() + "\n",
        encoding="utf-8",
    )
    result = runner.invoke(app, ["--json", "research", "show", "BR-001", "--md"])
    assert result.exit_code != 0
    assert "--md and --json are contradictory" in result.stdout


def test_audit_show_md_rejects_global_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    result = runner.invoke(app, ["--json", "audit", "show", "AU-1", "--md"])
    assert result.exit_code != 0
    assert "--md and --json are contradictory" in result.stdout


def test_draft_new_validate_and_plan_promote(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_path = _seed(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))

    created = runner.invoke(app, ["draft", "new", "plan", "p16", "--title", "P16 Plan"])
    assert created.exit_code == 0, created.output
    draft_path = tmp_path / ".ea" / "local" / "plan" / "p16.md"

    validated = runner.invoke(app, ["draft", "validate", str(draft_path)])
    assert validated.exit_code == 0, validated.output

    promoted = runner.invoke(app, ["plan", "promote", "p16", "--scrub"])
    assert promoted.exit_code == 0, promoted.output
    artifact_path = tmp_path / ".ea" / "artifacts" / "plan-p16.md"
    assert artifact_path.exists()
    assert not artifact_path.read_text(encoding="utf-8").startswith("<!-- eawf-template:")

    state = json.loads(state_path.read_text(encoding="utf-8"))
    artifact = state["artifacts"]["ART-plan-p16"]
    assert artifact["kind"] == "plan_spec"
    assert artifact["uri"] == "repo:.ea/artifacts/plan-p16.md"
