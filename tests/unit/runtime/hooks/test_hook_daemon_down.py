"""A daemon that is not running is an expected skip, and asks for one background start.

Hooks only attach to a running daemon, and the daemon exits when idle, so
after any pause the next hook finds none. That is no failure: nothing is
recorded and nothing is printed, while a running daemon that refuses or does
not answer a call still leaves one health row. A hook that found no daemon in
a managed tree asks for one detached start, at most once a minute across
processes. The launcher is stubbed, and fails the test if a hook waits on it.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import socket
import subprocess  # noqa: EAWF024 the wrapper's trap and walk exist only under bash
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import portalocker
import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the hook's exit and output are the contract

from eawf.kernel.state.resolve import home_state
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.runtime.hooks import daemon_start
from eawf.runtime.hooks import runner as runner_module
from eawf.runtime.hooks.context import HookContext, HookContextKind
from eawf.runtime.hooks.daemon_start import (
    DAEMON_SEEN_MARKER,
    DAEMON_START_HOOK,
    DAEMON_START_INTERVAL_S,
    DAEMON_START_STAMP,
    DAEMON_START_UNANSWERED_CODE,
    DAEMON_UNANSWERED_MARKER,
    HOOKS_DIR,
    SESSION_ENV_DROPPED,
    UNANSWERED_REPEAT_S,
    claim_daemon_start,
    daemon_start_argv,
    daemon_start_env,
)
from eawf.runtime.hooks.health import (
    DAEMON_CALL_FAILED,
    HEALTH_RECORD,
    HEALTH_ROTATED,
    HOOK_RAISED,
    HookFailure,
    read_cursor,
    read_health_rows,
    record_failures,
    write_cursor,
)
from eawf.runtime.runtimes.claude.plugin_package import package_plugin
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonNotRunningError, DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import hook as hook_command
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.workflow.skills.catalog import NOT_MANAGED_REFUSAL
from tests._linked_worktree import git

_ALL_EVENTS = (
    "session_start",
    "user_prompt_submit",
    "pre_tool_use",
    "post_tool_use",
    "post_tool_use_failure",
    "permission_request",
    "subagent_start",
    "subagent_stop",
    "pre_compact",
    "session_end",
)
_TOOL_EVENTS = ("pre_tool_use", "post_tool_use", "post_tool_use_failure")


class _Launches:
    """Stands in for ``subprocess.Popen`` and records every launch."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self._error = error

    def __call__(self, argv: list[str], **kwargs: Any) -> _Launches:
        if self._error is not None:
            raise self._error
        self.calls.append((argv, kwargs))
        return self

    def wait(self, *_args: object, **_kwargs: object) -> None:
        raise AssertionError("the hook waited on the daemon start")

    communicate = wait


def _not_running() -> Any:
    raise DaemonNotRunningError("no eawfd daemon is running")


class _Answering:
    """A daemon client context whose every call answers, or raises *error*."""

    def __init__(self, error: Exception | None = None) -> None:
        self._error = error

    def __enter__(self) -> _Answering:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, _method: str, _params: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {}


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _not_running)
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    (root / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    return root


def _context(monkeypatch: pytest.MonkeyPatch, tree: Path, kind: HookContextKind) -> None:
    monkeypatch.setattr(
        "eawf.runtime.hooks.context.resolve_hook_context",
        lambda _cwd: HookContext(kind=kind, top=tree, main=tree),
    )


def _launcher(monkeypatch: pytest.MonkeyPatch, error: Exception | None = None) -> _Launches:
    launches = _Launches(error)
    monkeypatch.setattr(daemon_start, "Popen", launches)
    return launches


def _payload(tree: Path | None, event: str) -> dict[str, Any]:
    payload: dict[str, Any] = {"session_id": "s1", "tool_name": "Bash"}
    payload["tool_input"] = {"command": "ls"}
    if event == "user_prompt_submit":
        payload = {"session_id": "s1", "prompt": "hello"}
    if tree is not None:
        payload["cwd"] = str(tree)
    return payload


def _hook_run(tree: Path, event: str, *, named: bool = True, cwd: bool = True) -> Any:
    head = ["--workspace", str(tree)] if named else []
    return CliRunner().invoke(
        app,
        [*head, "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(_payload(tree if cwd else None, event)),
    )


def _rows(tree: Path) -> list[tuple[str, str, str]]:
    return [(r.hook, r.code, r.error) for r in read_health_rows(tree / ".ea").rows]


# ---- a daemon that is not running is no failure ---------------------------------------------


def _short_dir() -> Iterator[Path]:
    # AF_UNIX paths are capped near 104 bytes, past what a pytest tmp_path leaves.
    directory = Path(tempfile.mkdtemp(prefix="eawf-dd-", dir="/tmp"))
    try:
        yield directory
    finally:
        shutil.rmtree(directory, ignore_errors=True)


@pytest.mark.parametrize(
    "case",
    [*(f"down-{event}" for event in _ALL_EVENTS), "rpc-error", "timeout", "stale-socket", "silent"],
)
def test_daemon_down_is_not_a_failure(
    tree: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    _context(monkeypatch, tree, "managed")
    _launcher(monkeypatch)
    if case.startswith("down-"):
        event = case.removeprefix("down-")

        result = _hook_run(tree, event)

        assert result.exit_code == 0, result.output
        assert _rows(tree) == []
        assert "DaemonNotRunning" not in result.output
        assert DAEMON_CALL_FAILED not in result.output
        if event in _TOOL_EVENTS:
            assert (result.stdout, result.stderr) == ("", "")
        return
    if case in {"rpc-error", "timeout"}:
        error: Exception = (
            DaemonRpcError(-32000, "refused")
            if case == "rpc-error"
            else TimeoutError("daemon call exceeded timeout")
        )
        monkeypatch.setattr(
            runner_module, "_default_daemon_client_factory", lambda: _Answering(error)
        )

        result = _hook_run(tree, "post_tool_use")

        assert result.exit_code == 0, result.output
        assert _rows(tree) == [("runtime.host_tool", DAEMON_CALL_FAILED, type(error).__name__)]
        return
    # The client says "not running" only when no listener takes a connection.
    for runtime_dir in _short_dir():
        sock_path = runtime_dir / "eawfd.sock"
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(str(sock_path))
        if case == "stale-socket":
            listener.close()
            with pytest.raises(DaemonNotRunningError):
                DaemonClient(runtime_dir=runtime_dir, spawn=False).__enter__()
            return
        # A saturated daemon: it takes the connection and answers nothing, and
        # its PID file is missing, so the ping cannot name it.
        listener.listen(8)
        try:
            client = DaemonClient(runtime_dir=runtime_dir, spawn=False, call_timeout_seconds=0.2)
            with pytest.raises(OSError) as raised:
                client.__enter__()
            assert not isinstance(raised.value, DaemonNotRunningError)
        finally:
            listener.close()


# ---- one rate-limited background start --------------------------------------------------------


def _answering_daemon() -> Any:
    return _Answering()


@dataclass(frozen=True)
class _Scene:
    """What a rate-limit scenario works on."""

    tree: Path
    monkeypatch: pytest.MonkeyPatch
    tmp_path: Path
    launches: _Launches

    @property
    def stamp(self) -> Path:
        return self.tree / ".ea" / DAEMON_START_STAMP


def _age(stamp: Path, seconds: float) -> None:
    at = time.time() - seconds
    os.utime(stamp, (at, at))


def _once_a_minute(s: _Scene) -> None:
    for _ in range(3):
        assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert len(s.launches.calls) == 1
    _age(s.stamp, DAEMON_START_INTERVAL_S + 1)
    assert _hook_run(s.tree, "pre_tool_use").exit_code == 0
    assert len(s.launches.calls) == 2


def _wrapper_071(s: _Scene) -> None:
    # The 0.7.1 wrapper passes no -w; the payload's cwd names the tree.
    result = _hook_run(s.tree, "post_tool_use", named=False)
    assert result.exit_code == 0, result.output
    assert [argv for argv, _ in s.launches.calls] == [daemon_start_argv(s.tree)]


def _process_cwd_only(s: _Scene) -> None:
    # Only a cwd the host reported may start a daemon; the process's own
    # directory is a test's or a shell's, never a session's.
    s.monkeypatch.chdir(s.tree)
    for event in ("session_start", "post_tool_use"):
        assert _hook_run(s.tree, event, named=False, cwd=False).exit_code == 0
    assert s.launches.calls == []


def _unmanaged(kind: HookContextKind) -> Callable[[_Scene], None]:
    def _scene(s: _Scene) -> None:
        _context(s.monkeypatch, s.tree, kind)
        assert _hook_run(s.tree, "post_tool_use", named=False).exit_code == 0
        assert s.launches.calls == []

    return _scene


def _daemon(factory: Callable[[], Any]) -> Callable[[_Scene], None]:
    def _scene(s: _Scene) -> None:
        s.monkeypatch.setattr(runner_module, "_default_daemon_client_factory", factory)
        assert _hook_run(s.tree, "post_tool_use").exit_code == 0
        assert s.launches.calls == []
        assert not s.stamp.exists()

    return _scene


def _daemonless(s: _Scene) -> None:
    s.monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    for event in ("session_start", "post_tool_use"):
        assert _hook_run(s.tree, event).exit_code == 0
    assert s.launches.calls == []


def _launch_fails(s: _Scene) -> None:
    ok = _hook_run(s.tree, "post_tool_use")
    _launcher(s.monkeypatch, OSError("no interpreter"))
    s.stamp.unlink()
    broken = _hook_run(s.tree, "post_tool_use")
    again = _hook_run(s.tree, "post_tool_use")
    assert (ok.exit_code, broken.exit_code, again.exit_code) == (0, 0, 0)
    assert (ok.stdout, broken.stdout) == ("", "")
    assert _rows(s.tree) == [(DAEMON_START_HOOK, HOOK_RAISED, "OSError")]


def _session_start_stamps(s: _Scene) -> None:
    assert _hook_run(s.tree, "session_start").exit_code == 0
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert len(s.launches.calls) == 1
    assert s.stamp.is_file()


def _stamp_path(_s: _Scene) -> None:
    # Every start file is named in daemon_start beside the health record.
    assert HEALTH_RECORD.parent == HOOKS_DIR
    names = {p.name for p in (DAEMON_START_STAMP, DAEMON_SEEN_MARKER, DAEMON_UNANSWERED_MARKER)}
    assert names == {"daemon-start.stamp", "daemon-seen", "daemon-start.unanswered"}
    assert {
        p.parent for p in (DAEMON_START_STAMP, DAEMON_SEEN_MARKER, DAEMON_UNANSWERED_MARKER)
    } == {HOOKS_DIR}
    assert pytest.approx(60.0) == DAEMON_START_INTERVAL_S
    assert pytest.approx(86400.0) == UNANSWERED_REPEAT_S


def _race(ea: Path) -> list[bool]:
    """Let eight threads claim *ea*'s start at once; return each one's answer."""
    won: list[bool] = []
    barrier = threading.Barrier(8)

    def _claim() -> None:
        barrier.wait()
        won.append(claim_daemon_start(ea))

    threads = [threading.Thread(target=_claim) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        # A ceiling against a hung claim, not a budget: it fires only if a claim never returns.
        thread.join(timeout=10)
        assert not thread.is_alive(), "a start claim never returned"
    return won


def _claim_race(s: _Scene) -> None:
    ea = s.tree / ".ea"
    for setup in ("fresh", "stale"):
        if setup == "stale":
            _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
        assert sorted(_race(ea)) == [False] * 7 + [True], setup
        # Only the stamp and the stale path's lock file, which is never removed.
        left = {p.name for p in s.stamp.parent.iterdir()}
        assert s.stamp.name in left
        assert left <= {"daemon-start.stamp", "daemon-start.stamp.lock"}, setup


def _claim_race_repeated(s: _Scene) -> None:
    # A double winner showed up in about one stale race in fifty.
    ea = s.tree / ".ea"
    assert claim_daemon_start(ea) is True
    for round_ in range(200):
        _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
        assert sorted(_race(ea)) == [False] * 7 + [True], round_


def _claim_interleaving(s: _Scene) -> None:
    # B looks at a stale stamp; A renews it before B acts on the look; C
    # claims while B acts on it. Each of B's freshness looks lets the next
    # of A and C run a whole claim right after the look.
    ea = s.tree / ".ea"
    assert claim_daemon_start(ea) is True
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    real_fresh = daemon_start._fresh
    others = ["A", "C"]
    nested: list[str] = []
    won: dict[str, bool] = {}

    def _look(stamp: Path) -> bool:
        fresh = real_fresh(stamp)
        if others and not nested:
            nested.append(others.pop(0))
            won[nested[0]] = claim_daemon_start(ea)
            nested.clear()
        return fresh

    s.monkeypatch.setattr(daemon_start, "_fresh", _look)
    won["B"] = claim_daemon_start(ea)
    assert won == {"A": True, "B": False, "C": False}


def _claim_lock_fails(s: _Scene) -> None:
    # A filesystem without file locks: the claimant loses and says so once.
    ea = s.tree / ".ea"
    assert claim_daemon_start(ea) is True
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)

    def _refuse(_file: object, _flags: object) -> None:
        raise portalocker.LockException(OSError(errno.ENOLCK, "No locks available"))

    s.monkeypatch.setattr(portalocker, "lock", _refuse)
    s.monkeypatch.setattr(daemon_start, "_warned_no_start", False)
    warnings: list[str] = []
    s.monkeypatch.setattr(daemon_start.sys.stderr, "write", warnings.append)
    assert claim_daemon_start(ea) is False
    assert claim_daemon_start(ea) is False
    assert len(warnings) == 1
    assert "no background daemon start" in warnings[0]
    assert "local/hooks/daemon-start.stamp.lock cannot be locked (LockException)" in warnings[0]
    assert str(s.tree) not in warnings[0]


def _claim_stamp_vanishes(s: _Scene) -> None:
    # The stamp is removed between the look under the lock and the re-date:
    # the claimant loses without a warning, and the next claim creates it.
    ea = s.tree / ".ea"
    assert claim_daemon_start(ea) is True
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    real_fresh = daemon_start._fresh
    looks: list[Path] = []

    def _look(stamp: Path) -> bool:
        fresh = real_fresh(stamp)
        looks.append(stamp)
        if len(looks) == 2:
            stamp.unlink()
        return fresh

    s.monkeypatch.setattr(daemon_start, "_fresh", _look)
    s.monkeypatch.setattr(daemon_start, "_warned_no_start", False)
    warnings: list[str] = []
    s.monkeypatch.setattr(daemon_start.sys.stderr, "write", warnings.append)
    assert claim_daemon_start(ea) is False
    assert len(looks) == 2
    assert warnings == []
    assert claim_daemon_start(ea) is True
    assert s.stamp.is_file()


def _claim_boundaries(s: _Scene) -> None:
    ea = s.tree / ".ea"
    # No .ea, nothing to stamp: no claim, and no directory is created.
    assert claim_daemon_start(s.tmp_path / "absent" / ".ea") is False
    assert not (s.tmp_path / "absent").exists()
    assert claim_daemon_start(ea) is True
    assert claim_daemon_start(ea) is False
    _age(s.stamp, DAEMON_START_INTERVAL_S - 2)
    assert claim_daemon_start(ea) is False
    _age(s.stamp, DAEMON_START_INTERVAL_S + 1)
    assert claim_daemon_start(ea) is True
    # A stamp from the future (a clock set back) must not block starts for an hour.
    _age(s.stamp, -3600)
    assert claim_daemon_start(ea) is True


_SCENES: dict[str, Callable[[_Scene], None]] = {
    "once-a-minute": _once_a_minute,
    "071-wrapper": _wrapper_071,
    "process-cwd-only": _process_cwd_only,
    "unmanaged-git": _unmanaged("unmanaged-git"),
    "no-git": _unmanaged("no-git"),
    "daemon-up": _daemon(_answering_daemon),
    "daemon-refused": _daemon(lambda: _refusing()),
    "daemonless": _daemonless,
    "launch-fails": _launch_fails,
    "session-start-stamps": _session_start_stamps,
    "stamp-path": _stamp_path,
    "claim-race": _claim_race,
    "claim-race-repeated": _claim_race_repeated,
    "claim-interleaving": _claim_interleaving,
    "claim-lock-fails": _claim_lock_fails,
    "claim-stamp-vanishes": _claim_stamp_vanishes,
    "claim-boundaries": _claim_boundaries,
}


@pytest.mark.parametrize(
    "case",
    [*(f"start-{e}" for e in (*_TOOL_EVENTS, "permission_request", "pre_compact")), *_SCENES],
)
def test_rate_limited_start(
    tree: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    _context(monkeypatch, tree, "managed")
    scene = _Scene(tree, monkeypatch, tmp_path, _launcher(monkeypatch))
    if case in _SCENES:
        _SCENES[case](scene)
        return
    result = _hook_run(tree, case.removeprefix("start-"))

    assert result.exit_code == 0, result.output
    ((argv, kwargs),) = scene.launches.calls
    assert argv == daemon_start_argv(tree)
    assert kwargs["env"]["EA_STATE"] == str(tree / ".ea" / "state.json")
    assert kwargs["start_new_session"] is True
    assert scene.stamp.is_file()
    assert _rows(tree) == []


def _refusing() -> Any:
    raise ConnectionRefusedError("refused")


# ---- a start nobody answered, an unwritable directory, the child's environment ----------------


def _unanswered_rows(tree: Path) -> list[tuple[str, str, str]]:
    return [row for row in _rows(tree) if row[1] == DAEMON_START_UNANSWERED_CODE]


def _down(s: _Scene) -> None:
    s.monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _not_running)


def _up(s: _Scene) -> None:
    s.monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _answering_daemon)


def _never_answered(s: _Scene) -> None:
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert _unanswered_rows(s.tree) == []
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    result = _hook_run(s.tree, "post_tool_use")
    assert result.exit_code == 0
    assert _unanswered_rows(s.tree) == [
        (DAEMON_START_HOOK, DAEMON_START_UNANSWERED_CODE, "DaemonStartUnanswered")
    ]
    (detail,) = [
        r.detail
        for r in read_health_rows(s.tree / ".ea").rows
        if r.code == DAEMON_START_UNANSWERED_CODE
    ]
    assert "interpreter=" in detail and " s ago" in detail
    assert len(s.launches.calls) == 2


def _answered_then_idled(s: _Scene) -> None:
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    _up(s)
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert (s.tree / ".ea" / DAEMON_SEEN_MARKER).is_file()
    # The daemon idles out a while later; the next start owes nothing.
    _down(s)
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert _unanswered_rows(s.tree) == []
    assert len(s.launches.calls) == 2


def _one_row_per_outage(s: _Scene) -> None:
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    for _ in range(3):
        _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
        for event in ("post_tool_use", "pre_tool_use", "session_start"):
            assert _hook_run(s.tree, event).exit_code == 0
    # Three more unanswered intervals, one row: the outage is reported once a day.
    assert len(_unanswered_rows(s.tree)) == 1
    # After the daemon was reached once, the next unanswered start is a new outage.
    _up(s)
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    _down(s)
    # Backdated in the order they happen: report, contact, then a new start.
    _age(s.tree / ".ea" / DAEMON_UNANSWERED_MARKER, 300)
    _age(s.tree / ".ea" / DAEMON_SEEN_MARKER, 200)
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert len(_unanswered_rows(s.tree)) == 2
    # A day after the last report the same outage is reported again.
    _age(s.tree / ".ea" / DAEMON_UNANSWERED_MARKER, UNANSWERED_REPEAT_S + 1)
    _age(s.tree / ".ea" / DAEMON_SEEN_MARKER, UNANSWERED_REPEAT_S + 10)
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    assert len(_unanswered_rows(s.tree)) == 3


def _session_start_reports(s: _Scene) -> None:
    assert _hook_run(s.tree, "session_start").exit_code == 0
    _age(s.stamp, DAEMON_START_INTERVAL_S + 5)
    assert _hook_run(s.tree, "session_start").exit_code == 0
    assert len(_unanswered_rows(s.tree)) == 1


def _unwritable(s: _Scene) -> None:
    if os.name == "nt" or os.geteuid() == 0:
        pytest.skip("needs a POSIX permission the user cannot override")
    hooks = s.tree / ".ea" / HOOKS_DIR
    hooks.mkdir(parents=True)
    hooks.chmod(0o500)
    s.monkeypatch.setattr(daemon_start, "_warned_no_start", False)
    try:
        warnings: list[str] = []
        s.monkeypatch.setattr(daemon_start.sys.stderr, "write", warnings.append)
        assert claim_daemon_start(s.tree / ".ea") is False
        assert claim_daemon_start(s.tree / ".ea") is False
    finally:
        hooks.chmod(0o700)
    assert len(warnings) == 1
    assert "no background daemon start" in warnings[0]
    assert "local/hooks is not writable (PermissionError)" in warnings[0]
    assert str(s.tree) not in warnings[0]


def _child_env(s: _Scene) -> None:
    hook_env = dict.fromkeys(SESSION_ENV_DROPPED, "x")
    kept = {"PATH": "/bin", "TMPDIR": "/tmp", "EAWF_DAEMONLESS": "0", "CLAUDE_CONFIG_DIR": "c"}
    env = daemon_start_env({**hook_env, **kept}, s.tree)
    assert env == {**kept, "EA_STATE": str(s.tree / ".ea" / "state.json")}
    assert daemon_start_env({}, s.tree) == {"EA_STATE": str(s.tree / ".ea" / "state.json")}
    s.monkeypatch.setenv("CLAUDECODE", "1")
    s.monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(s.tree))
    assert _hook_run(s.tree, "post_tool_use").exit_code == 0
    ((_argv, kwargs),) = s.launches.calls
    assert not SESSION_ENV_DROPPED & set(kwargs["env"])
    assert {"CLAUDECODE", "CLAUDE_PROJECT_DIR", "CLAUDE_PLUGIN_ROOT"} <= SESSION_ENV_DROPPED


_START_SCENES: dict[str, Callable[[_Scene], None]] = {
    "never-answered": _never_answered,
    "answered-then-idled": _answered_then_idled,
    "one-row-per-outage": _one_row_per_outage,
    "session-start-reports": _session_start_reports,
    "unwritable": _unwritable,
    "child-env": _child_env,
}


@pytest.mark.parametrize("case", list(_START_SCENES))
def test_unanswered_start_and_child_env(
    tree: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, case: str
) -> None:
    _context(monkeypatch, tree, "managed")
    _START_SCENES[case](_Scene(tree, monkeypatch, tmp_path, _launcher(monkeypatch)))


# ---- the interpreter the start runs ------------------------------------------------------------


def _venv(main: Path, layout: str) -> Path | None:
    """Lay out *main*'s ``.venv``; return the interpreter it should run, if any."""
    scripts = main / ".venv" / ("Scripts" if layout.startswith("windows") else "bin")
    python = scripts / ("python.exe" if layout.startswith("windows") else "python")
    launcher = scripts / ("eawf.exe" if layout.startswith("windows") else "eawf")
    scripts.mkdir(parents=True)
    if layout != "launcher-only":
        python.write_text("", encoding="utf-8")
        python.chmod(0o755)
    if layout in {"python-only"}:
        return None
    launcher.write_text("", encoding="utf-8")
    launcher.chmod(0o644 if layout == "launcher-not-executable" else 0o755)
    if layout in {"launcher-only", "launcher-not-executable"}:
        return None
    return python


@pytest.mark.parametrize(
    "layout",
    [
        "none",
        "python-only",
        "posix",
        "windows",
        "launcher-only",
        "launcher-not-executable",
        "cannot-launch",
    ],
)
def test_start_interpreter_choice(
    tree: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, layout: str
) -> None:
    module = "eawf.runtime.daemon.main"
    if layout == "none":
        assert daemon_start_argv(tmp_path) == [sys.executable, "-m", module]
    elif layout == "cannot-launch":
        # What a detached child cannot report is known at spawn time: the
        # interpreter must exist. The real launcher raises, so one row.
        _context(monkeypatch, tree, "managed")
        monkeypatch.setattr(daemon_start.sys, "executable", str(tmp_path / "gone" / "python"))
        result = _hook_run(tree, "session_start")
        assert result.exit_code == 0, result.output
        assert _rows(tree) == [(DAEMON_START_HOOK, HOOK_RAISED, "FileNotFoundError")]
    else:
        if layout == "launcher-not-executable" and os.name == "nt":
            pytest.skip("Windows has no executable bit")
        expected = _venv(tmp_path, layout)
        interpreter = str(expected) if expected is not None else sys.executable
        assert daemon_start_argv(tmp_path) == [interpreter, "-m", module]


# ---- a nested clone, the wrapper's leftover file and the cursor ------------------------------

_STUB_UV = """#!/bin/bash
printf '%s\\n' "$*" >>"$EAWF_TEST_LOG"
sleep "${EAWF_TEST_SLEEP:-0}"
"""


def _nested(tmp_path: Path) -> tuple[Path, Path]:
    outer = (tmp_path / "outer").resolve()
    outer.mkdir()
    git(outer, "init", "-q")
    (outer / ".ea").mkdir()
    (outer / ".ea" / "state.json").write_text("{}", encoding="utf-8")
    inner = outer / "vendor" / "clone"
    inner.mkdir(parents=True)
    git(inner, "init", "-q")
    return outer, inner


def _wrapper_env(tmp_path: Path, **extra: str) -> dict[str, str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    uv = bin_dir / "uv"
    uv.write_text(_STUB_UV, encoding="utf-8")
    uv.chmod(0o755)
    eawf = bin_dir / "eawf"
    eawf.write_text("#!/bin/bash\n", encoding="utf-8")
    eawf.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    scratch = tmp_path / "scratch"
    scratch.mkdir(exist_ok=True)
    return {
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(scratch),
        "EAWF_TEST_LOG": str(tmp_path / "calls.log"),
        **extra,
    }


def _wrapper(tmp_path: Path) -> Path:
    target = tmp_path / "plugin" / "eawf"
    package_plugin(target, include_marketplace=False, include_readme=False, extra_tools={})
    return target / "hooks" / "eawf-hook.sh"


def _failure() -> HookFailure:
    at = datetime(2026, 11, 10, tzinfo=UTC)
    return HookFailure(
        event="post_tool_use", hook="h", code=HOOK_RAISED, error="E", detail="", at=at
    )


@pytest.mark.parametrize(
    "case", ["nested-wrapper", "nested-python", "nested-unmanaged", "term-trap", "legacy-cursor"]
)
def test_nested_clone_cleanup_and_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    if case == "legacy-cursor":
        ea = tmp_path / ".ea"
        record = ea / HEALTH_RECORD
        record.parent.mkdir(parents=True)
        (ea / HEALTH_RECORD.with_name("health.jsonl.1")).write_bytes(b"{}\n" * 3)
        (ea / HEALTH_ROTATED).write_bytes(b"{}\n" * 9)
        record.write_bytes(b"{}\n")
        write_cursor(ea, 9 * 3)

        record_failures(ea, [_failure()])

        # The adopted generation is reported; only the row written after it is not.
        assert read_cursor(ea) == 3 * 3 + 3
        assert len(read_health_rows(ea, offset=read_cursor(ea)).rows) == 1
        return
    if sys.platform == "win32":
        pytest.skip("the wrapper is a bash script")
    if case == "term-trap":
        wrapper = _wrapper(tmp_path)
        repo = (tmp_path / "repo").resolve()
        repo.mkdir()
        git(repo, "init", "-q")
        (repo / ".ea").mkdir()
        (repo / ".ea" / "state.json").write_text("{}", encoding="utf-8")
        env = _wrapper_env(tmp_path, EAWF_TEST_SLEEP="5")
        process = subprocess.Popen(
            ["/bin/bash", str(wrapper), "post_tool_use"],
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=repo,
            env=env,
            start_new_session=True,
            **no_window_kwargs(),
        )
        assert process.stdin is not None
        process.stdin.write(json.dumps({"cwd": str(repo)}).encode())
        process.stdin.close()
        log = tmp_path / "calls.log"
        deadline = time.monotonic() + 10
        while not log.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert list((tmp_path / "scratch").glob("eawf-hook.*")), "the wrapper made no stderr file"
        # The host stops a hook at its timeout by signalling its process group.
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=10)
        assert list((tmp_path / "scratch").glob("eawf-hook.*")) == []
        return
    outer, inner = _nested(tmp_path)
    if case == "nested-python":
        stdin = json.dumps({"cwd": str(inner), "prompt": "/eawf:plan x"})
        context = hook_command._hook_context(stdin, GlobalFlags(), "user_prompt_submit")
        state, found = home_state(inner)
        assert (context.kind, context.main) == ("managed", outer)
        assert (state, found) == (outer / ".ea" / "state.json", True)
        return
    wrapper = _wrapper(tmp_path)
    prompt = {"cwd": str(inner), "prompt": "/eawf:plan x"}
    if case == "nested-unmanaged":
        (outer / ".ea" / "state.json").unlink()
        (outer / ".ea").rmdir()
    completed = subprocess.run(
        ["/bin/bash", str(wrapper), "user_prompt_submit"],
        input=json.dumps(prompt),
        capture_output=True,
        text=True,
        cwd=inner,
        env=_wrapper_env(tmp_path),
        timeout=30,
        check=False,
        **no_window_kwargs(),
    )
    log = tmp_path / "calls.log"
    calls = log.read_text(encoding="utf-8").splitlines() if log.exists() else []
    assert completed.returncode == 0, completed.stderr
    if case == "nested-unmanaged":
        assert NOT_MANAGED_REFUSAL in completed.stdout
        assert calls == []
        return
    assert NOT_MANAGED_REFUSAL not in completed.stdout
    assert len(calls) == 1
    assert f"-w {outer} hook run user_prompt_submit" in calls[0]
