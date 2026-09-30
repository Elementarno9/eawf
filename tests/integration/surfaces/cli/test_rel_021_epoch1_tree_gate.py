"""REL-021: after the flag day a plain epoch-1 tree refuses every mutating verb.

Each refused verb is invoked with no arguments against a plain epoch-1 tree:
the gate runs before the verb parses anything, so the refusal is the same
whatever the verb would have needed, and it must move nothing. The exempt
migration-support verbs pass the gate, an epoch-2 tree passes it, and an
epoch-1 verb refuses even where no tree exists, pointing at ``eawf init``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest
import typer
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.platform.install.epoch2_birth import bear_epoch2_tree
from eawf.surfaces.cli import exit_codes, flag_day_gate
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.error_codes import ErrorCode

pytestmark = pytest.mark.integration

runner = CliRunner()


def _tree(root: Path) -> Path:
    """Lay down a plain epoch-1 tree and return its ``state.json``."""
    state_path = root / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps({"schema_version": "1.0", "scope_kind": "repo"}))
    return state_path


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
    """A plain epoch-1 tree the CLI addresses through ``EA_STATE`` and the working directory."""
    root = tmp_path / "repo"
    monkeypatch.setenv("EA_STATE", str(_tree(root)))
    monkeypatch.chdir(root)
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return root


@pytest.mark.parametrize("verb", sorted(flag_day_gate.refused_verbs()))
def test_rel_021_every_refused_verb_refuses_on_a_plain_epoch1_tree(
    epoch1_tree: Path, verb: str
) -> None:
    before = _digest(epoch1_tree)
    result = runner.invoke(app, ["--json", *verb.split()])
    assert result.exit_code == exit_codes.ATTACH_FAILURE, result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["kind"] == flag_day_gate.MIGRATION_REQUIRED_KIND
    assert envelope["error_code"] == ErrorCode.MIGRATION_REQUIRED.value
    assert envelope["exit_code"] == ErrorCode.MIGRATION_REQUIRED.exit_code
    assert f"`eawf {verb}` was refused" in envelope["message"]
    assert "eawf migrate epoch2" in envelope["message"]
    assert _digest(epoch1_tree) == before


def _gate(argv: list[str]) -> None:
    """Run only the gate for ``argv``, as the root group would before dispatch."""
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    ctx = root.make_context("eawf", list(argv))
    flag_day_gate.enforce(root, ctx, [*ctx._protected_args, *ctx.args])


@pytest.mark.parametrize("verb", sorted(flag_day_gate.exempt_verbs()))
def test_rel_021_an_exempt_verb_passes_the_gate(epoch1_tree: Path, verb: str) -> None:
    _gate(verb.split())


@pytest.mark.parametrize("verb", ["memory add", "config set", "task create"])
def test_rel_021_the_gate_refuses_before_dispatch(epoch1_tree: Path, verb: str) -> None:
    with pytest.raises(click.exceptions.Exit) as caught:
        _gate(verb.split())
    assert caught.value.exit_code == exit_codes.ATTACH_FAILURE


def test_rel_021_a_group_option_before_the_verb_still_resolves(epoch1_tree: Path) -> None:
    root = typer.main.get_command(app)
    assert isinstance(root, click.Group)
    ctx = click.Context(root)
    assert flag_day_gate.command_path(root, ctx, ["migrate", "--dry-run", "status"]) == (
        "migrate status"
    )
    assert flag_day_gate.command_path(root, ctx, ["no-such-verb"]) == ""


def test_rel_021_an_epoch2_tree_passes_the_gate(epoch1_tree: Path) -> None:
    state_path = epoch1_tree / ".ea" / "state.json"
    bear_epoch2_tree(state_path, project_code="QR", born_at=datetime.now(UTC))
    assert resolve_authority(state_path.parent).epoch == 2
    _gate(["config", "set", "x", "1"])


def test_rel_021_a_cross_cutting_verb_passes_where_no_tree_exists(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EA_STATE", str(tmp_path / "none" / ".ea" / "state.json"))
    _gate(["config", "set", "x", "1"])


def test_rel_021_a_repo_rooted_verb_is_judged_by_the_tree_it_writes(
    epoch1_tree: Path, tmp_path: Path
) -> None:
    """``task create`` writes the ``--workspace`` tree, not the ``EA_STATE`` document."""
    native = tmp_path / "native"
    state_path = _tree(native)
    bear_epoch2_tree(state_path, project_code="QR", born_at=datetime.now(UTC))
    _gate(["--workspace", str(native), "task", "create"])
    with pytest.raises(click.exceptions.Exit):
        _gate(["--workspace", str(epoch1_tree), "task", "create"])


@pytest.mark.parametrize(
    ("argv", "replacement"),
    [
        (("workspace", "init", "MAIN"), "workspace add"),
        (("workspace", "add-repo", "X"), "workspace member add"),
        (("workspace", "remove-repo", "X"), "workspace member remove"),
        (("workspace", "validate"), "workspace show"),
        (("workspace", "status"), "workspace show"),
        (("repo", "link", "MAIN", "X"), "workspace member add"),
        (("repo", "link-workspace", "MAIN", "X"), "workspace member add"),
    ],
)
def test_auth_049_a_retired_workspace_pointer_verb_names_its_replacement(
    argv: tuple[str, ...], replacement: str
) -> None:
    result = runner.invoke(app, ["--json", *argv])
    assert result.exit_code == exit_codes.VALIDATION_ERROR, result.output
    envelope = json.loads(result.output)
    assert envelope["data"]["kind"] == "LegacyOperationRemoved"
    assert f"run `eawf {replacement}` instead" in envelope["message"]
    assert "No such command" not in result.output
