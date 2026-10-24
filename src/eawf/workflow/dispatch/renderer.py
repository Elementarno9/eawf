"""Role-contract projection and role-tier block resolution for dispatch.

:func:`build_role_contract` projects a registered
:class:`~eawf.workflow.agents.specs.roles.RoleSpec` into the typed
:class:`~eawf.workflow.agents.specs.models.RoleContract` every per-role
plugin surface reads, optionally splicing a profile's role-tier dispatch
block onto the system prompt under a token budget.
:func:`resolve_role_blocks` resolves those blocks and the effective token
cap from layered config and the enabled profiles.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from eawf.kernel.migration.v1_state import State
from eawf.platform.lint.tools.agents_md_budget import count_tokens
from eawf.platform.render_block import DEFAULT_ROLE_TIER_TOKEN_CAP
from eawf.runtime.sandbox.policy import resolve_denied_tools
from eawf.workflow.agents.specs.models import RoleContract, RoleTierBudgetError
from eawf.workflow.agents.specs.roles import RoleSpec

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ResolvedRoleBlocks:
    """The composed profiles' role-tier dispatch inputs, resolved once.

    Attributes:
        role_blocks: ``agent_role -> body`` map from
            :meth:`~eawf.platform.profiles.models.ComposedProfile.role_tier_blocks`.
            Empty when no enabled profile contributes one.
        token_cap: The effective per-block token cap — the
            ``dispatch.role_tier_token_cap`` config leaf when set, else
            :data:`~eawf.platform.render_block.DEFAULT_ROLE_TIER_TOKEN_CAP`.
    """

    role_blocks: dict[str, str]
    token_cap: int


def resolve_role_blocks(repo_root: Path | None, *, unattended: bool = False) -> ResolvedRoleBlocks:
    """Resolve the role-tier dispatch blocks + token cap for *repo_root*.

    The one production seam every dispatch render site calls before
    rendering: it reads ``profiles.enabled`` + the
    ``dispatch.role_tier_token_cap`` leaf from layered config, composes
    the enabled profiles, and returns their role-tier blocks with the
    effective cap. Resolution failures (broken profile YAML, unreadable
    config) degrade to an empty map + the code-default cap with a logged
    warning — a profile problem must not take the dispatch surface down;
    the profile lints own that failure.

    An unattended request composes without every enriched profile that is
    not certified as managed against the ``profiles.certified`` ledger
    (:mod:`eawf.platform.profiles.certification`), so such a profile never
    instructs an agent no operator is watching; each refusal is logged.

    Args:
        repo_root: Repository root anchoring layered config + profile
            discovery. ``None`` returns the empty resolution.
        unattended: ``True`` when no operator attends the Run the blocks
            are rendered for.
    """
    if repo_root is None:
        return ResolvedRoleBlocks(role_blocks={}, token_cap=DEFAULT_ROLE_TIER_TOKEN_CAP)
    from eawf.kernel.config.layered import merge_config
    from eawf.platform.profiles.certification import uncertified_enriched
    from eawf.platform.profiles.loader import load_composed_profile, load_profile

    try:
        merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(f"resolve_role_blocks status=skip err={exc!s}")
        return ResolvedRoleBlocks(role_blocks={}, token_cap=DEFAULT_ROLE_TIER_TOKEN_CAP)
    cap_raw = (merged.get("dispatch") or {}).get("role_tier_token_cap")
    token_cap = (
        int(cap_raw)
        if isinstance(cap_raw, (int, float)) and int(cap_raw) > 0
        else DEFAULT_ROLE_TIER_TOKEN_CAP
    )
    profiles = merged.get("profiles") or {}
    enabled = profiles.get("enabled") or []
    profile_ids = [pid for pid in enabled if isinstance(pid, str)]
    try:
        if unattended:
            refused = uncertified_enriched(
                {pid: load_profile(pid, workspace=repo_root) for pid in profile_ids},
                profiles.get("certified") or {},
            )
            for pid in refused:
                logger.warning(f"resolve_role_blocks status=refused profile={pid} uncertified")
            profile_ids = [pid for pid in profile_ids if pid not in refused]
        if not profile_ids:
            return ResolvedRoleBlocks(role_blocks={}, token_cap=token_cap)
        composed = load_composed_profile(profile_ids, workspace=repo_root)
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(f"resolve_role_blocks status=skip-compose err={exc!s}")
        return ResolvedRoleBlocks(role_blocks={}, token_cap=token_cap)
    return ResolvedRoleBlocks(role_blocks=composed.role_tier_blocks(), token_cap=token_cap)


def _enforce_role_tier_budget(role_value: str, body: str, *, cap: int) -> None:
    """Raise when *body*'s token weight exceeds the role-tier *cap*.

    The role zone honours a token budget the same way the AGENTS.md tier-0
    zone does (FLEET-6): an over-cap block fails fast at render time rather
    than shipping a clipped system prompt. The body is rejected by RAISING,
    never silently truncated. Reuses the canonical
    :func:`~eawf.platform.lint.tools.agents_md_budget.count_tokens` so the
    role tier and the AGENTS.md gate measure tokens identically.

    Args:
        role_value: The role identifier the block is keyed to (named in the
            error so an operator can locate the offending block).
        body: The already-stripped role-tier block body.
        cap: The maximum token weight one role-tier block may carry.

    Raises:
        RoleTierBudgetError: when ``count_tokens(body) > cap``.
    """
    tokens = count_tokens(body)
    if tokens > cap:
        raise RoleTierBudgetError(
            f"role-tier block for {role_value!r} is {tokens} tokens, over the cap of {cap}"
        )


def _inject_role_block(
    system_prompt: str,
    role_block_body: str | None,
    *,
    role_value: str,
    cap: int = DEFAULT_ROLE_TIER_TOKEN_CAP,
) -> str:
    """Return *system_prompt* with the role-tier *role_block_body* appended.

    The injection is additive and byte-stable for the absent case: when
    *role_block_body* is ``None`` or blank the static *system_prompt* is
    returned unchanged (no separator, no trailing-newline drift), so a role
    with no configured block renders byte-for-byte as before. When a body is
    present it is first measured against the role-tier token *cap* — an
    over-cap body RAISES :class:`RoleTierBudgetError` (never truncates,
    mirroring the AGENTS.md tier-0 gate) — then appended after a single
    blank-line separator with its own trailing whitespace stripped, so the
    merged prompt has one canonical shape regardless of how the block body
    was authored.

    Args:
        system_prompt: The static :attr:`RoleSpec.system_prompt`.
        role_block_body: The matching role-tier block body, or ``None`` when
            the role has no configured block.
        role_value: The role identifier the block is keyed to (named in the
            budget-overflow error).
        cap: The role-tier token cap; defaults to
            :data:`~eawf.platform.render_block.DEFAULT_ROLE_TIER_TOKEN_CAP`.

    Returns:
        The static prompt unchanged when no block applies, else the prompt
        with the block body spliced on.

    Raises:
        RoleTierBudgetError: when a non-blank ``role_block_body`` exceeds
            *cap* tokens.
    """
    if role_block_body is None:
        return system_prompt
    body = role_block_body.strip()
    if not body:
        return system_prompt
    _enforce_role_tier_budget(role_value, body, cap=cap)
    return f"{system_prompt.rstrip()}\n\n{body}"


def build_role_contract(
    role: RoleSpec,
    *,
    state: State | None = None,
    wave_id: str | None = None,
    role_block_body: str | None = None,
    role_tier_token_cap: int = DEFAULT_ROLE_TIER_TOKEN_CAP,
) -> RoleContract:
    """Project a :class:`RoleSpec` into a typed :class:`RoleContract`.

    This projection is the keystone seam every
    per-role plugin surface (Claude / Codex / OpenCode / dispatch
    :class:`SubagentSpec`) reads from. The contract is the role-level
    invariants — ``system_prompt``, ``allowed_tools``, ``denied_tools``,
    ``model``, ``memory``, ``report_schema_ref``, ``stop_conditions`` —
    copied off the registered :class:`RoleSpec`.

    Sandbox enforcement: when ``state`` and ``wave_id`` are supplied,
    the wave's :class:`~eawf.runtime.sandbox.policy.SandboxPolicy`
    deny-list is unioned into the contract's ``denied_tools`` and any
    intersecting ``allowed_tools`` are dropped, so a role's tool grant
    cannot leak past the per-wave sandbox.

    Role-tier injection (FLEET-5): when ``role_block_body`` is supplied and
    non-blank, it is appended to the contract's ``system_prompt`` (the
    profile's per-role "Zone 3" dispatch rule). ``None`` or a blank body is
    a true no-op — the static ``RoleSpec.system_prompt`` is copied
    byte-for-byte, so a role with no configured block renders unchanged.

    Role-tier budget (FLEET-6): an injected block whose token weight exceeds
    ``role_tier_token_cap`` is REJECTED by raising
    :class:`~eawf.workflow.agents.specs.models.RoleTierBudgetError` rather than
    truncated — the role zone honours a budget the same way the AGENTS.md
    tier-0 zone does.

    Args:
        role: The source :class:`RoleSpec` (typically resolved from
            :data:`~eawf.workflow.agents.specs.roles.ROLE_REGISTRY` via
            :func:`~eawf.workflow.agents.specs.roles.get_role_spec`).
        state: Optional validated state snapshot. Required when
            ``wave_id`` is supplied so the sandbox deny-list resolves.
        wave_id: Optional wave id. When supplied, the wave's sandbox
            deny-list is intersected into the contract.
        role_block_body: Optional per-role dispatch block body to inject
            into ``system_prompt``. ``None`` (the default) leaves the
            static prompt unchanged.
        role_tier_token_cap: Maximum token weight one injected role-tier
            block may carry; defaults to
            :data:`~eawf.platform.render_block.DEFAULT_ROLE_TIER_TOKEN_CAP`.

    Returns:
        A :class:`RoleContract` carrying the projected role-level
        invariants. The ``allowed_tools`` / ``denied_tools`` lists are
        sorted for deterministic output.

    Raises:
        RoleTierBudgetError: when a non-blank ``role_block_body`` exceeds
            ``role_tier_token_cap`` tokens.
    """
    allowed = set(role.allowed_tools)
    denied = set(role.denied_tools)
    if state is not None and wave_id is not None:
        policy_denied = resolve_denied_tools(state.sandbox_policies, wave_id=wave_id)
        if policy_denied:
            denied |= policy_denied
            removed = allowed & policy_denied
            if removed:
                logger.debug(
                    f"build_role_contract role={role.role.value!r} wave={wave_id!r} "
                    f"removed={sorted(removed)}"
                )
            allowed -= policy_denied
    return RoleContract(
        role=role.role.value,
        summary=role.summary,
        system_prompt=_inject_role_block(
            role.system_prompt,
            role_block_body,
            role_value=role.role.value,
            cap=role_tier_token_cap,
        ),
        allowed_tools=sorted(allowed),
        denied_tools=sorted(denied),
        model=role.model,
        memory=role.memory,
        report_schema_ref=role.report_schema_ref,
        stop_conditions=list(role.stop_conditions),
    )


__all__ = [
    "build_role_contract",
]
