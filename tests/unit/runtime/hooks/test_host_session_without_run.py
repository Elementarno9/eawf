"""A host session on no live Run is skipped quietly, not reported as an error.

An operator's own session is not a Run, so its tool calls and compactions have nowhere
to land. The daemon's host-context lookup answers such a session with the typed outcome
``no_run``, and the host-call hooks skip it without a failure line or an
``identity_not_found`` refusal on stderr. A session on two live Runs, and a daemon that
cannot be reached, still surface.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Final

import pytest
from typer.testing import CliRunner  # noqa: EAWF024 the exit code and stderr are the contract

from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.authority import require_native_authority
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.host_context import (
    HOST_CONTEXT_BOUNDARY_METHOD,
    HostContextNoRun,
    host_session_run,
)
from eawf.runtime.daemon.methods.permission import host_run
from eawf.runtime.hooks import host_calls
from eawf.runtime.hooks import runner as hook_runner
from eawf.runtime.hooks.health import HEALTH_RECORD
from eawf.surfaces.cli.app import app
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    tree_root,
)
from tests.integration.runtime.daemon.test_provider_permission_producer import (
    RUN_KEY,
    RUN_URN,
    SESSION,
    _running_run,
)

#: A host session no Run was dispatched on, as an operator's own session is.
OPERATOR_SESSION: Final = "operator-session-0001"

TWIN_KEY: Final = "RUN-00000011"


def _tool_payload(session: str) -> dict[str, Any]:
    return {
        "hook_event_name": "PostToolUse",
        "session_id": session,
        "tool_use_id": "toolu-0001",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_response": {"stdout": "ok"},
    }


@pytest.fixture
def canary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> CanaryProvision:
    """A canary holding one running Run on :data:`SESSION`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    provisioned = provision(tmp_path / "repo")
    seed(provisioned, {"run": {RUN_KEY: _running_run(SESSION)}})
    return provisioned


@pytest.fixture
def ctx(tmp_path: Path) -> MethodContext:
    return method_context(tmp_path / "runtime")


def _twin(canary: CanaryProvision) -> None:
    """Put a second running Run on :data:`SESSION`."""
    twin = _running_run(SESSION)
    twin["key"] = TWIN_KEY
    twin["urn"] = RUN_URN.replace(RUN_KEY, TWIN_KEY)
    seed(canary, {"run": {TWIN_KEY: twin}})


def _call(method: str, ctx: MethodContext, canary: CanaryProvision, **params: Any) -> Any:
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


class _InProcessDaemon:
    """A daemon client that dispatches each call to the daemon's verbs in process."""

    def __init__(self, ctx: MethodContext, canary: CanaryProvision) -> None:
        self._ctx = ctx
        self._canary = canary

    def __enter__(self) -> _InProcessDaemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> Any:
        return _call(method, self._ctx, self._canary, **params)


def _hook(
    canary: CanaryProvision,
    event: str,
    payload: dict[str, Any],
    factory: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> Any:
    monkeypatch.setattr(hook_runner, "_default_daemon_client_factory", factory)
    return CliRunner().invoke(
        app,
        ["--workspace", str(canary.root), "hook", "run", event, "--runtime", "claude"],
        input=json.dumps(payload),
    )


def _boundary(ctx: MethodContext, canary: CanaryProvision, session: str) -> Any:
    return _call(
        HOST_CONTEXT_BOUNDARY_METHOD,
        ctx,
        canary,
        harness="claude-code",
        host_session_id=session,
        boundary="compacting",
    )


def test_the_lookup_answers_no_run_for_a_session_on_no_run(canary: CanaryProvision) -> None:
    authority = require_native_authority(tree_root(canary))

    assert host_session_run(authority, OPERATOR_SESSION) == "no_run"


def test_the_lookup_returns_the_one_run_on_the_session(canary: CanaryProvision) -> None:
    authority = require_native_authority(tree_root(canary))

    assert host_session_run(authority, SESSION) == parse_qualified_urn(RUN_URN)


def test_the_lookup_raises_for_a_session_on_two_runs(canary: CanaryProvision) -> None:
    _twin(canary)
    authority = require_native_authority(tree_root(canary))

    with pytest.raises(DaemonValidationError, match="identity_not_found: 2 live Runs"):
        host_session_run(authority, SESSION)


def test_the_boundary_verb_answers_no_run_typed(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    answer = _boundary(ctx, canary, OPERATOR_SESSION)

    assert answer == HostContextNoRun().model_dump(mode="json") == {"outcome": "no_run"}


def test_the_boundary_verb_states_the_one_run(canary: CanaryProvision, ctx: MethodContext) -> None:
    answer = _boundary(ctx, canary, SESSION)

    assert answer["outcome"] == "stated"
    assert answer["run_ref"] == str(parse_qualified_urn(RUN_URN))


def test_the_boundary_verb_refuses_a_session_on_two_runs(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    _twin(canary)

    with pytest.raises(DaemonValidationError, match="identity_not_found: 2 live Runs"):
        _boundary(ctx, canary, SESSION)


@pytest.mark.parametrize(
    ("event", "payload"),
    [
        ("post_tool_use", _tool_payload(OPERATOR_SESSION)),
        ("pre_tool_use", _tool_payload(OPERATOR_SESSION)),
        ("permission_request", _tool_payload(OPERATOR_SESSION)),
        ("pre_compact", {"hook_event_name": "PreCompact", "session_id": OPERATOR_SESSION}),
        (
            "session_start",
            {
                "hook_event_name": "SessionStart",
                "session_id": OPERATOR_SESSION,
                "source": "compact",
            },
        ),
    ],
)
def test_a_hook_from_a_session_on_no_run_exits_zero_and_quiet(
    canary: CanaryProvision,
    ctx: MethodContext,
    monkeypatch: pytest.MonkeyPatch,
    event: str,
    payload: dict[str, Any],
) -> None:
    result = _hook(canary, event, payload, lambda: _InProcessDaemon(ctx, canary), monkeypatch)

    assert result.exit_code == 0, result.output
    assert result.stderr == ""


def test_a_tool_hook_from_the_one_run_is_accounted(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = _hook(
        canary,
        "post_tool_use",
        _tool_payload(SESSION),
        lambda: _InProcessDaemon(ctx, canary),
        monkeypatch,
    )

    assert result.exit_code == 0, result.output
    assert f"runtime.host_tool ok run={parse_qualified_urn(RUN_URN)}" in result.stderr


def test_a_tool_hook_from_a_session_on_two_runs_still_surfaces(
    canary: CanaryProvision, ctx: MethodContext, monkeypatch: pytest.MonkeyPatch
) -> None:
    _twin(canary)

    result = _hook(
        canary,
        "post_tool_use",
        _tool_payload(SESSION),
        lambda: _InProcessDaemon(ctx, canary),
        monkeypatch,
    )

    assert result.exit_code == 0, result.output
    # The failure is named on one stderr line; its detail goes to the health record.
    assert result.stderr.splitlines() == [
        "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool (DaemonValidationError)"
    ]
    record = (canary.root / ".ea" / HEALTH_RECORD).read_text(encoding="utf-8")
    assert "identity_not_found: 2 live Runs" in json.loads(record)["detail"]


def test_an_unreachable_daemon_still_surfaces(
    canary: CanaryProvision, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refusing() -> Any:
        raise ConnectionRefusedError("no daemon")

    result = _hook(canary, "post_tool_use", _tool_payload(OPERATOR_SESSION), refusing, monkeypatch)

    assert result.exit_code == 0, result.output
    assert "eawf hook post_tool_use: daemon_call_failed in runtime.host_tool" in result.stderr


def test_the_zero_run_refusal_is_the_one_the_daemon_raises(canary: CanaryProvision) -> None:
    authority = require_native_authority(tree_root(canary))

    with pytest.raises(DaemonValidationError) as raised:
        host_run(authority, OPERATOR_SESSION)

    assert host_calls._NO_RUN_REFUSAL in str(raised.value)


def test_no_run_skip_swallows_the_zero_run_refusal() -> None:
    refusal = DaemonValidationError(
        "validation_failed: identity_not_found: 0 live Runs are on the host session, "
        "so the held call is bound to no one Run"
    )

    with host_calls._NoRunSkip() as no_run:
        raise refusal

    assert no_run.skipped


@pytest.mark.parametrize(
    "error",
    [
        DaemonValidationError("validation_failed: identity_not_found: 2 live Runs are on the "),
        DaemonValidationError("validation_failed: identity_not_found: 10 live Runs are on the "),
        DaemonValidationError(""),
        ConnectionRefusedError("no daemon"),
        KeyError("run_ref"),
        TypeError("not a dict"),
    ],
)
def test_no_run_skip_passes_every_other_failure(error: Exception) -> None:
    skip = host_calls._NoRunSkip()

    with pytest.raises(type(error)), skip:
        raise error

    assert not skip.skipped


def test_no_run_skip_passes_a_clean_block() -> None:
    with host_calls._NoRunSkip() as no_run:
        pass

    assert not no_run.skipped
