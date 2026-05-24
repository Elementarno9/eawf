"""Regression tests for the shared piped-stdin guard.

Three commands (``eawf cc statusline``, ``eawf cc statusline prewarm``,
``eawf render-output``) consume a JSON envelope from stdin via blocking
``sys.stdin.read()``. When invoked at a TTY with no piped data the read
blocks indefinitely — :func:`eawf.surfaces.cli._stdin.require_piped_stdin` exits
``2`` with a hint instead. This module pins:

- the helper itself (TTY → exit 2 + hint; piped → no exit);
- ``--help`` text on each affected command surfaces the stdin
  requirement so the operator does not hit the hang in the first place;
- the three end-to-end commands all exit 2 when stdin is a TTY.

The end-to-end tests patch the ``sys`` module reference inside
:mod:`eawf.surfaces.cli._stdin` rather than ``sys.stdin`` itself — Click's
:class:`CliRunner` replaces the real ``sys.stdin`` with its own input
buffer at invoke time, which would otherwise mask any direct
``sys.stdin`` patch we set before the invocation.
"""

from __future__ import annotations

import re
from unittest.mock import patch

import pytest
import typer
from typer.testing import CliRunner

from eawf.surfaces.cli._stdin import require_piped_stdin
from eawf.surfaces.cli.app import app

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_runner = CliRunner()


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


class _StdinStub:
    """Minimal ``sys.stdin`` shim with a configurable ``isatty`` flag."""

    def __init__(self, *, isatty: bool) -> None:
        self._isatty = isatty

    def isatty(self) -> bool:
        return self._isatty


class _SysStub:
    """Minimal ``sys`` shim exposing only the attribute the guard touches."""

    def __init__(self, *, isatty: bool) -> None:
        self.stdin = _StdinStub(isatty=isatty)


# ---- helper ---------------------------------------------------------------


def test_require_piped_stdin_exits_with_hint_when_stdin_is_tty(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (
        patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=True)),
        pytest.raises(typer.Exit) as exc_info,
    ):
        require_piped_stdin("eawf cc statusline")
    assert exc_info.value.exit_code == 2
    err = capsys.readouterr().err
    assert "eawf cc statusline expects a JSON envelope on stdin" in err


def test_require_piped_stdin_returns_silently_when_stdin_is_piped() -> None:
    with patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=False)):
        # Must not raise, must not write to stderr.
        require_piped_stdin("eawf cc statusline")


def test_require_piped_stdin_quotes_command_name_in_hint(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=True)), pytest.raises(typer.Exit):
        require_piped_stdin("eawf render-output")
    err = capsys.readouterr().err
    # Both placeholders ({name}) populated with the same label.
    assert err.count("eawf render-output") >= 2


# ---- --help surfaces ------------------------------------------------------


def test_cc_statusline_help_mentions_stdin() -> None:
    result = _runner.invoke(app, ["cc", "statusline", "--help"])
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.stdout).lower()
    assert "stdin" in out


def test_cc_statusline_prewarm_help_mentions_stdin() -> None:
    result = _runner.invoke(app, ["cc", "statusline", "prewarm", "--help"])
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.stdout).lower()
    assert "stdin" in out


def test_render_output_help_mentions_stdin() -> None:
    result = _runner.invoke(app, ["render-output", "--help"])
    assert result.exit_code == 0, result.output
    out = _strip_ansi(result.stdout).lower()
    assert "stdin" in out


# ---- end-to-end exit codes ------------------------------------------------


def test_cc_statusline_exits_2_when_invoked_at_tty() -> None:
    with patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=True)):
        result = _runner.invoke(app, ["cc", "statusline"])
    assert result.exit_code == 2, result.output
    assert "expects a JSON envelope on stdin" in result.stderr


def test_cc_statusline_prewarm_exits_2_when_invoked_at_tty() -> None:
    with patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=True)):
        result = _runner.invoke(app, ["cc", "statusline", "prewarm"])
    assert result.exit_code == 2, result.output
    assert "expects a JSON envelope on stdin" in result.stderr


def test_render_output_exits_2_when_invoked_at_tty() -> None:
    with patch("eawf.surfaces.cli._stdin.sys", _SysStub(isatty=True)):
        result = _runner.invoke(app, ["render-output"])
    assert result.exit_code == 2, result.output
    assert "expects a JSON envelope on stdin" in result.stderr
