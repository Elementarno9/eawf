"""CLI tests for ``eawf daemon replay-wal``.

The verb reads the local WAL directory directly; the daemon need not
be running. Tests redirect ``runtime_dir`` to ``tmp_path`` via the
``XDG_RUNTIME_DIR`` env var (Linux) and a ``monkeypatch`` on the
:mod:`eawf.daemon.runtime_dir` resolver (other platforms).
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import orjson
import pytest
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.daemon import wal
from eawf.daemon.wal import (
    WalRecord,
    mark_applied,
    mark_fsynced,
    mark_poisoned,
    write_pending,
)
from eawf.state.enums import StoreKind
from eawf.store.envelope import Envelope

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


def _record(record_id: str = "rec-001") -> WalRecord:
    return WalRecord(
        record_id=record_id,
        envelope=_envelope(),
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )


def _redirect_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point ``runtime_dir()`` at ``tmp_path / "eawfd"``; return that dir."""
    target = tmp_path / "eawfd"
    target.mkdir(parents=True, exist_ok=True)
    # Override the resolver so every platform path resolves identically.
    monkeypatch.setattr("eawf.daemon.runtime_dir.runtime_dir", lambda: target)
    monkeypatch.setattr("eawf.cli.commands.daemon.runtime_dir", lambda: target)
    return target


def test_replay_wal_requires_exactly_one_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["daemon", "replay-wal"])
    assert res.exit_code == 2
    assert "exactly one of --inspect / --gc" in res.output

    res = runner.invoke(app, ["daemon", "replay-wal", "--inspect", "--gc"])
    assert res.exit_code == 2


def test_replay_wal_inspect_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["daemon", "replay-wal", "--inspect"])
    assert res.exit_code == 0, res.output
    assert "no poisoned WAL records" in res.output


def test_replay_wal_inspect_lists_poisoned_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-poison-a"))
    write_pending(wal_dir, _record("rec-poison-b"))
    mark_poisoned(wal_dir, "rec-poison-a", reason="daemon_crashed_pre_apply")
    mark_poisoned(wal_dir, "rec-poison-b", reason="manual_review")

    res = runner.invoke(app, ["daemon", "replay-wal", "--inspect"])
    assert res.exit_code == 0, res.output
    assert "rec-poison-a" in res.output
    assert "rec-poison-b" in res.output
    assert "daemon_crashed_pre_apply" in res.output
    assert "manual_review" in res.output


def test_replay_wal_inspect_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    write_pending(wal_dir, _record("rec-poison-c"))
    mark_poisoned(wal_dir, "rec-poison-c", reason="pre_apply_crash")

    res = runner.invoke(app, ["--json", "daemon", "replay-wal", "--inspect"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["count"] == 1
    assert payload["records"][0]["record_id"] == "rec-poison-c"
    assert payload["records"][0]["poison_reason"] == "pre_apply_crash"


def test_replay_wal_gc_empty_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["daemon", "replay-wal", "--gc"])
    assert res.exit_code == 0, res.output
    assert "gc removed 0 WAL record(s)" in res.output


def test_replay_wal_gc_removes_aged_fsynced_records(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    aged = _record("rec-aged")
    recent = _record("rec-recent")
    for record in (aged, recent):
        write_pending(wal_dir, record)
        mark_applied(wal_dir, record.record_id)
        mark_fsynced(wal_dir, record.record_id)
    past = (datetime.now(UTC) - timedelta(hours=2)).timestamp()
    os.utime(wal_dir / "rec-aged.fsynced.json", (past, past))

    res = runner.invoke(app, ["daemon", "replay-wal", "--gc"])
    assert res.exit_code == 0, res.output
    assert "gc removed 1 WAL record(s)" in res.output
    assert not (wal_dir / "rec-aged.fsynced.json").exists()
    assert (wal_dir / "rec-recent.fsynced.json").exists()


def test_replay_wal_gc_respects_max_age_seconds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    record = _record("rec-recent")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    mark_fsynced(wal_dir, record.record_id)
    # File is fresh — default max_age (3600) keeps it; max_age=0 removes it.

    res = runner.invoke(app, ["daemon", "replay-wal", "--gc", "--max-age-seconds", "0"])
    assert res.exit_code == 0, res.output
    assert "gc removed 1 WAL record(s)" in res.output

    res = runner.invoke(app, ["daemon", "replay-wal", "--gc"])
    assert res.exit_code == 0, res.output
    assert "gc removed 0 WAL record(s)" in res.output


def test_replay_wal_gc_rejects_negative_max_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["daemon", "replay-wal", "--gc", "--max-age-seconds", "-1"])
    assert res.exit_code == 2
    assert "--max-age-seconds must be between" in res.output


def test_replay_wal_gc_rejects_huge_max_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _redirect_runtime(monkeypatch, tmp_path)
    res = runner.invoke(app, ["daemon", "replay-wal", "--gc", "--max-age-seconds", "9999999999"])
    assert res.exit_code == 2
    assert "--max-age-seconds must be between" in res.output


def test_replay_wal_gc_json_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    wal_dir = runtime / "wal"
    record = _record("rec-recent")
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record.record_id)
    mark_fsynced(wal_dir, record.record_id)

    res = runner.invoke(app, ["--json", "daemon", "replay-wal", "--gc", "--max-age-seconds", "0"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["removed_count"] == 1
    assert payload["max_age_seconds"] == 0


def test_wal_admin_methods_register_when_imported() -> None:
    """Importing ``wal_admin`` populates the JSON-RPC registry.

    W09 wires the import into the main dispatcher; until then the
    admin methods register only when a caller explicitly imports the
    module. Test imports the module then asserts every ``wal.*`` name
    is present in the registry (test execution order may already have
    imported it via another test, so we assert subset-presence rather
    than fresh-registration).
    """
    import importlib

    from eawf.daemon.methods import registered_methods

    importlib.import_module("eawf.daemon.methods.wal_admin")
    methods = set(registered_methods())
    expected = {"wal.list_pending", "wal.list_poisoned", "wal.gc", "wal.inspect"}
    assert expected.issubset(methods)


@pytest.mark.skipif(sys.platform.startswith("win"), reason="POSIX behaviour")
def test_wal_module_exposes_runtime_dir_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``_wal_dir()`` resolves to ``<runtime_dir>/wal``."""
    from eawf.cli.commands.daemon import _wal_dir

    runtime = _redirect_runtime(monkeypatch, tmp_path)
    assert _wal_dir() == runtime / "wal"


def test_wal_module_is_importable() -> None:
    """Sanity: the daemon.wal module is on the import path."""
    assert hasattr(wal, "write_pending")
