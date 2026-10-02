"""The Milestone target date at the wire: ``milestone set-target`` and ``create --date``.

The day is parsed once on the command line, so a malformed one never reaches the
daemon, and it travels as the ``YYYY-MM-DD`` the daemon's closed model reads.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon.epoch2_transaction import MutationReceipt
from eawf.runtime.daemon.methods.domain_envelope import accepted_envelope
from eawf.runtime.daemon.methods.milestone_target import MILESTONE_SET_TARGET
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import domain_target
from tests.contract.surfaces.cli.conftest import FakeDaemon

runner = CliRunner()

_URN = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY/milestone/MLS-0001"
_DOCUMENT = (
    Path(__file__).resolve().parents[3] / "fixtures" / "epoch2" / "create" / "milestone.json"
)
_ANCHORS = ("--expected-revision", "4", "--idempotency-key", "key-0001", "--actor", "OPERATOR")


def _ok(method: str, *, before: int | None, after: int) -> dict[str, object]:
    receipt = MutationReceipt(
        event_name=method,
        entity_ref=_URN,
        revision_before=before,
        revision_after=after,
        canonical_sequence=7,
        event_id="evt-0007",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 10, 2, tzinfo=UTC),
        wal_record_id="wal-0007",
    )
    return accepted_envelope(receipt, operation=method).model_dump(mode="json")


def _set_target(tmp_path: Path, *args: str) -> object:
    return runner.invoke(
        app, ["--workspace", str(tmp_path), "milestone", "set-target", _URN, *args, *_ANCHORS]
    )


def test_set_target_sends_the_day_and_both_anchors(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ok(MILESTONE_SET_TARGET, before=4, after=5)

    result = _set_target(tmp_path, "2026-10-09", "--yes")

    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = daemon.calls
    assert method == MILESTONE_SET_TARGET == domain_target.MILESTONE_SET_TARGET
    assert params["target_date"] == "2026-10-09"
    assert (params["urn"], params["expected_revision"]) == (_URN, 4)
    assert params["idempotency_key"] == "key-0001"


def test_set_target_clear_sends_an_explicit_null(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ok(MILESTONE_SET_TARGET, before=4, after=5)

    result = _set_target(tmp_path, "--clear", "--yes")

    assert result.exit_code == exit_codes.OK, result.output
    [(_, params)] = daemon.calls
    assert "target_date" in params and params["target_date"] is None


@pytest.mark.parametrize(
    "args",
    [
        ("2026-02-29",),
        ("2026-10-9",),
        ("20261009",),
        ("2026-W41-5",),
        (),
        ("2026-10-09", "--clear"),
    ],
)
def test_set_target_refuses_a_malformed_request_before_the_wire(
    daemon: FakeDaemon, tmp_path: Path, args: tuple[str, ...]
) -> None:
    result = _set_target(tmp_path, *args, "--yes")

    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert daemon.calls == []


def test_set_target_dry_run_states_the_consequence_and_sends_nothing(
    daemon: FakeDaemon, tmp_path: Path
) -> None:
    result = _set_target(tmp_path, "2026-10-09", "--dry-run")

    assert result.exit_code == exit_codes.OK, result.output
    assert "its status does not move" in result.output
    assert daemon.calls == []


def test_create_date_joins_the_create_document(daemon: FakeDaemon, tmp_path: Path) -> None:
    daemon.result = _ok(domain_cmd.MILESTONE_CREATE, before=None, after=1)

    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "milestone", "create", _URN,
            "--expected-tree-revision", "0", "--idempotency-key", "key-0001",
            "--actor", "OPERATOR", "--from-spec", str(_DOCUMENT), "--date", "2026-10-02",
            "--yes",
        ],
    )  # fmt: skip

    assert result.exit_code == exit_codes.OK, result.output
    [(_, params)] = daemon.calls
    assert params["spec"]["target_date"] == "2026-10-02"


@pytest.mark.parametrize(("stated", "flag"), [("2026-10-05", "2026-10-02"), (None, "2026-02-30")])
def test_create_date_refuses_a_disagreeing_or_malformed_day(
    daemon: FakeDaemon, tmp_path: Path, stated: str | None, flag: str
) -> None:
    document = orjson.loads(_DOCUMENT.read_bytes())
    if stated is not None:
        document["target_date"] = stated
    spec = tmp_path / "milestone.json"
    spec.write_bytes(orjson.dumps(document))

    result = runner.invoke(
        app,
        [
            "--workspace", str(tmp_path), "milestone", "create", _URN,
            "--expected-tree-revision", "0", "--idempotency-key", "key-0001",
            "--actor", "OPERATOR", "--from-spec", str(spec), "--date", flag, "--yes",
        ],
    )  # fmt: skip

    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert daemon.calls == []
