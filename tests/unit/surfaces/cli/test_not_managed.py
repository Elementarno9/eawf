"""A project verb outside an eawf-managed repository is refused with one fixed text.

In a directory with no git and no ``.ea/`` a verb that needs project state
exits 1 (``USER_ERROR``, kind ``NotManaged``) with the refusal the hook uses
for a managed-only skill, while the verbs that work anywhere still run there
with no daemon.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the contract is each verb's exit status

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.scope import NOT_MANAGED_MESSAGE, NotManaged, resolve_state_path
from eawf.workflow.skills.catalog import NOT_MANAGED_REFUSAL

runner = CliRunner()

_BRIEF = """# Pick a cache

## Question

Which cache?

## Findings

The repository has one [1].

## Alternatives

None.

## Recommendation

Keep it.

## Open gaps

None.

## References

| # | Reference |
| --- | --- |
| 1 | src/cache.py |
"""


@pytest.fixture
def no_git(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Run from a directory no git repository and no ``.ea/`` encloses."""
    directory = tmp_path / "loose"
    directory.mkdir()
    monkeypatch.chdir(directory)
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    return directory


def test_not_managed_text(no_git: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # A private runtime dir proves no verb below starts a daemon of its own.
    runtime_dir = no_git.parent / "runtime"
    runtime_dir.mkdir()
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(runtime_dir))

    refused = runner.invoke(app, ["memory", "list"])
    assert refused.exit_code == exit_codes.USER_ERROR == 1
    assert f"error: {NOT_MANAGED_REFUSAL}" in refused.output
    assert "kind: NotManaged" in refused.output

    envelope = runner.invoke(app, ["--json", "memory", "list"])
    assert envelope.exit_code == 1
    body = json.loads(envelope.output)
    assert body["message"] == NOT_MANAGED_REFUSAL
    assert body["data"]["kind"] == "NotManaged"

    (no_git / "brief.md").write_text(_BRIEF, encoding="utf-8")
    assert runner.invoke(app, ["research", "check", "brief.md"]).exit_code == 0
    report = no_git / "report.json"
    report.write_text(json.dumps({"skill_id": "research"}), encoding="utf-8")
    check_report = runner.invoke(app, ["skill", "check-report", "/research", str(report)])
    assert check_report.exit_code != exit_codes.INTERNAL_ERROR
    assert NOT_MANAGED_REFUSAL not in check_report.output
    assert runner.invoke(app, ["rules", "view", "eawf.core.vcs"]).exit_code == 0
    assert not (runtime_dir / "eawfd.sock").exists()

    # Init bootstraps the tree with no daemon to proxy through.
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    init = runner.invoke(app, ["--no-input", "init", "--project-code", "LOOSE"])
    assert init.exit_code == 0, init.output
    assert (no_git / ".ea").is_dir()
    assert not (runtime_dir / "eawfd.sock").exists()


def test_not_managed_message_equals_catalog_refusal() -> None:
    assert NOT_MANAGED_MESSAGE == NOT_MANAGED_REFUSAL


def test_resolve_state_path_raises_not_managed(no_git: Path) -> None:
    with pytest.raises(NotManaged) as raised:
        resolve_state_path(None)
    assert str(raised.value) == NOT_MANAGED_REFUSAL
    assert raised.value.exit_code == exit_codes.USER_ERROR


def test_not_managed_is_still_a_missing_file(no_git: Path) -> None:
    # A caller that handles a missing state file keeps handling the refusal.
    with pytest.raises(FileNotFoundError):
        resolve_state_path(None)


def test_explicit_workspace_is_not_refused(no_git: Path) -> None:
    assert resolve_state_path(no_git) == no_git / ".ea" / "state.json"


def test_managed_directory_is_not_refused(no_git: Path) -> None:
    (no_git / ".ea").mkdir()
    (no_git / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    assert resolve_state_path(None).name == "state.json"
