"""REL-021: a 0.6 tree refuses every writing verb, with the path that still upgrades it.

This release reads and writes only the generation store, and the cutover that
builds one from a 0.6 tree shipped last in 0.7.0rc4. Each verb the effect
table classifies as writing, and the console, is invoked with no arguments
against a plain 0.6 tree: the root group refuses before the verb parses
anything, so the refusal is the same whatever the verb would have needed, and
it must move nothing. Read verbs and help still answer there, and a tree that
carries the epoch marker passes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.migration.epoch2.canary import GENERATIONS_DIRNAME, MARKER_FILENAME
from eawf.surfaces.cli import exit_codes, upgrade_refusal
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.error_codes import ErrorCode
from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS
from eawf.surfaces.cli.verb_effects import CLI_VERB_EFFECTS

pytestmark = pytest.mark.integration

runner = CliRunner()

#: Every verb of a contract group the effect table classifies as writing.
#: The daemon stop verbs answer on a 0.6 tree so a user can stop an old daemon.
STOP_VERBS = ("daemon restart", "daemon stop")

WRITING_VERBS = sorted(
    verb
    for verb, effect in CLI_VERB_EFFECTS.items()
    if effect.effect_class != "read"
    and verb.split(" ", 1)[0] in CONTRACT_GROUPS
    and verb not in STOP_VERBS
)


def _tree(root: Path) -> Path:
    """Lay down a plain 0.6 tree and return its ``state.json``."""
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"schema_version": "1.20", "scope_kind": "repo"}))
    return state_path


def _mark(state_path: Path) -> None:
    """Give the tree holding ``state_path`` the epoch marker."""
    marker = state_path.parent / GENERATIONS_DIRNAME / MARKER_FILENAME
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("{}")


def _digest(root: Path) -> str:
    """Digest every file under ``root/.ea``, names included."""
    ea = root / ".ea"
    lines = sorted(
        f"{p.relative_to(ea)}:{hashlib.sha256(p.read_bytes()).hexdigest()}"
        for p in ea.rglob("*")
        if p.is_file()
    )
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


@pytest.fixture
def epoch1_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A plain 0.6 tree the CLI addresses through ``EA_STATE`` and the working directory."""
    root = tmp_path / "repo"
    monkeypatch.setenv("EA_STATE", str(_tree(root)))
    monkeypatch.chdir(root)
    return root


@pytest.mark.parametrize("verb", WRITING_VERBS)
def test_rel_021_every_writing_verb_refuses_on_a_plain_epoch1_tree(
    epoch1_tree: Path, verb: str
) -> None:
    before = _digest(epoch1_tree)
    result = runner.invoke(app, ["--json", *verb.split()])
    assert result.exit_code == exit_codes.ATTACH_FAILURE, result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["kind"] == upgrade_refusal.MIGRATION_REQUIRED_KIND
    assert envelope["error_code"] == ErrorCode.MIGRATION_REQUIRED.value
    assert upgrade_refusal.UPGRADE_STEPS in envelope["message"]
    assert f"`eawf {verb}` was refused" in envelope["message"]
    assert "0.7.0rc4" in envelope["suggested_next_step"]
    assert _digest(epoch1_tree) == before


def test_rel_021_a_bare_invocation_refuses_before_the_console_opens(epoch1_tree: Path) -> None:
    result = runner.invoke(app, [])
    assert result.exit_code == exit_codes.ATTACH_FAILURE
    assert "0.7.0rc4" in result.output
    assert "migrate epoch2 --apply --plan-digest" not in result.output


@pytest.mark.parametrize("argv", [["ui"], ["--json", "ui"]])
def test_rel_021_the_console_refuses_on_an_epoch1_tree(epoch1_tree: Path, argv: list[str]) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.ATTACH_FAILURE
    assert "0.7.0rc4" in result.output


def test_rel_021_the_refusal_renders_as_text_without_json(epoch1_tree: Path) -> None:
    result = runner.invoke(app, ["task", "create"])
    assert result.exit_code == exit_codes.ATTACH_FAILURE
    assert "0.7.0rc4" in result.output


@pytest.mark.parametrize(
    "argv", [["help"], ["completion", "--help"], ["task", "create", "--help"], ["status"]]
)
def test_rel_021_reads_and_help_still_answer_on_an_epoch1_tree(
    epoch1_tree: Path, argv: list[str]
) -> None:
    result = runner.invoke(app, argv)
    assert result.exit_code != exit_codes.ATTACH_FAILURE, result.output


def test_rel_021_a_marked_tree_passes_the_refusal(epoch1_tree: Path) -> None:
    _mark(epoch1_tree / ".ea" / "state.json")
    result = runner.invoke(app, ["--json", "validate", str(epoch1_tree / "missing.json")])
    assert result.exit_code != exit_codes.ATTACH_FAILURE


def test_rel_021_the_workspace_flag_addresses_the_refused_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("EA_STATE", raising=False)
    monkeypatch.chdir(tmp_path)
    root = tmp_path / "repo"
    _tree(root)
    result = runner.invoke(app, ["--json", "--workspace", str(root), "task", "create"])
    assert result.exit_code == exit_codes.ATTACH_FAILURE, result.output


@pytest.mark.parametrize("verb", STOP_VERBS)
def test_rel_021_a_daemon_stop_verb_is_not_refused_on_a_plain_epoch1_tree(
    epoch1_tree: Path, verb: str
) -> None:
    result = runner.invoke(app, ["--json", *verb.split()])
    assert result.exit_code != exit_codes.ATTACH_FAILURE, result.output
    assert upgrade_refusal.MIGRATION_REQUIRED_KIND not in result.output


@pytest.mark.parametrize(
    ("verb", "bare", "expected"),
    [
        ("", True, True),
        ("ui", False, True),
        ("task create", False, True),
        ("status", False, False),
        ("doctor", False, False),
        ("backup create", False, False),
        ("daemon stop", False, False),
        ("daemon restart", False, False),
        ("task", False, False),
        ("no such verb", False, False),
        ("", False, False),
    ],
)
def test_refuses_boundaries(verb: str, bare: bool, expected: bool) -> None:
    assert upgrade_refusal.refuses(verb, bare=bare) is expected


def test_is_unmigrated_tree_boundaries(tmp_path: Path) -> None:
    state_path = tmp_path / ".ea" / "state.json"
    assert not upgrade_refusal.is_unmigrated_tree(state_path)
    state_path.parent.mkdir()
    state_path.mkdir()
    assert not upgrade_refusal.is_unmigrated_tree(state_path)
    state_path.rmdir()
    state_path.write_text("{}")
    assert upgrade_refusal.is_unmigrated_tree(state_path)
    _mark(state_path)
    assert not upgrade_refusal.is_unmigrated_tree(state_path)


def test_addressed_document_precedence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    near = tmp_path / "outer" / "inner"
    _tree(tmp_path / "outer")
    near.mkdir()
    monkeypatch.chdir(near)
    env_target = tmp_path / "env" / ".ea" / "state.json"
    workspace = tmp_path / "ws"

    env = {"EA_STATE": str(env_target)}
    assert upgrade_refusal.addressed_document("config set", workspace, env) == env_target
    assert upgrade_refusal.addressed_document("config set", workspace, {}) == (
        workspace / ".ea" / "state.json"
    )
    walked = upgrade_refusal.addressed_document("config set", None, {"EA_STATE": ""})
    assert walked == (tmp_path / "outer" / ".ea" / "state.json").resolve()
    rooted = upgrade_refusal.addressed_document("task create", None, env)
    assert rooted == (near / ".ea" / "state.json").resolve()
    assert upgrade_refusal.addressed_document("task create", workspace, env) == (
        workspace.resolve() / ".ea" / "state.json"
    )


def test_addressed_document_finds_a_marked_tree_without_a_document(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ea_dir = tmp_path / "native" / ".ea"
    _mark(ea_dir / "state.json")
    monkeypatch.chdir(ea_dir.parent)
    found = upgrade_refusal.addressed_document("status", None, {})
    assert found == (ea_dir / "state.json").resolve()


def test_addressed_document_is_none_where_no_tree_is_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    assert upgrade_refusal.addressed_document("status", None, {}) is None
