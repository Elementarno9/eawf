"""Gate-fire proof: a dropped MCP entry refuses the launch, typed.

A native launcher that built (or was handed) a per-Run MCP configuration
naming no server would, without a check, start the provider anyway --
silently running the Run without its semantic tool catalog rather than
refusing. This seeds exactly that defect (a configuration renderer that
drops the entry) against the real
:class:`~eawf.runtime.runtimes.codex.adapter.CodexNativeLauncher` and shows
the launch is refused with a typed error before any child is ever forked.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.mcp import native_launch
from eawf.runtime.mcp.semantic_stdio import RunServerConfig
from eawf.runtime.runtimes.adapter import NativeLaunchRequest, RuntimeSpawnError
from eawf.runtime.runtimes.codex.adapter import CodexAdapter, CodexNativeLauncher
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

pytestmark = pytest.mark.integration


def _spec() -> CompiledRunSpec:
    """Return a compiled codex-lane spec."""
    return compile_run_spec(
        fx.task_request(),
        configuration=fx.configuration(),
        bindings=[fx.binding()],
        compiled_at=fx.COMPILED_AT,
    )


def _request(spec: CompiledRunSpec, workspace: Path) -> NativeLaunchRequest:
    """Return the launch request one fake lane is started with."""
    capsule = seal_capsule(
        spec=spec, request=CapsuleRequest.model_validate(capsule_request(token_budget=1_000))
    )
    return NativeLaunchRequest(
        spec=spec,
        capsule=capsule,
        workspace_handle=f"wsh-{'a' * 32}",
        workspace=workspace,
        prompt="implement the fixture task",
        hello_sequence=1,
        usage_sink=None,
    )


def _dropped_config(*_args: object, **_kwargs: object) -> RunServerConfig:
    """Stand in for a renderer that built a configuration naming no server."""
    return RunServerConfig(runtime_id="codex")


def _forbidden_spawn(*_args: object, **_kwargs: object) -> None:
    """Fail the test if the child is ever forked past the dropped entry."""
    raise AssertionError("the child must never be spawned once the MCP entry is dropped")


def test_native_launch_refuses_when_the_mcp_entry_is_dropped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A renderer that drops the entry refuses the launch before any spawn."""
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = _request(_spec(), workspace)
    monkeypatch.setattr(native_launch, "run_server_config_for", _dropped_config)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", _forbidden_spawn)

    with pytest.raises(RuntimeSpawnError, match="mcp_entry_dropped"):
        asyncio.run(CodexNativeLauncher(adapter=CodexAdapter()).launch(request))


def test_native_launch_does_not_refuse_a_real_configuration(tmp_path: Path) -> None:
    """The refusal is not a false positive: an unpatched render passes it.

    Calls the builder directly (not through the launcher) so this proves the
    check alone, with no subprocess involved.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    request = _request(_spec(), workspace)

    config = native_launch.native_run_server_config(request, runtime_id="codex")

    assert config.argv_flags
