"""The claim verbs send one daemon RPC each, anchored and keyed, and render its answer."""

from __future__ import annotations

from pathlib import Path
from typing import Final

import click
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands.claim import CLAIM_ATTEST, CLAIM_CHECK, CLAIM_FILE
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


_SUBJECT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042"
_FILE_ANCHORS: Final = (
    "--expected-revision", "12", "--idempotency-key", "file-1", "--actor", "OPERATOR",
)  # fmt: skip


def _claim_file(*extra: str) -> list[str]:
    return ["claim", "file", _SUBJECT, "--title", "Replay keeps order", *extra, *_FILE_ANCHORS]


def test_claim_file_sends_keys_and_parsed_spans(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ANSWER
    argv = _claim_file(
        "--evidence", "EVD-0002", "--evidence", "RCP-0001",
        "--anchor", "docs/notes.md:3-4", "--anchor", "EVD-0002:src/a.py:7-7", "--yes",
    )  # fmt: skip
    result = runner.invoke(app, ["--workspace", str(tmp_path), *argv])
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == CLAIM_FILE
    assert (params["urn"], params["expected_revision"]) == (_SUBJECT, 12)
    assert params["evidence"] == ["EVD-0002", "RCP-0001"]
    assert params["anchors"] == [
        {"path": "docs/notes.md", "start_line": 3, "end_line": 4},
        {"evidence": "EVD-0002", "path": "src/a.py", "start_line": 7, "end_line": 7},
    ]
    assert "CLM-0004" in result.output


def test_claim_file_dry_run_sends_nothing(daemon: FakeDaemon, tmp_path: Path) -> None:
    result = runner.invoke(app, ["--workspace", str(tmp_path), *_claim_file("--dry-run")])
    assert result.exit_code == exit_codes.OK, result.output
    assert "nothing was sent" in result.output
    assert daemon.calls == []


@pytest.mark.parametrize(
    "anchor", ["docs/notes.md", "docs/notes.md:3", "docs/notes.md:a-b", ":1-2"]
)
def test_claim_file_refuses_a_malformed_span(
    daemon: FakeDaemon, tmp_path: Path, anchor: str
) -> None:
    """Error path: a span is ``[EVD-####:]path:start-end`` or nothing is sent."""
    argv = _claim_file("--anchor", anchor, "--yes")
    result = runner.invoke(app, ["--workspace", str(tmp_path), *argv])
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []


def test_claim_file_without_the_tree_anchor_sends_nothing(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    argv = ["claim", "file", _SUBJECT, "--title", "t", *_FILE_ANCHORS[2:]]
    result = runner.invoke(app, ["--workspace", str(tmp_path), *argv])
    assert result.exit_code == click.UsageError("x").exit_code
    assert daemon.calls == []
