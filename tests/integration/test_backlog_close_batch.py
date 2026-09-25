"""Loop semantics of an operator batch close over ``eawf backlog close``.

There is no batch verb: a closeout runs one ``backlog close`` per row. These
tests pin what that loop relies on against a temporary state -- each row
closes independently, one bad row does not block the rest, a re-run refuses
every already-closed row without touching it, and the historical two-digit
ids still close even though ``backlog add`` no longer mints them.
"""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import BacklogPriority, BacklogStatus
from eawf.kernel.state.models import BacklogItem
from eawf.surfaces.cli._mutation import state_transaction
from eawf.surfaces.cli.app import app

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
AUDIT_ID = "A-CLOSEOUT"
runner = CliRunner()


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / "state.json"
    shutil.copy(FIXTURE, target)
    monkeypatch.setenv("EA_STATE", str(target))
    return target


def _invoke(*argv: str) -> int:
    return runner.invoke(app, list(argv)).exit_code


def _seed_complete_audit() -> None:
    artifact_id = f"ART-{AUDIT_ID}"
    uri = f"repo:.ea/artifacts/{artifact_id}.md"
    assert _invoke("artifact", "add", artifact_id, "--kind", "audit_report", "--uri", uri) == 0
    assert (
        _invoke(
            "audit", "add", AUDIT_ID, "--scope-id", "QR", "--kind", "evaluation",
            "--report", artifact_id, "--verdict", "pass",
        )
        == 0
    )  # fmt: skip


def _seed_rows(*item_ids: str) -> None:
    for item_id in item_ids:
        assert _invoke("backlog", "add", item_id, "--title", f"Row {item_id}") == 0


def _close(item_id: str, *, commit: str = "abc1234", audit: str = AUDIT_ID) -> int:
    return _invoke(
        "backlog", "close", item_id, "--resolution", f"fixed {item_id}",
        "--commit", commit, "--audit", audit,
    )  # fmt: skip


def _backlog(state_path: Path) -> dict[str, dict[str, object]]:
    return json.loads(state_path.read_text())["backlog"]


def _close_events(state_path: Path) -> list[str]:
    events = state_path.parent / "store" / "event.jsonl"
    return [
        json.loads(line)["summary"]
        for line in events.read_text().splitlines()
        if '"backlog.close"' in line
    ]


def test_backlog_close_batch_closes_every_listed_row(state_path: Path) -> None:
    _seed_rows("B001", "B002", "B003", "B004")
    _seed_complete_audit()

    codes = [_close(item_id) for item_id in ("B001", "B002", "B003")]

    assert codes == [0, 0, 0]
    rows = _backlog(state_path)
    for item_id in ("B001", "B002", "B003"):
        assert rows[item_id]["status"] == "closed"
        assert rows[item_id]["resolution"] == f"fixed {item_id}"
        assert rows[item_id]["commit"] == "abc1234"
    assert rows["B004"]["status"] == "open"
    assert _close_events(state_path) == [f"backlog {i} closed" for i in ("B001", "B002", "B003")]


def test_backlog_close_batch_single_row(state_path: Path) -> None:
    _seed_rows("B001")
    _seed_complete_audit()

    assert _close("B001") == 0
    assert _backlog(state_path)["B001"]["status"] == "closed"


def test_backlog_close_batch_rerun_refuses_closed_rows_unchanged(state_path: Path) -> None:
    _seed_rows("B001", "B002")
    _seed_complete_audit()
    assert [_close("B001"), _close("B002")] == [0, 0]
    before = _backlog(state_path)

    rerun = [_close(item_id, commit="fedcba9") for item_id in ("B001", "B002")]

    assert all(code != 0 for code in rerun)
    assert _backlog(state_path) == before
    assert len(_close_events(state_path)) == 2


def test_backlog_close_batch_unknown_row_does_not_block_the_rest(state_path: Path) -> None:
    _seed_rows("B001", "B003")
    _seed_complete_audit()

    codes = [_close(item_id) for item_id in ("B001", "B002", "B003")]

    assert codes[0] == 0
    assert codes[1] != 0
    assert codes[2] == 0
    rows = _backlog(state_path)
    assert "B002" not in rows
    assert rows["B001"]["status"] == rows["B003"]["status"] == "closed"


def test_backlog_close_batch_unknown_audit_leaves_rows_open(state_path: Path) -> None:
    _seed_rows("B001", "B002")
    _seed_complete_audit()

    codes = [_close(item_id, audit="A-MISSING") for item_id in ("B001", "B002")]

    assert codes == [2, 2]
    assert {row["status"] for row in _backlog(state_path).values()} == {"open"}
    assert _close_events(state_path) == []


def test_backlog_close_batch_closes_historical_two_digit_ids(state_path: Path) -> None:
    # ``backlog add`` refuses a two-digit id, so the legacy rows are seeded
    # directly; the closeout still has to reach them.
    with state_transaction(state_path) as state:
        backlog = dict(state.backlog or {})
        for item_id in ("B70", "B71"):
            backlog[item_id] = BacklogItem(
                id=item_id,
                scope_id="QR",
                title=f"Legacy row {item_id}",
                priority=BacklogPriority.P2,
                status=BacklogStatus.OPEN,
                created_at=datetime.now(UTC),
            )
        state.backlog = backlog
    _seed_complete_audit()

    assert [_close("B70"), _close("B71")] == [0, 0]
    rows = _backlog(state_path)
    assert rows["B70"]["status"] == rows["B71"]["status"] == "closed"
    assert rows["B70"]["resolution"] == "fixed B70"
