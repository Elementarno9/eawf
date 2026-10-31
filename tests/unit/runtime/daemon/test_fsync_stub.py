"""The suite's fsync stand-in refuses what the real call refuses and flushes nothing."""

from __future__ import annotations

import os
import resource
from pathlib import Path

import pytest

from tests._fsync_stub import _checked_fsync


def test_the_daemon_tests_run_under_the_stand_in() -> None:
    assert os.fsync is _checked_fsync


def test_an_open_descriptor_is_accepted(tmp_path: Path) -> None:
    with (tmp_path / "file").open("w", encoding="utf-8") as handle:
        handle.write("x")
        handle.flush()
        assert os.fsync(handle.fileno()) is None


def test_a_descriptor_past_the_open_file_limit_is_refused() -> None:
    # no descriptor at or past the soft limit can be open, so no thread can race it
    limit, _hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    with pytest.raises(OSError):
        os.fsync(limit)


def test_a_negative_descriptor_is_refused() -> None:
    with pytest.raises(OSError):
        os.fsync(-1)


def test_a_non_integer_descriptor_is_refused() -> None:
    with pytest.raises(TypeError):
        os.fsync("3")  # type: ignore[arg-type]
