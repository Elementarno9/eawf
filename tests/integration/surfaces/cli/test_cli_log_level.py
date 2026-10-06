"""The CLI's stderr log sink stays quiet below WARNING.

The installed ``eawf`` entry point attaches a stderr handler before the app
runs; at INFO it printed daemon and library chatter on every command. These
tests run the real entry point in a subprocess, so the handler is the one
``main`` installs, not the one pytest's capture provides.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from eawf import __version__
from eawf.observability.logging.scrub import configure_cli_logging
from eawf.runtime.daemon import lifecycle
from eawf.runtime.daemon.lifecycle import DaemonLifecycleError, DaemonLifecycleResult
from eawf.surfaces.cli._daemon_client import DaemonClient
from eawf.surfaces.cli.errors import DaemonVersionMismatch

pytestmark = pytest.mark.integration


def _run_eawf(*args: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run the installed CLI entry point with the test's isolated environment."""
    return subprocess.run(
        [sys.executable, "-m", "eawf", *args],
        cwd=cwd,
        env=dict(os.environ),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _info_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if " INFO " in line]


def test_init_quick_writes_no_info_line_to_stderr(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "pyproject.toml").write_text("[project]\nname = 'demo'\n", encoding="utf-8")
    result = _run_eawf("init", "--quick", "--target", str(repo), cwd=repo)
    assert result.returncode == 0, result.stderr
    assert (repo / ".ea").is_dir()
    assert _info_lines(result.stderr) == []


def test_a_failing_command_still_writes_no_info_line(tmp_path: Path) -> None:
    """The error path of a verb keeps the sink at WARNING too."""
    result = _run_eawf("status", "--no-such-flag", cwd=tmp_path)
    assert result.returncode != 0
    assert _info_lines(result.stderr) == []


@pytest.fixture
def _bare_root_logger() -> Iterator[logging.Logger]:
    """Save and restore the root logger around a test that strips its handlers.

    pytest attaches its capture handlers once the test body starts, so the
    body itself clears them before exercising the sink.
    """
    root = logging.getLogger()  # noqa: EAWF003 (root-logger handler config, not library acquisition)
    saved_handlers = root.handlers[:]
    saved_level = root.level
    try:
        yield root
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


def test_configure_logging_sets_the_root_level_to_warning(
    _bare_root_logger: logging.Logger,
) -> None:
    _bare_root_logger.handlers.clear()
    configure_cli_logging()
    assert _bare_root_logger.level == logging.WARNING
    assert len(_bare_root_logger.handlers) == 1
    assert _bare_root_logger.handlers[0].level == logging.NOTSET


def test_configure_logging_leaves_a_preconfigured_root_alone(
    _bare_root_logger: logging.Logger,
) -> None:
    existing = logging.NullHandler()
    _bare_root_logger.handlers.clear()
    _bare_root_logger.addHandler(existing)
    _bare_root_logger.setLevel(logging.DEBUG)
    configure_cli_logging()
    assert _bare_root_logger.handlers == [existing]
    assert _bare_root_logger.level == logging.DEBUG


def _stale_client(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, versions: list[str]
) -> DaemonClient:
    """Return a client whose pings answer ``versions`` in order, with no socket."""
    client = DaemonClient(runtime_dir=tmp_path)
    answers = iter(versions)
    monkeypatch.setattr(client, "call", lambda method, *a, **kw: {"version": next(answers)})
    monkeypatch.setattr(client, "_connect_socket", lambda: None)
    return client


def test_stale_daemon_is_refused_not_restarted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _stale_client(tmp_path, monkeypatch, versions=["0.6.8", __version__])
    restarts: list[object] = []
    monkeypatch.setattr(lifecycle, "restart_daemon", lambda **kw: restarts.append(kw))
    with caplog.at_level(logging.DEBUG), pytest.raises(DaemonVersionMismatch):
        client._ensure_current_daemon()
    assert restarts == []
    assert "daemon restarted stale" not in caplog.text


def test_restarted_stale_not_logged_when_the_daemon_is_current(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _stale_client(tmp_path, monkeypatch, versions=[__version__])
    with caplog.at_level(logging.DEBUG):
        client._ensure_current_daemon()
    assert "daemon restarted stale" not in caplog.text


def test_restarted_stale_not_logged_when_the_restart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    client = _stale_client(tmp_path, monkeypatch, versions=["0.6.8"])

    def _fail(**_: object) -> DaemonLifecycleResult:
        raise DaemonLifecycleError("boom")

    monkeypatch.setattr(lifecycle, "restart_daemon", _fail)
    with caplog.at_level(logging.DEBUG), pytest.raises(DaemonVersionMismatch):
        client._ensure_current_daemon()
    assert "daemon restarted stale" not in caplog.text
