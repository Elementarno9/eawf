"""Let every test's fsync return without flushing the disk under it.

A test tree lives under the host temp directory, and where that is a
disk-backed filesystem every durable write the daemon makes becomes a
physical flush: a single dispatch test makes hundreds, and the flushes
were most of its wall time. No test observes durability past a power loss
-- a killed worker's writes stay in the page cache whether or not they were
flushed -- so the flush proves nothing a test asserts. The stand-in still
checks its descriptor, so a write path that fsyncs a closed or bogus
descriptor raises exactly as it would against the real call.
"""

from __future__ import annotations

import os

import pytest


def _checked_fsync(fd: int) -> None:
    """Raise ``OSError`` for a descriptor the real fsync would refuse; flush nothing.

    Raises:
        OSError: *fd* is not an open descriptor.
    """
    os.fstat(fd)


@pytest.fixture(autouse=True)
def fsync_without_flush(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swap ``os.fsync`` for the descriptor-checking stand-in for one test."""
    monkeypatch.setattr(os, "fsync", _checked_fsync)
