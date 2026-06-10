"""Rotation + env-guard coverage for the daemon ``eawfd.log`` handler.

The daemon's non-foreground logging branch wires a
:class:`logging.handlers.RotatingFileHandler` so a long-lived daemon
rolls a bounded number of backups instead of growing the live log
unbounded. These tests pin both halves of the contract: the env-tuned
cap actually rotates on disk, and a misconfigured (non-positive or
unparseable) override falls back to the shipped defaults.
"""

from __future__ import annotations

import logging
import logging.handlers
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf.runtime.daemon import main as daemon_main


@pytest.fixture
def _restore_root_logging() -> Iterator[None]:
    """Snapshot + restore the root logger so wiring tests stay isolated."""
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    try:
        yield
    finally:
        for handler in list(root.handlers):
            if handler not in saved_handlers:
                handler.close()
        root.handlers = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_uses_rotating_file_handler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_root_logging: None
) -> None:
    """The non-foreground branch attaches a ``RotatingFileHandler``."""
    log_file = tmp_path / "runtime" / "eawfd.log"
    monkeypatch.setattr(daemon_main, "log_path", lambda: log_file)
    logging.getLogger().handlers = []

    daemon_main._configure_logging(foreground=False)

    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert rotating
    handler = rotating[0]
    assert handler.maxBytes == daemon_main.DEFAULT_DAEMON_LOG_MAX_BYTES
    assert handler.backupCount == daemon_main.DEFAULT_DAEMON_LOG_BACKUP_COUNT


def test_configure_logging_rotates_past_the_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _restore_root_logging: None
) -> None:
    """Writing past a tiny env-tuned cap rolls backups and caps their count."""
    log_file = tmp_path / "runtime" / "eawfd.log"
    monkeypatch.setattr(daemon_main, "log_path", lambda: log_file)
    monkeypatch.setenv("EAWF_DAEMON_LOG_MAX_BYTES", "512")
    monkeypatch.setenv("EAWF_DAEMON_LOG_BACKUP_COUNT", "2")
    logging.getLogger().handlers = []

    daemon_main._configure_logging(foreground=False)
    handler = next(
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    )
    assert handler.maxBytes == 512
    assert handler.backupCount == 2

    log = logging.getLogger("eawf.test.daemon.rotation")
    # Each line is ~120 bytes after formatting; 200 writes is ~24 KiB,
    # far past the 512-byte cap, so rotation must have fired repeatedly.
    for index in range(200):
        log.info("rotation probe line index=%03d %s", index, "x" * 96)
    handler.flush()

    backups = sorted(log_file.parent.glob("eawfd.log.*"))
    # backupCount=2 means at most eawfd.log.1 + eawfd.log.2 survive; older
    # rolls are unlinked so the on-disk footprint stays bounded.
    assert backups, "expected rotation to produce at least one backup file"
    assert len(backups) <= 2
    assert {p.name for p in backups} <= {"eawfd.log.1", "eawfd.log.2"}
    # The live log was rolled, so it holds only the most recent tail, not
    # the whole 24 KiB stream.
    assert log_file.exists()
    assert log_file.stat().st_size <= 512


@pytest.mark.parametrize("raw", ["0", "-1", "not-an-int"])
def test_resolve_log_max_bytes_guards_bad_override(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-positive or unparseable cap override falls back to the default."""
    monkeypatch.setenv("EAWF_DAEMON_LOG_MAX_BYTES", raw)
    assert daemon_main._resolve_log_max_bytes() == daemon_main.DEFAULT_DAEMON_LOG_MAX_BYTES


def test_resolve_log_max_bytes_unset_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset cap env resolves to the shipped default."""
    monkeypatch.delenv("EAWF_DAEMON_LOG_MAX_BYTES", raising=False)
    assert daemon_main._resolve_log_max_bytes() == daemon_main.DEFAULT_DAEMON_LOG_MAX_BYTES


def test_resolve_log_max_bytes_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive cap override is honoured verbatim."""
    monkeypatch.setenv("EAWF_DAEMON_LOG_MAX_BYTES", "4096")
    assert daemon_main._resolve_log_max_bytes() == 4096


@pytest.mark.parametrize("raw", ["0", "-3", "garbage"])
def test_resolve_log_backup_count_guards_bad_override(
    raw: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-positive or unparseable backup count falls back to the default."""
    monkeypatch.setenv("EAWF_DAEMON_LOG_BACKUP_COUNT", raw)
    assert daemon_main._resolve_log_backup_count() == daemon_main.DEFAULT_DAEMON_LOG_BACKUP_COUNT


def test_resolve_log_backup_count_unset_is_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unset backup-count env resolves to the shipped default."""
    monkeypatch.delenv("EAWF_DAEMON_LOG_BACKUP_COUNT", raising=False)
    assert daemon_main._resolve_log_backup_count() == daemon_main.DEFAULT_DAEMON_LOG_BACKUP_COUNT


def test_resolve_log_backup_count_valid_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """A positive backup-count override is honoured verbatim."""
    monkeypatch.setenv("EAWF_DAEMON_LOG_BACKUP_COUNT", "9")
    assert daemon_main._resolve_log_backup_count() == 9
