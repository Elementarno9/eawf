"""The claim verbs send one daemon RPC each, anchored and keyed, and render its answer."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import click
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.claim import CLAIM_ATTEST, CLAIM_CHECK
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_CLAIM: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
_EVIDENCE: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0002"
_ANSWER: Final = {
    "claim_ref": _CLAIM,
    "status": "OPEN",
    "outcomes": ["passed", "failed", "not_run", "not_run"],
    "promotion_blockers": ["rung 4 (entail) has not passed: not_run"],
}
_ANCHORS: Final = (
    "--expected-revision", "1", "--idempotency-key", "check-1", "--actor", "OPERATOR",
)  # fmt: skip


def test_claim_check_sends_the_rung_fragment_and_both_anchors(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    daemon.result = _ANSWER
    argv = ["--workspace", str(tmp_path), "claim", "check", f"{_CLAIM}#rung-2", *_ANCHORS]
    result = runner.invoke(app, argv)
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == CLAIM_CHECK
    assert (params["urn"], params["expected_revision"]) == (f"{_CLAIM}#rung-2", 1)
    assert params["idempotency_key"] == "check-1"
    assert "not_run" in result.output


def test_claim_check_without_the_anchor_sends_nothing(daemon: FakeDaemon, tmp_path: Path) -> None:
    """Error path: the revision anchor is required at argument parsing."""
    argv = ["--workspace", str(tmp_path), "claim", "check", _CLAIM, *_ANCHORS[2:]]
    result = runner.invoke(app, argv)
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []


def test_claim_attest_forwards_the_outside_decision(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ANSWER
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "claim", "attest", f"{_CLAIM}#rung-4",
            "--evidence", _EVIDENCE, "--outcome", "passed",
            "--finding", "the reviewer reproduced the replay", *_ANCHORS,
        ],
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == CLAIM_ATTEST
    assert (params["evidence_ref"], params["outcome"]) == (_EVIDENCE, "passed")


def test_a_refused_attestation_prints_the_daemon_code(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.error = DaemonRpcError(
        -32002, "validation_failed: rung_not_runnable: rung 2 of CLM-0004 has not passed"
    )
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "claim", "attest", f"{_CLAIM}#rung-4",
            "--evidence", _EVIDENCE, "--outcome", "passed", "--finding", "signed", *_ANCHORS,
        ],
    )  # fmt: skip
    assert result.exit_code != exit_codes.OK
    assert "rung_not_runnable" in result.output
