"""Tests for the Seal image brand mark (graphics-protocol path).

``textual-image`` + Pillow now ship in the default deps, so the seal renders as
an inline image when three preconditions hold: the import resolves, ``resvg`` is
on PATH with the asset on disk, and the host terminal advertises a graphics
protocol. When any is absent -- or the ``EAWF_SEAL_DISABLE`` kill switch is set
(every CI / snapshot run) -- it reports not-capable and hands back ``None`` so
callers fall back to the unicode brand glyph and the goldens stay glyph-based.
These tests pin each precondition independently regardless of the running env.
"""

from __future__ import annotations

import pytest

from eawf.surfaces.tui.widgets import seal as seal_mod
from eawf.surfaces.tui.widgets.seal import (
    _RESVG,
    _SEAL_SVG,
    SEAL_DISABLE_ENV,
    deps_present,
    resvg_present,
    seal_capable,
    seal_image_widget,
    terminal_supports_images,
)


@pytest.fixture(autouse=True)
def _clear_seal_cache() -> None:
    """Drop the :func:`seal_capable` ``lru_cache`` around each test."""
    seal_capable.cache_clear()
    yield
    seal_capable.cache_clear()


def _make_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force every seal precondition True (kill switch cleared)."""
    monkeypatch.delenv(SEAL_DISABLE_ENV, raising=False)
    monkeypatch.setattr(seal_mod, "deps_present", lambda: True)
    monkeypatch.setattr(seal_mod, "resvg_present", lambda: True)
    monkeypatch.setattr(seal_mod, "terminal_supports_images", lambda: True)


def test_seal_capable_true_when_all_preconditions_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    # All three preconditions present and the kill switch unset -> capable.
    _make_capable(monkeypatch)
    assert seal_capable() is True


def test_seal_capable_false_when_deps_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "deps_present", lambda: False)
    assert seal_capable() is False


def test_seal_capable_false_when_resvg_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "resvg_present", lambda: False)
    assert seal_capable() is False


def test_seal_capable_false_when_terminal_unsupported(monkeypatch: pytest.MonkeyPatch) -> None:
    _make_capable(monkeypatch)
    monkeypatch.setattr(seal_mod, "terminal_supports_images", lambda: False)
    assert seal_capable() is False


def test_seal_disable_env_forces_false(monkeypatch: pytest.MonkeyPatch) -> None:
    # The kill switch wins even when every other precondition holds -- this is
    # the autouse switch the snapshot harness sets to keep goldens glyph-based.
    _make_capable(monkeypatch)
    monkeypatch.setenv(SEAL_DISABLE_ENV, "1")
    assert seal_capable() is False


def test_seal_disable_env_empty_string_does_not_disable(monkeypatch: pytest.MonkeyPatch) -> None:
    # Boundary: an empty value is not "set" for the kill switch.
    _make_capable(monkeypatch)
    monkeypatch.setenv(SEAL_DISABLE_ENV, "")
    assert seal_capable() is True


def test_seal_image_widget_none_when_not_capable(monkeypatch: pytest.MonkeyPatch) -> None:
    # When not capable the factory returns None (never raises), so a caller can
    # write ``seal_image_widget() or glyph_fallback`` safely.
    monkeypatch.setenv(SEAL_DISABLE_ENV, "1")
    assert seal_image_widget() is None


def test_deps_present_returns_bool() -> None:
    # textual-image ships in default deps, so this is True in the test env; the
    # contract under test is the bool return + import-guard, not the value.
    assert isinstance(deps_present(), bool)


def test_resvg_present_false_when_binary_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: None)
    assert resvg_present() is False


def test_resvg_present_false_when_asset_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: object
) -> None:
    from pathlib import Path

    monkeypatch.setattr(seal_mod.shutil, "which", lambda _name: "/usr/bin/resvg")
    monkeypatch.setattr(seal_mod, "_SEAL_SVG", Path(tmp_path) / "does-not-exist.svg")
    assert resvg_present() is False


def test_terminal_supports_images_false_when_not_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    # A non-TTY stdout (pipe / CI / snapshot harness) is never image-capable.
    class _NotTTY:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _NotTTY())
    assert terminal_supports_images() is False


def test_terminal_supports_images_false_when_stdout_none(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(seal_mod.sys, "__stdout__", None)
    assert terminal_supports_images() is False


def test_terminal_supports_images_true_for_kitty_window(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.setenv("KITTY_WINDOW_ID", "1")
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    assert terminal_supports_images() is True


def test_terminal_supports_images_true_for_ghostty(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "ghostty")
    assert terminal_supports_images() is True


def test_terminal_supports_images_false_for_plain_xterm(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TTY:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(seal_mod.sys, "__stdout__", _TTY())
    monkeypatch.delenv("KITTY_WINDOW_ID", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setenv("TERM_PROGRAM", "Apple_Terminal")
    assert terminal_supports_images() is False


def test_seal_asset_exists() -> None:
    # The Seal SVG ships in the package so a graphics terminal can rasterise it.
    assert _SEAL_SVG.exists()
    assert _SEAL_SVG.suffix == ".svg"
    assert _RESVG == "resvg"
