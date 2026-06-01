"""CLI tests for the read-only ``eawf wal`` group (status / list / show).

The verbs read the local daemon WAL directory directly; the daemon need
not be running. Tests redirect ``runtime_dir()`` to ``tmp_path`` via a
``monkeypatch`` on the :mod:`eawf.runtime.daemon.runtime_dir` resolver
and on the lazily-imported reference in the ``wal`` command module so
every platform path resolves identically.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.runtime.daemon.wal import (
    WalRecord,
    mark_applied,
    mark_fsynced,
    mark_poisoned,
    write_pending,
)
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def _envelope(env_id: str = "env-test-001") -> Envelope:
    return Envelope(
        id=env_id,
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="test envelope",
        payload={"action": "noop"},
    )


def _record(record_id: str = "rec-001", *, when: datetime | None = None) -> WalRecord:
    written = when if when is not None else datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC)
    return WalRecord(
        record_id=record_id,
        envelope=_envelope(f"env-{record_id}"),
        idempotency_key=None,
        written_at=written,
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def _redirect_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``runtime_dir()`` at ``tmp_path / "eawfd"``; return that dir."""
    target = tmp_path / "eawfd"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("eawf.runtime.daemon.runtime_dir.runtime_dir", lambda: target)
    return target


# --- status: empty + mixed ---------------------------------------------------


def test_wal_status_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An absent / empty WAL reports honest zeros, exit 0."""
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["wal", "status"])
    assert res.exit_code == 0, res.output
    assert "total=0" in res.output
    assert "pending=0" in res.output
    assert "poisoned=0" in res.output
    assert "oldest=None" in res.output


def test_wal_status_counts_mixed_statuses(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Per-status counts reflect a WAL with pending/applied/fsynced/poisoned."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    # one pending
    write_pending(wal_dir, _record("rec-pending"))
    # one applied
    write_pending(wal_dir, _record("rec-applied"))
    mark_applied(wal_dir, "rec-applied")
    # one fsynced
    write_pending(wal_dir, _record("rec-fsynced"))
    mark_applied(wal_dir, "rec-fsynced")
    mark_fsynced(wal_dir, "rec-fsynced")
    # one poisoned
    write_pending(wal_dir, _record("rec-poisoned"))
    mark_poisoned(wal_dir, "rec-poisoned", reason="manual_review")

    res = runner.invoke(app, ["--json", "wal", "status"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["total"] == 4
    assert payload["counts"] == {
        "pending": 1,
        "applied": 1,
        "fsynced": 1,
        "poisoned": 1,
    }
    assert payload["total_bytes"] > 0
    assert payload["oldest"] is not None
    assert payload["newest"] is not None


def test_wal_status_newest_oldest_span(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Oldest/newest bracket the records' written_at timestamps."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    old = datetime(2026, 5, 19, 8, 0, 0, tzinfo=UTC)
    new = datetime(2026, 5, 19, 20, 0, 0, tzinfo=UTC)
    write_pending(wal_dir, _record("rec-old", when=old))
    write_pending(wal_dir, _record("rec-new", when=new))

    res = runner.invoke(app, ["--json", "wal", "status"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["oldest"] == old.isoformat()
    assert payload["newest"] == new.isoformat()


# --- list: empty + mixed + filter + json -------------------------------------


def test_wal_list_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Empty WAL prints an honest 'no WAL records' line, exit 0."""
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["wal", "list"])
    assert res.exit_code == 0, res.output
    assert "no WAL records" in res.output


def test_wal_list_lists_records(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """List surfaces every live + poisoned record id with its status."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-a"))
    write_pending(wal_dir, _record("rec-b"))
    mark_applied(wal_dir, "rec-b")
    write_pending(wal_dir, _record("rec-c"))
    mark_poisoned(wal_dir, "rec-c", reason="pre_apply_crash")

    res = runner.invoke(app, ["wal", "list"])
    assert res.exit_code == 0, res.output
    assert "rec-a" in res.output
    assert "rec-b" in res.output
    assert "rec-c" in res.output
    assert "status=pending" in res.output
    assert "status=applied" in res.output
    assert "status=poisoned" in res.output


def test_wal_list_status_filter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--status applied`` narrows the listing to applied records only."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-pending"))
    write_pending(wal_dir, _record("rec-applied"))
    mark_applied(wal_dir, "rec-applied")

    res = runner.invoke(app, ["--json", "wal", "list", "--status", "applied"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["status"] == "applied"
    assert payload["count"] == 1
    assert payload["records"][0]["record_id"] == "rec-applied"
    assert payload["records"][0]["status"] == "applied"


def test_wal_list_status_filter_poisoned(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``--status poisoned`` walks only the poisoned subdirectory."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-live"))
    write_pending(wal_dir, _record("rec-bad"))
    mark_poisoned(wal_dir, "rec-bad", reason="manual_review")

    res = runner.invoke(app, ["--json", "wal", "list", "--status", "poisoned"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["count"] == 1
    assert payload["records"][0]["record_id"] == "rec-bad"
    assert payload["records"][0]["status"] == "poisoned"


def test_wal_list_json_shape(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The JSON row carries id, status, envelope kind + summary, timestamp."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-shape"))

    res = runner.invoke(app, ["--json", "wal", "list"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["count"] == 1
    row = payload["records"][0]
    assert set(row) == {
        "record_id",
        "status",
        "envelope_id",
        "envelope_kind",
        "envelope_summary",
        "written_at",
        "path",
    }
    assert row["record_id"] == "rec-shape"
    assert row["envelope_kind"] == "event"
    assert row["envelope_summary"] == "test envelope"


def test_wal_list_rejects_unknown_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown ``--status`` token exits 2 with the known-set hint."""
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["wal", "list", "--status", "bogus"])
    assert res.exit_code == 2
    assert "unknown --status: 'bogus'" in res.output


# --- show --------------------------------------------------------------------


def test_wal_show_decodes_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``show <id>`` dumps the decoded envelope for a live record."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-show"))

    res = runner.invoke(app, ["--json", "wal", "show", "rec-show"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["record_id"] == "rec-show"
    assert payload["status"] == "pending"
    assert payload["record"]["envelope"]["id"] == "env-rec-show"
    assert payload["record"]["before_state_version"] == "sha:before"


def test_wal_show_finds_poisoned_record(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """``show`` falls through to the poisoned subdirectory + surfaces reason."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-poison"))
    mark_poisoned(wal_dir, "rec-poison", reason="pre_apply_crash")

    res = runner.invoke(app, ["wal", "show", "rec-poison"])
    assert res.exit_code == 0, res.output
    assert "status=poisoned" in res.output
    assert "pre_apply_crash" in res.output


def test_wal_show_missing_record_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """An unknown record id exits 1 with a not-found message."""
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["wal", "show", "rec-nope"])
    assert res.exit_code == 1
    assert "wal record not found: 'rec-nope'" in res.output


def test_wal_list_unreadable_record_is_listed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt record body lists with an 'unreadable' marker, never crashes."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    (wal_dir / "rec-corrupt.pending.json").write_bytes(b"{not json")

    res = runner.invoke(app, ["--json", "wal", "list"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["count"] == 1
    row = payload["records"][0]
    assert row["record_id"] == "rec-corrupt"
    assert row["envelope_summary"].startswith("unreadable:")
