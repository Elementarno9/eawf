"""A managed claude Run sees only its granted MCP tools and no operator config.

The launch cases drive the REAL :class:`~eawf.runtime.runtimes.claude.adapter.ClaudeNativeLauncher`
against a fake ``claude`` that records its argv and environment, inside a
fake operator home seeded with the configuration a managed Run must not
inherit: a user settings file with a hook, a user-scoped MCP server, an
operator ``CLAUDE_CONFIG_DIR``, and a workspace carrying project settings
and a project MCP document. No model is reached.
"""

from __future__ import annotations

import asyncio
import json
import stat
import sys
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.capsule import AuthorityCapsule
from eawf.kernel.runtime.compiled import CompiledRunSpec
from eawf.kernel.runtime.semantic import SemanticToolId
from eawf.kernel.state.enums import AgentSessionRole
from eawf.runtime.daemon.native_dispatch import CapsuleRequest, seal_capsule
from eawf.runtime.mcp.grant import (
    assert_capsule_within_intersection,
    certifies_semantic_tools,
    intersect_run_tools,
    role_admitted_tools,
)
from eawf.runtime.mcp.native_launch import MCP_ARTIFACT_DIRNAME
from eawf.runtime.runtimes.adapter import NativeLaunchRequest, RuntimeSpawnError
from eawf.runtime.runtimes.claude import adapter as claude_adapter
from eawf.runtime.runtimes.claude.adapter import ClaudeAdapter, ClaudeNativeLauncher
from eawf.runtime.runtimes.claude.managed_run import (
    CONFIG_DIR_ENV,
    MANAGED_CONFIG_DIRNAME,
    SECURE_STORAGE_ENV,
    prepare_managed_isolation,
)
from eawf.workflow.runtime.compile import compile_run_spec
from tests import _provider_helpers as fx
from tests.integration.runtime.daemon.test_native_dispatch import capsule_request

#: The MCP server the operator installed for themselves.
OPERATOR_SERVER: Final = "operator-server"

_RESULT_LINE: Final = json.dumps(
    {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "sess-fake-claude",
        "result": "ok",
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }
)


def _bypass_jail(
    argv: list[str], *, runtime: str, cwd: str | None, session: str = "", sink: object = None
) -> list[str]:
    return argv


def _write_fake_claude(directory: Path) -> Path:
    """Write a fake ``claude`` that records its argv and env, then succeeds."""
    script = directory / "claude"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open('argv.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(sys.argv, handle)\n"
        "with open('env.json', 'w', encoding='utf-8') as handle:\n"
        "    json.dump(dict(os.environ), handle)\n"
        f"print({_RESULT_LINE!r})\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def _claude_spec() -> CompiledRunSpec:
    """Return a certified executor spec whose winning profile is the claude lane."""
    profile = fx.profile_document(
        profile_id="fixture_claude",
        driver_ref=fx.OTHER_DRIVER_REF,
        auth_profile_ref=fx.OTHER_AUTH_REF,
        provider_options={"provider_kind": "claude"},
    )
    configuration = fx.configuration(
        profiles=[profile],
        routes=[fx.task_route_document(allowed_profiles=["fixture_claude"])],
        global_routes=[],
    )
    return compile_run_spec(
        fx.task_request(),
        configuration=configuration,
        bindings=[fx.binding(driver_ref=fx.OTHER_DRIVER_REF)],
        compiled_at=fx.COMPILED_AT,
    )


def _operator_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Seed an operator home the managed child must not read, and point at it."""
    home = tmp_path / "home"
    operator_config = home / ".claude-operator"
    operator_config.mkdir(parents=True)
    hook = {"hooks": {"PreToolUse": [{"hooks": [{"type": "command", "command": "true"}]}]}}
    (operator_config / "settings.json").write_text(json.dumps(hook), encoding="utf-8")
    (operator_config / "CLAUDE.md").write_text("operator instructions\n", encoding="utf-8")
    servers = {"mcpServers": {OPERATOR_SERVER: {"command": "operator-mcp"}}}
    (operator_config / ".claude.json").write_text(json.dumps(servers), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv(CONFIG_DIR_ENV, str(operator_config))
    monkeypatch.delenv(SECURE_STORAGE_ENV, raising=False)
    return operator_config


def _workspace(tmp_path: Path) -> Path:
    """Return a workspace carrying project settings and a project MCP document."""
    workspace = tmp_path / "ws"
    (workspace / ".claude").mkdir(parents=True)
    (workspace / ".claude" / "settings.json").write_text('{"hooks": {}}', encoding="utf-8")
    servers = {"mcpServers": {OPERATOR_SERVER: {"command": "operator-mcp"}}}
    (workspace / ".mcp.json").write_text(json.dumps(servers), encoding="utf-8")
    _write_fake_claude(workspace)
    return workspace


def _request(
    spec: CompiledRunSpec, workspace: Path, *, capsule: AuthorityCapsule
) -> NativeLaunchRequest:
    return NativeLaunchRequest(
        spec=spec,
        capsule=capsule,
        workspace_handle=f"wsh-{'a' * 32}",
        workspace=workspace,
        prompt="implement the fixture task",
        hello_sequence=1,
    )


def _launch(request: NativeLaunchRequest) -> None:
    adapter = ClaudeAdapter()
    adapter.cli_binary = str(request.workspace / "claude")
    asyncio.run(ClaudeNativeLauncher(adapter=adapter).launch(request))


def _sealed(spec: CompiledRunSpec, grants: list[str]) -> AuthorityCapsule:
    return seal_capsule(
        spec=spec, request=CapsuleRequest.model_validate(capsule_request(tool_grants=grants))
    )


# ---------------------------------------------------------------------------
# The spawn: clean configuration home, intersected MCP grant
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_launch_isolates_the_child_config_dir_and_renders_only_the_intersection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    operator_config = _operator_home(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    spec = _claude_spec()
    capsule = _sealed(spec, ["budget_status", "submit_plan"])

    _launch(_request(spec, workspace, capsule=capsule))

    env = json.loads((workspace / "env.json").read_text(encoding="utf-8"))
    argv = json.loads((workspace / "argv.json").read_text(encoding="utf-8"))
    config_dir = Path(env[CONFIG_DIR_ENV])
    run_root = workspace / MCP_ARTIFACT_DIRNAME
    assert config_dir == run_root / MANAGED_CONFIG_DIRNAME
    assert config_dir.resolve().is_relative_to(workspace.resolve())
    assert config_dir.resolve() != operator_config.resolve()
    assert env[SECURE_STORAGE_ENV] == str(operator_config)
    assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
    assert env["CLAUDE_CODE_DISABLE_AUTO_MEMORY"] == "1"
    assert sorted(config_dir.iterdir()) == []
    assert argv[argv.index("--setting-sources") + 1] == ""
    assert "--strict-mcp-config" in argv
    document = json.loads(Path(argv[argv.index("--mcp-config") + 1]).read_text("utf-8"))
    assert list(document["mcpServers"]) == ["eawf-semantic"]
    assert OPERATOR_SERVER not in json.dumps(document)
    assert argv[argv.index("--allowedTools") + 1].split() == ["mcp__eawf-semantic__budget_status"]


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_launch_starts_every_attempt_from_an_empty_config_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    _operator_home(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    stale = workspace / MCP_ARTIFACT_DIRNAME / MANAGED_CONFIG_DIRNAME / "settings.json"
    stale.parent.mkdir(parents=True)
    stale.write_text('{"hooks": {}}', encoding="utf-8")
    spec = _claude_spec()

    _launch(_request(spec, workspace, capsule=_sealed(spec, ["budget_status"])))

    assert not stale.exists()


@pytest.mark.skipif(sys.platform == "win32", reason="the fake child is a shebang script")
def test_launch_refuses_a_capsule_wider_than_the_intersection(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(claude_adapter, "_maybe_jail_argv", _bypass_jail)
    _operator_home(tmp_path, monkeypatch)
    workspace = _workspace(tmp_path)
    spec = _claude_spec()
    widened = _hand_sealed(spec, grants=("budget_status", "submit_plan"))

    with pytest.raises(RuntimeSpawnError, match="mcp_grant_widened"):
        _launch(_request(spec, workspace, capsule=widened))

    assert not (workspace / "argv.json").exists()


def _hand_sealed(spec: CompiledRunSpec, *, grants: tuple[str, ...]) -> AuthorityCapsule:
    """Seal a capsule directly, bypassing the dispatcher's intersection."""
    sealed = _sealed(spec, ["budget_status"])
    fields = sealed.model_dump(mode="json", exclude={"contract_digest"})
    fields["tool_grants"] = list(grants)
    return AuthorityCapsule.seal(fields)


# ---------------------------------------------------------------------------
# The intersection itself
# ---------------------------------------------------------------------------


def _certified() -> list[Any]:
    return [fx.observation("semantic_tools")]


def _uncertified(status: str) -> list[Any]:
    return [
        fx.observation(
            "semantic_tools",
            status=status,
            basis=None,
            certification_evidence_ref=None,
            verified_at=None,
            expires_at=None,
            reason_code="not_observed",
        )
    ]


def test_seal_capsule_narrows_grants_to_the_role_ceiling() -> None:
    capsule = _sealed(_claude_spec(), ["budget_status", "submit_plan", "submit_candidate"])

    assert capsule.tool_grants == ("budget_status", "submit_candidate")


def test_intersect_run_tools_empty_task_grants_nothing() -> None:
    result = intersect_run_tools(
        role=AgentSessionRole.EXECUTOR, task_grants=(), capabilities=_certified()
    )

    assert result.granted == ()
    assert result.certified is True


def test_intersect_run_tools_single_admitted_tool_is_granted() -> None:
    result = intersect_run_tools(
        role=AgentSessionRole.REVIEWER, task_grants=("repo_read",), capabilities=_certified()
    )

    assert result.granted == (SemanticToolId.REPO_READ,)


def test_intersect_run_tools_drops_a_gated_tool_the_role_lacks_and_keeps_order() -> None:
    result = intersect_run_tools(
        role=AgentSessionRole.REVIEWER,
        task_grants=("submit_report", "submit_candidate", "repo_read"),
        capabilities=_certified(),
    )

    assert result.granted == (SemanticToolId.SUBMIT_REPORT, SemanticToolId.REPO_READ)
    assert SemanticToolId.SUBMIT_CANDIDATE not in result.role_admitted


@pytest.mark.parametrize("status", ["unsupported", "unknown", "expired", "denied"])
def test_intersect_run_tools_uncertified_runtime_grants_nothing(status: str) -> None:
    result = intersect_run_tools(
        role=AgentSessionRole.EXECUTOR,
        task_grants=("submit_report",),
        capabilities=_uncertified(status),
    )

    assert result.certified is False
    assert result.granted == ()


def test_certifies_semantic_tools_counts_a_degraded_row_and_not_an_absent_one() -> None:
    degraded = fx.observation(
        "semantic_tools", status="degraded", degradation_workflow_ref="workflow://fallback/v1"
    )

    assert certifies_semantic_tools([degraded]) is True
    assert certifies_semantic_tools([]) is False
    assert certifies_semantic_tools([fx.observation("event_replay")]) is False


def test_intersect_run_tools_rejects_a_non_catalog_grant() -> None:
    with pytest.raises(ValueError, match="not_a_tool"):
        intersect_run_tools(
            role=AgentSessionRole.EXECUTOR, task_grants=("not_a_tool",), capabilities=_certified()
        )


def test_role_admitted_tools_rejects_an_unknown_role() -> None:
    with pytest.raises(KeyError):
        role_admitted_tools("not-a-role")  # type: ignore[arg-type]


def test_assert_capsule_within_intersection_accepts_a_dispatcher_sealed_capsule() -> None:
    spec = _claude_spec()

    result = assert_capsule_within_intersection(spec=spec, capsule=_sealed(spec, ["budget_status"]))

    assert result.granted == (SemanticToolId.BUDGET_STATUS,)


def test_assert_capsule_within_intersection_rejects_a_widened_capsule() -> None:
    spec = _claude_spec()

    with pytest.raises(ValueError, match="submit_plan"):
        assert_capsule_within_intersection(
            spec=spec, capsule=_hand_sealed(spec, grants=("submit_plan",))
        )


# ---------------------------------------------------------------------------
# The configuration home
# ---------------------------------------------------------------------------


def test_prepare_managed_isolation_defaults_login_storage_to_the_host_default(
    tmp_path: Path,
) -> None:
    isolation = prepare_managed_isolation(tmp_path, operator_env={})

    assert isolation.env[SECURE_STORAGE_ENV] == ""
    assert isolation.env[CONFIG_DIR_ENV] == str(tmp_path / MANAGED_CONFIG_DIRNAME)
    assert isolation.argv_flags == ("--setting-sources", "")


def test_prepare_managed_isolation_honours_an_explicit_storage_pin(tmp_path: Path) -> None:
    operator_env = {CONFIG_DIR_ENV: "/operator/config", SECURE_STORAGE_ENV: "/pinned"}

    isolation = prepare_managed_isolation(tmp_path, operator_env=operator_env)

    assert isolation.env[SECURE_STORAGE_ENV] == "/pinned"
    assert isolation.env[CONFIG_DIR_ENV] != "/operator/config"


def test_prepare_managed_isolation_creates_a_private_empty_home(tmp_path: Path) -> None:
    isolation = prepare_managed_isolation(tmp_path / "run", operator_env={})

    assert isolation.config_dir.is_dir()
    assert list(isolation.config_dir.iterdir()) == []
    if sys.platform != "win32":
        assert stat.S_IMODE(isolation.config_dir.stat().st_mode) & 0o077 == 0


def test_prepare_managed_isolation_fails_when_the_run_dir_is_a_file(tmp_path: Path) -> None:
    blocker = tmp_path / "run"
    blocker.write_text("", encoding="utf-8")

    with pytest.raises(OSError):
        prepare_managed_isolation(blocker, operator_env={})
