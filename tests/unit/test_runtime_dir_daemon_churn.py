"""Gate-fire proof: the live runtime-dir guard tells daemon churn from suite writes.

A tmp dir stands in for the operator's live ``~/.eawfd``. The daemon's
real exit and boot helpers (``eawf.runtime.daemon.main``) drive the churn,
so the proof covers the records the daemon actually writes, not a
hand-built ledger. The guard verdict is
``eawf.runtime.daemon.churn.unattributed_changes``, the same call
``tests/unit/test_runtime_dir_isolation.py`` makes against the live dir.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.runtime.daemon import churn
from eawf.runtime.daemon.churn import (
    CHURN_LEDGER_NAME,
    CHURN_LEDGER_ROTATED_NAME,
    DIRECTORY_CHANGE,
    SUITE_SESSION_ENV,
    ChurnOp,
    ChurnRecord,
    read_churn_records,
    record_churn,
    snapshot_runtime_dir,
    unattributed_changes,
)
from eawf.runtime.daemon.main import _boot_touched_names, _remove_runtime_entries

_OLD_MTIME_NS: int = 1_000_000_000


@pytest.fixture
def live_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A stand-in live runtime dir with a running daemon's socket and PID file.

    The suite tag is cleared so records written here look like the
    operator's daemon, the one whose churn the guard must admit.
    """
    monkeypatch.delenv(SUITE_SESSION_ENV, raising=False)
    rt_dir = tmp_path / "eawfd"
    rt_dir.mkdir()
    (rt_dir / "eawfd.sock").write_text("", encoding="utf-8")
    (rt_dir / "eawfd.pid").write_text("1\n", encoding="utf-8")
    (rt_dir / "eawfd.log").write_text("", encoding="utf-8")
    os.utime(rt_dir, ns=(_OLD_MTIME_NS, _OLD_MTIME_NS))
    return rt_dir


def _idle_out(rt_dir: Path) -> None:
    """Run the daemon's own exit path, as an idle-out shutdown does."""
    _remove_runtime_entries(rt_dir, pid_file=rt_dir / "eawfd.pid", sock_path=rt_dir / "eawfd.sock")


def _verdict(rt_dir: Path, before: churn.RuntimeDirSnapshot) -> tuple[str, ...]:
    """Close the window and return what the guard would report."""
    after = snapshot_runtime_dir(rt_dir)
    return unattributed_changes(before, after, read_churn_records(rt_dir))


def test_unattributed_changes_daemon_idle_out_passes(live_dir: Path) -> None:
    """The daemon removing its socket and PID file mid-window is explained."""
    before = snapshot_runtime_dir(live_dir)
    _idle_out(live_dir)
    assert not (live_dir / "eawfd.sock").exists()
    assert _verdict(live_dir, before) == ()


def test_unattributed_changes_suite_write_reds(live_dir: Path) -> None:
    """A file the suite writes into the live dir reds the guard."""
    before = snapshot_runtime_dir(live_dir)
    (live_dir / "stray.json").write_text("{}", encoding="utf-8")
    assert _verdict(live_dir, before) == ("stray.json",)


def test_unattributed_changes_suite_write_reds_during_idle_out(live_dir: Path) -> None:
    """Daemon churn in the same window does not launder a suite write."""
    before = snapshot_runtime_dir(live_dir)
    _idle_out(live_dir)
    (live_dir / "stray.json").write_text("{}", encoding="utf-8")
    assert _verdict(live_dir, before) == ("stray.json",)


def test_unattributed_changes_suite_spawned_daemon_reds(
    live_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon carrying the suite tag is the suite, so its churn reds."""
    monkeypatch.setenv(SUITE_SESSION_ENV, "suite-tag")
    before = snapshot_runtime_dir(live_dir)
    _idle_out(live_dir)
    assert _verdict(live_dir, before) == ("eawfd.pid", "eawfd.sock")


def test_unattributed_changes_suite_unlinks_socket_reds(live_dir: Path) -> None:
    """The suite removing the live socket with no daemon record reds."""
    before = snapshot_runtime_dir(live_dir)
    (live_dir / "eawfd.sock").unlink()
    assert _verdict(live_dir, before) == ("eawfd.sock",)


def test_unattributed_changes_daemon_boot_passes(live_dir: Path) -> None:
    """A fresh daemon writing its PID file, WAL dir and socket is explained."""
    _idle_out(live_dir)
    before = snapshot_runtime_dir(live_dir)
    pid_file = live_dir / "eawfd.pid"
    sock_path = live_dir / "eawfd.sock"
    record_churn(live_dir, op=ChurnOp.BOOT, touched=_boot_touched_names(pid_file, sock_path))
    pid_file.write_text("2\n", encoding="utf-8")
    (live_dir / "wal").mkdir()
    sock_path.write_text("", encoding="utf-8")
    assert _verdict(live_dir, before) == ()


def test_unattributed_changes_replaced_entry_reds(live_dir: Path) -> None:
    """An atomic replace keeps the name set but moves the inode, and reds."""
    before = snapshot_runtime_dir(live_dir)
    tmp = live_dir / "eawfd.log.new"
    tmp.write_text("x", encoding="utf-8")
    tmp.replace(live_dir / "eawfd.log")
    assert _verdict(live_dir, before) == ("eawfd.log",)


def test_unattributed_changes_transient_write_reds(live_dir: Path) -> None:
    """An entry created and removed inside the window reds via the dir mtime."""
    before = snapshot_runtime_dir(live_dir)
    transient = live_dir / "transient.tmp"
    transient.write_text("x", encoding="utf-8")
    transient.unlink()
    assert _verdict(live_dir, before) == (DIRECTORY_CHANGE,)


def test_unattributed_changes_record_before_window_reds(live_dir: Path) -> None:
    """A record older than the lookback cannot explain a later change."""
    record_churn(live_dir, op=ChurnOp.EXIT, touched=("eawfd.sock",))
    time.sleep(0.001)
    before = snapshot_runtime_dir(live_dir)
    (live_dir / "eawfd.sock").unlink()
    after = snapshot_runtime_dir(live_dir)
    records = read_churn_records(live_dir)
    assert unattributed_changes(before, after, records, lookback_ns=0) == ("eawfd.sock",)


def test_unattributed_changes_record_inside_lookback_passes(live_dir: Path) -> None:
    """A boot record written just before the window explains its later bind."""
    record_churn(live_dir, op=ChurnOp.BOOT, touched=("eawfd.sock",))
    before = snapshot_runtime_dir(live_dir)
    (live_dir / "eawfd.sock").unlink()
    assert _verdict(live_dir, before) == ()


def test_unattributed_changes_unchanged_dir_passes(live_dir: Path) -> None:
    """No change at all passes, ledger or not."""
    before = snapshot_runtime_dir(live_dir)
    assert _verdict(live_dir, before) == ()


def test_unattributed_changes_absent_dir_passes(tmp_path: Path) -> None:
    """A live dir absent on both sides passes."""
    missing = tmp_path / "absent"
    before = snapshot_runtime_dir(missing)
    assert before.exists is False
    assert before.entries == {}
    assert _verdict(missing, before) == ()


def test_unattributed_changes_created_empty_dir_reds(tmp_path: Path) -> None:
    """Creating the live dir itself, with no daemon record, reds."""
    rt_dir = tmp_path / "eawfd"
    before = snapshot_runtime_dir(rt_dir)
    rt_dir.mkdir()
    assert _verdict(rt_dir, before) == (DIRECTORY_CHANGE,)


def test_unattributed_changes_ledger_creation_exempt(live_dir: Path) -> None:
    """The ledger's own first write is bookkeeping, not churn."""
    before = snapshot_runtime_dir(live_dir)
    record_churn(live_dir, op=ChurnOp.EXIT, touched=("eawfd.sock",))
    assert (live_dir / CHURN_LEDGER_NAME).exists()
    assert _verdict(live_dir, before) == ()


def test_unattributed_changes_negative_lookback_raises(live_dir: Path) -> None:
    """A negative lookback is a caller error."""
    snap = snapshot_runtime_dir(live_dir)
    with pytest.raises(ValueError, match="lookback_ns must be non-negative"):
        unattributed_changes(snap, snap, (), lookback_ns=-1)


def test_record_churn_rotates_at_cap(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Past the cap the ledger rotates once, and both generations are read."""
    monkeypatch.setattr(churn, "CHURN_LEDGER_MAX_BYTES", 1)
    record_churn(live_dir, op=ChurnOp.BOOT, touched=("eawfd.pid",))
    record_churn(live_dir, op=ChurnOp.EXIT, touched=("eawfd.sock",))
    assert (live_dir / CHURN_LEDGER_ROTATED_NAME).exists()
    ops = [record.op for record in read_churn_records(live_dir)]
    assert ops == [ChurnOp.BOOT, ChurnOp.EXIT]


def test_record_churn_carries_suite_tag(live_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The inherited suite tag lands on the record; an empty tag is ``None``."""
    monkeypatch.setenv(SUITE_SESSION_ENV, "suite-tag")
    record_churn(live_dir, op=ChurnOp.EXIT, touched=("eawfd.sock",))
    monkeypatch.setenv(SUITE_SESSION_ENV, "")
    record_churn(live_dir, op=ChurnOp.EXIT, touched=("eawfd.sock",))
    tags = [record.suite_session for record in read_churn_records(live_dir)]
    assert tags == ["suite-tag", None]


def test_record_churn_missing_dir_does_not_raise(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A daemon that cannot write its ledger still exits cleanly."""
    record_churn(tmp_path / "absent", op=ChurnOp.EXIT, touched=("eawfd.sock",))
    assert "record_churn write-failed" in caplog.text


def test_read_churn_records_skips_torn_line(live_dir: Path) -> None:
    """A torn tail line is skipped and the entry it named stays unexplained."""
    record_churn(live_dir, op=ChurnOp.BOOT, touched=("eawfd.pid",))
    with (live_dir / CHURN_LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write('{"pid": 7, "op": "exit", "touched": ["eawfd.so\n')
    records = read_churn_records(live_dir)
    assert [record.op for record in records] == [ChurnOp.BOOT]


def test_read_churn_records_no_ledger_is_empty(live_dir: Path) -> None:
    """No ledger yet reads as no records."""
    assert read_churn_records(live_dir) == ()


def test_churn_record_rejects_extra_field() -> None:
    """The ledger schema is closed."""
    with pytest.raises(ValidationError):
        ChurnRecord.model_validate(
            {
                "pid": 1,
                "op": "exit",
                "at_ns": 0,
                "suite_session": None,
                "touched": ["eawfd.sock"],
                "extra": 1,
            }
        )


def test_churn_record_rejects_empty_touched() -> None:
    """A record that explains nothing is malformed."""
    with pytest.raises(ValidationError):
        ChurnRecord(pid=1, op=ChurnOp.EXIT, at_ns=0, suite_session=None, touched=())


def test_boot_touched_names_windows_has_no_socket(tmp_path: Path) -> None:
    """Without a socket path the boot record names only the PID file and WAL."""
    assert _boot_touched_names(tmp_path / "eawfd.pid", None) == (
        "eawfd.pid",
        "eawfd.pid.tmp",
        "wal",
    )
