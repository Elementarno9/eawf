"""A Batch title at the wire: ``batch set-title`` sends the title beside both anchors.

Whether the title fits is the daemon's closed model to decide, so the command forwards
the operator's text as typed and refuses only a request missing a required option.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from click.testing import Result
from typer.testing import CliRunner

from eawf.runtime.daemon.epoch2_transaction import MutationReceipt
from eawf.runtime.daemon.methods.batch_title import BATCH_SET_TITLE, TITLE_SET_EVENT
from eawf.runtime.daemon.methods.domain_envelope import accepted_envelope
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain_target
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/batch/BAT-0001"
_ANCHORS = ("--expected-revision", "4", "--idempotency-key", "key-0001", "--actor", "OPERATOR")


def _ok() -> dict[str, object]:
    receipt = MutationReceipt.model_validate(
        {
            "event_name": TITLE_SET_EVENT,
            "entity_ref": _URN,
            "revision_before": 4,
            "revision_after": 5,
            "canonical_sequence": 7,
            "event_id": "evt-0007",
            "idempotency_key": "key-0001",
            "occurred_at": datetime(2026, 10, 9, tzinfo=UTC),
            "wal_record_id": "wal-0007",
        }
    )
    return accepted_envelope(receipt, operation=BATCH_SET_TITLE).model_dump(mode="json")


def _set_title(tmp_path: Path, *args: str) -> Result:
    return runner.invoke(
        app, ["--workspace", str(tmp_path), "batch", "set-title", _URN, *args, *_ANCHORS]
    )


def test_set_title_sends_the_title_and_both_anchors(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ok()

    result = _set_title(tmp_path, "--title", "Ship the wheel build", "--yes")

    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == BATCH_SET_TITLE == domain_target.BATCH_SET_TITLE
    assert params["title"] == "Ship the wheel build"
    assert (params["urn"], params["expected_revision"]) == (_URN, 4)
    assert (params["idempotency_key"], params["actor"]) == ("key-0001", "OPERATOR")


def test_set_title_without_a_title_sends_nothing(daemon: FakeDaemon, tmp_path: Path) -> None:
    result = _set_title(tmp_path, "--yes")

    assert result.exit_code != exit_codes.OK
    assert daemon.calls == []


@pytest.mark.parametrize("revision", ["0", "-1"])
def test_set_title_refuses_a_non_positive_revision_before_the_wire(
    daemon: FakeDaemon, tmp_path: Path, revision: str
) -> None:
    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "batch", "set-title", _URN, "--title", "T",
            "--expected-revision", revision, "--idempotency-key", "key-0001",
            "--actor", "OPERATOR", "--yes",
        ],
    )  # fmt: skip

    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert daemon.calls == []


def test_set_title_dry_run_states_the_consequence_and_sends_nothing(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    result = _set_title(tmp_path, "--title", "Ship the wheel build", "--dry-run")

    assert result.exit_code == exit_codes.OK, result.output
    assert "its status does not move" in result.output
    assert daemon.calls == []
