"""A console launched without ``--actor`` acts as the principal its operator claims.

The claim is the user layer's ``operator.principal``, written by the command the
console names when it acts as nobody. A tree's Track owners are never assumed: in a
shared repository the person at this console need not be the one who owns them.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.config import layered
from eawf.surfaces.cli.app import app
from eawf.surfaces.tui.console.operations import CLAIM_COMMAND, NO_PRINCIPAL_REASON, Operator
from eawf.surfaces.tui.launch import claimed_principal, resolve_operator

runner = CliRunner()


@pytest.fixture
def user_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user layer at a file under this test and run the CLI in a bare repo."""
    path = tmp_path / "home" / "config.yaml"
    monkeypatch.setattr(layered, "global_config_path", lambda: path)
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    return path


def _claim(principal: str, *, scope: str = "global") -> int:
    """Run the claim command the console names, for *principal*, and return its exit code."""
    argv = CLAIM_COMMAND.replace("<key>", principal).split()[1:]
    if scope != "global":
        argv[argv.index("global")] = scope
    result = runner.invoke(app, argv)
    return result.exit_code


@pytest.mark.parametrize("principal", ["OP-0001", "OP-0002"], ids=["owner", "not-an-owner"])
def test_a_claimed_principal_is_who_the_console_acts_as(user_layer: Path, principal: str) -> None:
    """Whoever the claim names is acted as; a claim is the person's own, never a Track's."""
    assert _claim(principal) == 0

    assert claimed_principal() == principal
    assert resolve_operator(actor=None, receipt_ref=None) == Operator(principal=principal)


def test_no_claim_acts_as_nobody_and_names_the_claim_command(user_layer: Path) -> None:
    assert claimed_principal() is None
    assert resolve_operator(actor=None, receipt_ref=None) is None
    assert CLAIM_COMMAND in NO_PRINCIPAL_REASON


def test_a_named_actor_overrides_the_claim(user_layer: Path) -> None:
    assert _claim("OP-0001") == 0

    assert resolve_operator(actor="OP-0003", receipt_ref=None) == Operator(principal="OP-0003")


def test_a_receipt_with_no_actor_and_no_claim_is_refused(user_layer: Path) -> None:
    with pytest.raises(ValueError, match=r"claim one with eawf config set operator\.principal"):
        resolve_operator(actor=None, receipt_ref="eawf://P/P/P/evidence/EVD-0001")


@pytest.mark.parametrize("value", ["someone@example.test", "op-0001", "O", "A" * 33])
def test_a_claim_that_is_not_a_principal_key_is_refused_when_written(
    user_layer: Path, value: str
) -> None:
    assert _claim(value) != 0
    assert claimed_principal() is None


@pytest.mark.parametrize("scope", ["repo", "local"])
def test_a_claim_written_outside_the_user_layer_claims_no_one(user_layer: Path, scope: str) -> None:
    """A committed or repository layer would make everyone who clones it one principal."""
    _claim("OP-0001", scope=scope)

    assert claimed_principal() is None


def test_a_principal_named_in_the_repository_config_is_never_claimed(user_layer: Path) -> None:
    (Path.cwd() / ".ea" / "config.yaml").write_text(
        "operator:\n  principal: OP-0001\n", encoding="utf-8"
    )

    assert claimed_principal() is None


@pytest.mark.parametrize(
    "body",
    ["operator:\n  principal: not a key\n", "operator:\n  principal: OP-0001\n  team: X\n"],
    ids=["malformed", "unknown-field"],
)
def test_a_hand_edited_claim_that_does_not_validate_fails_before_the_console_opens(
    user_layer: Path, body: str
) -> None:
    user_layer.parent.mkdir(parents=True)
    user_layer.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=r"operator\.principal in the user config"):
        resolve_operator(actor=None, receipt_ref=None)


def test_an_empty_operator_block_claims_no_one(user_layer: Path) -> None:
    user_layer.parent.mkdir(parents=True)
    user_layer.write_text("operator: {}\n", encoding="utf-8")

    assert claimed_principal() is None
