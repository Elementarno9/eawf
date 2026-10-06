"""Tests for the suite-wide mirror of ``HOME`` into the Windows home variables."""

from __future__ import annotations

import ntpath
import os
import sys
from pathlib import Path

import pytest

from tests.conftest import WINDOWS_HOME_VARIABLES


def _windows_home() -> str:
    """Resolve ``~`` the way Windows does, from the live environment."""
    return ntpath.expanduser("~")


def test_set_home_governs_every_home_lookup_under_win32(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "elsewhere"))
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert Path.home() == tmp_path
    assert os.path.expanduser("~") == str(tmp_path)
    assert _windows_home() == str(tmp_path)
    assert os.environ["USERPROFILE"] == str(tmp_path)


def test_mirror_is_undone_with_home(tmp_path: Path) -> None:
    before = {name: os.environ.get(name) for name in ("HOME", *WINDOWS_HOME_VARIABLES)}
    patcher = pytest.MonkeyPatch()
    patcher.setenv("HOME", str(tmp_path))
    patcher.undo()
    assert {name: os.environ.get(name) for name in before} == before


def test_windows_drive_path_splits_into_homedrive_and_homepath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HOME", "C:\\Users\\someone")
    assert os.environ["USERPROFILE"] == "C:\\Users\\someone"
    assert os.environ["HOMEDRIVE"] == "C:"
    assert os.environ["HOMEPATH"] == "\\Users\\someone"


def test_posix_home_mirrors_with_an_empty_drive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "/")
    assert os.environ["HOMEDRIVE"] == ""
    assert os.environ["HOMEPATH"] == "/"


def test_empty_home_mirrors_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "")
    assert [os.environ[name] for name in WINDOWS_HOME_VARIABLES] == ["", "", ""]


def test_last_home_set_wins(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "first"))
    monkeypatch.setenv("HOME", str(tmp_path / "second"))
    assert _windows_home() == str(tmp_path / "second")


def test_prepended_home_mirrors_the_resulting_value(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", "base")
    monkeypatch.setenv("HOME", "front", prepend=":")
    assert os.environ["USERPROFILE"] == os.environ["HOME"] == "front:base"


def test_other_variables_are_not_mirrored(monkeypatch: pytest.MonkeyPatch) -> None:
    before = {name: os.environ.get(name) for name in WINDOWS_HOME_VARIABLES}
    monkeypatch.setenv("HOMEX", "/somewhere")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/somewhere")
    assert {name: os.environ.get(name) for name in WINDOWS_HOME_VARIABLES} == before


def test_unsetting_home_changes_nothing_else(monkeypatch: pytest.MonkeyPatch) -> None:
    before = dict(os.environ)
    before.pop("HOME", None)
    monkeypatch.delenv("HOME", raising=False)
    assert "HOME" not in os.environ
    assert dict(os.environ) == before


def test_unsetting_a_missing_home_strictly_raises_key_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("HOME", raising=False)
    with pytest.raises(KeyError):
        monkeypatch.delenv("HOME")


def test_non_string_name_raises_type_error(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(TypeError):
        monkeypatch.setenv(1, "/somewhere")  # type: ignore[arg-type]


def test_non_string_home_value_is_coerced_and_mirrored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.warns(pytest.PytestWarning):
        monkeypatch.setenv("HOME", tmp_path)  # type: ignore[arg-type]
    assert os.environ["USERPROFILE"] == str(tmp_path)
