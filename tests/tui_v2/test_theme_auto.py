"""Tests for ``/theme auto`` OSC 11 terminal-background detection (P26-I02-W16).

Covers the three detection units added to :mod:`eawf.tui_v2.theme` and the
``apply_theme("auto")`` wiring in :mod:`eawf.tui_v2.app`:

* :func:`resolve_auto_theme` — the pure luminance classifier (no I/O):
  light vs dark vs ``None`` plus the threshold boundary.
* :func:`_parse_osc11` — parsing real xterm replies (4-digit + 2-digit hex,
  BEL + ST terminators) and rejecting garbage / empty.
* :func:`query_terminal_background` — the bounded live probe: returns
  ``None`` (no write, no hang) for a non-TTY stream and when ``CI`` is set,
  and parses + classifies a canned reply when ``_read_osc_reply`` is
  monkeypatched (so no real terminal is required).
* :meth:`EaApp.apply_theme` — with ``_auto_logical`` monkeypatched to
  ``light`` / ``dark``, ``apply_theme("auto")`` sets the matching theme and
  returns ``True``.

Every wait is bounded and no test touches a real terminal: detection runs at
App construction under ``run_test()`` (non-TTY → ``None`` → dark baseline),
and the probe tests use fake streams / a monkeypatched reader.
"""

from __future__ import annotations

import asyncio
import io
import termios
import tty
from pathlib import Path

import pytest

from eawf.tui_v2 import theme as theme_mod
from eawf.tui_v2.app import EaApp
from eawf.tui_v2.theme import (
    EA_DARK,
    EA_LIGHT,
    OSC11_QUERY,
    detect_auto_theme,
    query_terminal_background,
    resolve_auto_theme,
)
from eawf.tui_v2.theme import (
    _parse_osc11 as parse_osc11,
)

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


# --------------------------------------------------------------------------
# resolve_auto_theme — pure luminance classifier
# --------------------------------------------------------------------------


def test_resolve_auto_theme_white_is_light() -> None:
    assert resolve_auto_theme((255, 255, 255)) == "light"


def test_resolve_auto_theme_light_grey_is_light() -> None:
    assert resolve_auto_theme((200, 200, 200)) == "light"


def test_resolve_auto_theme_black_is_dark() -> None:
    assert resolve_auto_theme((0, 0, 0)) == "dark"


def test_resolve_auto_theme_near_black_is_dark() -> None:
    assert resolve_auto_theme((20, 20, 30)) == "dark"


def test_resolve_auto_theme_none_falls_back_to_dark() -> None:
    assert resolve_auto_theme(None) == "dark"


def test_resolve_auto_theme_threshold_boundary() -> None:
    # Luminance of a neutral grey equals the channel value, so 128 sits just
    # above the 127.5 midpoint (light) and 127 just below (dark).
    assert resolve_auto_theme((128, 128, 128)) == "light"
    assert resolve_auto_theme((127, 127, 127)) == "dark"


# --------------------------------------------------------------------------
# _parse_osc11 — real xterm replies + garbage rejection
# --------------------------------------------------------------------------


def test_parse_osc11_four_digit_black_bel() -> None:
    assert parse_osc11(b"\x1b]11;rgb:0000/0000/0000\x07") == (0, 0, 0)


def test_parse_osc11_four_digit_white_st() -> None:
    assert parse_osc11(b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\") == (255, 255, 255)


def test_parse_osc11_two_digit_white() -> None:
    assert parse_osc11(b"\x1b]11;rgb:ff/ff/ff\x07") == (255, 255, 255)


def test_parse_osc11_two_digit_mid_channels() -> None:
    # 0x80 == 128 over a 2-digit max of 0xff scales to 128.
    assert parse_osc11(b"\x1b]11;rgb:80/80/80\x07") == (128, 128, 128)


def test_parse_osc11_garbage_returns_none() -> None:
    assert parse_osc11(b"not an osc reply at all") is None


def test_parse_osc11_empty_returns_none() -> None:
    assert parse_osc11(b"") is None


# --------------------------------------------------------------------------
# query_terminal_background — bounded probe, safe fallbacks
# --------------------------------------------------------------------------


class _FakeTTY(io.StringIO):
    """A stream that claims to be a TTY and records what was written."""

    def __init__(self, *, isatty: bool, fileno: int = 0) -> None:
        super().__init__()
        self._isatty = isatty
        self._fileno = fileno
        self.written: list[str] = []

    def isatty(self) -> bool:
        return self._isatty

    def fileno(self) -> int:
        return self._fileno

    def write(self, s: str) -> int:
        self.written.append(s)
        return len(s)

    def flush(self) -> None:
        return None


def test_query_terminal_background_non_tty_returns_none_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-TTY stdin short-circuits to None without writing the query."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EAWF_NO_OSC", raising=False)
    stdin = _FakeTTY(isatty=False)
    stdout = _FakeTTY(isatty=True)
    assert query_terminal_background(stdin=stdin, stdout=stdout, timeout_s=0.01) is None
    assert stdout.written == [], "no query may be written when stdin is not a TTY"


def test_query_terminal_background_ci_env_returns_none_no_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The CI guard returns None and writes nothing (no hang in CI)."""
    monkeypatch.setenv("CI", "1")
    monkeypatch.delenv("EAWF_NO_OSC", raising=False)
    stdout = _FakeTTY(isatty=True)
    assert (
        query_terminal_background(stdin=_FakeTTY(isatty=True), stdout=stdout, timeout_s=0.01)
        is None
    )
    assert stdout.written == []


def test_query_terminal_background_eawf_no_osc_env_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The explicit opt-out env returns None without probing."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.setenv("EAWF_NO_OSC", "1")
    stdout = _FakeTTY(isatty=True)
    assert (
        query_terminal_background(stdin=_FakeTTY(isatty=True), stdout=stdout, timeout_s=0.01)
        is None
    )
    assert stdout.written == []


def _stub_termios(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise the real termios/tty syscalls on the fake fd.

    ``query_terminal_background`` imports ``termios`` / ``tty`` lazily, but
    Python caches the module objects in ``sys.modules`` so the lazily-bound
    references are the same objects patched here.
    """
    monkeypatch.setattr(termios, "tcgetattr", lambda fd: object())
    monkeypatch.setattr(termios, "tcsetattr", lambda fd, when, attrs: None)
    monkeypatch.setattr(tty, "setcbreak", lambda fd: None)


def test_query_terminal_background_parses_dark_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canned dark reply is written-then-read-then-parsed to RGB."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EAWF_NO_OSC", raising=False)
    _stub_termios(monkeypatch)
    monkeypatch.setattr(
        theme_mod,
        "_read_osc_reply",
        lambda fd, timeout_s: b"\x1b]11;rgb:0000/0000/0000\x07",
    )
    stdout = _FakeTTY(isatty=True)
    rgb = query_terminal_background(stdin=_FakeTTY(isatty=True), stdout=stdout, timeout_s=0.01)
    assert rgb == (0, 0, 0)
    assert stdout.written == [OSC11_QUERY], "the OSC 11 query must be written exactly once"


def test_query_terminal_background_parses_light_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A canned light reply parses to white RGB."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EAWF_NO_OSC", raising=False)
    _stub_termios(monkeypatch)
    monkeypatch.setattr(
        theme_mod,
        "_read_osc_reply",
        lambda fd, timeout_s: b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\",
    )
    rgb = query_terminal_background(
        stdin=_FakeTTY(isatty=True), stdout=_FakeTTY(isatty=True), timeout_s=0.01
    )
    assert rgb == (255, 255, 255)


def test_query_terminal_background_no_reply_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty (timed-out) read parses to None — the no-reply fallback."""
    monkeypatch.delenv("CI", raising=False)
    monkeypatch.delenv("EAWF_NO_OSC", raising=False)
    _stub_termios(monkeypatch)
    monkeypatch.setattr(theme_mod, "_read_osc_reply", lambda fd, timeout_s: b"")
    assert (
        query_terminal_background(
            stdin=_FakeTTY(isatty=True), stdout=_FakeTTY(isatty=True), timeout_s=0.01
        )
        is None
    )


def test_detect_auto_theme_falls_back_to_dark_on_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """detect_auto_theme glues query → classify; a None query → dark."""
    monkeypatch.setattr(theme_mod, "query_terminal_background", lambda: None)
    assert detect_auto_theme() == "dark"


def test_detect_auto_theme_light_when_query_is_light(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(theme_mod, "query_terminal_background", lambda: (255, 255, 255))
    assert detect_auto_theme() == "light"


# --------------------------------------------------------------------------
# apply_theme("auto") — uses the cached _auto_logical verdict
# --------------------------------------------------------------------------


def test_apply_theme_auto_uses_cached_light_logical() -> None:
    """With the cached verdict forced to light, /theme auto sets the light theme."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        app._auto_logical = "light"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.apply_theme("auto") is True
            await pilot.pause()
            assert app.theme == EA_LIGHT.name

    asyncio.run(body())


def test_apply_theme_auto_uses_cached_dark_logical() -> None:
    """With the cached verdict forced to dark, /theme auto sets the dark theme."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        app._auto_logical = "dark"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.apply_theme("auto") is True
            await pilot.pause()
            assert app.theme == EA_DARK.name

    asyncio.run(body())


def test_apply_theme_auto_returns_true_under_default_construction() -> None:
    """Default (non-TTY) construction caches dark; apply_theme("auto") still True."""

    async def body() -> None:
        app = EaApp(scope="repo", state_path=_PHASE_ITER_WAVE)
        # run_test()/non-TTY construction → detect_auto_theme() → dark.
        assert app._auto_logical == "dark"
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app.apply_theme("auto") is True
            await pilot.pause()
            assert app.theme == EA_DARK.name

    asyncio.run(body())
