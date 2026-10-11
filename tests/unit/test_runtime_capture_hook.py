"""Tests for the runtime capture hook registry and the daemon methods its hooks call."""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import eawf.runtime.hooks
from eawf.runtime.daemon.methods import registered_methods
from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import HookRunner, register_runtime_capture_hooks

_HOOKS_PACKAGE = Path(eawf.runtime.hooks.__file__).parent


def _event(event_type: HookEventType, payload: dict[str, Any], *, runtime: str) -> HookEvent:
    return HookEvent(
        event_type=event_type,
        scope_id="",
        command="",
        args={},
        runtime=runtime,  # type: ignore[arg-type]
        occurred_at=datetime(2026, 6, 10, 12, 0, 0, tzinfo=UTC),
        payloads={event_type.value: payload},
    )


class _RecordingClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __enter__(self) -> _RecordingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, params))
        return {"ok": True}


class _FailingClient:
    def __enter__(self) -> _FailingClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RuntimeError(f"{method} unavailable")


def _module_strings(tree: ast.Module) -> dict[str, list[str]]:
    """Map each module-level name bound to a string or a dict of strings to those strings."""
    bound: dict[str, list[str]] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1:
            target, value = node.targets[0], node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            target, value = node.target, node.value
        else:
            continue
        if not isinstance(target, ast.Name):
            continue
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            bound[target.id] = [value.value]
        elif isinstance(value, ast.Dict):
            strings = [
                v.value
                for v in value.values
                if isinstance(v, ast.Constant) and isinstance(v.value, str)
            ]
            if len(strings) == len(value.values):
                bound[target.id] = strings
    return bound


def _called_methods(source: str, *, origin: str) -> set[str]:
    """Return every daemon method name a ``<client>.call(...)`` in *source* names.

    Raises:
        ValueError: When a call's method argument is neither a string literal nor a
            module-level string or dict-of-strings binding, so a new call shape fails
            the scan instead of slipping past it.
    """
    tree = ast.parse(source)
    bound = _module_strings(tree)
    methods: set[str] = set()
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "call"
            and node.args
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            methods.add(arg.value)
        elif isinstance(arg, ast.Name) and arg.id in bound:
            methods.update(bound[arg.id])
        elif (
            isinstance(arg, ast.Subscript)
            and isinstance(arg.value, ast.Name)
            and arg.value.id in bound
        ):
            methods.update(bound[arg.value.id])
        else:
            raise ValueError(f"{origin}:{node.lineno}: unresolvable daemon method argument")
    return methods


def _hook_called_methods() -> dict[str, set[str]]:
    return {
        path.name: _called_methods(path.read_text(encoding="utf-8"), origin=path.name)
        for path in sorted(_HOOKS_PACKAGE.glob("*.py"))
    }


def test_every_daemon_method_a_hook_calls_has_a_registered_handler() -> None:
    registered = set(registered_methods())
    called = _hook_called_methods()

    unregistered = {
        module: sorted(methods - registered)
        for module, methods in called.items()
        if methods - registered
    }

    assert unregistered == {}
    # The scan must see the hooks' calls, or an empty registry check passes vacuously.
    assert "runtime.host.subagent.start" in set().union(*called.values())


def test_scan_flags_a_call_to_a_retired_method() -> None:
    source = 'def hook(client):\n    client.call("runtime.capture", {})\n'

    assert _called_methods(source, origin="probe.py") == {"runtime.capture"}
    assert "runtime.capture" not in registered_methods()


def test_scan_resolves_constant_and_dict_bindings() -> None:
    source = (
        'ONE = "a.one"\n'
        'MANY: dict[str, str] = {"x": "b.two", "y": "b.three"}\n'
        "def hook(client, key):\n"
        "    client.call(ONE, {})\n"
        "    client.call(MANY[key], {})\n"
    )

    assert _called_methods(source, origin="probe.py") == {"a.one", "b.two", "b.three"}


def test_scan_of_a_module_without_calls_is_empty() -> None:
    assert _called_methods("", origin="empty.py") == set()


def test_scan_refuses_an_unresolvable_method_argument() -> None:
    source = "def hook(client, name):\n    client.call(name, {})\n"

    with pytest.raises(ValueError, match=r"probe\.py:2"):
        _called_methods(source, origin="probe.py")


@pytest.mark.parametrize("runtime", ["claude", "codex"])
def test_session_end_only_stamps_and_calls_no_daemon_method(runtime: str, tmp_path: Path) -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client, repo_root=tmp_path)

    results = runner.run_event(
        _event(
            HookEventType.SESSION_END,
            {"hook_event_name": "Stop", "session_id": "session-1"},
            runtime=runtime,
        )
    )

    assert [result.name for result in results] == ["session.end_stamp"]
    assert results[0].block is False
    assert results[0].output == "session.end_stamp skipped: no live session resolved"
    assert client.calls == []


def test_codex_session_start_calls_only_registered_methods(tmp_path: Path) -> None:
    client = _RecordingClient()
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=lambda: client, repo_root=tmp_path)

    results = runner.run_event(
        _event(
            HookEventType.SESSION_START,
            {"hook_event_name": "SessionStart", "session_id": "provider-session-1"},
            runtime="codex",
        )
    )

    assert "runtime.codex_lifecycle" not in {result.name for result in results}
    assert {method for method, _params in client.calls} <= set(registered_methods())


def test_subagent_hook_daemon_error_is_non_blocking(tmp_path: Path) -> None:
    runner = HookRunner()
    register_runtime_capture_hooks(runner, daemon_client_factory=_FailingClient, repo_root=tmp_path)

    results = runner.run_event(
        _event(
            HookEventType.SUBAGENT_START,
            {"hook_event_name": "SubagentStart", "session_id": "s-1", "agent_id": "agent-1"},
            runtime="claude",
        )
    )

    (adopt,) = (result for result in results if result.name == "runtime.host_subagent")
    assert adopt.block is False
    assert "runtime.host.subagent.start unavailable" in adopt.output
