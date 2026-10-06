"""CLI-integration test for the doctor plugin cross-scope duplicate row.

``eawf doctor`` carries a ``plugin_cross_scope_dup`` row in the JSON envelope
that warns when the manifest installs one region under two scopes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app
from eawf.surfaces.render.manifest import Manifest, ManifestEntry
from eawf.surfaces.render.manifest import save_atomic as save_manifest_atomic


def _write_minimal_state(workspace: Path) -> Path:
    """Drop a hermetic ``state.json`` into *workspace*/.ea/."""
    state_path = workspace / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ZZ",
        "updated_at": "2026-05-27T00:00:00Z",
        "project": {
            "code": "ZZ",
            "slug": "zz",
            "title": "ZZ",
            "description": "",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ZZ",
        },
        "current": {
            "project_code": "ZZ",
            "track_id": None,
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {
            "P28-I01-W01": {
                "id": "P28-I01-W01",
                "iter_id": "P28-I01",
                "title": "drifted wave",
                "status": "closed",
                "deps": [],
                "blocks": [],
                "file_scopes": [],
                "claim_session_id": None,
                "worktree_id": None,
                "token_budget": None,
                "tokens_consumed": 0,
                "outcome": None,
                "commit": "a" * 40,
                "opened_at": "2026-05-27T00:00:00Z",
                "closed_at": "2026-05-27T00:01:00Z",
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }
    state_path.write_text(json.dumps(payload), encoding="utf-8")
    return state_path


def _find_check(envelope: dict[str, Any], name: str) -> dict[str, Any]:
    for check in envelope["checks"]:
        if check["name"] == name:
            return check
    raise AssertionError(f"check {name!r} not in envelope: {envelope}")


def test_doctor_surfaces_plugin_cross_scope_dup_check(tmp_path: Path) -> None:
    """``eawf --json doctor`` carries a ``plugin_cross_scope_dup`` row when manifest collides."""
    _write_minimal_state(tmp_path)
    target = tmp_path / ".ea" / "indexes" / "generated.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    manifest = Manifest(
        version=1,
        generated={
            "a::skill.x": ManifestEntry(
                target="a",
                region_id="plugin.codex.skill.x",
                version="1.0",
                hash="0123456789abcdef",
                generator="eawf-plugin-codex",
                generated_at="2026-05-27T00:00:00+00:00",
                scope="project",
            ),
            "b::skill.x": ManifestEntry(
                target="b",
                region_id="plugin.codex.skill.x",
                version="1.0",
                hash="0123456789abcdef",
                generator="eawf-plugin-codex",
                generated_at="2026-05-27T00:00:00+00:00",
                scope="user",
            ),
        },
    )
    save_manifest_atomic(target, manifest)
    runner = CliRunner()
    res = runner.invoke(app, ["--json", "-w", str(tmp_path), "doctor"])
    assert res.exit_code == 0, res.output
    envelope = json.loads(res.output)
    cross_scope = _find_check(envelope, "plugin_cross_scope_dup")
    assert cross_scope["status"] == "warn"
    assert "plugin.codex.skill.x" in cross_scope["duplicates"]
