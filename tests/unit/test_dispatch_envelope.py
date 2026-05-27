"""Unit tests for :func:`eawf.workflow.dispatch.renderer.render_dispatch_envelope` (P10 W03).

Exercises the pure dispatch adapter against hand-built
:class:`~eawf.kernel.state.models.State` instances. The adapter is pure — no
I/O — so each test composes a state in memory and inspects the typed
:class:`~eawf.workflow.dispatch.renderer.DispatchEnvelope` it returns.

Coverage:

- Both runtime branches (``claude-code`` and ``claude-agent-sdk``)
  return envelopes with identical ``prompt`` bodies.
- The SDK branch projects :attr:`State.mcp_servers` into the envelope's
  ``mcp_servers`` list (sorted by id, JSON-safe primitives).
- The SDK branch projects wave-scoped grants from ``state.mcp_grants``
  into ``allowed_tools`` as ``mcp__<server_id>__*`` globs.
- The SDK branch renders cleanly when ``mcp_grants`` is absent (the
  field lands in a sibling wave; this wave must be forward-compatible).
- Unknown runtime raises :class:`ValueError` with the canonical
  ``unknown runtime ...; expected one of [...]`` message.
- Unknown wave id propagates a :class:`KeyError` from
  :func:`render_wave_prompt`.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from eawf.kernel.state.enums import McpRisk, McpStatus, ProjectStatus, ScopeKind
from eawf.kernel.state.models import CurrentPointers, McpGrant, McpServer, Project, State
from eawf.runtime.sandbox.policy import SandboxPolicy
from eawf.workflow.dispatch import render_dispatch_envelope, render_wave_prompt
from eawf.workflow.dispatch.renderer import DispatchEnvelope
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave

# ---- Builders ---------------------------------------------------------------


def _empty_state() -> State:
    """Return a minimal State with project=QR and an empty world."""
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="QR",
                slug="qr",
                title="QR",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_single_wave(state: State, wave_id: str = "P01-I01-W01") -> None:
    """Seed P01 → P01-I01 → *wave_id*. The wave has no deps."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id=wave_id,
        iter_id="P01-I01",
        title="Solo wave",
        file_scopes=["src/"],
        effort_bucket="M",
    )


def _mcp_server(server_id: str, *, command: str = "mcp-stdio-bin") -> McpServer:
    """Construct a minimal :class:`McpServer` for envelope-projection tests."""
    return McpServer(
        id=server_id,
        owner="eawf",
        command=command,
        args=["--quiet"],
        env_refs=["${ENV:MCP_TOKEN}"],
        risk=McpRisk.READ,
        write_capable=False,
        status=McpStatus.CONFIGURED,
        installed_targets=[],
    )


def _wave_grant(grant_id: str, *, server_id: str, wave_id: str = "P01-I01-W01") -> McpGrant:
    """Construct a wave-scoped :class:`McpGrant` for *server_id*."""
    return McpGrant(
        id=grant_id,
        scope_kind="wave",
        scope_id=wave_id,
        server_id=server_id,
        granted_at=datetime.now(UTC),
    )


def _deny_policy(
    policy_id: str,
    *,
    scope_kind: str,
    scope_id: str,
    denied: list[str],
) -> SandboxPolicy:
    """Construct a :class:`SandboxPolicy` with a deny-list for enforcement tests."""
    return SandboxPolicy(
        id=policy_id,
        scope_kind=scope_kind,  # type: ignore[arg-type]
        scope_id=scope_id,
        denied_tools=denied,
        granted_at=datetime.now(UTC),
    )


# ---- claude-code branch -----------------------------------------------------


def test_render_dispatch_envelope_claude_code_wraps_render_wave_prompt() -> None:
    """The claude-code branch returns the existing prompt verbatim."""
    state = _empty_state()
    _seed_single_wave(state)
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")
    assert isinstance(envelope, DispatchEnvelope)
    assert envelope.runtime == "claude-code"
    assert envelope.wave_id == "P01-I01-W01"
    # Prompt body is byte-equal to render_wave_prompt — the adapter
    # must not mutate the prompt on the claude-code branch.
    expected = render_wave_prompt(state, "P01-I01-W01")
    assert envelope.prompt == expected
    # MCP wiring stays out of the claude-code envelope — the runtime
    # reads it from .claude/settings.json on disk.
    assert envelope.mcp_servers == []
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_claude_code_ignores_mcp_servers() -> None:
    """Setting state.mcp_servers does not bleed into the claude-code branch."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")
    assert envelope.mcp_servers == []
    assert envelope.allowed_tools == []


# ---- claude-agent-sdk branch ------------------------------------------------


def test_render_dispatch_envelope_sdk_empty_state_renders_cleanly() -> None:
    """SDK branch returns an envelope with empty MCP lists when nothing is wired."""
    state = _empty_state()
    _seed_single_wave(state)
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.runtime == "claude-agent-sdk"
    assert envelope.wave_id == "P01-I01-W01"
    assert envelope.prompt == render_wave_prompt(state, "P01-I01-W01")
    assert envelope.mcp_servers == []
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_sdk_projects_mcp_servers_sorted() -> None:
    """SDK branch projects state.mcp_servers into sorted JSON-safe dicts."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {
        "beta": _mcp_server("beta", command="beta-bin"),
        "alpha": _mcp_server("alpha", command="alpha-bin"),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    # Sorted by id so the wire-form is deterministic.
    assert [s["id"] for s in envelope.mcp_servers] == ["alpha", "beta"]
    assert envelope.mcp_servers[0]["command"] == "alpha-bin"
    assert envelope.mcp_servers[0]["args"] == ["--quiet"]
    assert envelope.mcp_servers[0]["env_refs"] == ["${ENV:MCP_TOKEN}"]
    # No grants ⇒ no projected tools.
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_sdk_handles_missing_mcp_grants_field() -> None:
    """A state.json without mcp_grants must still render — W02 ships the field."""
    state = _empty_state()
    _seed_single_wave(state)
    # Confirm the field is absent on the current model (forward-compat
    # contract): if W02 has landed and ``mcp_grants`` exists, the
    # default-None semantics still keep this test green.
    assert getattr(state, "mcp_grants", None) is None
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_sdk_projects_grants_when_present() -> None:
    """When mcp_grants holds a wave-scoped grant, allowed_tools picks it up.

    The W03 model does not own ``mcp_grants`` (sibling wave); we stub it
    via ``setattr`` on the State instance to simulate the post-W02
    shape. The adapter must read with ``getattr`` semantics so the stub
    works regardless of whether the field exists on the Pydantic model.
    """
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}

    class _GrantStub:
        def __init__(self, scope_kind: str, scope_id: str, server_id: str) -> None:
            self.scope_kind = scope_kind
            self.scope_id = scope_id
            self.server_id = server_id

    # Wave-scoped grant for the dispatched wave — must surface.
    in_scope = _GrantStub("wave", "P01-I01-W01", "alpha")
    # Wave-scoped grant for a different wave — must NOT surface.
    other_wave = _GrantStub("wave", "P01-I01-W02", "alpha")
    # Profile / global grants are not projected on the wave branch — must NOT surface.
    profile_grant = _GrantStub("profile", "DEFAULT", "alpha")
    object.__setattr__(
        state,
        "mcp_grants",
        {
            "GRANT-1": in_scope,
            "GRANT-2": other_wave,
            "GRANT-3": profile_grant,
        },
    )
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == ["mcp__alpha__*"]


def test_render_dispatch_envelope_sdk_deduplicates_repeat_grants() -> None:
    """Two grants for the same server collapse to one allow-list entry."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}

    class _GrantStub:
        def __init__(self, scope_kind: str, scope_id: str, server_id: str) -> None:
            self.scope_kind = scope_kind
            self.scope_id = scope_id
            self.server_id = server_id

    object.__setattr__(
        state,
        "mcp_grants",
        {
            "GRANT-1": _GrantStub("wave", "P01-I01-W01", "alpha"),
            "GRANT-2": _GrantStub("wave", "P01-I01-W01", "alpha"),
        },
    )
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == ["mcp__alpha__*"]


def test_render_dispatch_envelope_sdk_handles_empty_mcp_grants() -> None:
    """An empty mcp_grants dict yields an empty allow-list (boundary)."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    object.__setattr__(state, "mcp_grants", {})
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == []


# ---- Error paths ------------------------------------------------------------


def test_render_dispatch_envelope_unknown_runtime_raises_value_error() -> None:
    """An unsupported runtime raises ValueError with the canonical message."""
    state = _empty_state()
    _seed_single_wave(state)
    with pytest.raises(ValueError, match="unknown runtime 'bogus'; expected one of"):
        render_dispatch_envelope(state, "P01-I01-W01", "bogus")


def test_render_dispatch_envelope_unknown_runtime_lists_supported_in_message() -> None:
    """The error message enumerates the supported runtime names."""
    state = _empty_state()
    _seed_single_wave(state)
    with pytest.raises(ValueError) as excinfo:
        render_dispatch_envelope(state, "P01-I01-W01", "opencode")
    msg = str(excinfo.value)
    assert "claude-code" in msg
    assert "claude-agent-sdk" in msg


def test_render_dispatch_envelope_unknown_wave_raises_key_error() -> None:
    """A missing wave id propagates a KeyError from render_wave_prompt."""
    state = _empty_state()
    _seed_single_wave(state)
    with pytest.raises(KeyError, match="unknown wave"):
        render_dispatch_envelope(state, "P01-I01-W99", "claude-code")


# ---- Purity / boundary cases ------------------------------------------------


def test_render_dispatch_envelope_is_pure_no_state_mutation() -> None:
    """The adapter does not mutate state — call twice, fields stay equal."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    before = state.mcp_servers
    render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert state.mcp_servers is before


# ---- SandboxPolicy deny enforcement (W21) -----------------------------------


def test_render_dispatch_envelope_wave_deny_removes_projected_tool() -> None:
    """A wave-scoped deny intersects its tool out — the wave cannot dispatch it.

    The grant projects ``mcp__alpha__*``; a wave-scoped sandbox policy
    that denies that exact tool must knock it out of ``allowed_tools``, so
    the dispatched envelope never carries the denied tool.
    """
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.mcp_grants = {"GRANT-1": _wave_grant("GRANT-1", server_id="alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W01",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_global_deny_removes_projected_tool() -> None:
    """A global deny applies to every wave and removes the projected tool."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.mcp_grants = {"GRANT-1": _wave_grant("GRANT-1", server_id="alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="global",
            scope_id="global",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_deny_keeps_non_denied_tools() -> None:
    """Deny only removes the named tool — sibling grants stay in allowed_tools."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha"), "beta": _mcp_server("beta")}
    state.mcp_grants = {
        "GRANT-1": _wave_grant("GRANT-1", server_id="alpha"),
        "GRANT-2": _wave_grant("GRANT-2", server_id="beta"),
    }
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W01",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == ["mcp__beta__*"]


def test_render_dispatch_envelope_deny_for_other_wave_does_not_apply() -> None:
    """A deny scoped at a different wave leaves this wave's tools intact."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.mcp_grants = {"GRANT-1": _wave_grant("GRANT-1", server_id="alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W02",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == ["mcp__alpha__*"]


def test_render_dispatch_envelope_profile_deny_does_not_apply_to_wave() -> None:
    """Profile-scoped deny is not resolved on the wave-dispatch path."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.mcp_grants = {"GRANT-1": _wave_grant("GRANT-1", server_id="alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="profile",
            scope_id="research",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == ["mcp__alpha__*"]


def test_render_dispatch_envelope_deny_with_no_grants_is_noop() -> None:
    """Deny with nothing to remove yields an empty allow-list (boundary)."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W01",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-agent-sdk")
    assert envelope.allowed_tools == []


def test_render_dispatch_envelope_claude_code_ignores_deny_policy() -> None:
    """Deny enforcement does not touch the claude-code branch (always empty)."""
    state = _empty_state()
    _seed_single_wave(state)
    state.mcp_servers = {"alpha": _mcp_server("alpha")}
    state.mcp_grants = {"GRANT-1": _wave_grant("GRANT-1", server_id="alpha")}
    state.sandbox_policies = {
        "POL-1": _deny_policy(
            "POL-1",
            scope_kind="wave",
            scope_id="P01-I01-W01",
            denied=["mcp__alpha__*"],
        ),
    }
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")
    assert envelope.allowed_tools == []


# ---- role_contract threading (P28-I01-W13) ---------------------------------


def _seed_wave_with_role(state: State, *, role: str) -> None:
    """Seed P01 → P01-I01 → P01-I01-W01 carrying *role* as agent_role."""
    from eawf.kernel.state.enums import AgentSessionRole, EffortBucket

    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Solo wave",
        file_scopes=["src/"],
        agent_role=AgentSessionRole(role),
        effort_bucket=EffortBucket.M,
    )


@pytest.mark.parametrize("runtime", ["claude-code", "claude-agent-sdk"])
def test_render_dispatch_envelope_carries_role_contract_when_role_set(runtime: str) -> None:
    """A wave with ``agent_role`` set surfaces a typed ``RoleContract`` on the envelope."""
    state = _empty_state()
    _seed_wave_with_role(state, role="auditor")
    envelope = render_dispatch_envelope(state, "P01-I01-W01", runtime)
    assert envelope.role_contract is not None
    assert envelope.role_contract.role == "auditor"
    assert envelope.role_contract.system_prompt  # non-empty registry body
    assert envelope.role_contract.report_schema_ref == "auditor_report"


@pytest.mark.parametrize("runtime", ["claude-code", "claude-agent-sdk"])
def test_render_dispatch_envelope_role_contract_none_when_no_role(runtime: str) -> None:
    """A wave without ``agent_role`` keeps ``role_contract`` at ``None``."""
    state = _empty_state()
    _seed_single_wave(state)  # no agent_role
    envelope = render_dispatch_envelope(state, "P01-I01-W01", runtime)
    assert envelope.role_contract is None


def test_render_dispatch_envelope_does_not_double_render_prompt() -> None:
    """The envelope reuses the SubagentSpec render — no double walk over the spec.

    The shared-spec wiring (P28-I01-W13) builds the spec once and reads
    both the rendered prompt body AND the typed role contract off the
    same object, so the dispatcher avoids the previous render-then-
    re-walk pattern. This test pins byte-equivalence between the
    envelope's prompt and the standalone render path so the shared seam
    stays observably correct.
    """
    state = _empty_state()
    _seed_wave_with_role(state, role="executor")
    envelope = render_dispatch_envelope(state, "P01-I01-W01", "claude-code")
    assert envelope.prompt == render_wave_prompt(state, "P01-I01-W01")
