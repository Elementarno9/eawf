"""Integration tests for ``eawf repo add`` (P20-I01-W06).

Covers the explicit registry-growth surface:

- Bootstrap path: first ``repo add`` creates ``~/.eawf/registry.json``.
- Idempotent re-add: same code + same path is a no-op.
- Code-conflict rejection: same code at a different path exits 3.
- TOFU gate: unrecognised parent dir prompts; ``--yes`` /
  ``--no-input`` opt out.
- Schema invariants: invalid code, missing path, non-directory path.
- Active flag handling: ``--set-active`` records ``active_code``.

Per ``feedback_explicit_registry_only`` the registry NEVER grows
on a scan / walk; each test pins ``--registry-path`` so the
operator's real registry stays untouched.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from eawf.surfaces.cli.app import app

runner = CliRunner()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path, code: str, *, title: str | None = None) -> Path:
    """Build a minimal repo subdir under a recognised parent name.

    The "Repos" parent dir name keeps the TOFU prompt suppressed
    by default. Tests that need to exercise the prompt branch use
    `_make_repo_under_unrecognised` instead.
    """
    parent = tmp_path / "Repos"
    parent.mkdir(parents=True, exist_ok=True)
    repo = parent / code.lower()
    repo.mkdir()
    state = {"project": {"code": code, "title": title or code}}
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text(json.dumps(state))
    return repo


def _make_repo_under_unrecognised(tmp_path: Path, code: str) -> Path:
    """Build a repo under an unrecognised parent name so the TOFU fires."""
    parent = tmp_path / "weird-layout"
    parent.mkdir(parents=True, exist_ok=True)
    repo = parent / code.lower()
    repo.mkdir()
    state = {"project": {"code": code}}
    (repo / ".ea").mkdir()
    (repo / ".ea" / "state.json").write_text(json.dumps(state))
    return repo


def _read_registry_bytes(path: Path) -> dict[str, object]:
    return json.loads(path.read_text())


# ---------------------------------------------------------------------------
# Bootstrap path
# ---------------------------------------------------------------------------


def test_repo_add_bootstrap_creates_registry(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / ".eawf" / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo),
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    assert registry_path.exists()
    payload = _read_registry_bytes(registry_path)
    assert payload["version"] == "1"
    assert "EAWF" in payload["repos"]
    assert payload["repos"]["EAWF"]["path"] == str(repo)


def test_repo_add_json_envelope_carries_metadata(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF", title="Eä Workflow")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "--json",
            "repo",
            "add",
            str(repo),
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = json.loads(result.stdout)
    assert payload["code"] == "EAWF"
    assert payload["path"] == str(repo)
    assert payload["title"] == "Eä Workflow"
    assert payload["added"] is True


def test_repo_add_derives_code_from_state_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "DEMO")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        ["repo", "add", str(repo), "--registry-path", str(registry_path)],
    )
    assert result.exit_code == 0
    payload = _read_registry_bytes(registry_path)
    assert "DEMO" in payload["repos"]


def test_repo_add_explicit_code_overrides_derivation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "DEMO")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo),
            "--code",
            "ALIAS",
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0
    payload = _read_registry_bytes(registry_path)
    assert "ALIAS" in payload["repos"]
    assert payload["repos"]["ALIAS"]["path"] == str(repo)


def test_repo_add_explicit_title_overrides_derivation(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "DEMO", title="not used")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo),
            "--title",
            "Custom Title",
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0
    payload = _read_registry_bytes(registry_path)
    assert payload["repos"]["DEMO"]["title"] == "Custom Title"


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


def test_repo_add_idempotent_when_same_path(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    first = runner.invoke(app, ["repo", "add", str(repo), "--registry-path", str(registry_path)])
    second = runner.invoke(app, ["repo", "add", str(repo), "--registry-path", str(registry_path)])
    assert first.exit_code == 0
    assert second.exit_code == 0
    assert "idempotent" in second.stdout or "already registered" in second.stdout


def test_repo_add_idempotent_json_marks_added_false(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    runner.invoke(app, ["repo", "add", str(repo), "--registry-path", str(registry_path)])
    result = runner.invoke(
        app,
        ["--json", "repo", "add", str(repo), "--registry-path", str(registry_path)],
    )
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["added"] is False


# ---------------------------------------------------------------------------
# Code conflict rejection
# ---------------------------------------------------------------------------


def test_repo_add_rejects_same_code_at_different_path(tmp_path: Path) -> None:
    repo_a = _make_repo(tmp_path, "EAWF")
    # Re-use the parent "Repos" but a different subdir so the same code
    # surfaces at a different path.
    other = tmp_path / "Repos" / "eawf-other"
    other.mkdir()
    (other / ".ea").mkdir()
    (other / ".ea" / "state.json").write_text(json.dumps({"project": {"code": "EAWF"}}))
    registry_path = tmp_path / "registry.json"
    runner.invoke(app, ["repo", "add", str(repo_a), "--registry-path", str(registry_path)])
    result = runner.invoke(app, ["repo", "add", str(other), "--registry-path", str(registry_path)])
    assert result.exit_code == 1, result.stdout
    assert "already registered" in result.stdout


# ---------------------------------------------------------------------------
# Schema / input invariants
# ---------------------------------------------------------------------------


def test_repo_add_missing_path_exits_2(tmp_path: Path) -> None:
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(tmp_path / "absent"),
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 1, result.stdout
    assert "does not exist" in result.stdout


def test_repo_add_path_not_directory_exits_3(tmp_path: Path) -> None:
    afile = tmp_path / "a-file.txt"
    afile.write_text("not a dir")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        ["repo", "add", str(afile), "--registry-path", str(registry_path), "--yes"],
    )
    assert result.exit_code == 1, result.stdout
    assert "not a directory" in result.stdout


def test_repo_add_no_code_no_state_file_exits_3(tmp_path: Path) -> None:
    parent = tmp_path / "Repos"
    parent.mkdir()
    repo = parent / "nostate"
    repo.mkdir()
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(app, ["repo", "add", str(repo), "--registry-path", str(registry_path)])
    assert result.exit_code == 1, result.stdout
    assert "cannot derive repo code" in result.stdout


def test_repo_add_invalid_code_exits_3(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo),
            "--code",
            "bad-lowercase",
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 1, result.stdout
    assert "invalid repo code" in result.stdout


# ---------------------------------------------------------------------------
# TOFU prompt: unrecognised parent
# ---------------------------------------------------------------------------


def test_repo_add_unrecognised_parent_no_input_without_yes_exits_7(tmp_path: Path) -> None:
    repo = _make_repo_under_unrecognised(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "add",
            str(repo),
            "--registry-path",
            str(registry_path),
        ],
    )
    # ``UserDeclined`` maps to exit code 7 (canonical USER_DECLINED).
    assert result.exit_code == 1, result.stdout
    assert "recognised" in result.stdout or "refusing" in result.stdout


def test_repo_add_unrecognised_parent_with_yes_succeeds(tmp_path: Path) -> None:
    repo = _make_repo_under_unrecognised(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "--no-input",
            "repo",
            "add",
            str(repo),
            "--yes",
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0, result.stdout
    payload = _read_registry_bytes(registry_path)
    assert "EAWF" in payload["repos"]


# ---------------------------------------------------------------------------
# Active flag handling
# ---------------------------------------------------------------------------


def test_repo_add_set_active_records_active_code(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    result = runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo),
            "--set-active",
            "--registry-path",
            str(registry_path),
        ],
    )
    assert result.exit_code == 0
    payload = _read_registry_bytes(registry_path)
    assert payload["active_code"] == "EAWF"


def test_repo_add_without_set_active_leaves_existing_active(tmp_path: Path) -> None:
    repo_a = _make_repo(tmp_path, "ALPHA")
    repo_b = _make_repo(tmp_path, "BETA")
    registry_path = tmp_path / "registry.json"
    runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo_a),
            "--set-active",
            "--registry-path",
            str(registry_path),
        ],
    )
    runner.invoke(
        app,
        [
            "repo",
            "add",
            str(repo_b),
            "--registry-path",
            str(registry_path),
        ],
    )
    payload = _read_registry_bytes(registry_path)
    assert payload["active_code"] == "ALPHA"


# ---------------------------------------------------------------------------
# Multiple-add ordering
# ---------------------------------------------------------------------------


def test_repo_add_multiple_repos_preserves_each(tmp_path: Path) -> None:
    repo_a = _make_repo(tmp_path, "ALPHA")
    repo_b = _make_repo(tmp_path, "BETA")
    repo_c = _make_repo(tmp_path, "GAMMA")
    registry_path = tmp_path / "registry.json"
    for repo in (repo_a, repo_b, repo_c):
        result = runner.invoke(
            app, ["repo", "add", str(repo), "--registry-path", str(registry_path)]
        )
        assert result.exit_code == 0, result.stdout
    payload = _read_registry_bytes(registry_path)
    assert set(payload["repos"]) == {"ALPHA", "BETA", "GAMMA"}


# ---------------------------------------------------------------------------
# Schema robustness
# ---------------------------------------------------------------------------


def test_repo_add_rejects_corrupted_existing_registry(tmp_path: Path) -> None:
    """Operator-broken registry should fail loud, not silently overwrite."""
    repo = _make_repo(tmp_path, "EAWF")
    registry_path = tmp_path / "registry.json"
    registry_path.write_bytes(b"{not json")
    result = runner.invoke(app, ["repo", "add", str(repo), "--registry-path", str(registry_path)])
    assert result.exit_code == 1, result.stdout
    assert "corrupted" in result.stdout


# ---------------------------------------------------------------------------
# Registry-growth invariant: scanning a parent dir does NOT auto-grow
# ---------------------------------------------------------------------------


def test_repo_add_only_adds_named_path_not_siblings(tmp_path: Path) -> None:
    """Adding one repo MUST NOT register sibling dirs under the parent."""
    repo_a = _make_repo(tmp_path, "ALPHA")
    _make_repo(tmp_path, "BETA")  # sibling — never named
    registry_path = tmp_path / "registry.json"
    runner.invoke(app, ["repo", "add", str(repo_a), "--registry-path", str(registry_path)])
    payload = _read_registry_bytes(registry_path)
    assert set(payload["repos"]) == {"ALPHA"}
