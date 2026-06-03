"""Integration tests for ``eawf memory digest`` driven through the CLI.

Proves the command is a pure projection: ``state.json``, ``memory.jsonl``,
and ``event.jsonl`` are byte-equal before and after the command. Also pins
the ``--md`` and ``--json`` surfaces.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.paths import store_path
from eawf.surfaces.cli.app import app

runner = CliRunner()


def _seed_state(tmp_path: Path) -> Path:
    """Write a valid mid-flight ``state.json`` and seed the memory/event stores."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_path = state_dir / "state.json"
    body = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-05-08T00:00:00Z",
        "project": {
            "code": "QR",
            "slug": "quant",
            "title": "Quant",
            "domains": ["quant"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {
            "project_code": "QR",
            "subproject_id": None,
            "phase_id": "P29",
            "iter_id": "P29-I08",
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "QR",
                "title": "Ship the v0.5.0 mega-phase",
                "status": "active",
                "iter_ids": ["P29-I07", "P29-I08"],
                "opened_at": "2026-05-31T00:00:00Z",
            }
        },
        "iters": {
            "P29-I08": {
                "id": "P29-I08",
                "phase_id": "P29",
                "title": "Refactor the interface and arm the QC gate",
                "status": "active",
                "wave_ids": [],
                "opened_at": "2026-06-03T00:00:00Z",
            },
            "P29-I07": {
                "id": "P29-I07",
                "phase_id": "P29",
                "title": "Land the doc-clarity and math-explainer layer",
                "status": "closed",
                "wave_ids": [],
                "opened_at": "2026-06-01T00:00:00Z",
                "closed_at": "2026-06-02T00:00:00Z",
            },
        },
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "decisions": {
            "D01": {
                "id": "D01",
                "scope_id": "QR",
                "title": "Pick portalocker for cross-platform file locks",
                "rationale": "portalocker is the only maintained cross-platform lock.",
                "status": "active",
                "created_at": "2026-05-08T00:00:00Z",
            }
        },
        "indexes": {},
    }
    state_path.write_text(json.dumps(body, indent=2), encoding="utf-8")
    # Seed the memory + event stores so the read-only assertion has real bytes.
    memory_path = store_path(state_path, StoreKind.MEMORY)
    event_path = store_path(state_path, StoreKind.EVENT)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text('{"kind": "memory", "id": "MEM-1"}\n', encoding="utf-8")
    event_path.write_text('{"kind": "event", "id": "EVT-1"}\n', encoding="utf-8")
    return state_path


@pytest.fixture
def tmp_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state_path = _seed_state(tmp_path)
    monkeypatch.setenv("EA_STATE", str(state_path))
    monkeypatch.delenv("EA_LOCK_TIMEOUT", raising=False)
    return state_path


def test_memory_digest_md_renders_standup(tmp_state: Path) -> None:
    """``eawf memory digest --md`` emits the markdown standup with glossed ids."""
    result = runner.invoke(app, ["memory", "digest", "--md"])
    assert result.exit_code == 0, result.output
    assert "# Standup digest" in result.stdout
    assert "## Current focus" in result.stdout
    assert "P29 (Ship the v0.5.0 mega-phase)" in result.stdout
    assert "P29-I08 (Refactor the interface and arm the QC gate)" in result.stdout
    assert "P29-I07 (Land the doc-clarity and math-explainer layer)" in result.stdout
    assert "D01 (Pick portalocker for cross-platform file locks)" in result.stdout


def test_memory_digest_default_is_md(tmp_state: Path) -> None:
    """No format flag defaults to the markdown standup."""
    result = runner.invoke(app, ["memory", "digest"])
    assert result.exit_code == 0, result.output
    assert "# Standup digest" in result.stdout


def test_memory_digest_json_surface(tmp_state: Path) -> None:
    """``eawf memory digest --json`` emits the structured projection."""
    result = runner.invoke(app, ["memory", "digest", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["format"] == "json"
    assert payload["phase"]["ref_id"] == "P29"
    assert payload["iter"]["ref_id"] == "P29-I08"
    assert [e["ref_id"] for e in payload["recently_closed"]] == ["P29-I07"]
    assert [e["ref_id"] for e in payload["recent_decisions"]] == ["D01"]


def test_memory_digest_root_json_flag_emits_json(tmp_state: Path) -> None:
    """The root ``--json`` flag selects the structured surface too."""
    result = runner.invoke(app, ["--json", "memory", "digest"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["format"] == "json"


def test_memory_digest_is_byte_read_only(tmp_state: Path) -> None:
    """The digest mutates nothing: state.json + memory.jsonl + event.jsonl unchanged.

    Hashes all three files before and after the command and asserts every
    digest is identical, so a future regression that writes through the
    projection path is caught (the digest is a PURE read).
    """
    memory_path = store_path(tmp_state, StoreKind.MEMORY)
    event_path = store_path(tmp_state, StoreKind.EVENT)

    def _digests() -> dict[str, str]:
        return {
            "state": hashlib.sha256(tmp_state.read_bytes()).hexdigest(),
            "memory": hashlib.sha256(memory_path.read_bytes()).hexdigest(),
            "event": hashlib.sha256(event_path.read_bytes()).hexdigest(),
        }

    before = _digests()
    result = runner.invoke(app, ["memory", "digest", "--json"])
    assert result.exit_code == 0, result.output
    after = _digests()
    assert before == after
