"""``eawf question open-decision`` forwards the decision document to its daemon verb.

SURF-091: the verb the skills name is reachable from the root ``question`` group; it
sends the document unchanged with the actor beside it, prints the daemon's answer as
its envelope, and turns a daemon refusal into a refusal envelope with a failing exit.
The daemon is replaced by a recorder, so nothing here reaches a socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain
from eawf.surfaces.cli.commands.question_decision import QUESTION_OPEN_DECISION
from tests._epoch2_helpers import lay_epoch2_tree


@pytest.fixture(autouse=True)
def _epoch2_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run every CLI verb here from an epoch-2 tree, as the flag day requires."""
    monkeypatch.setenv("EA_STATE", str(lay_epoch2_tree(tmp_path / "epoch2")))


SUBJECT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
ACTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
SPEC: Final[dict[str, Any]] = {
    "urn": SUBJECT,
    "idempotency_key": "req-commit-policy-1",
    "requested_by": {"principal_kind": "human", "principal_id": "OP-0001"},
    "question": "Should finished steps be committed without asking?",
    "options": [],
}


@pytest.fixture
def spec_file(tmp_path: Path) -> Path:
    path = tmp_path / "decision.json"
    path.write_text(json.dumps(SPEC), encoding="utf-8")
    return path


def _invoke(spec_file: Path) -> Any:
    return CliRunner().invoke(
        app,
        [
            "--json",
            "question",
            "open-decision",
            "--actor",
            "AG-0001",
            "--from-spec",
            str(spec_file),
        ],
    )


def test_surf_091_the_document_reaches_the_decision_verb_with_its_actor(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The verb sends the document unchanged, and prints the answer the daemon gave."""
    sent: list[tuple[str, dict[str, Any]]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        sent.append((method, params))
        return {
            "action_ref": ACTION,
            "status": "WAITING",
            "revision": 1,
            "created": True,
            "reason": "ACT-0001 asks the operator to decide",
        }

    monkeypatch.setattr(domain, "_native_answer", answer)
    result = _invoke(spec_file)

    assert result.exit_code == exit_codes.OK, result.output
    assert sent == [(QUESTION_OPEN_DECISION, {**SPEC, "actor": "AG-0001"})]
    assert ACTION in result.stdout


def test_surf_091_a_daemon_refusal_prints_a_refusal_and_fails(
    spec_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: an unpresentable question is refused by the daemon, and the verb says so."""

    def refuse(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        raise DaemonRpcError(
            cli_errors.RPC_VALIDATION_FAILED,
            "validation_failed: recommendation_unstated: ACT-0001 recommends no option",
        )

    monkeypatch.setattr(domain, "_native_answer", refuse)
    result = _invoke(spec_file)

    assert result.exit_code != exit_codes.OK
    assert "recommendation_unstated" in result.stdout


def test_surf_091_a_missing_spec_file_is_refused_before_any_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: a document that cannot be read sends nothing."""
    sent: list[str] = []
    monkeypatch.setattr(domain, "_native_answer", lambda method, *_a, **_k: sent.append(method))

    result = _invoke(tmp_path / "absent.json")

    assert result.exit_code != exit_codes.OK
    assert sent == []
