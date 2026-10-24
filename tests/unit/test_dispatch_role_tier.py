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
- :func:`build_role_contract` appends a role-tier body after one blank line,
  copies the static prompt byte-for-byte when the body is absent or blank,
  and accepts a body at the token cap but raises one token over it.
- :func:`resolve_role_blocks` folds the cap leaf and the composed blocks, and
  degrades to the empty map and the code-default cap on any failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.state.enums import AgentSessionRole
from eawf.platform.lint.tools.agents_md_budget import count_tokens
from eawf.platform.profiles.models import ComposedProfile, RenderBlock
from eawf.platform.render_block import (
    DEFAULT_ROLE_TIER_TOKEN_CAP,
    DISPATCH_SYSTEM_PROMPT_TARGET,
)
from eawf.workflow.agents.specs.models import RoleTierBudgetError
from eawf.workflow.agents.specs.roles import RoleSpec
from eawf.workflow.dispatch.renderer import build_role_contract, resolve_role_blocks

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


_STATIC_PROMPT = "You are the executor.\n"


def _executor_role() -> RoleSpec:
    """Build an executor RoleSpec with a known static prompt."""
    return RoleSpec(
        role=AgentSessionRole.EXECUTOR,
        summary="s",
        system_prompt=_STATIC_PROMPT,
        allowed_tools=["Read"],
        report_schema_ref="executor_report",
        report_store_kind="executor_report",
    )


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


# ---- build_role_contract: role-tier injection ------------------------------


def test_role_block_is_appended_after_one_blank_line() -> None:
    """A role-tier body lands after the stripped static prompt and one blank line."""
    contract = build_role_contract(_executor_role(), role_block_body=f"{_EXECUTOR_BLOCK_BODY}\n\n")
    assert contract.system_prompt == f"You are the executor.\n\n{_EXECUTOR_BLOCK_BODY}"


@pytest.mark.parametrize("body", [None, "", "   \n\t "])
def test_absent_or_blank_role_block_keeps_the_static_prompt(body: str | None) -> None:
    """No block, an empty block and a whitespace block all copy the prompt byte-for-byte."""
    contract = build_role_contract(_executor_role(), role_block_body=body)
    assert contract.system_prompt == _STATIC_PROMPT


def test_role_block_at_the_token_cap_is_accepted() -> None:
    """A body weighing exactly the cap is within budget."""
    cap = count_tokens(_EXECUTOR_BLOCK_BODY)
    contract = build_role_contract(
        _executor_role(), role_block_body=_EXECUTOR_BLOCK_BODY, role_tier_token_cap=cap
    )
    assert contract.system_prompt.endswith(_EXECUTOR_BLOCK_BODY)


def test_role_block_one_token_over_the_cap_raises() -> None:
    """One token over the cap raises instead of truncating, naming role and cap."""
    cap = count_tokens(_EXECUTOR_BLOCK_BODY) - 1
    with pytest.raises(RoleTierBudgetError, match=rf"'executor'.*over the cap of {cap}$"):
        build_role_contract(
            _executor_role(), role_block_body=_EXECUTOR_BLOCK_BODY, role_tier_token_cap=cap
        )


def test_blank_role_block_skips_the_budget_check() -> None:
    """A blank body is a no-op even under a zero cap: nothing is measured."""
    contract = build_role_contract(_executor_role(), role_block_body="  ", role_tier_token_cap=0)
    assert contract.system_prompt == _STATIC_PROMPT


# ---- W41: resolve_role_blocks + live production wiring ----------------------


def test_resolve_role_blocks_none_root_is_empty_default() -> None:
    resolved = resolve_role_blocks(None)
    assert resolved.role_blocks == {}
    assert resolved.token_cap == DEFAULT_ROLE_TIER_TOKEN_CAP


def test_resolve_role_blocks_reads_leaf_and_composed_blocks(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """CR-01/CR-03: the resolver folds the config leaf + composed blocks."""
    from eawf.kernel.config import layered
    from eawf.platform.profiles import loader as profile_loader

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


def _patch_merged_config(monkeypatch: pytest.MonkeyPatch, merged: dict[str, object]) -> None:
    """Make layered config resolve to *merged* for every root."""
    from eawf.kernel.config import layered

    monkeypatch.setattr(layered, "merge_config", lambda **_kw: (merged, {}))


def test_resolve_role_blocks_degrades_on_config_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unreadable layered config yields the empty map and the code-default cap."""
    from eawf.kernel.config import layered

    def _boom(**_kw: object) -> object:
        raise OSError("config unreadable")

    monkeypatch.setattr(layered, "merge_config", _boom)
    resolved = resolve_role_blocks(tmp_path)
    assert resolved.role_blocks == {}
    assert resolved.token_cap == DEFAULT_ROLE_TIER_TOKEN_CAP


@pytest.mark.parametrize("cap_raw", [0, -5, "1200", None])
def test_resolve_role_blocks_ignores_an_unusable_cap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, cap_raw: object
) -> None:
    """A zero, negative, string or missing cap falls back to the code default."""
    _patch_merged_config(monkeypatch, {"dispatch": {"role_tier_token_cap": cap_raw}})
    assert resolve_role_blocks(tmp_path).token_cap == DEFAULT_ROLE_TIER_TOKEN_CAP


def test_resolve_role_blocks_float_cap_is_truncated(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A float cap from YAML is honoured as its integer part."""
    _patch_merged_config(monkeypatch, {"dispatch": {"role_tier_token_cap": 1200.9}})
    assert resolve_role_blocks(tmp_path).token_cap == 1200


def test_resolve_role_blocks_without_enabled_profiles_composes_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No enabled string profile id means no composition, only the configured cap."""
    from eawf.platform.profiles import loader as profile_loader

    def _never(*_a: object, **_k: object) -> object:
        raise AssertionError("composed with no enabled profile")

    monkeypatch.setattr(profile_loader, "load_composed_profile", _never)
    _patch_merged_config(
        monkeypatch,
        {"dispatch": {"role_tier_token_cap": 900}, "profiles": {"enabled": [7, None]}},
    )
    resolved = resolve_role_blocks(tmp_path)
    assert resolved.role_blocks == {}
    assert resolved.token_cap == 900


def test_role_tier_token_cap_leaf_registered() -> None:
    """CR-03: the leaf ships in defaults + the leaf catalog, default 2400."""
    from eawf.kernel.config.defaults import _BUILT_IN_DEFAULTS
    from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY

    assert _BUILT_IN_DEFAULTS["dispatch"]["role_tier_token_cap"] == 2400
    row = LEAF_KEY_REGISTRY["dispatch.role_tier_token_cap"]
    assert row.default == 2400
    assert row.type == "int"
