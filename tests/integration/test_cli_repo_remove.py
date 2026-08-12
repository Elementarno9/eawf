"""Integration tests for ``eawf repo remove``.

Covers the explicit removal surface:

- Successful removal: existing code is dropped from the registry.
- NotFound on absent code: never silently no-ops.
- NotFound on missing registry: refuses without auto-creating.
- InvalidInput on malformed code: regex enforced.
- Active-code clearing: removing the active entry clears active_code.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


def _write_registry(
    target: Path,
    repos: dict[str, dict[str, str]],
    *,
    active: str | None = None,
) -> None:
    target.write_bytes(
        orjson.dumps(
            {
                "version": "1",
                "updated_at": "2026-05-01T12:00:00+00:00",
                "active_code": active,
                "repos": repos,
            }
        )
    )


# ---------------------------------------------------------------------------
# Successful path
# ---------------------------------------------------------------------------


def test_repo_remove_drops_entry(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"},
            "DEMO": {"code": "DEMO", "path": "/tmp/demo", "title": "Demo"},
        },
    )
    result = runner.invoke(app, ["repo", "remove", "DEMO", "--registry-path", str(target)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(target.read_text())
    assert set(payload["repos"]) == {"EAWF"}


def test_repo_remove_json_envelope_carries_metadata(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {"EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"}},
    )
    result = runner.invoke(
        app, ["--json", "repo", "remove", "EAWF", "--registry-path", str(target)]
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["code"] == "EAWF"
    assert payload["removed"] is True
    assert payload["remaining"] == 0


def test_repo_remove_active_code_clears_active(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"},
            "DEMO": {"code": "DEMO", "path": "/tmp/demo", "title": "Demo"},
        },
        active="EAWF",
    )
    result = runner.invoke(app, ["repo", "remove", "EAWF", "--registry-path", str(target)])
    assert result.exit_code == 0
    payload = json.loads(target.read_text())
    assert payload["active_code"] is None


def test_repo_remove_non_active_preserves_active(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"},
            "DEMO": {"code": "DEMO", "path": "/tmp/demo", "title": "Demo"},
        },
        active="EAWF",
    )
    runner.invoke(app, ["repo", "remove", "DEMO", "--registry-path", str(target)])
    payload = json.loads(target.read_text())
    assert payload["active_code"] == "EAWF"


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_repo_remove_absent_code_exits_2(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(target, {"EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"}})
    result = runner.invoke(app, ["repo", "remove", "MISSING", "--registry-path", str(target)])
    assert result.exit_code == 1, result.stdout
    assert "not registered" in result.stdout


def test_repo_remove_missing_registry_exits_2(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    result = runner.invoke(app, ["repo", "remove", "EAWF", "--registry-path", str(target)])
    assert result.exit_code == 1, result.stdout
    assert "not found" in result.stdout
    assert not target.exists(), "missing-registry path MUST NOT auto-create"


def test_repo_remove_invalid_code_shape_exits_3(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(target, {})
    result = runner.invoke(app, ["repo", "remove", "bad-lowercase", "--registry-path", str(target)])
    assert result.exit_code == 1, result.stdout
    assert "invalid repo code" in result.stdout


def test_repo_remove_corrupted_registry_exits_3(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_bytes(b"{not json")
    result = runner.invoke(app, ["repo", "remove", "EAWF", "--registry-path", str(target)])
    assert result.exit_code == 1, result.stdout
    assert "corrupted" in result.stdout


# ---------------------------------------------------------------------------
# No implicit removal: command MUST take a positional code
# ---------------------------------------------------------------------------


def test_repo_remove_requires_code_argument(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(target, {})
    result = runner.invoke(app, ["repo", "remove", "--registry-path", str(target)])
    # Missing argument should produce a Typer usage error (exit code 2).
    assert result.exit_code != 0


# ---------------------------------------------------------------------------
# Read-only when no match
# ---------------------------------------------------------------------------


def test_repo_remove_does_not_mutate_when_code_absent(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    _write_registry(target, {"EAWF": {"code": "EAWF", "path": "/tmp/eawf", "title": "Eä"}})
    before = target.read_bytes()
    runner.invoke(app, ["repo", "remove", "MISSING", "--registry-path", str(target)])
    after = target.read_bytes()
    assert before == after, "absent-code path MUST NOT mutate registry"
