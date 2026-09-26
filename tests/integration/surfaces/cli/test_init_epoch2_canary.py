"""``eawf init --epoch2-canary`` provisions a canary and tears it down cleanly.

Every test runs under a fresh ``EAWF_RUNTIME_DIR``, a home directory and a
temp root inside its own ``tmp_path``, and the daemonless registry arm, so
nothing here can reach the operator's registry, runtime directory or live
daemon. Provisioning must leave a tree the authority resolver grants epoch
2, answer with the typed canary reference, and register exactly one row;
teardown must leave the registry without any row for the canary. The
refusals run before any write, so each one is checked against an
unchanged registry and an unchanged tree.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import CANARY_DECLARATION_FILENAME
from eawf.kernel.migration.epoch2.generation import tree_digests
from eawf.kernel.state.epoch2.authority import CanaryRepositoryRef, resolve_authority
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path
from eawf.platform.install.canary import (
    PROVISION_RECORD_LOCATOR,
    RUNTIME_DIR_PREFIX,
    CanaryProvisionError,
    canary_ref,
    provision_canary,
)
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import repo as repo_cmd

runner = CliRunner()

CANARY_URN = "eawf://WSP-CANARY/PRJ-CANARY/REP-CANARY/repository/REP-CANARY"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the registry, the runtime directory and every temp allocation."""
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    runtime = tmp_path / "rt"
    runtime.mkdir()
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    return home_dir


def _registry_file(home: Path) -> Path:
    return home / ".eawf" / "registry.json"


def _repos(registry: Path) -> dict[str, Any]:
    repos: dict[str, Any] = json.loads(registry.read_text(encoding="utf-8"))["repos"]
    return repos


def _canary(action: str, target: Path, *extra: str) -> Result:
    return runner.invoke(
        app, ["--json", "init", "--epoch2-canary", action, "--target", str(target), *extra]
    )


def _payload(result: Result) -> dict[str, Any]:
    assert result.exit_code == 0, result.output
    payload: dict[str, Any] = json.loads(result.stdout)
    return payload


def test_init_epoch2_canary_provision_writes_declaration_marker_and_ref(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "canary"

    payload = _payload(_canary("provision", target))

    ref = CanaryRepositoryRef.model_validate(payload["canary"])
    assert payload["canary"] == {"project_code": "CANARY", "repository": CANARY_URN}
    assert ref.project_code == "CANARY"
    assert payload["epoch"] == 2
    assert (target / ".ea" / CANARY_DECLARATION_FILENAME).is_file()
    assert (target / ".ea" / "generations" / "EPOCH2_ACTIVE.json").is_file()
    assert (target / ".ea" / "state.json").is_file()
    assert (target / ".ea" / PROVISION_RECORD_LOCATOR).is_file()
    authority = resolve_authority(target / ".ea")
    assert authority.epoch == 2
    assert authority.generation_id == payload["generation_id"]
    assert _repos(_registry_file(home)) == {
        "CANARY": {
            "code": "CANARY",
            "last_seen": _repos(_registry_file(home))["CANARY"]["last_seen"],
            "path": str(target.resolve()),
            "title": "epoch-2 canary CANARY",
        }
    }


def test_init_epoch2_canary_provision_allocates_fresh_runtime_dir(
    tmp_path: Path, home: Path
) -> None:
    payload = _payload(_canary("provision", tmp_path / "canary"))

    runtime_dir = Path(payload["runtime_dir"])
    assert runtime_dir.parent == (tmp_path / "canary" / ".ea" / "local").resolve()
    assert list((tmp_path / "scratch").glob(f"{RUNTIME_DIR_PREFIX}*")) == []
    assert runtime_dir.is_dir()
    assert runtime_dir.name.startswith(RUNTIME_DIR_PREFIX)
    assert runtime_dir != tmp_path / "rt"
    assert list(runtime_dir.iterdir()) == []


def test_init_epoch2_canary_provision_tree_has_declared_commit_policy(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "canary"
    _payload(_canary("provision", target))
    ea_files = sorted(
        path.relative_to(target).as_posix()
        for path in (target / ".ea").rglob("*")
        if path.is_file()
    )

    policies = {path: classify_path(path).policy for path in ea_files}

    assert policies[f".ea/{CANARY_DECLARATION_FILENAME}"] is CommitPolicy.NOT_COMMITTED
    assert policies[f".ea/{PROVISION_RECORD_LOCATOR}"] is CommitPolicy.NOT_COMMITTED
    assert policies[".ea/generations/EPOCH2_ACTIVE.json"] is CommitPolicy.COMMITTED
    assert policies[".ea/generations/selected.json"] is CommitPolicy.COMMITTED


def test_init_epoch2_canary_teardown_leaves_no_registry_row(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "canary"
    provisioned = _payload(_canary("provision", target))

    payload = _payload(_canary("teardown", target))

    assert _repos(_registry_file(home)) == {}
    assert payload["removed_registry_codes"] == ["CANARY"]
    assert payload["runtime_dir_removed"] is True
    assert not target.exists()
    assert not Path(provisioned["runtime_dir"]).exists()


def test_init_epoch2_canary_explicit_registry_path_is_the_one_written(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "canary"
    registry = tmp_path / "elsewhere" / "registry.json"

    _payload(_canary("provision", target, "--registry-path", str(registry)))

    assert list(_repos(registry)) == ["CANARY"]
    assert not _registry_file(home).exists()
    _payload(_canary("teardown", target, "--registry-path", str(registry)))
    assert _repos(registry) == {}


def test_init_epoch2_canary_teardown_keeps_unrelated_rows(tmp_path: Path, home: Path) -> None:
    other = tmp_path / "Repos" / "other"
    other.mkdir(parents=True)
    added = runner.invoke(
        app,
        ["--no-input", "repo", "add", str(other), "--code", "OTHER", "--yes"],
    )
    assert added.exit_code == 0, added.output
    target = tmp_path / "canary"
    _payload(_canary("provision", target, "--project-code", "CANARY2"))

    payload = _payload(_canary("teardown", target))

    assert list(_repos(_registry_file(home))) == ["OTHER"]
    assert payload["removed_registry_codes"] == ["CANARY2"]


def test_init_epoch2_canary_default_init_is_unchanged(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "plain"

    result = runner.invoke(
        app, ["--no-input", "init", "--project-code", "DEMO", "--target", str(target)]
    )

    assert result.exit_code == 0, result.output
    assert not (target / ".ea" / CANARY_DECLARATION_FILENAME).exists()
    assert not (target / ".ea" / "generations").exists()
    assert resolve_authority(target / ".ea").epoch == 1
    assert not _registry_file(home).exists()


def test_init_epoch2_canary_provision_refuses_non_empty_target(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "busy"
    target.mkdir()
    (target / "README.md").write_text("mine\n", encoding="utf-8")
    before = tree_digests(target)

    result = _canary("provision", target)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "fresh directory" in result.output
    assert tree_digests(target) == before
    assert not (target / ".ea").exists()
    assert not _registry_file(home).exists()


def test_init_epoch2_canary_provision_accepts_empty_existing_target(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "empty"
    target.mkdir()

    payload = _payload(_canary("provision", target))

    assert payload["root"] == str(target.resolve())


def test_init_epoch2_canary_provision_refuses_taken_code(tmp_path: Path, home: Path) -> None:
    first = tmp_path / "first"
    _payload(_canary("provision", first))
    registry_before = _registry_file(home).read_bytes()

    result = _canary("provision", tmp_path / "second")

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "already names a repository" in result.output
    assert not (tmp_path / "second").exists()
    assert _registry_file(home).read_bytes() == registry_before


@pytest.mark.parametrize(
    ("code", "message"),
    [
        pytest.param("ABCDEFGHIJKLM", "at most 12 characters", id="too-long-once-prefixed"),
        pytest.param("lower", "not a project code", id="not-a-code"),
    ],
)
def test_init_epoch2_canary_provision_refuses_bad_code(
    tmp_path: Path, home: Path, code: str, message: str
) -> None:
    target = tmp_path / "canary"

    result = _canary("provision", target, "--project-code", code)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert message in result.output
    assert not target.exists()


def test_init_epoch2_canary_provision_accepts_twelve_character_code(
    tmp_path: Path, home: Path
) -> None:
    payload = _payload(_canary("provision", tmp_path / "canary", "--project-code", "ABCDEFGHIJKL"))

    assert payload["canary"]["project_code"] == "ABCDEFGHIJKL"


def test_init_epoch2_canary_registry_failure_removes_tree(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object) -> None:
        raise cli_errors.StateConflict("registry held elsewhere", kind="LockConflict")

    monkeypatch.setattr(repo_cmd, "_persist_registry", refuse)
    target = tmp_path / "canary"

    result = _canary("provision", target)

    assert result.exit_code == cli_errors.StateConflict.exit_code
    assert not target.exists()
    assert list((tmp_path / "scratch").glob(f"{RUNTIME_DIR_PREFIX}*")) == []


def test_init_epoch2_canary_teardown_refuses_production_tree(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "plain"
    init = runner.invoke(
        app, ["--no-input", "init", "--project-code", "DEMO", "--target", str(target)]
    )
    assert init.exit_code == 0, init.output
    before = tree_digests(target)

    result = _canary("teardown", target)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "not a disposable canary" in result.output
    assert tree_digests(target) == before


def test_init_epoch2_canary_teardown_refuses_declared_tree_without_record(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "canary"
    _payload(_canary("provision", target))
    (target / ".ea" / PROVISION_RECORD_LOCATOR).unlink()
    registry_before = _registry_file(home).read_bytes()

    result = _canary("teardown", target)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "not provisioned by eawf init" in result.output
    assert target.is_dir()
    assert _registry_file(home).read_bytes() == registry_before


def test_init_epoch2_canary_teardown_refuses_copied_tree(tmp_path: Path, home: Path) -> None:
    original = tmp_path / "canary"
    _payload(_canary("provision", original))
    copy = tmp_path / "copy"
    shutil.copytree(original, copy)
    registry_before = _registry_file(home).read_bytes()

    result = _canary("teardown", copy)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "is a copy" in result.output
    assert copy.is_dir()
    assert original.is_dir()
    assert _registry_file(home).read_bytes() == registry_before


def test_provision_canary_refuses_already_declared_tree(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "canary"
    _payload(_canary("provision", target))
    before = tree_digests(target)

    with pytest.raises(CanaryProvisionError, match="already declares"):
        provision_canary(
            repo_root=target,
            ref=canary_ref("CANARY"),
            provisioned_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    assert tree_digests(target) == before


def test_init_epoch2_canary_teardown_refuses_live_daemon(tmp_path: Path, home: Path) -> None:
    target = tmp_path / "canary"
    provisioned = _payload(_canary("provision", target))
    (Path(provisioned["runtime_dir"]) / "eawfd.pid").write_text("4242\n", encoding="utf-8")
    registry_before = _registry_file(home).read_bytes()

    result = _canary("teardown", target)

    assert result.exit_code == cli_errors.UserError.exit_code
    assert "stop its daemon" in result.output
    assert target.is_dir()
    assert _registry_file(home).read_bytes() == registry_before


def test_init_epoch2_canary_teardown_is_repeatable_after_registry_removed(
    tmp_path: Path, home: Path
) -> None:
    target = tmp_path / "canary"
    _payload(_canary("provision", target))
    removed = runner.invoke(app, ["--no-input", "repo", "remove", "CANARY"])
    assert removed.exit_code == 0, removed.output

    payload = _payload(_canary("teardown", target))

    assert payload["removed_registry_codes"] == []
    assert not target.exists()


def test_init_epoch2_canary_rejects_unknown_action(tmp_path: Path, home: Path) -> None:
    result = _canary("rebuild", tmp_path / "canary")

    assert result.exit_code != 0
    assert not (tmp_path / "canary").exists()
