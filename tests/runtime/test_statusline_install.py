"""Tests for the pure statusline installer library (P29-I13-W41).

Pins the namespace-narrow settings patch (only the statusLine key changes),
the empty/absent-file boundaries, idempotence detection, deterministic JSON
rendering, and the invalid-JSON error path -- all without a live CLI or a
real ``~/.claude`` tree.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from eawf.runtime.runtimes.claude.statusline_install import (
    build_statusline_command,
    install_statusline,
    is_already_installed,
    patch_settings,
    read_settings,
    render_settings,
)

_EXPECTED_COMMAND = {"type": "command", "command": "eawf cc statusline"}


def test_build_statusline_command_shape() -> None:
    assert build_statusline_command() == _EXPECTED_COMMAND


# --- read_settings -----------------------------------------------------------


def test_read_settings_absent_file_is_empty(tmp_path: Path) -> None:
    # boundary: a fresh install starts from an empty object.
    assert read_settings(tmp_path / "settings.json") == {}


def test_read_settings_empty_file_is_empty(tmp_path: Path) -> None:
    # boundary: a whitespace-only file is treated as empty.
    path = tmp_path / "settings.json"
    path.write_text("   \n", encoding="utf-8")
    assert read_settings(path) == {}


def test_read_settings_round_trips_object(tmp_path: Path) -> None:
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"a": 1}), encoding="utf-8")
    assert read_settings(path) == {"a": 1}


def test_read_settings_rejects_non_json(tmp_path: Path) -> None:
    # error-path: a corrupt file is rejected, not silently emptied.
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid json"):
        read_settings(path)


def test_read_settings_rejects_non_object_json(tmp_path: Path) -> None:
    # error-path: a JSON array is not a settings object.
    path = tmp_path / "settings.json"
    path.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a json object"):
        read_settings(path)


# --- patch_settings ----------------------------------------------------------


def test_patch_settings_inserts_statusline_into_empty() -> None:
    assert patch_settings({}) == {"statusLine": _EXPECTED_COMMAND}


def test_patch_settings_preserves_other_keys() -> None:
    # The patch only touches statusLine; sibling keys survive.
    patched = patch_settings({"model": "opus"})
    assert patched["model"] == "opus"
    assert patched["statusLine"] == _EXPECTED_COMMAND


def test_patch_settings_does_not_mutate_input() -> None:
    original = {"model": "opus"}
    patch_settings(original)
    assert "statusLine" not in original  # input untouched


# --- is_already_installed ----------------------------------------------------


def test_is_already_installed_true_when_command_matches() -> None:
    assert is_already_installed({"statusLine": _EXPECTED_COMMAND}) is True


def test_is_already_installed_false_when_absent() -> None:
    assert is_already_installed({}) is False


def test_is_already_installed_false_when_different_command() -> None:
    assert is_already_installed({"statusLine": {"type": "command", "command": "other"}}) is False


# --- render_settings ---------------------------------------------------------


def test_render_settings_is_deterministic_sorted_with_trailing_newline() -> None:
    rendered = render_settings({"b": 2, "a": 1})
    assert rendered.endswith("\n")
    # sorted keys -> "a" before "b"
    assert rendered.index('"a"') < rendered.index('"b"')


# --- install_statusline ------------------------------------------------------


def test_install_statusline_writes_and_creates_parent(tmp_path: Path) -> None:
    path = tmp_path / "nested" / "settings.json"
    written = install_statusline(path)
    assert path.is_file()
    assert written["statusLine"] == _EXPECTED_COMMAND
    assert json.loads(path.read_text(encoding="utf-8"))["statusLine"] == _EXPECTED_COMMAND


def test_install_statusline_is_byte_stable_on_rerun(tmp_path: Path) -> None:
    # boundary: a second install produces byte-identical output.
    path = tmp_path / "settings.json"
    install_statusline(path)
    first = path.read_bytes()
    install_statusline(path)
    assert path.read_bytes() == first


def test_install_statusline_propagates_invalid_json(tmp_path: Path) -> None:
    # error-path: a corrupt existing file is not overwritten silently.
    path = tmp_path / "settings.json"
    path.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ValueError, match="not valid json"):
        install_statusline(path)
