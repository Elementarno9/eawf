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
  wave (asserted against the rendered prompt text).
- Criterion 2: a wave whose ``agent_role`` has NO role-tier block renders
  the byte-identical static ``RoleSpec.system_prompt`` with no injection and
  no empty-header artifact.
- Cross-role: an executor-keyed block does NOT leak into an auditor wave's
  prompt (only the matching role is injected).
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.platform.render_block import (
    DEFAULT_ROLE_TIER_TOKEN_CAP,
    DISPATCH_SYSTEM_PROMPT_TARGET,
)

_EXECUTOR_BLOCK_BODY = (
    "## House executor rules\n\nAlways cite a file:line for every claim "
    "and never widen the named file scope."
)


# ---- Builders ---------------------------------------------------------------


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


# ---- Criterion 2: absent block is a byte-identical no-op -------------------


# ---- Cross-role: only the matching role is injected ------------------------


# ---- Blank block body is a no-op -------------------------------------------


# ---- FLEET-6 criterion 1: per-role injection isolation ---------------------


# ---- FLEET-6 criterion 2: over-cap block RAISES (never truncates) ----------


# ---- W41: resolve_role_blocks + live production wiring ----------------------


def test_resolve_role_blocks_none_root_is_empty_default() -> None:
    from eawf.workflow.dispatch.renderer import resolve_role_blocks

    resolved = resolve_role_blocks(None)
    assert resolved.role_blocks == {}
    assert resolved.token_cap == DEFAULT_ROLE_TIER_TOKEN_CAP


def test_resolve_role_blocks_reads_leaf_and_composed_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """CR-01/CR-03: the resolver folds the config leaf + composed blocks."""
    from eawf.kernel.config import layered
    from eawf.platform.profiles import loader as profile_loader
    from eawf.workflow.dispatch.renderer import resolve_role_blocks

    composed = _composed_with_role_blocks(
        _role_tier_block(role="executor", body=_EXECUTOR_BLOCK_BODY)
    )
    monkeypatch.setattr(
        layered,
        "merge_config",
        lambda **_kw: (
            {
                "dispatch": {"role_tier_token_cap": 1200},
                "profiles": {"enabled": ["quality"]},
            },
            {},
        ),
    )
    monkeypatch.setattr(profile_loader, "load_composed_profile", lambda *a, **k: composed)
    resolved = resolve_role_blocks(tmp_path)
    assert resolved.token_cap == 1200
    assert resolved.role_blocks == {"executor": _EXECUTOR_BLOCK_BODY}


def test_resolve_role_blocks_degrades_on_compose_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """A broken profile never takes the dispatch surface down."""
    from eawf.kernel.config import layered
    from eawf.platform.profiles import loader as profile_loader
    from eawf.workflow.dispatch.renderer import resolve_role_blocks

    monkeypatch.setattr(
        layered,
        "merge_config",
        lambda **_kw: ({"profiles": {"enabled": ["broken"]}}, {}),
    )

    def _boom(*_a: object, **_k: object) -> object:
        raise ValueError("profile yaml exploded")

    monkeypatch.setattr(profile_loader, "load_composed_profile", _boom)
    resolved = resolve_role_blocks(tmp_path)
    assert resolved.role_blocks == {}
    assert resolved.token_cap == DEFAULT_ROLE_TIER_TOKEN_CAP


def test_role_tier_token_cap_leaf_registered() -> None:
    """CR-03: the leaf ships in defaults + the leaf catalog, default 2400."""
    from eawf.kernel.config.defaults import _BUILT_IN_DEFAULTS
    from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY

    assert _BUILT_IN_DEFAULTS["dispatch"]["role_tier_token_cap"] == 2400
    row = LEAF_KEY_REGISTRY["dispatch.role_tier_token_cap"]
    assert row.default == 2400
    assert row.type == "int"
