"""Unit tests for the Doctor-mode probe stdin isolation (P30-I16-W29, P30-I18-W11).

The Doctor-mode health gather fans out to blocking subprocesses (the
instrument version-probes + the per-wave ``git log`` drift scan). Inside the
live TUI the parent fd 0 is the controlling TTY; a child that inherited it
could touch the TTY and solicit an escape-sequence reply that the App's stdin
reader then mis-parses as a synthetic key (``c`` -> config, ``?`` -> help) the
instant the operator enters Doctor mode.

W29 first closed this by re-pointing the PROCESS-GLOBAL fd 0 at ``/dev/null``
for the gather's duration -- but that ``os.dup2`` over fd 0 corrupted the live
App's asyncio stdin reader and crashed the running TUI with
``OSError: [Errno 9] Bad file descriptor`` (the reader already held the
original fd). W11 moves the isolation to the correct seam: each probe
``subprocess.run`` call site passes ``stdin=subprocess.DEVNULL`` directly, so
its child inherits a dead stdin and can solicit no TTY reply WITHOUT ever
touching the App's own fd 0.

These tests pin the per-subprocess isolation contract:

* the instrument version-probe (:func:`eawf.platform.install.instrument_probe.probe_one`)
  passes ``stdin=subprocess.DEVNULL`` to ``subprocess.run``;
* the per-wave ``git log`` drift scan
  (:func:`eawf.workflow.lifecycle.wave_sha.build_wave_sha_index`) passes
  ``stdin=subprocess.DEVNULL`` to ``subprocess.run``;
* the process-global ``detached_tty_stdin`` redirect is GONE from the doctor
  checks module (re-introducing it would re-open the live-App crash).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.doctor import checks
from eawf.platform.install import instrument_probe
from eawf.platform.install.instrument_probe import InstrumentSpec, probe_one
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


def test_wave_sha_drift_scan_passes_devnull_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-wave ``git log`` drift scan runs with ``stdin=DEVNULL``.

    The drift scan is the second TTY-touching probe the Doctor gather fans out
    to; under the live TUI its child must also inherit a dead stdin. Mock
    ``shutil.which`` (so git resolves) and capture the ``subprocess.run`` kwargs.
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


def test_detached_tty_stdin_is_gone() -> None:
    """The process-global fd-0 redirect is removed from the doctor checks module.

    Re-introducing ``detached_tty_stdin`` would re-open the live-App crash the
    W11 fix closed (``os.dup2`` over fd 0 corrupts Textual's stdin reader). The
    isolation lives at each subprocess (``stdin=subprocess.DEVNULL``) instead,
    so the symbol must NOT exist on the checks module.
    """
    assert not hasattr(checks, "detached_tty_stdin")
