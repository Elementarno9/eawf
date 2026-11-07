"""The operator principal this machine's user claims, read from the user layer only.

Every layer is a file under ``tmp_path``: the user layer by patching its path, the
repository layer by the working directory. Nothing reads the real home directory.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.kernel.config import layered
from eawf.kernel.config.operator_claim import claimed_operator
from eawf.surfaces.tui.console.operations import CLAIM_COMMAND
from eawf.surfaces.tui.launch import claimed_principal


@pytest.fixture
def user_layer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the user layer at a file under this test and work in a bare repository."""
    path = tmp_path / "home" / "config.yaml"
    path.parent.mkdir(parents=True)
    monkeypatch.setattr(layered, "global_config_path", lambda: path)
    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    monkeypatch.chdir(repo)
    return path


def test_a_user_layer_claim_is_the_claimed_operator(user_layer: Path) -> None:
    user_layer.write_text("operator:\n  principal: OP-0001\n", encoding="utf-8")

    assert claimed_operator() == "OP-0001"
    assert claimed_principal() == claimed_operator()


def test_no_user_layer_claims_no_one(user_layer: Path) -> None:
    assert not user_layer.exists()

    assert claimed_operator() is None
    assert claimed_principal() is None


@pytest.mark.parametrize(
    "body",
    ["", "ui:\n  toasts: all\n", "operator:\n", "operator: {}\n", "operator:\n  principal: null\n"],
    ids=["empty", "no-operator-block", "null-block", "empty-block", "null-principal"],
)
def test_a_user_layer_without_a_principal_claims_no_one(user_layer: Path, body: str) -> None:
    user_layer.write_text(body, encoding="utf-8")

    assert claimed_operator() is None


def test_a_repository_layer_naming_a_principal_is_ignored(user_layer: Path) -> None:
    """A committed claim would make everyone who clones the repository one principal."""
    (Path.cwd() / ".ea" / "config.yaml").write_text(
        "operator:\n  principal: OP-0001\n", encoding="utf-8"
    )

    assert claimed_operator() is None


def test_the_user_claim_wins_over_a_repository_one(user_layer: Path) -> None:
    user_layer.write_text("operator:\n  principal: OP-0002\n", encoding="utf-8")
    (Path.cwd() / ".ea" / "config.yaml").write_text(
        "operator:\n  principal: OP-0001\n", encoding="utf-8"
    )

    assert claimed_operator() == "OP-0002"


@pytest.mark.parametrize("value", ["O", "A" * 32, "AB", "A" * 33, "op-0001", "OP 0001"])
def test_the_principal_key_grammar_holds_at_both_bounds(user_layer: Path, value: str) -> None:
    """Two to thirty-two upper-case characters is a key; one fewer or more is not."""
    user_layer.write_text(f"operator:\n  principal: '{value}'\n", encoding="utf-8")
    admitted = value in {"A" * 32, "AB"}

    if admitted:
        assert claimed_operator() == value
    else:
        with pytest.raises(ValueError, match=r"operator\.principal in the user config"):
            claimed_operator()


@pytest.mark.parametrize(
    "body",
    [
        "operator:\n  principal: not a key\n",
        "operator:\n  principal: 1\n",
        "operator:\n  principal: OP-0001\n  team: X\n",
        "operator: OP-0001\n",
        "operator: [OP-0001]\n",
        "operator: {principal: [\n",
        "- operator\n",
    ],
    ids=["not-a-key", "integer", "unknown-field", "scalar-block", "list-block", "yaml", "list"],
)
def test_a_malformed_user_layer_is_refused(user_layer: Path, body: str) -> None:
    user_layer.write_text(body, encoding="utf-8")

    with pytest.raises(ValueError, match=r"operator\.principal in the user config"):
        claimed_operator()


def test_the_console_refusal_names_the_claim_command(user_layer: Path) -> None:
    user_layer.write_text("operator:\n  principal: not a key\n", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        claimed_principal()
    assert str(caught.value).startswith("operator.principal in the user config")
    assert str(caught.value).endswith(f"; set it again with {CLAIM_COMMAND}")
    with pytest.raises(ValueError) as library:
        claimed_operator()
    assert CLAIM_COMMAND not in str(library.value)
