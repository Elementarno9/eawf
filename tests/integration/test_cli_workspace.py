"""Integration tests for the ``eawf workspace`` registry subcommands (P20-I01-W05).

Covers the CLI surface that exposes the read-only registry path:

- ``eawf workspace registry-list`` — JSON + text emission, sorting,
  stale flag propagation.
- ``eawf workspace registry-status`` — text frame emission, JSON
  envelope, registry-unavailable fallback.

Every test pins the registry path via ``--registry-path`` (or builds
the file directly under ``tmp_path``) so the operator's real
``~/.eawf/registry.json`` stays untouched.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_registry(tmp_path: Path, payload: dict[str, object]) -> Path:
    target = tmp_path / "registry.json"
    target.write_bytes(orjson.dumps(payload))
    return target


def _fresh_payload(
    repos: dict[str, dict[str, str]], *, active: str | None = None
) -> dict[str, object]:
    return {
        "version": "1",
        "updated_at": datetime.now(UTC).isoformat(),
        "active_code": active,
        "repos": repos,
    }


def _make_repo(tmp_path: Path, code: str) -> Path:
    """Build a minimal repo subdir with a placeholder ``.ea/state.json``."""
    repo = tmp_path / code.lower()
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        json.dumps({"scope_kind": "repo", "project": {"code": code}})
    )
    return repo


# ---------------------------------------------------------------------------
# workspace registry-list
# ---------------------------------------------------------------------------


def test_registry_list_emits_text_envelope(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    repo_demo = _make_repo(tmp_path, "DEMO")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {
                "EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"},
                "DEMO": {"code": "DEMO", "path": str(repo_demo), "title": "Demo"},
            },
            active="EAWF",
        ),
    )
    result = runner.invoke(app, ["workspace", "registry-list", "--registry-path", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "EAWF" in result.stdout
    assert "DEMO" in result.stdout
    assert "(active)" in result.stdout


def test_registry_list_emits_json_envelope(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    result = runner.invoke(
        app,
        ["--json", "workspace", "registry-list", "--registry-path", str(target)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["count"] == 1
    assert payload["active_code"] == "EAWF"
    assert payload["registry_version"] == "1"
    assert payload["repos"][0]["code"] == "EAWF"
    assert payload["repos"][0]["active"] is True


def test_registry_list_missing_file_exits_2(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "workspace",
            "registry-list",
            "--registry-path",
            str(tmp_path / "missing.json"),
        ],
    )
    assert result.exit_code == 2, result.stdout
    assert "not found" in result.stdout


def test_registry_list_corrupted_exits_3(tmp_path: Path) -> None:
    target = tmp_path / "bad.json"
    target.write_bytes(b"{not json")
    result = runner.invoke(app, ["workspace", "registry-list", "--registry-path", str(target)])
    assert result.exit_code == 3, result.stdout
    assert "corrupted" in result.stdout


def test_registry_list_sorts_alphabetically(tmp_path: Path) -> None:
    repo_zed = _make_repo(tmp_path, "ZED")
    repo_alpha = _make_repo(tmp_path, "ALPHA")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {
                "ZED": {"code": "ZED", "path": str(repo_zed), "title": "Zed"},
                "ALPHA": {"code": "ALPHA", "path": str(repo_alpha), "title": "Alpha"},
            }
        ),
    )
    result = runner.invoke(
        app,
        ["--json", "workspace", "registry-list", "--registry-path", str(target)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    codes = [row["code"] for row in payload["repos"]]
    assert codes == ["ALPHA", "ZED"]


def test_registry_list_marks_stale_entries(tmp_path: Path) -> None:
    """Repo without a state.json fires the stale chip via signal (c)."""
    ghost_repo = tmp_path / "ghost"
    ghost_repo.mkdir()
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"GHOST": {"code": "GHOST", "path": str(ghost_repo), "title": "Ghost"}},
        ),
    )
    result = runner.invoke(
        app,
        ["--json", "workspace", "registry-list", "--registry-path", str(target)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["repos"][0]["stale"] is True


# ---------------------------------------------------------------------------
# workspace registry-status
# ---------------------------------------------------------------------------


def test_registry_status_emits_rendered_frame(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    result = runner.invoke(app, ["workspace", "registry-status", "--registry-path", str(target)])
    assert result.exit_code == 0, result.stdout
    assert "Eä" in result.stdout
    assert "EAWF" in result.stdout
    # Quadrant pane titles must surface in the rendered frame.
    assert "roadmap" in result.stdout
    assert "backlog" in result.stdout


def test_registry_status_json_envelope_carries_rendered(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    result = runner.invoke(
        app,
        ["--json", "workspace", "registry-status", "--registry-path", str(target)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["registry_available"] is True
    assert payload["active_code"] == "EAWF"
    assert payload["count"] == 1
    assert "rendered" in payload
    assert "Eä" in payload["rendered"]


def test_registry_status_missing_registry_returns_placeholder_frame(tmp_path: Path) -> None:
    """``registry-status`` MUST not exit non-zero — it falls back to a placeholder."""
    result = runner.invoke(
        app,
        [
            "workspace",
            "registry-status",
            "--registry-path",
            str(tmp_path / "missing.json"),
        ],
    )
    assert result.exit_code == 0
    assert "registry unavailable" in result.stdout
    assert "Eä" in result.stdout


def test_registry_status_json_missing_registry_envelope(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "--json",
            "workspace",
            "registry-status",
            "--registry-path",
            str(tmp_path / "missing.json"),
        ],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["registry_available"] is False
    assert "not found" in payload["error"]
    assert "rendered" in payload


def test_registry_status_width_flag_overrides_console_width(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    narrow = runner.invoke(
        app,
        [
            "workspace",
            "registry-status",
            "--registry-path",
            str(target),
            "--width",
            "40",
        ],
    )
    wide = runner.invoke(
        app,
        [
            "workspace",
            "registry-status",
            "--registry-path",
            str(target),
            "--width",
            "200",
        ],
    )
    assert narrow.exit_code == 0 and wide.exit_code == 0
    # The two outputs MUST differ in column count somewhere (the narrow
    # variant wraps; the wide one does not).
    assert narrow.stdout != wide.stdout


# ---------------------------------------------------------------------------
# Read-only invariant: subcommands MUST NOT touch the registry file
# ---------------------------------------------------------------------------


def test_registry_list_does_not_mutate_file(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    before = target.read_bytes()
    before_mtime = target.stat().st_mtime
    result = runner.invoke(app, ["workspace", "registry-list", "--registry-path", str(target)])
    assert result.exit_code == 0
    after = target.read_bytes()
    after_mtime = target.stat().st_mtime
    assert before == after, "registry-list MUST be read-only — bytes drifted"
    assert before_mtime == after_mtime, "registry-list MUST not touch the file mtime"


def test_registry_status_does_not_mutate_file(tmp_path: Path) -> None:
    repo_eawf = _make_repo(tmp_path, "EAWF")
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"EAWF": {"code": "EAWF", "path": str(repo_eawf), "title": "Eä"}},
            active="EAWF",
        ),
    )
    before = target.read_bytes()
    result = runner.invoke(app, ["workspace", "registry-status", "--registry-path", str(target)])
    assert result.exit_code == 0
    assert target.read_bytes() == before


def test_registry_list_does_not_create_file_when_absent(tmp_path: Path) -> None:
    """Missing-file path MUST NOT auto-init the registry."""
    target = tmp_path / "absent.json"
    runner.invoke(app, ["workspace", "registry-list", "--registry-path", str(target)])
    assert not target.exists()


def test_registry_status_does_not_create_file_when_absent(tmp_path: Path) -> None:
    target = tmp_path / "absent.json"
    runner.invoke(app, ["workspace", "registry-status", "--registry-path", str(target)])
    assert not target.exists()


# ---------------------------------------------------------------------------
# Schema rejections
# ---------------------------------------------------------------------------


def test_registry_list_extra_field_rejected(tmp_path: Path) -> None:
    target = _write_registry(
        tmp_path,
        {
            "version": "1",
            "updated_at": datetime.now(UTC).isoformat(),
            "active_code": None,
            "repos": {},
            "rogue_field": "bad",
        },
    )
    result = runner.invoke(app, ["workspace", "registry-list", "--registry-path", str(target)])
    assert result.exit_code == 3, result.stdout
    assert "invalid registry schema" in result.stdout


@pytest.mark.parametrize(
    "code",
    ["EAWF", "DEMO", "OTHER"],
)
def test_registry_list_handles_known_active_codes(tmp_path: Path, code: str) -> None:
    repo = _make_repo(tmp_path, code)
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {code: {"code": code, "path": str(repo), "title": code}},
            active=code,
        ),
    )
    result = runner.invoke(
        app,
        ["--json", "workspace", "registry-list", "--registry-path", str(target)],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["active_code"] == code
    assert payload["repos"][0]["active"] is True


def test_registry_status_stale_chip_propagates(tmp_path: Path) -> None:
    """The rendered frame must carry ``(stale)`` when an entry trips signal (b)."""
    repo = tmp_path / "old-repo"
    (repo / ".ea").mkdir(parents=True)
    state_file = repo / ".ea" / "state.json"
    state_file.write_text("{}")
    import os
    import time as time_mod

    very_old = time_mod.time() - timedelta(days=60).total_seconds()
    os.utime(state_file, (very_old, very_old))
    target = _write_registry(
        tmp_path,
        _fresh_payload(
            {"OLD": {"code": "OLD", "path": str(repo), "title": "Old"}},
            active="OLD",
        ),
    )
    result = runner.invoke(app, ["workspace", "registry-status", "--registry-path", str(target)])
    assert result.exit_code == 0
    assert "(stale)" in result.stdout
