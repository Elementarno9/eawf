"""A temporary root leaves room for a bindable socket path on every POSIX host."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from eawf.runtime.daemon.runtime_dir import SUN_PATH_MAX, short_temp_root


def _temp_of_length(monkeypatch: pytest.MonkeyPatch, length: int) -> Path:
    """Point the system temp directory at a path of *length* bytes; the root never exists."""
    temp = Path("/" + "d" * (length - 1))
    assert len(os.fsencode(temp)) == length
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    return temp


def test_a_temp_directory_that_leaves_the_room_is_kept(monkeypatch: pytest.MonkeyPatch) -> None:
    temp = _temp_of_length(monkeypatch, 60)

    assert short_temp_root(0) == temp
    # The longest room that still leaves the NUL its byte.
    assert short_temp_root(SUN_PATH_MAX - 61) == temp


def test_one_byte_past_the_cap_falls_back_to_tmp(monkeypatch: pytest.MonkeyPatch) -> None:
    _temp_of_length(monkeypatch, 60)

    assert short_temp_root(SUN_PATH_MAX - 60) == Path("/tmp")


def test_a_temp_directory_as_deep_as_the_cap_falls_back_to_tmp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _temp_of_length(monkeypatch, SUN_PATH_MAX + 8)

    assert short_temp_root(0) == Path("/tmp")


def test_a_negative_room_is_refused() -> None:
    with pytest.raises(ValueError, match="must not be negative"):
        short_temp_root(-1)


def test_a_room_too_long_even_under_tmp_is_refused() -> None:
    with pytest.raises(ValueError, match="exceed the 104-byte cap"):
        short_temp_root(SUN_PATH_MAX - len("/tmp"))
