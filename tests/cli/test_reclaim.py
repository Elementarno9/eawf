"""CLI tests for the one-shot ``eawf daemon reclaim`` verb.

``reclaim`` runs a single WAL-GC sweep (drops aged ``.fsynced.json``
records via the W01 helper) and trims ``state.json.bak.*`` backups beyond
a kept-count. Both steps read the local filesystem directly, so the tests
redirect ``runtime_dir()`` at ``tmp_path`` and point ``EA_STATE`` at a
seeded ``.ea/state.json`` -- no running daemon is required.

The over-grown-repo fixture seeds many backups plus one aged fsynced WAL
record; the tests assert backups trimmed beyond the keep window WITH the
newest retained, and that the WAL sweep ran. A boundary case (a repo
already under the keep window) asserts the trim removes nothing.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta
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
    write_pending,
)
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.unit

runner = CliRunner()


def _redirect_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Point the daemon command's ``runtime_dir()`` at ``tmp_path / "eawfd"``.

    The ``daemon`` command module binds ``runtime_dir`` at import time, so
    the patch targets that module-level name (mirrors ``test_wal_admin_cli``).
    """
    target = tmp_path / "eawfd"
    target.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr("eawf.surfaces.cli.commands.daemon.runtime_dir", lambda: target)
    return target


def _seed_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Write a stub ``.ea/state.json`` and anchor ``EA_STATE`` at it."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(b'{"schema_version": "1.7"}\n')
    monkeypatch.setenv("EA_STATE", str(state_path))
    return state_path


def _seed_backups(state_path: Path, count: int) -> list[Path]:
    """Write *count* ``state.json.bak.*`` files with ascending mtimes.

    Returns the backup paths in oldest-first order; the last entry is the
    newest by mtime, mirroring how the trim ranks recency.
    """
    base = time.time() - 10_000
    paths: list[Path] = []
    for index in range(count):
        backup = state_path.with_name(f"{state_path.name}.bak.v1.{index}.v1.{index + 1}")
        backup.write_bytes(b'{"schema_version": "stub"}\n')
        # Strictly ascending mtimes so the newest is unambiguous.
        stamp = base + index * 100
        os.utime(backup, (stamp, stamp))
        paths.append(backup)
    return paths


def _seed_aged_fsynced_record(runtime: Path) -> Path:
    """Write one aged ``.fsynced.json`` WAL record eligible for GC."""
    wal_dir = runtime / "wal"
    record_id = "rec-aged"
    envelope = Envelope(
        id="env-aged",
        kind=StoreKind.EVENT,
        scope_id=None,
        created_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        summary="aged envelope",
        payload={"action": "noop"},
    )
    record = WalRecord(
        record_id=record_id,
        envelope=envelope,
        idempotency_key=None,
        written_at=datetime(2026, 5, 19, 12, 0, 0, tzinfo=UTC),
        before_state_version="sha:before",
        after_state_version="sha:after",
    )
    write_pending(wal_dir, record)
    mark_applied(wal_dir, record_id)
    fsynced = mark_fsynced(wal_dir, record_id)
    # Backdate the mtime well past the default retention window.
    old = (datetime.now(UTC) - timedelta(days=2)).timestamp()
    os.utime(fsynced, (old, old))
    return fsynced


# --- over-grown repo: backups trimmed + WAL swept ----------------------------


def test_reclaim_trims_old_backups_and_sweeps_wal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An over-grown repo trims old backups beyond --keep and sweeps the WAL."""
    runtime = _redirect_runtime(monkeypatch, tmp_path)
    state_path = _seed_state(monkeypatch, tmp_path)
    backups = _seed_backups(state_path, count=8)
    newest = backups[-1]
    fsynced = _seed_aged_fsynced_record(runtime)

    res = runner.invoke(app, ["--json", "daemon", "reclaim", "--keep", "3"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())

    # 8 backups - keep 3 == 5 removed; the newest 3 survive on disk.
    assert payload["backups_trimmed_count"] == 5
    assert payload["keep"] == 3
    surviving = sorted(state_path.parent.glob(f"{state_path.name}.bak.*"))
    assert len(surviving) == 3
    assert newest.exists()
    # The five oldest are gone.
    for stale in backups[:5]:
        assert not stale.exists()

    # The WAL sweep ran and removed the aged fsynced record.
    assert payload["wal_swept_count"] == 1
    assert not fsynced.exists()


def test_reclaim_retains_newest_backup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Even at keep=1 the single newest backup is the one retained."""
    _redirect_runtime(monkeypatch, tmp_path)
    state_path = _seed_state(monkeypatch, tmp_path)
    backups = _seed_backups(state_path, count=6)
    newest = backups[-1]

    res = runner.invoke(app, ["--json", "daemon", "reclaim", "--keep", "1"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())

    assert payload["backups_trimmed_count"] == 5
    surviving = list(state_path.parent.glob(f"{state_path.name}.bak.*"))
    assert surviving == [newest]


# --- boundary: repo already under the keep window ----------------------------


def test_reclaim_under_keep_window_removes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A repo with fewer backups than --keep trims nothing, exit 0."""
    _redirect_runtime(monkeypatch, tmp_path)
    state_path = _seed_state(monkeypatch, tmp_path)
    backups = _seed_backups(state_path, count=2)

    res = runner.invoke(app, ["--json", "daemon", "reclaim", "--keep", "3"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())

    assert payload["backups_trimmed_count"] == 0
    for backup in backups:
        assert backup.exists()


def test_reclaim_no_backups_is_noop(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A repo with zero backups reports an honest noop, exit 0."""
    _redirect_runtime(monkeypatch, tmp_path)
    _seed_state(monkeypatch, tmp_path)

    res = runner.invoke(app, ["--json", "daemon", "reclaim"])
    assert res.exit_code == 0, res.output
    payload = orjson.loads(res.output.strip())
    assert payload["backups_trimmed_count"] == 0
    assert payload["wal_swept_count"] == 0
    assert payload["keep"] == 3


# --- usage errors ------------------------------------------------------------


def test_reclaim_rejects_negative_keep(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A negative --keep is a usage error (exit 2)."""
    _redirect_runtime(monkeypatch, tmp_path)
    _seed_state(monkeypatch, tmp_path)

    res = runner.invoke(app, ["daemon", "reclaim", "--keep", "-1"])
    assert res.exit_code == 2
    assert "--keep must be >= 0" in res.output


def test_reclaim_rejects_out_of_range_max_age(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An out-of-range --max-age-seconds is a usage error (exit 2)."""
    _redirect_runtime(monkeypatch, tmp_path)
    _seed_state(monkeypatch, tmp_path)

    res = runner.invoke(app, ["daemon", "reclaim", "--max-age-seconds", "-5"])
    assert res.exit_code == 2
    assert "--max-age-seconds must be between 0 and 2592000" in res.output


# --- text-mode smoke ---------------------------------------------------------


def test_reclaim_text_output(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The text body reports both swept and trimmed counts."""
    _redirect_runtime(monkeypatch, tmp_path)
    state_path = _seed_state(monkeypatch, tmp_path)
    _seed_backups(state_path, count=5)

    res = runner.invoke(app, ["daemon", "reclaim", "--keep", "2"])
    assert res.exit_code == 0, res.output
    assert "trimmed 3 state backup(s)" in res.output
    assert "keep=2" in res.output
