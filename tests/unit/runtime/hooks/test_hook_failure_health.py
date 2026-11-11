"""A hook handler failure stays fail-open but is recorded, named and counted.

A handler that raises, or whose daemon call fails, must not break the host
session, so the hook still exits 0; the failure lands as one row in the hook
health record, as one stderr line naming the event and error code, and as the
``hooks!<n>`` token on the status line's hooks segment.
"""

from __future__ import annotations

import json
import subprocess  # noqa: EAWF024 a linked worktree is built with git itself
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the exit code and stderr are the contract

from eawf.runtime.hooks import runner as runner_module
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.health import (
    DAEMON_CALL_FAILED,
    HEALTH_RECORD,
    HOOK_RAISED,
    HookFailure,
    failure_line,
    record_failures,
)
from eawf.runtime.hooks.runner import HookResult, HookRunner
from eawf.runtime.runtimes.claude.statusline_modules import hooks_plugins
from eawf.surfaces.cli.app import app

_TOOL_PAYLOAD = {"session_id": "s1", "tool_name": "Bash", "tool_response": {"stdout": "ok"}}


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Return a workspace with an ``.ea`` directory and no host plugin record."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / ".ea").mkdir()
    return tmp_path


def _refusing_daemon() -> Any:
    raise ConnectionRefusedError("no daemon")


def _hook_run(tree: Path, event: str, payload: dict[str, Any], runtime: str = "generic") -> Any:
    return CliRunner().invoke(
        app,
        ["--workspace", str(tree), "hook", "run", event, "--runtime", runtime],
        input=json.dumps(payload),
    )


def _rows(tree: Path) -> list[dict[str, Any]]:
    text = (tree / ".ea" / HEALTH_RECORD).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


def _segment_text(tree: Path) -> str:
    return hooks_plugins.build({}, tree / ".ea" / "state.json").text


def _failure(detail: str = "OSError('x')") -> HookFailure:
    return HookFailure(
        event="post_tool_use",
        hook="runtime.host_tool",
        code=DAEMON_CALL_FAILED,
        error="OSError",
        detail=detail,
        at=datetime(2026, 11, 6, tzinfo=UTC),
    )


def _event(event_type: HookEventType = HookEventType.SUBAGENT_STOP) -> HookEvent:
    return HookEvent(event_type=event_type, occurred_at=datetime.now(UTC))


def test_failed_daemon_call_exits_zero_records_one_row_and_shows_the_token(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)

    result = _hook_run(tree, "post_tool_use", _TOOL_PAYLOAD, runtime="claude")

    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == [
        "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool (ConnectionRefusedError)"
    ]
    rows = _rows(tree)
    assert [(row["event"], row["hook"], row["code"]) for row in rows] == [
        ("post_tool_use", "runtime.host_tool", DAEMON_CALL_FAILED)
    ]
    assert rows[0]["detail"] == "ConnectionRefusedError('no daemon')"
    assert _segment_text(tree).endswith(" hooks!1")


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_worktree_session_shows_the_failures_its_hook_recorded_in_the_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", "-b", "main")
    _git(main, "-c", "user.name=ci", "-c", "user.email=ci@example.com", "commit", "-q",
         "--allow-empty", "-m", "root")  # fmt: skip
    linked = tmp_path / "linked"
    _git(main, "worktree", "add", "-q", "--detach", str(linked))
    (main / ".ea").mkdir()
    (linked / ".ea").mkdir()

    result = _hook_run(linked, "post_tool_use", _TOOL_PAYLOAD, runtime="claude")

    assert result.exit_code == 0, result.output
    assert not (linked / ".ea" / HEALTH_RECORD).exists()
    assert len(_rows(main)) == 1
    assert _segment_text(linked).endswith(" hooks!1")


def test_raising_handler_exits_zero_and_each_run_adds_one_row(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _register(runner: HookRunner, **_kwargs: object) -> None:
        def _boom(event: HookEvent) -> HookResult:
            raise RuntimeError("handler broke")

        runner.register(HookEventType.SUBAGENT_STOP, _boom, name="test.boom")

    monkeypatch.setattr(runner_module, "register_runtime_capture_hooks", _register)

    first = _hook_run(tree, "subagent_stop", {})
    second = _hook_run(tree, "subagent_stop", {})

    assert (first.exit_code, second.exit_code) == (0, 0)
    assert first.stderr.splitlines() == [
        "eawf hook subagent_stop: hook_raised in test.boom (RuntimeError)"
    ]
    assert [row["code"] for row in _rows(tree)] == [HOOK_RAISED, HOOK_RAISED]
    assert json.loads(second.stdout)["body"]["blocked"] is False
    assert "hooks!2" in _segment_text(tree).split()


def test_unwritable_record_still_exits_zero_and_names_the_failure(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)
    (tree / ".ea" / HEALTH_RECORD).mkdir(parents=True)

    result = _hook_run(tree, "post_tool_use", _TOOL_PAYLOAD, runtime="claude")

    assert result.exit_code == 0, result.output
    lines = result.stderr.splitlines()
    assert lines[0].startswith("eawf hook: health record not written (")
    assert lines[1:] == [
        "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool (ConnectionRefusedError)"
    ]


def test_clean_run_records_nothing_and_shows_no_token(tree: Path) -> None:
    result = _hook_run(tree, "pre_commit", {})

    assert result.exit_code == 0, result.output
    assert result.stderr == ""
    assert not (tree / ".ea" / HEALTH_RECORD).exists()
    segment = hooks_plugins.build({}, tree / ".ea" / "state.json")
    assert "hooks!" not in segment.text
    assert segment.status == "missing"


def test_hook_outside_an_eawf_tree_writes_no_record(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", _refusing_daemon)

    result = _hook_run(tmp_path, "post_tool_use", _TOOL_PAYLOAD, runtime="claude")

    assert result.exit_code == 0, result.output
    assert result.stderr.splitlines() == [
        "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool (ConnectionRefusedError)"
    ]
    assert not (tmp_path / ".ea").exists()


def test_daemon_failure_a_handler_lets_propagate_counts_once() -> None:
    runner = HookRunner()
    factory = runner.recording_factory(_refusing_daemon)

    def _unguarded(event: HookEvent) -> HookResult:
        with factory() as client:
            client.call("never", {})
        return HookResult(name="unreached")

    runner.register(HookEventType.SUBAGENT_STOP, _unguarded, name="test.unguarded")
    results = runner.run_event(_event())

    assert [result.raised for result in results] == [True]
    assert [(f.hook, f.code, f.error) for f in runner.failures] == [
        ("test.unguarded", DAEMON_CALL_FAILED, "ConnectionRefusedError")
    ]


class _Client:
    def __init__(self, error: Exception | None = None) -> None:
        self._error = error
        self.exited = False

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.exited = True

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        return {"method": method}


def test_recording_factory_passes_a_successful_call_through() -> None:
    runner = HookRunner()
    client = _Client()

    with runner.recording_factory(lambda: client)() as opened:
        answer = opened.call("m", {})

    assert answer == {"method": "m"}
    assert client.exited
    assert runner.failures == []


def test_recording_factory_notes_a_call_that_raises_and_closes_the_client() -> None:
    runner = HookRunner()
    client = _Client(KeyError("permission"))
    runner.register(HookEventType.PRE_TOOL_USE, lambda event: (False, ""), name="noop")
    runner.run_event(_event(HookEventType.PRE_TOOL_USE))

    with pytest.raises(KeyError), runner.recording_factory(lambda: client)() as opened:
        opened.call("m", {})

    assert client.exited
    assert [(f.event, f.hook, f.error) for f in runner.failures] == [
        ("pre_tool_use", "noop", "KeyError")
    ]


def test_recording_factory_without_a_base_opens_the_default_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _Client()
    monkeypatch.setattr(runner_module, "_default_daemon_client_factory", lambda: client)

    with HookRunner().recording_factory(None)() as opened:
        assert opened is client


def test_record_failures_appends_rows_in_order(tmp_path: Path) -> None:
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()

    assert record_failures(ea_dir, [_failure("a")]) == 1
    assert record_failures(ea_dir, [_failure("b"), _failure("c")]) == 2

    rows = (ea_dir / HEALTH_RECORD).read_text(encoding="utf-8").splitlines()
    assert [json.loads(row)["detail"] for row in rows] == ["a", "b", "c"]


def test_record_failures_with_no_failures_writes_nothing(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()

    assert record_failures(tmp_path / ".ea", []) == 0
    assert not (tmp_path / ".ea" / HEALTH_RECORD).exists()


def test_record_failures_outside_a_tree_writes_nothing(tmp_path: Path) -> None:
    assert record_failures(tmp_path / ".ea", [_failure()]) == 0
    assert not (tmp_path / ".ea").exists()


def test_record_failures_caps_the_detail(tmp_path: Path) -> None:
    (tmp_path / ".ea").mkdir()

    record_failures(tmp_path / ".ea", [_failure("x" * 501)])

    row = json.loads((tmp_path / ".ea" / HEALTH_RECORD).read_text(encoding="utf-8"))
    assert row["detail"] == "x" * 500


def test_record_failures_raises_when_the_record_cannot_be_written(tmp_path: Path) -> None:
    (tmp_path / ".ea" / HEALTH_RECORD).mkdir(parents=True)

    with pytest.raises(OSError):
        record_failures(tmp_path / ".ea", [_failure()])


def test_failure_line_names_event_code_hook_and_error_not_detail() -> None:
    line = failure_line(_failure("secret detail"))

    assert line == "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool (OSError)"


def test_segment_ignores_an_unreadable_record(tree: Path) -> None:
    (tree / ".ea" / HEALTH_RECORD).mkdir(parents=True)

    assert "hooks!" not in _segment_text(tree)


def test_segment_with_a_plugin_record_turns_degraded_on_failures(tree: Path) -> None:
    record = tree / "home" / ".claude" / "plugins" / "installed_plugins.json"
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"plugins": {"p": [{"scope": "user"}]}}), encoding="utf-8")
    record_failures(tree / ".ea", [replace(_failure(), at=datetime.now(UTC))])

    segment = hooks_plugins.build({}, tree / ".ea" / "state.json")

    assert segment.text == "hooks:0 plugins:1 hooks!1"
    assert segment.status == "degraded"
    assert ".ea/local/hooks/health.jsonl" in segment.truth.provenance_refs
