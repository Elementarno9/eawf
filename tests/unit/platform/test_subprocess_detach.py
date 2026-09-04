"""Unit tests for the platform subprocess-kwargs helpers.

Covers both helpers in :mod:`eawf.platform.subprocess_detach`: the TUI
detach kwargs and the daemon console-suppression kwargs. The win32 branch
is exercised by monkeypatching ``sys.platform`` -- both helpers read it at
call time, so a POSIX runner can still prove the Windows shape.

This is a unit-tier file, so it must not ``import subprocess`` (EAWF024).
The win32 creation flag is therefore asserted against the module's own
``CREATE_NO_WINDOW`` constant, which is where the ``getattr`` resolution of
the stdlib value already lives.
"""

from __future__ import annotations

import sys

import pytest

from eawf.platform import subprocess_detach
from eawf.platform.subprocess_detach import (
    CREATE_NO_WINDOW,
    detached_subprocess_kwargs,
    no_window_kwargs,
)

_NON_WIN32_PLATFORMS = ("darwin", "linux", "freebsd", "cygwin")
"""Every platform string that must take the non-win32 branch.

``cygwin`` is included deliberately: it runs on Windows but reports its own
platform string and has no win32 console-allocation semantics, so it must
fall through to the POSIX branch rather than be special-cased.
"""


def test_no_window_kwargs_returns_creationflags_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """win32 yields exactly the ``CREATE_NO_WINDOW`` creation flag."""
    monkeypatch.setattr(sys, "platform", "win32")

    assert no_window_kwargs() == {"creationflags": CREATE_NO_WINDOW}


@pytest.mark.parametrize("platform", _NON_WIN32_PLATFORMS)
def test_no_window_kwargs_returns_empty_mapping_off_win32(
    monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    """Every non-win32 platform yields an empty mapping, so the splat is a no-op."""
    monkeypatch.setattr(sys, "platform", platform)

    assert no_window_kwargs() == {}


def test_no_window_kwargs_carries_no_posix_detach_knob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The console helper never smuggles in ``start_new_session``.

    Session detachment is the detach helper's job and each adapter's own
    decision; leaking it here would silently change POSIX process-group
    semantics at every git-probe call site.
    """
    monkeypatch.setattr(sys, "platform", "linux")
    assert "start_new_session" not in no_window_kwargs()

    monkeypatch.setattr(sys, "platform", "win32")
    assert "start_new_session" not in no_window_kwargs()


def test_no_window_kwargs_returns_a_fresh_mapping_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutating a returned mapping cannot poison a later call site."""
    monkeypatch.setattr(sys, "platform", "win32")

    first = no_window_kwargs()
    first["creationflags"] = 0xDEAD

    assert no_window_kwargs() == {"creationflags": CREATE_NO_WINDOW}


def test_create_no_window_constant_is_zero_off_win32() -> None:
    """Off win32 the flag resolves to the ``0`` fallback, never raising on import.

    On a win32 runner the stdlib constant is present and non-zero; the
    assertion is branched on the live platform so the same test states the
    contract on both.
    """
    if sys.platform == "win32":
        assert CREATE_NO_WINDOW != 0
    else:
        assert CREATE_NO_WINDOW == 0


def test_detached_subprocess_kwargs_returns_new_session_off_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI detach helper keeps its POSIX session-leader knob."""
    monkeypatch.setattr(sys, "platform", "darwin")

    assert detached_subprocess_kwargs() == {"start_new_session": True}


def test_detached_subprocess_kwargs_returns_creationflags_on_win32(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TUI detach helper shares the one ``CREATE_NO_WINDOW`` constant."""
    monkeypatch.setattr(sys, "platform", "win32")

    assert detached_subprocess_kwargs() == {"creationflags": CREATE_NO_WINDOW}


def test_module_exports_both_helpers_and_the_flag() -> None:
    """``__all__`` names the public surface call sites are allowed to import."""
    assert set(subprocess_detach.__all__) == {
        "CREATE_NO_WINDOW",
        "detached_subprocess_kwargs",
        "no_window_kwargs",
    }
