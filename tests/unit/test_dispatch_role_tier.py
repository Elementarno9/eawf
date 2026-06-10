"""Unit tests for the per-role dispatch render tier (FLEET-5 / P30-I06-W05).

A profile may attach a per-role "Zone 3" render block keyed by
``agent_role``. The dispatch renderer injects the matching role's block
body into the dispatched ``system_prompt`` for waves of that role. The
injection is additive: a role with no configured block renders the
static :attr:`~eawf.workflow.agents.specs.roles.RoleSpec.system_prompt`
byte-for-byte with no injection and no empty-header artifact.

Coverage:

- ``RenderBlock`` accepts the new ``agent_role`` field for a dispatch-tier
  block and rejects the incoherent combinations (dispatch target without a
  role; a role on a managed-file target).
- :meth:`ComposedProfile.role_tier_blocks` projects role-tier blocks into an
  ``agent_role -> body`` map, last-declared-wins.
- Criterion 1: a profile carrying an executor role-tier block yields a
  dispatched ``system_prompt`` containing that block body for an executor
  wave (asserted against the rendered envelope text).
- Criterion 2: a wave whose ``agent_role`` has NO role-tier block renders
  the byte-identical static ``RoleSpec.system_prompt`` with no injection and
  no empty-header artifact.
- Cross-role: an executor-keyed block does NOT leak into an auditor wave's
  prompt (only the matching role is injected).
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    ProjectStatus,
    ScopeKind,
)
from eawf.kernel.state.models import CurrentPointers, Project, State
from eawf.platform.lint.tools.agents_md_budget import count_tokens
from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.platform.render_block import (
    DEFAULT_ROLE_TIER_TOKEN_CAP,
    DISPATCH_SYSTEM_PROMPT_TARGET,
)
from eawf.workflow.agents.specs.roles import get_role_spec
from eawf.workflow.dispatch import RoleTierBudgetError, render_dispatch_envelope
from eawf.workflow.lifecycle.transitions import open_iter, open_phase, plan_wave
from tests.conftest import make_intent

_EXECUTOR_BLOCK_BODY = (
    "## House executor rules\n\nAlways cite a file:line for every claim "
    "and never widen the named file scope."
)


# ---- Builders ---------------------------------------------------------------


def _empty_state() -> State:
    """Return a minimal State with project=QR scope_id=QR."""
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


def _seed_wave_with_role(state: State, *, role: AgentSessionRole) -> str:
    """Seed P01 -> P01-I01 -> P01-I01-W01 carrying *role* as ``agent_role``."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Wave with role",
        file_scopes=["src/foo/"],
        agent_role=role,
        effort_bucket=EffortBucket.M,
        intent=make_intent(),
    )
    return "P01-I01-W01"


def _seed_roleless_wave(state: State) -> str:
    """Seed P01 -> P01-I01 -> P01-I01-W01 with no ``agent_role``."""
    open_phase(state, phase_id="P01", title="Bootstrap")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="Iter1")
    plan_wave(
        state,
        wave_id="P01-I01-W01",
        iter_id="P01-I01",
        title="Roleless wave",
        file_scopes=["src/"],
        effort_bucket="M",
        intent=make_intent(),
    )
    return "P01-I01-W01"


def _role_tier_block(*, role: str, body: str, block_id: str = "exec-house-rules") -> RenderBlock:
    """Build a role-tier dispatch render block bound to *role*."""
    return RenderBlock(
        id=block_id,
        target=DISPATCH_SYSTEM_PROMPT_TARGET,
        body_template=body,
        agent_role=role,
    )


def _composed_with_role_blocks(*blocks: RenderBlock) -> ComposedProfile:
    """Wrap *blocks* in a minimal ComposedProfile for the role-tier projection."""
    return ComposedProfile(name="test", render_blocks=list(blocks))


# ---- RenderBlock model: role binding strict-schema invariants --------------


def test_render_block_accepts_role_for_dispatch_target() -> None:
    """A dispatch-tier block accepts ``agent_role`` and reads as role-tier."""
    block = _role_tier_block(role="executor", body=_EXECUTOR_BLOCK_BODY)
    assert block.is_role_tier is True
    assert block.agent_role == "executor"
    assert block.body_text == _EXECUTOR_BLOCK_BODY


def test_render_block_rejects_dispatch_target_without_role() -> None:
    """A dispatch-tier block missing ``agent_role`` is a ValidationError."""
    with pytest.raises(ValidationError, match="must name its role"):
        RenderBlock(
            id="bad-no-role",
            target=DISPATCH_SYSTEM_PROMPT_TARGET,
            body_template="body",
        )


def test_render_block_rejects_role_on_managed_file_target() -> None:
    """A managed-file block carrying ``agent_role`` is a ValidationError."""
    with pytest.raises(ValidationError, match="reserved for the dispatch tier"):
        RenderBlock(
            id="bad-role-on-file",
            target="AGENTS.md",
            body_template="body",
            agent_role="executor",
        )


def test_render_block_managed_file_default_role_is_none() -> None:
    """A legacy managed-file block leaves ``agent_role`` at ``None`` (no-op)."""
    block = RenderBlock(id="legacy", target="AGENTS.md", body_template="body")
    assert block.agent_role is None
    assert block.is_role_tier is False


# ---- ComposedProfile.role_tier_blocks projection ---------------------------


def test_role_tier_blocks_maps_role_to_body() -> None:
    """``role_tier_blocks`` projects role-tier blocks into a role->body map."""
    composed = _composed_with_role_blocks(
        _role_tier_block(role="executor", body=_EXECUTOR_BLOCK_BODY),
        RenderBlock(
            id="auditor-house",
            target=DISPATCH_SYSTEM_PROMPT_TARGET,
            body_template="## Auditor rules\n\nBe skeptical of green tests.",
            agent_role="auditor",
        ),
    )
    blocks = composed.role_tier_blocks()
    assert blocks == {
        "executor": _EXECUTOR_BLOCK_BODY,
        "auditor": "## Auditor rules\n\nBe skeptical of green tests.",
    }


def test_role_tier_blocks_ignores_managed_file_blocks() -> None:
    """Managed-file render blocks never appear in the role-tier map."""
    composed = _composed_with_role_blocks(
        RenderBlock(id="agents-block", target="AGENTS.md", body_template="rule text"),
        _role_tier_block(role="executor", body=_EXECUTOR_BLOCK_BODY),
    )
    assert composed.role_tier_blocks() == {"executor": _EXECUTOR_BLOCK_BODY}


def test_role_tier_blocks_empty_when_no_role_blocks() -> None:
    """A profile with no dispatch-tier block yields an empty map."""
    composed = _composed_with_role_blocks(
        RenderBlock(id="agents-block", target="AGENTS.md", body_template="rule text"),
    )
    assert composed.role_tier_blocks() == {}


# ---- Criterion 1: executor block injected into the dispatched prompt -------


def test_executor_role_block_injected_into_dispatch_system_prompt() -> None:
    """An executor role-tier block body appears in the executor wave's prompt.

    Criterion 1: a profile carrying a role-tier block for
    ``agent_role=executor`` yields a dispatched ``system_prompt`` CONTAINING
    that block's body for an executor wave (asserted against the rendered
    envelope text).
    """
    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=AgentSessionRole.EXECUTOR)
    composed = _composed_with_role_blocks(
        _role_tier_block(role="executor", body=_EXECUTOR_BLOCK_BODY),
    )

    envelope = render_dispatch_envelope(
        state, wave_id, "claude-code", role_blocks=composed.role_tier_blocks()
    )

    # The injected block body is present in both the typed contract and the
    # rendered prompt text.
    assert envelope.role_contract is not None
    assert _EXECUTOR_BLOCK_BODY in envelope.role_contract.system_prompt
    assert "Always cite a file:line for every claim" in envelope.prompt
    # The static role body still leads the injected prompt — the block is
    # additive, not a replacement.
    assert "You implement what the planner specified." in envelope.prompt


# ---- Criterion 2: absent block is a byte-identical no-op -------------------


def test_roleless_wave_renders_static_prompt_byte_identical() -> None:
    """A roleless wave keeps the pre-injection prompt byte-for-byte.

    Criterion 2 baseline: with no ``agent_role`` the prompt carries no Role
    contract section at all, and supplying role_blocks does not change a
    single byte (the absent role is a true no-op).
    """
    baseline_state = _empty_state()
    wave_id = _seed_roleless_wave(baseline_state)
    baseline = render_dispatch_envelope(baseline_state, wave_id, "claude-code")

    injected_state = _empty_state()
    _seed_roleless_wave(injected_state)
    injected = render_dispatch_envelope(
        injected_state,
        wave_id,
        "claude-code",
        role_blocks={"executor": _EXECUTOR_BLOCK_BODY},
    )

    assert injected.prompt == baseline.prompt
    assert "## Role contract" not in injected.prompt


def test_unconfigured_role_renders_static_system_prompt_byte_identical() -> None:
    """An executor wave with no executor block keeps the static system_prompt.

    Criterion 2: a wave whose ``agent_role`` has NO role-tier block renders
    the byte-identical static ``RoleSpec.system_prompt`` with no injection and
    no empty-header artifact. Captures the no-injection baseline and asserts
    the role_blocks variant (with an entry for a DIFFERENT role) is identical.
    """
    static_prompt = get_role_spec(AgentSessionRole.EXECUTOR).system_prompt

    # Baseline: no role_blocks at all.
    baseline_state = _empty_state()
    wave_id = _seed_wave_with_role(baseline_state, role=AgentSessionRole.EXECUTOR)
    baseline = render_dispatch_envelope(baseline_state, wave_id, "claude-code")
    assert baseline.role_contract is not None
    # The contract carries the static prompt verbatim — no injection.
    assert baseline.role_contract.system_prompt == static_prompt

    # Variant: role_blocks present, but only for a DIFFERENT role (auditor).
    variant_state = _empty_state()
    _seed_wave_with_role(variant_state, role=AgentSessionRole.EXECUTOR)
    variant = render_dispatch_envelope(
        variant_state,
        wave_id,
        "claude-code",
        role_blocks={"auditor": "## Auditor rules\n\nBe skeptical."},
    )
    assert variant.role_contract is not None
    assert variant.role_contract.system_prompt == static_prompt

    # Byte-identical prompts: an absent matching block is a true no-op, with
    # no empty header and no trailing-newline drift.
    assert variant.prompt == baseline.prompt
    assert "## Auditor rules" not in variant.prompt


# ---- Cross-role: only the matching role is injected ------------------------


def test_executor_block_does_not_leak_into_auditor_prompt() -> None:
    """An executor-keyed block does not inject into an auditor wave's prompt.

    Strengthens criterion 1's "only the matching role is injected" intent:
    the same executor block config renders byte-identically to the
    no-config baseline for an auditor wave.
    """
    role_blocks = {"executor": _EXECUTOR_BLOCK_BODY}

    baseline_state = _empty_state()
    wave_id = _seed_wave_with_role(baseline_state, role=AgentSessionRole.AUDITOR)
    baseline = render_dispatch_envelope(baseline_state, wave_id, "claude-code")

    injected_state = _empty_state()
    _seed_wave_with_role(injected_state, role=AgentSessionRole.AUDITOR)
    injected = render_dispatch_envelope(
        injected_state, wave_id, "claude-code", role_blocks=role_blocks
    )

    assert injected.prompt == baseline.prompt
    assert _EXECUTOR_BLOCK_BODY not in injected.prompt
    assert injected.role_contract is not None
    assert (
        injected.role_contract.system_prompt
        == get_role_spec(AgentSessionRole.AUDITOR).system_prompt
    )


# ---- Blank block body is a no-op -------------------------------------------


def test_blank_role_block_body_is_no_op() -> None:
    """A whitespace-only block body injects nothing (byte-identical no-op)."""
    static_prompt = get_role_spec(AgentSessionRole.EXECUTOR).system_prompt

    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=AgentSessionRole.EXECUTOR)
    envelope = render_dispatch_envelope(
        state, wave_id, "claude-code", role_blocks={"executor": "   \n  "}
    )
    assert envelope.role_contract is not None
    assert envelope.role_contract.system_prompt == static_prompt


# ---- FLEET-6 criterion 1: per-role injection isolation ---------------------

_AUDITOR_BLOCK_BODY = "## House auditor rules\n\nBe skeptical of every green test."

#: Two distinct roles each carrying a distinct role-tier block. The
#: parametrization asserts each block lands only in its own role's prompt and
#: never in a sibling role's prompt.
_ISOLATION_BLOCKS: dict[str, str] = {
    "executor": _EXECUTOR_BLOCK_BODY,
    "auditor": _AUDITOR_BLOCK_BODY,
}
_ISOLATION_CASES = [
    pytest.param(AgentSessionRole.EXECUTOR, "executor", "auditor", id="executor-not-auditor"),
    pytest.param(AgentSessionRole.AUDITOR, "auditor", "executor", id="auditor-not-executor"),
]


@pytest.mark.parametrize(("role", "own_key", "sibling_key"), _ISOLATION_CASES)
def test_role_tier_block_lands_only_in_its_own_role_prompt(
    role: AgentSessionRole, own_key: str, sibling_key: str
) -> None:
    """A configured role block lands in its role's prompt, absent from a sibling.

    FLEET-6 criterion 1: parametrized over each role that has a configured
    role-tier block, that block lands in THAT role's dispatched
    ``system_prompt`` and NOT in a sibling role's prompt. The same two-block
    config is fed to both a wave of *role* and (in the sibling assertion) the
    sibling role, proving the injection is keyed strictly by ``agent_role``.
    """
    own_body = _ISOLATION_BLOCKS[own_key]
    sibling_body = _ISOLATION_BLOCKS[sibling_key]

    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=role)
    envelope = render_dispatch_envelope(
        state, wave_id, "claude-code", role_blocks=_ISOLATION_BLOCKS
    )

    assert envelope.role_contract is not None
    # The role's OWN block is injected into its contract + rendered prompt.
    assert own_body in envelope.role_contract.system_prompt
    assert own_body in envelope.prompt
    # The SIBLING role's block never leaks into this role's prompt.
    assert sibling_body not in envelope.role_contract.system_prompt
    assert sibling_body not in envelope.prompt
    # The static role body still leads — the block is additive, not a replacement.
    assert get_role_spec(role).system_prompt.splitlines()[0] in envelope.prompt


# ---- FLEET-6 criterion 2: over-cap block RAISES (never truncates) ----------


def _over_cap_body(*, cap: int) -> str:
    """Return a role-tier block body whose token weight exceeds *cap*."""
    return "## Oversized rules\n\n" + " ".join(f"word{n}" for n in range(cap + 50))


@pytest.mark.parametrize("role", [AgentSessionRole.EXECUTOR, AgentSessionRole.AUDITOR])
def test_over_cap_role_block_raises_and_emits_no_truncated_prompt(
    role: AgentSessionRole,
) -> None:
    """An over-cap role-tier block fails the render-budget gate by RAISING.

    FLEET-6 criterion 2: a role-tier block whose body exceeds the configured
    role-tier token cap (:data:`DEFAULT_ROLE_TIER_TOKEN_CAP`) FAILS the
    render-budget gate by RAISING rather than truncating — proving the role
    zone honours a budget like the AGENTS.md tier-0 zone. The error names the
    offending role and the cap, and NO truncated prompt is produced.
    """
    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=role)
    body = _over_cap_body(cap=DEFAULT_ROLE_TIER_TOKEN_CAP)
    # Sanity: the body really is over the default cap.
    assert count_tokens(body) > DEFAULT_ROLE_TIER_TOKEN_CAP

    with pytest.raises(RoleTierBudgetError) as excinfo:
        render_dispatch_envelope(state, wave_id, "claude-code", role_blocks={role.value: body})

    message = str(excinfo.value)
    # The error names the offending role and the cap that was breached.
    assert repr(role.value) in message
    assert str(DEFAULT_ROLE_TIER_TOKEN_CAP) in message
    # No truncated prompt: the body never leaks as a clipped fragment.
    assert "Oversized rules" not in message


def test_under_cap_block_passes_but_over_lower_override_raises() -> None:
    """The cap is overridable: a body fitting the default fails a tighter cap.

    Proves the budget cap is a live, overridable seam (not a hardcoded
    constant): the same small executor block injects cleanly under the default
    cap, yet RAISES under a deliberately tight override below its token weight.
    """
    state = _empty_state()
    wave_id = _seed_wave_with_role(state, role=AgentSessionRole.EXECUTOR)
    block_tokens = count_tokens(_EXECUTOR_BLOCK_BODY)
    assert block_tokens < DEFAULT_ROLE_TIER_TOKEN_CAP

    # Default cap: the small block injects without error.
    clean = render_dispatch_envelope(
        state, wave_id, "claude-code", role_blocks={"executor": _EXECUTOR_BLOCK_BODY}
    )
    assert clean.role_contract is not None
    assert _EXECUTOR_BLOCK_BODY in clean.role_contract.system_prompt

    # Override below the block's token weight: the same block now RAISES.
    with pytest.raises(RoleTierBudgetError) as excinfo:
        render_dispatch_envelope(
            state,
            wave_id,
            "claude-code",
            role_blocks={"executor": _EXECUTOR_BLOCK_BODY},
            role_tier_token_cap=block_tokens - 1,
        )
    message = str(excinfo.value)
    assert repr("executor") in message
    assert str(block_tokens - 1) in message
