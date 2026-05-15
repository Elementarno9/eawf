"""Integration tests for ``eawf repo prune`` (P20-I01-W06).

Covers the explicit-pruner surface:

- Successful prune: entries with missing paths are dropped after
  confirmation.
- No-op path: zero missing entries → registry unchanged.
- Confirmation gate: ``--yes`` (or ``--no-input --yes``) required.
- TTY without ``--yes`` → UserDeclined exit 6.
- Active-code handling: pruning the active entry clears
  ``active_code``.
- Schema invariants: missing registry exits 2, corrupted exits 3.
"""

from __future__ import annotations

import json
from pathlib import Path

import orjson
from typer.testing import CliRunner

from eawf.cli.app import app

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
# Successful prune
# ---------------------------------------------------------------------------


def test_repo_prune_drops_missing_paths_with_yes(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"},
            "GONE": {"code": "GONE", "path": str(missing), "title": "Gone"},
        },
    )
    result = runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    assert result.exit_code == 0, result.stdout
    payload = json.loads(target.read_text())
    assert set(payload["repos"]) == {"ALIVE"}


def test_repo_prune_json_envelope_lists_dropped(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"},
            "GONE": {"code": "GONE", "path": str(missing), "title": "Gone"},
        },
    )
    result = runner.invoke(
        app, ["--json", "repo", "prune", "--yes", "--registry-path", str(target)]
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["remaining"] == 1
    pruned_codes = {d["code"] for d in payload["pruned"]}
    assert pruned_codes == {"GONE"}


def test_repo_prune_zero_missing_is_noop(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {"ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"}},
    )
    before = target.read_bytes()
    result = runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    assert result.exit_code == 0, result.stdout
    after = target.read_bytes()
    assert before == after, "no-op prune MUST NOT mutate the registry"


def test_repo_prune_no_input_yes_is_silent_success(tmp_path: Path) -> None:
    """CI cleanup hooks must be able to pass --no-input --yes together."""
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {"GONE": {"code": "GONE", "path": str(missing), "title": "Gone"}},
    )
    result = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "prune",
            "--yes",
            "--registry-path",
            str(target),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(target.read_text())
    assert payload["repos"] == {}


# ---------------------------------------------------------------------------
# Active-code propagation
# ---------------------------------------------------------------------------


def test_repo_prune_clears_active_when_active_is_pruned(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"},
            "GONE": {"code": "GONE", "path": str(missing), "title": "Gone"},
        },
        active="GONE",
    )
    result = runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    assert result.exit_code == 0
    payload = json.loads(target.read_text())
    assert payload["active_code"] is None


def test_repo_prune_preserves_active_when_active_survives(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"},
            "GONE": {"code": "GONE", "path": str(missing), "title": "Gone"},
        },
        active="ALIVE",
    )
    runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    payload = json.loads(target.read_text())
    assert payload["active_code"] == "ALIVE"


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------


def test_repo_prune_no_input_without_yes_exits_7(tmp_path: Path) -> None:
    """--no-input alone (no --yes) MUST refuse to delete silently."""
    missing = tmp_path / "missing-dir"
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {"GONE": {"code": "GONE", "path": str(missing), "title": "Gone"}},
    )
    result = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "prune",
            "--registry-path",
            str(target),
        ],
    )
    assert result.exit_code == 7, result.stdout
    assert "refusing" in result.stdout or "user declined" in result.stdout
    # Registry unchanged.
    payload = json.loads(target.read_text())
    assert "GONE" in payload["repos"]


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_repo_prune_missing_registry_exits_2(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    result = runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    assert result.exit_code == 2, result.stdout
    assert "not found" in result.stdout


def test_repo_prune_corrupted_registry_exits_3(tmp_path: Path) -> None:
    target = tmp_path / "registry.json"
    target.write_bytes(b"{not json")
    result = runner.invoke(app, ["repo", "prune", "--yes", "--registry-path", str(target)])
    assert result.exit_code == 3, result.stdout
    assert "corrupted" in result.stdout


# ---------------------------------------------------------------------------
# Multiple missing entries
# ---------------------------------------------------------------------------


def test_repo_prune_multiple_missing_dropped_together(tmp_path: Path) -> None:
    extant = tmp_path / "extant"
    extant.mkdir()
    target = tmp_path / "registry.json"
    _write_registry(
        target,
        {
            "ALIVE": {"code": "ALIVE", "path": str(extant), "title": "Alive"},
            "GONE1": {"code": "GONE1", "path": str(tmp_path / "g1"), "title": "G1"},
            "GONE2": {"code": "GONE2", "path": str(tmp_path / "g2"), "title": "G2"},
        },
    )
    result = runner.invoke(
        app,
        [
            "--json",
            "repo",
            "prune",
            "--yes",
            "--registry-path",
            str(target),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["count"] == 2
    assert {d["code"] for d in payload["pruned"]} == {"GONE1", "GONE2"}
    saved = json.loads(target.read_text())
    assert set(saved["repos"]) == {"ALIVE"}
