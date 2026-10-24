"""Unit tests for the role binding of a dispatch-tier render block.

A profile may attach a render block keyed by ``agent_role`` to the
dispatch system-prompt target. ``RenderBlock`` accepts the ``agent_role``
field for such a block and rejects the incoherent combinations: a
dispatch target without a role, and a role on a managed-file target.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.platform.profiles.models import RenderBlock
from eawf.platform.render_block import DISPATCH_SYSTEM_PROMPT_TARGET

_EXECUTOR_BLOCK_BODY = (
    "## House executor rules\n\nAlways cite a file:line for every claim "
    "and never widen the named file scope."
)


def _role_tier_block(*, role: str, body: str, block_id: str = "exec-house-rules") -> RenderBlock:
    """Build a role-tier dispatch render block bound to *role*."""
    return RenderBlock(
        id=block_id,
        target=DISPATCH_SYSTEM_PROMPT_TARGET,
        body_template=body,
        agent_role=role,
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
