"""``eawf hook run permission_request`` hands Claude a principal's recorded decision.

RUN-051: a provider permission is a daemon-owned record a principal decides through
``runtime.permission.decide``. Claude's ``PermissionRequest`` hook records the held call,
then reads the record back for up to ``runtime.claude.permission_wait_s`` seconds. A
decision recorded in that window is printed as the hook's
``hookSpecificOutput.decision.behavior``; past it, on a daemon error, or when the call
could not be bound to one Run, nothing is printed and Claude's own prompt decides. The
hook never denies by timing out.

The daemon is a stub answering the two verbs the hook calls, with a decision scheduled
at a monotonic offset, so each case is driven by the configured wait rather than by the
real daemon's clock.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.config.layered import resolve_permission_wait_seconds
from eawf.kernel.config.schema import DEFAULT_PERMISSION_WAIT_SECONDS, MAX_PERMISSION_WAIT_SECONDS
from eawf.runtime.hooks import runner as hook_runner
from eawf.runtime.hooks.runner import HookResult, host_permission_decision
from eawf.surfaces.cli.app import app

pytestmark = pytest.mark.integration

RUN: Final = "urn:eawf:ws:proj/run/RUN-0001"
REQUEST: Final = "runtime.host.permission.request"
READ: Final = "runtime.permission.read"
PAYLOAD: Final[dict[str, Any]] = {
    "hook_event_name": "PermissionRequest",
    "session_id": "host-session-0001",
    "tool_name": "Bash",
    "tool_input": {"command": "pytest -q", "description": "Run the test suite"},
}

#: The wait each timed case configures, and the slack a fall-through may take past it.
WAIT_S: Final = 2
SLACK_S: Final = 1.0


class _Daemon:
    """The two verbs the hook calls, over one permission a principal may decide.

    Attributes:
        calls: Every verb called, in order.
        decision: The resolution a principal records, ``decide_after`` seconds after the
            call is recorded.
        read_error: What every read raises instead of answering, when set.
    """

    def __init__(self, *, decision: str | None = None, decide_after: float = 0.0) -> None:
        self.calls: list[str] = []
        self.decision = decision
        self._decide_after = decide_after
        self._decide_at = float("inf")
        self.read_error: Exception | None = None

    def permission(self) -> dict[str, Any]:
        """Return the record as the daemon holds it now."""
        decided = self.decision is not None and time.monotonic() >= self._decide_at
        resolution = {"decision": self.decision} if decided else None
        return {"key": "PERM-0001", "run_ref": RUN, "resolution": resolution}

    def __call__(self) -> _Daemon:
        return self

    def __enter__(self) -> _Daemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(method)
        if method == REQUEST:
            self._decide_at = time.monotonic() + self._decide_after
            return {"permission": self.permission()}
        assert params["urn"] == RUN
        if self.read_error is not None:
            raise self.read_error
        return {"permissions": [{"permission": self.permission()}]}


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose Claude hook waits ``WAIT_S`` and re-reads every 50 ms."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(hook_runner, "_DECISION_POLL_S", 0.05)
    root = tmp_path / "demo"
    configure(root, WAIT_S)
    return root


def configure(root: Path, wait: object) -> None:
    """Write *wait* as the repository layer's ``runtime.claude.permission_wait_s``."""
    path = root / ".ea" / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump({"runtime": {"claude": {"permission_wait_s": wait}}}), encoding="utf-8"
    )


def run_hook(root: Path, daemon: Any, monkeypatch: pytest.MonkeyPatch) -> tuple[int, str, float]:
    """Run the Claude permission hook through the CLI; return its exit, stdout and seconds."""
    monkeypatch.setattr(hook_runner, "_default_daemon_client_factory", daemon)
    started = time.monotonic()
    result = CliRunner().invoke(
        app,
        ["-w", str(root), "hook", "run", "permission_request", "--runtime", "claude"],
        input=json.dumps(PAYLOAD),
    )
    return result.exit_code, result.stdout, time.monotonic() - started


def test_run_051_a_decision_inside_the_wait_is_handed_to_the_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon(decision="approved", decide_after=0.2)

    code, stdout, _elapsed = run_hook(repo, daemon, monkeypatch)

    assert code == 0
    assert json.loads(stdout) == {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }
    assert daemon.calls[0] == REQUEST
    assert set(daemon.calls[1:]) == {READ}


def test_run_051_a_denial_is_handed_to_the_host_with_its_reason(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, stdout, _elapsed = run_hook(repo, _Daemon(decision="denied"), monkeypatch)

    decision = json.loads(stdout)["hookSpecificOutput"]["decision"]
    assert code == 0
    assert decision["behavior"] == "deny"
    assert decision["message"]


def test_run_051_no_decision_inside_the_wait_leaves_the_host_prompt_to_decide(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon()

    code, stdout, elapsed = run_hook(repo, daemon, monkeypatch)

    assert (code, stdout) == (0, "")
    assert WAIT_S <= elapsed < WAIT_S + SLACK_S
    # a short poll interval, not a spin: roughly one read per interval over the wait
    assert 2 <= daemon.calls.count(READ) <= WAIT_S / 0.05 + 2


def test_run_051_a_decision_after_the_wait_is_recorded_but_never_handed_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon(decision="approved", decide_after=WAIT_S + SLACK_S)

    code, stdout, _elapsed = run_hook(repo, daemon, monkeypatch)
    time.sleep(2 * SLACK_S)

    assert (code, stdout) == (0, "")
    assert daemon.permission()["resolution"] == {"decision": "approved"}


def test_run_051_an_expiry_is_never_handed_back_as_a_denial(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, stdout, elapsed = run_hook(repo, _Daemon(decision="expired"), monkeypatch)

    assert (code, stdout) == (0, "")
    assert elapsed < WAIT_S


def test_run_051_a_daemon_that_is_down_falls_through_at_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def down() -> Any:
        raise ConnectionRefusedError("no daemon")

    code, stdout, elapsed = run_hook(repo, down, monkeypatch)

    assert (code, stdout) == (0, "")
    assert elapsed < SLACK_S


def test_run_051_a_daemon_error_mid_wait_falls_through_at_once(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon()
    daemon.read_error = TimeoutError("daemon call exceeded timeout")

    code, stdout, elapsed = run_hook(repo, daemon, monkeypatch)

    assert (code, stdout) == (0, "")
    assert elapsed < SLACK_S
    assert daemon.calls == [REQUEST, READ]


def test_run_051_a_call_bound_to_no_run_falls_through(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    daemon = _Daemon()

    def refused(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError("identity_not_found: 0 live Runs are on the host session")

    daemon.call = refused  # type: ignore[method-assign]

    assert run_hook(repo, daemon, monkeypatch)[:2] == (0, "")


def test_run_051_a_zero_wait_records_the_call_and_never_reads_back(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(repo, 0)
    daemon = _Daemon(decision="approved", decide_after=0.2)

    assert run_hook(repo, daemon, monkeypatch)[:2] == (0, "")
    assert daemon.calls == [REQUEST]


def test_run_051_the_wait_defaults_below_the_host_hook_window(tmp_path: Path) -> None:
    assert resolve_permission_wait_seconds(tmp_path) == DEFAULT_PERMISSION_WAIT_SECONDS
    assert 0 < DEFAULT_PERMISSION_WAIT_SECONDS < MAX_PERMISSION_WAIT_SECONDS < 60


@pytest.mark.parametrize("wait", [0, 1, MAX_PERMISSION_WAIT_SECONDS])
def test_run_051_a_wait_inside_the_window_is_read_as_set(tmp_path: Path, wait: int) -> None:
    configure(tmp_path, wait)

    assert resolve_permission_wait_seconds(tmp_path) == wait


@pytest.mark.parametrize("wait", [-1, MAX_PERMISSION_WAIT_SECONDS + 1, "20", 2.5])
def test_run_051_a_wait_outside_the_window_is_refused(tmp_path: Path, wait: object) -> None:
    configure(tmp_path, wait)

    with pytest.raises(ValidationError):
        resolve_permission_wait_seconds(tmp_path)


def test_run_051_a_malformed_wait_never_blocks_the_host(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure(repo, MAX_PERMISSION_WAIT_SECONDS + 1)
    daemon = _Daemon(decision="approved")

    assert run_hook(repo, daemon, monkeypatch)[:2] == (0, "")
    assert daemon.calls == []


@pytest.mark.parametrize(
    ("results", "expected"),
    [
        ((), None),
        ((HookResult(name="runtime.host_permission", output=""),), None),
        ((HookResult(name="runtime.host_permission", output="runtime.host_permission"),), None),
        (
            (HookResult(name="runtime.host_permission", output="runtime.host_permission ok x"),),
            None,
        ),
        (
            (
                HookResult(
                    name="runtime.host_permission",
                    output="runtime.host_permission approved permission=PERM-0001",
                ),
            ),
            "approved",
        ),
        (
            (
                HookResult(
                    name="runtime.host_permission",
                    output="runtime.host_permission denied permission=PERM-0001",
                ),
            ),
            "denied",
        ),
        ((HookResult(name="runtime.host_permission", output="KeyError('a approved')"),), None),
        ((HookResult(name="runtime.capture", output="runtime.host_permission approved"),), None),
    ],
)
def test_run_051_only_the_permission_hook_carries_a_decision(
    results: tuple[HookResult, ...], expected: str | None
) -> None:
    assert host_permission_decision(results) == expected
