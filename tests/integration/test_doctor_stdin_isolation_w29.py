"""Unit tests for the Doctor-mode probe stdin isolation.

The Doctor-mode health gather fans out to blocking subprocesses (the
instrument version-probes + the per-wave ``git log`` drift scan). Inside the
live TUI the parent fd 0 is the controlling TTY; a child that inherited it
could touch the TTY and solicit an escape-sequence reply that the App's stdin
reader then mis-parses as a synthetic key the instant the operator enters
Doctor mode.

W29 first closed this by re-pointing the PROCESS-GLOBAL fd 0 at ``/dev/null``
for the gather's duration -- but that ``os.dup2`` over fd 0 corrupted the live
App's asyncio stdin reader and crashed the running TUI with
``OSError: [Errno 9] Bad file descriptor`` (the reader already held the
original fd). W11 moved the isolation to the correct seam: each probe
``subprocess.run`` call site passes ``stdin=subprocess.DEVNULL`` directly.

But ``stdin=DEVNULL`` only stops a child from *reading* the TTY; the child
still SHARES the parent's controlling terminal (same session). On a graphics
terminal that shared-session child can still provoke a terminal escape-reply
(a Device-Attributes / capability response such as ``\\x1b[?62;1;...c``)
written onto the shared TTY -- which the App's stdin reader then parses as
synthetic digit-mode-switch keypresses (the 7 / 9 the operator saw). W10 adds
the robust fix: every probe child is DETACHED from the controlling terminal
(``start_new_session=True`` on POSIX, ``CREATE_NO_WINDOW`` on win32 via
:func:`eawf.platform.subprocess_detach.detached_subprocess_kwargs`) so it has
no controlling terminal and can neither provoke nor receive such a reply.

These tests pin the per-subprocess isolation contract:

* the instrument version-probe (:func:`eawf.platform.install.instrument_probe.probe_one`)
  passes ``stdin=subprocess.DEVNULL`` AND ``start_new_session=True`` (POSIX) to
  ``subprocess.run``;
* the per-wave ``git log`` drift scan
  (:func:`eawf.workflow.lifecycle.wave_sha.build_wave_sha_index`), the merge-base
  lookup, and the ``git log --grep`` derive all pass ``stdin=subprocess.DEVNULL``
  AND ``start_new_session=True`` (POSIX) to ``subprocess.run``;
* the shared :func:`detached_subprocess_kwargs` helper returns the
  platform-appropriate detach knobs;
* the process-global ``detached_tty_stdin`` redirect is GONE from the doctor
  checks module (re-introducing it would re-open the live-App crash).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.doctor import checks
from eawf.platform.install import instrument_probe
from eawf.platform.install.instrument_probe import InstrumentSpec, probe_one
from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.workflow.lifecycle import wave_sha


class _FakeProc:
    """Minimal stand-in for a ``subprocess.CompletedProcess``."""

    def __init__(self, *, stdout: str = "", returncode: int = 0) -> None:
        self.stdout = stdout
        self.stderr = ""
        self.returncode = returncode


def test_instrument_version_probe_passes_devnull_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The instrument version-probe runs ``subprocess.run`` with ``stdin=DEVNULL``.

    The behavioural heart of the W11 fix at the instrument seam: a probe child
    must inherit a dead stdin, never the live App's controlling TTY. We mock
    ``shutil.which`` to resolve the binary and capture the ``subprocess.run``
    kwargs to assert the isolation kwarg is threaded.
    """
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc(stdout="git version 2.46.0\n", returncode=0)

    monkeypatch.setattr(instrument_probe.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(instrument_probe.subprocess, "run", fake_run)

    spec = InstrumentSpec(
        name="git",
        kind="hard",
        probe="version",
        version_args=["--version"],
        version_regex=r"^git version",
    )
    result = probe_one(spec)

    assert result.status == "ok"
    assert captured.get("stdin") is subprocess.DEVNULL
    # The probe must still capture output (it parses the version stdout).
    assert captured.get("capture_output") is True
    # W10: the child is detached from the controlling terminal so a graphics
    # terminal cannot provoke an escape-reply on the shared session.
    if sys.platform != "win32":
        assert captured.get("start_new_session") is True
    else:
        assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_wave_sha_drift_scan_passes_devnull_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-wave ``git log`` drift scan runs with ``stdin=DEVNULL`` + detach.

    The drift scan is the second TTY-touching probe the Doctor gather fans out
    to; under the live TUI its child must inherit a dead stdin AND be detached
    from the controlling terminal. Mock ``shutil.which`` (so git resolves) and
    capture the ``subprocess.run`` kwargs.
    """
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc(stdout="", returncode=0)

    monkeypatch.setattr(wave_sha.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wave_sha.subprocess, "run", fake_run)

    wave_sha.build_wave_sha_index(repo_root=Path("/tmp"))

    assert captured.get("stdin") is subprocess.DEVNULL
    assert captured.get("capture_output") is True
    if sys.platform != "win32":
        assert captured.get("start_new_session") is True
    else:
        assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_wave_sha_merge_base_passes_detach_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``git merge-base`` diff-base lookup runs detached + with dead stdin.

    :func:`eawf.workflow.lifecycle.wave_sha.derive_diff_base` falls back to a
    ``git merge-base HEAD main`` shell-out; that child fans out from the same
    Doctor gather, so it must also be detached from the controlling terminal.
    """
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc(stdout="deadbeef\n", returncode=0)

    monkeypatch.setattr(wave_sha.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wave_sha.subprocess, "run", fake_run)

    wave_sha._git_merge_base_head_main(repo_root=Path("/tmp"))

    assert captured.get("stdin") is subprocess.DEVNULL
    assert captured.get("capture_output") is True
    if sys.platform != "win32":
        assert captured.get("start_new_session") is True
    else:
        assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_wave_sha_derive_grep_passes_detach_kwargs(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``git log --grep`` wave-SHA derive runs detached + with dead stdin.

    :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha` shells out a
    ``git log --grep=[P##-W##]`` lookup per candidate prefix; each child must be
    detached from the controlling terminal like the other Doctor-gather probes.
    """
    captured: dict[str, Any] = {}

    def fake_run(*args: Any, **kwargs: Any) -> _FakeProc:
        captured.update(kwargs)
        return _FakeProc(stdout="cafef00d\n", returncode=0)

    monkeypatch.setattr(wave_sha.shutil, "which", lambda _name: "/usr/bin/git")
    monkeypatch.setattr(wave_sha.subprocess, "run", fake_run)

    sha = wave_sha.derive_wave_sha("P30-I19-W10", repo_root=Path("/tmp"))

    assert sha == "cafef00d"
    assert captured.get("stdin") is subprocess.DEVNULL
    assert captured.get("capture_output") is True
    if sys.platform != "win32":
        assert captured.get("start_new_session") is True
    else:
        assert captured.get("creationflags") == getattr(subprocess, "CREATE_NO_WINDOW", 0)


def test_detached_subprocess_kwargs_platform_shape() -> None:
    """The shared helper returns the platform-appropriate detach knobs.

    POSIX yields ``{"start_new_session": True}`` (a new session leader has no
    controlling terminal); win32 yields ``{"creationflags": CREATE_NO_WINDOW}``
    (no console window). The helper carries ONLY the detach knobs so callers
    keep ``stdin=DEVNULL`` + ``capture_output`` visible at the call site.
    """
    kwargs = detached_subprocess_kwargs()
    if sys.platform == "win32":
        assert kwargs == {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        assert "start_new_session" not in kwargs
    else:
        assert kwargs == {"start_new_session": True}
        assert "creationflags" not in kwargs


def test_detached_tty_stdin_is_gone() -> None:
    """The process-global fd-0 redirect is removed from the doctor checks module.

    Re-introducing ``detached_tty_stdin`` would re-open the live-App crash the
    W11 fix closed (``os.dup2`` over fd 0 corrupts Textual's stdin reader). The
    isolation lives at each subprocess (``stdin=subprocess.DEVNULL``) instead,
    so the symbol must NOT exist on the checks module.
    """
    assert not hasattr(checks, "detached_tty_stdin")
