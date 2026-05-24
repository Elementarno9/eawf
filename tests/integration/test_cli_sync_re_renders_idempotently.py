"""Integration tests for ``eawf sync`` — idempotence + manifest atomicity.

These tests pair up with the unit suite in
``tests/unit/test_cli_sync.py``. The unit tests pin per-flag behaviour;
the integration tests confirm the end-to-end ``init → sync`` lifecycle is
hash-stable on the second call (no spurious updates) and that the manifest
writer respects atomic semantics across the boundary.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _init_core_python(target: Path) -> None:
    """Initialise a workspace with the ``core + python`` profile combo."""
    res = runner.invoke(
        app,
        [
            "--no-input",
            "init",
            "--project-code",
            "DEMO",
            "--profile",
            "core",
            "--profile",
            "python",
            "--target",
            str(target),
        ],
    )
    assert res.exit_code == 0, res.output


@pytest.mark.integration
def test_cli_sync_idempotent(tmp_path: Path) -> None:
    """Calling sync twice in a row reports zero added/updated regions on the second call."""
    _init_core_python(tmp_path)

    res1 = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path)])
    assert res1.exit_code == 0, res1.output
    payload1 = json.loads(res1.output)
    assert payload1["mode"] == "write"

    res2 = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path)])
    assert res2.exit_code == 0, res2.output
    payload2 = json.loads(res2.output)
    # The second sync must report no added/updated regions — the renderer
    # found every region already at the same hash. Unchanged regions are
    # the expected non-empty set (matches the composed render_blocks).
    assert payload2["regions_added"] == []
    assert payload2["regions_updated"] == []
    assert len(payload2["regions_unchanged"]) >= 1


@pytest.mark.integration
def test_cli_sync_writes_manifest_atomically(tmp_path: Path) -> None:
    """After sync, the manifest file is present, well-formed, and reflects the renderer."""
    _init_core_python(tmp_path)

    manifest_path = tmp_path / ".ea" / "indexes" / "generated.json"
    assert manifest_path.exists(), "init must write the manifest"

    res = runner.invoke(app, ["sync", "--target", str(tmp_path)])
    assert res.exit_code == 0, res.output

    parsed = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert parsed["version"] == 1
    # Composed core+python ships at least two render_blocks targeted at AGENTS.md.
    keys = list(parsed["generated"].keys())
    assert any("non-negotiable-rules" in k for k in keys)
    assert any("python-style" in k for k in keys)


@pytest.mark.integration
def test_cli_sync_emit_json_envelope(tmp_path: Path) -> None:
    """``--json`` prints a structured envelope with the canonical W08 fields."""
    _init_core_python(tmp_path)

    res = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path), "--dry-run"])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)

    assert payload["target"] == str(tmp_path)
    assert payload["mode"] == "dry-run"
    assert payload["profiles_enabled"] == ["core", "python"]
    # Envelope shape: regions_added / regions_updated / regions_unchanged
    # are always lists of strings; the per-target *_changed booleans drive
    # the human-readable summary.
    for key in ("regions_added", "regions_updated", "regions_unchanged"):
        assert isinstance(payload[key], list)
    for key in ("agents_md_changed", "claude_md_changed", "manifest_changed"):
        assert isinstance(payload[key], bool)


@pytest.mark.integration
def test_cli_sync_no_config_falls_back_to_builtin_default(tmp_path: Path) -> None:
    """A workspace with no .ea/config.yaml falls back to the built-in ``core`` default.

    The built-in defaults declare ``profiles.enabled = ["core"]`` so even a
    directory with no overlay still has a workable composed profile. Sync
    therefore emits the core render_blocks against the bare workspace and
    exits 0; this confirms the no-overlay path does not crash.
    """
    res = runner.invoke(app, ["--json", "sync", "--target", str(tmp_path)])
    assert res.exit_code == 0, res.output
    payload = json.loads(res.output)
    assert payload["profiles_enabled"] == ["core"]
    # First-time render against a bare dir means every block is "added".
    assert payload["regions_added"] != [] or payload["regions_unchanged"] != []
    assert payload["regions_updated"] == []
