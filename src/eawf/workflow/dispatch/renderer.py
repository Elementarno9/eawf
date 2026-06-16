"""Subagent prompt renderer (B025) + dispatch envelope (P10 W03).

The renderer takes a validated :class:`~eawf.kernel.state.models.State` and a
target wave id and returns a self-contained Markdown prompt suitable
for handing to an executor subagent. The prompt mirrors the agent-side
context: the wave's scope, its file_scopes, its blocking deps, any
decisions / hypotheses recorded under the same scope, the most recent
audits in scope, and the working-tree wire-up.

P27-I03-W14 reshapes the render path around a typed spec: instead of
concatenating section strings straight out of ``State``,
:func:`build_subagent_spec` projects ``State`` into a typed
:class:`~eawf.workflow.agents.specs.models.SubagentSpec`, and
:func:`render_wave_prompt` renders that spec. The rendered bytes are
unchanged — the spec's section renderers reproduce the legacy output —
but the prompt is now produced from a typed, individually-testable
structure rather than an ad-hoc string blob.

The renderer is a pure function: it takes a snapshot of state plus an
optional repo_root and returns a string. The CLI handlers own all
stdout / file writes (see :mod:`eawf.surfaces.cli.commands.lifecycle`).

Scope resolution: a wave's effective scope is its phase's ``scope_id``.
The chain is ``wave.iter_id`` → :class:`~eawf.kernel.state.models.Iter` →
``iter.phase_id`` → :class:`~eawf.kernel.state.models.Phase` → ``phase.scope_id``.
A broken link surfaces as a :class:`KeyError` so callers can map the
missing edge to the canonical NOT_FOUND exit code.

P10 W03 adds :func:`render_dispatch_envelope`, a pure dispatch adapter
that wraps the wave prompt in a typed :class:`DispatchEnvelope` for
either the ``claude-code`` runtime (single-string prompt) or the
``claude-agent-sdk`` runtime (SDK invocation block with ``mcp_servers``
and ``allowed_tools`` projected from :attr:`State.mcp_grants`). No new
runtime dependency on the SDK package — render-only. P27-I03-W21 hardens
the SDK ``allowed_tools`` projection into an enforcement seam: tools the
dispatched wave's :class:`~eawf.runtime.sandbox.policy.SandboxPolicy` deny-list
names are intersected out, so a wave with a denied tool cannot dispatch
it.

P20 W14 adds spike-brief surfacing: when ``repo_root`` is supplied, the
renderer scans ``<repo_root>/.ea/local/`` and
``<repo_root>/.ea/local/research/`` for ``*.md`` files whose filename
references the wave id, iter id, or phase id (case-insensitive
substring match). Matched briefs render under a ``## References``
section so the dispatched subagent reads them before starting work.
``.ea/local/`` is gitignored — briefs stay local-only per the
``spike-workflow`` AGENTS.md rule. When no brief exists (or
``repo_root`` is ``None``) the section is omitted.

Memory recall is read-only. When ``state.memory_index`` carries active
entries, the prompt includes a ``## Memory`` section rendered through the
same token-budgeted context walker as ``eawf memory render-context``. The
state object and memory store are never mutated by dispatch rendering.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.kernel.state.enums import AgentSessionRole, DecisionStatus, StoreKind
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import (
    Audit,
    Decision,
    Hypothesis,
    State,
    Wave,
    WorktreeRecord,
)
from eawf.kernel.store.paths import store_path
from eawf.platform.lint.tools.agents_md_budget import count_tokens
from eawf.platform.memory.render_context import DEFAULT_BUDGET, render_context
from eawf.platform.render_block import DEFAULT_ROLE_TIER_TOKEN_CAP
from eawf.runtime.sandbox.policy import resolve_denied_tools
from eawf.workflow.agents.specs.models import (
    RoleContract,
    RoleTierBudgetError,
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecEstimate,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)
from eawf.workflow.agents.specs.roles import RoleSpec, get_role_spec
from eawf.workflow.lifecycle.ceremony import compute_ceremony

logger = logging.getLogger(__name__)


# CLI-facing runtime names. The installer's ``_SUPPORTED_RUNTIMES`` uses
# the internal canonical name ``"claude"`` for back-compat with v0.1
# settings.json writes; the dispatch adapter exposes ``"claude-code"``
# at the CLI surface so the two SDK-vs-CLI cousins read symmetrically.
# This tuple is the source of truth — the CLI layer imports it directly
# rather than re-declaring the allow-list. ``codex`` (D12) and ``opencode``
# share the claude-code envelope shape: ``prompt`` carries the full Markdown
# body and MCP wiring rides through the runtime's own config file rather than
# the envelope, so the dispatch surface stays symmetric across the three
# vendor families.
_CLI_RUNTIME_CLAUDE_CODE: str = "claude-code"
_CLI_RUNTIME_CLAUDE_AGENT_SDK: str = "claude-agent-sdk"
_CLI_RUNTIME_CODEX: str = "codex"
_CLI_RUNTIME_OPENCODE: str = "opencode"
DISPATCH_RUNTIMES: tuple[str, ...] = (
    _CLI_RUNTIME_CLAUDE_CODE,
    _CLI_RUNTIME_CLAUDE_AGENT_SDK,
    _CLI_RUNTIME_CODEX,
    _CLI_RUNTIME_OPENCODE,
)
_HIDDEN_DECISION_STATUSES: frozenset[DecisionStatus] = frozenset(
    {DecisionStatus.OBSOLETE, DecisionStatus.SUPERSEDED}
)


# ---- Public API -------------------------------------------------------------


@dataclass(frozen=True)
class DispatchEnvelope:
    """Typed return value of :func:`render_dispatch_envelope`.

    The same shape covers both runtimes — only the per-field contents
    differ:

    - ``claude-code``: ``prompt`` carries the full Markdown prompt from
      :func:`render_wave_prompt`; ``mcp_servers`` and ``allowed_tools``
      are empty lists (the claude-code agent reads MCP wiring from the
      runtime config on disk, not the envelope).
    - ``claude-agent-sdk``: ``prompt`` carries the same Markdown body
      (the SDK consumer slots it into the system prompt); ``mcp_servers``
      is a list of per-server invocation dicts projected from
      :attr:`State.mcp_servers`; ``allowed_tools`` is a list of
      ``mcp__<server_id>__*`` glob strings projected from
      :attr:`State.mcp_grants` (W02), with any tool the wave's
      :class:`~eawf.runtime.sandbox.policy.SandboxPolicy` deny-list names
      intersected out — empty when ``mcp_grants`` is absent, has no
      grant for the dispatched wave, or every projected tool is denied.

    Attributes:
        runtime: CLI-facing runtime name (``"claude-code"`` or
            ``"claude-agent-sdk"``).
        wave_id: The wave the envelope was rendered for.
        prompt: The Markdown prompt body (shared across both runtimes).
        mcp_servers: SDK-side MCP wiring (empty on the claude-code
            branch). Each entry is a dict with ``id``, ``command``,
            ``args``, ``env_refs`` mirroring :class:`McpServer` fields.
        allowed_tools: SDK ``allowed_tools`` allow-list (empty on the
            claude-code branch). Each entry is a
            ``"mcp__<server_id>__*"`` glob string; entries the wave's
            sandbox-policy deny-list names are removed before projection.
        role_contract: Typed projection of the dispatched wave's role
            (P28-I01-W13). When set, the caller forwards this contract
            to the spawn seam (``RuntimeAdapter.open_session`` accepts a
            matching ``role_contract`` keyword) so the freshly-spawned
            runtime receives the role registry's body without
            re-walking the registry. ``None`` for waves without
            ``agent_role`` so the dispatch stays byte-equivalent to
            the pre-W13 envelope shape.
    """

    runtime: str
    wave_id: str
    prompt: str
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)
    role_contract: RoleContract | None = None


def render_dispatch_envelope(
    state: State,
    wave_id: str,
    runtime: str,
    *,
    repo_root: Path | None = None,
    role_blocks: Mapping[str, str] | None = None,
    role_tier_token_cap: int = DEFAULT_ROLE_TIER_TOKEN_CAP,
    headless: bool = False,
) -> DispatchEnvelope:
    """Return a typed :class:`DispatchEnvelope` for *wave_id* and *runtime*.

    Pure function — no I/O, no logging side-effects beyond the module
    logger. The CLI handler owns stdout/file writes; this function only
    produces the typed envelope.

    Args:
        state: Validated, read-only state snapshot.
        wave_id: Target wave id (must exist in ``state.waves``).
        runtime: CLI-facing runtime name. Must be one of
            :data:`DISPATCH_RUNTIMES` (``"claude-code"`` /
            ``"claude-agent-sdk"`` / ``"codex"`` / ``"opencode"``); anything
            else raises :class:`ValueError` with the canonical
            ``unknown runtime ...; expected one of [...]`` format that
            matches :func:`eawf.runtime.mcp.installer._validate_runtime`.
        repo_root: Optional repo root used for spike-brief discovery and
            memory-body lookup under ``.ea/store/memory.jsonl``.
        role_blocks: Optional ``agent_role -> body`` map of per-role
            dispatch render blocks (the "Zone 3" role tier, FLEET-5). When
            the dispatched wave's ``agent_role`` has an entry, the body is
            injected into the role's ``system_prompt``; a role absent from
            the map (or ``None`` / empty) is a true no-op and the static
            ``RoleSpec.system_prompt`` renders byte-for-byte. Callers
            populate this from
            :meth:`~eawf.platform.profiles.models.ComposedProfile.role_tier_blocks`.
        role_tier_token_cap: Maximum token weight one injected role-tier
            block may carry (FLEET-6). An over-cap block is rejected by
            raising :class:`~eawf.workflow.agents.specs.models.RoleTierBudgetError`,
            never truncated, mirroring the AGENTS.md tier-0 budget gate.
            Defaults to
            :data:`~eawf.platform.render_block.DEFAULT_ROLE_TIER_TOKEN_CAP`.
        headless: ``True`` for the live-spawn (daemon) dispatch path, whose
            downstream reads the spawned model's final message as a JSON
            ``ExecutorReportBody``. When set and the wave's ``agent_role`` is
            ``executor``, the rendered prompt carries a trailing
            ``## Report output`` section pinning the report-body JSON schema
            so the model emits a parseable report on the first try. ``False``
            (default) keeps the prompt byte-equivalent to the interactive
            render an operator-facing Claude Code session sees.

    Returns:
        :class:`DispatchEnvelope` with ``runtime``, ``wave_id``,
        ``prompt`` populated for every runtime, plus ``mcp_servers`` and
        ``allowed_tools`` populated on the ``claude-agent-sdk`` branch.

    Raises:
        ValueError: ``runtime`` is unsupported.
        KeyError: ``wave_id`` is missing or the wave → iter → phase →
            scope chain has a broken link (propagated from
            :func:`render_wave_prompt`).
        RoleTierBudgetError: The dispatched wave's role-tier block exceeds
            ``role_tier_token_cap`` tokens.
    """
    if runtime not in DISPATCH_RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}; expected one of {list(DISPATCH_RUNTIMES)}")
    # Build the typed spec once: its rendered output IS the prompt body,
    # and its `role_contract` IS the typed projection the spawn seam reads
    # (P28-I01-W13). Building the spec once avoids the previous
    # render-then-re-walk pattern where the renderer projected the role
    # registry inside `render_wave_prompt` and the envelope would have to
    # re-project it. The shared spec keeps the two surfaces aligned.
    spec = build_subagent_spec(
        state,
        wave_id,
        repo_root=repo_root,
        role_blocks=role_blocks,
        role_tier_token_cap=role_tier_token_cap,
    )
    prompt = _render_spec_prompt(
        state, spec, wave_id=wave_id, repo_root=repo_root, headless=headless
    )
    if runtime in {_CLI_RUNTIME_CLAUDE_CODE, _CLI_RUNTIME_CODEX, _CLI_RUNTIME_OPENCODE}:
        # codex + opencode share the claude-code envelope shape: ``prompt``
        # carries the full Markdown body and the runtime reads MCP wiring from
        # its own config file (codex ``.codex/config.toml`` per D12, opencode
        # ``opencode.json``) rather than the envelope, mirroring how the
        # claude-code agent reads ``settings.json`` on disk.
        return DispatchEnvelope(
            runtime=runtime,
            wave_id=wave_id,
            prompt=prompt,
            role_contract=spec.role_contract,
        )
    # claude-agent-sdk branch: project MCP servers + grant-derived tools.
    mcp_servers = _project_mcp_servers(state)
    allowed_tools = _project_allowed_tools(state, wave_id=wave_id)
    return DispatchEnvelope(
        runtime=runtime,
        wave_id=wave_id,
        prompt=prompt,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
        role_contract=spec.role_contract,
    )


def _project_mcp_servers(state: State) -> list[dict[str, Any]]:
    """Return the SDK-side ``mcp_servers`` list from :attr:`State.mcp_servers`.

    The shape is intentionally a list of dicts (not :class:`McpServer`
    instances) because the SDK consumer expects JSON-safe primitives.
    Returns ``[]`` when ``state.mcp_servers`` is ``None`` or empty.
    """
    pool = state.mcp_servers or {}
    out: list[dict[str, Any]] = []
    for server_id in sorted(pool):
        server = pool[server_id]
        out.append(
            {
                "id": server.id,
                "command": server.command,
                "args": list(server.args),
                "env_refs": list(server.env_refs),
            }
        )
    return out


def _project_allowed_tools(state: State, *, wave_id: str) -> list[str]:
    """Project grants from ``state.mcp_grants`` to SDK ``allowed_tools`` globs.

    For every grant whose ``scope_kind == "wave"`` and ``scope_id ==
    wave_id``, emit ``"mcp__<server_id>__*"``. The result is sorted and
    de-duplicated so identical grants collapse to one allow-list entry.
    ``state.mcp_grants`` is the nullable map W02 added to :class:`State`;
    ``None`` or an empty map yield ``[]``.

    Sandbox enforcement: any projected tool that the wave's
    :class:`~eawf.runtime.sandbox.policy.SandboxPolicy` deny-list names is
    intersected out before returning, so a wave with a denied tool cannot
    dispatch it. The deny set is resolved by
    :func:`~eawf.runtime.sandbox.policy.resolve_denied_tools` from wave-scoped and
    global policies on ``state.sandbox_policies``.
    """
    grants = state.mcp_grants or {}
    tools: set[str] = set()
    for grant in grants.values():
        if grant.scope_kind == "wave" and grant.scope_id == wave_id:
            tools.add(f"mcp__{grant.server_id}__*")
    denied = resolve_denied_tools(state.sandbox_policies, wave_id=wave_id)
    allowed = tools - denied
    if denied & tools:
        logger.debug(
            f"_project_allowed_tools wave={wave_id!r} "
            f"projected={len(tools)} denied={len(denied & tools)} allowed={len(allowed)}"
        )
    return sorted(allowed)


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

    P28-I01-W12 introduces this projection as the keystone seam every
    per-role plugin surface (Claude / Codex / OpenCode / dispatch
    :class:`SubagentSpec`) reads from. The contract is the role-level
    invariants — ``system_prompt``, ``allowed_tools``, ``denied_tools``,
    ``model``, ``memory``, ``report_schema_ref``, ``stop_conditions`` —
    copied off the registered :class:`RoleSpec`.

    Sandbox enforcement: when ``state`` and ``wave_id`` are supplied,
    the wave's :class:`~eawf.runtime.sandbox.policy.SandboxPolicy`
    deny-list is unioned into the contract's ``denied_tools`` and any
    intersecting ``allowed_tools`` are dropped. This mirrors the
    SDK-envelope projection in
    :func:`_project_allowed_tools` — a role's tool grant cannot leak
    past the per-wave sandbox.

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


def build_subagent_spec(
    state: State,
    wave_id: str,
    *,
    repo_root: Path | None = None,
    role_blocks: Mapping[str, str] | None = None,
    role_tier_token_cap: int = DEFAULT_ROLE_TIER_TOKEN_CAP,
) -> SubagentSpec:
    """Project *state* + *wave_id* into a typed :class:`SubagentSpec`.

    This is the ``State`` → typed-spec half of the render path: it walks
    the wave → iter → phase → scope chain, collects the in-scope
    decisions / hypotheses / recent audits, resolves the worktree
    wire-up, and surfaces matching spike briefs. The returned spec
    renders itself via :meth:`SubagentSpec.render`; this function does no
    string formatting.

    Args:
        state: Validated, read-only state snapshot.
        wave_id: Target wave id (must exist in ``state.waves``).
        repo_root: Optional repo root. When supplied, spike briefs whose
            filename mentions the wave / iter / phase id are surfaced in
            :attr:`SubagentSpec.references`; ``None`` skips the scan.
        role_blocks: Optional ``agent_role -> body`` map of per-role
            dispatch render blocks. When the wave's ``agent_role`` has an
            entry, that body is injected into the projected
            :class:`RoleContract.system_prompt`; an absent role is a true
            no-op (the static prompt is copied byte-for-byte).
        role_tier_token_cap: Maximum token weight one injected role-tier
            block may carry; defaults to
            :data:`~eawf.platform.render_block.DEFAULT_ROLE_TIER_TOKEN_CAP`.

    Returns:
        A fully-populated :class:`SubagentSpec`.

    Raises:
        KeyError: When the wave id is missing or the
            wave → iter → phase → scope chain has a broken link.
        RoleTierBudgetError: When the wave's role-tier block exceeds
            ``role_tier_token_cap`` tokens.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")

    scope_id = _resolve_scope_for_wave(state, wave)

    spec = SubagentSpec(
        wave_id=wave.id,
        iter_id=wave.iter_id,
        title=wave.title,
        description=wave.description,
        scope_id=scope_id,
        agent_role=wave.agent_role.value if wave.agent_role else None,
        effort_bucket=wave.effort_bucket.value if wave.effort_bucket else None,
        success_criteria=[c.text for c in wave.success_criteria],
        file_scopes=list(wave.file_scopes),
        dependencies=_build_dependencies(state, wave),
        decisions=_build_decisions(state, scope_id=scope_id),
        hypotheses=_build_hypotheses(state, scope_id=scope_id),
        recent_audits=_build_recent_audits(state, scope_id=scope_id),
        references=_find_spike_briefs(wave, repo_root=repo_root) if repo_root is not None else [],
        worktree=_build_worktree(state, wave),
        estimate=_build_estimate(state, wave),
        role_contract=_build_role_contract_for_wave(
            state, wave, role_blocks=role_blocks, role_tier_token_cap=role_tier_token_cap
        ),
    )
    logger.debug(
        f"build_subagent_spec wave={wave.id!r} scope={scope_id!r} "
        f"deps={len(spec.dependencies)} decisions={len(spec.decisions)} "
        f"refs={len(spec.references)} role_contract={spec.role_contract is not None}"
    )
    return spec


def _build_estimate(state: State, wave: Wave) -> SpecEstimate:
    """Return dispatch estimate hints for *wave*."""
    estimate = (state.estimates or {}).get(wave.id)
    return SpecEstimate(
        effort_bucket=wave.effort_bucket.value if wave.effort_bucket else None,
        expected_eu=estimate.expected_eu if estimate is not None else None,
        expected_minutes=estimate.expected_minutes if estimate is not None else None,
        token_budget=wave.token_budget,
        parallel_siblings=_parallel_siblings(state, wave),
    )


def _parallel_siblings(state: State, wave: Wave) -> list[str]:
    """Return active sibling waves in the same iter, excluding *wave*."""
    siblings: list[str] = []
    for active_wave_id in state.current.active_wave_ids:
        if active_wave_id == wave.id:
            continue
        active_wave = state.waves.get(active_wave_id)
        if active_wave is None or active_wave.iter_id != wave.iter_id:
            continue
        siblings.append(active_wave_id)
    return sorted(siblings, key=natural_key)


def _build_role_contract_for_wave(
    state: State,
    wave: Wave,
    *,
    role_blocks: Mapping[str, str] | None = None,
    role_tier_token_cap: int = DEFAULT_ROLE_TIER_TOKEN_CAP,
) -> RoleContract | None:
    """Return the wave's :class:`RoleContract`, or ``None`` when no role set.

    Looks up the wave's :attr:`Wave.agent_role` in the global
    :data:`~eawf.workflow.agents.specs.roles.ROLE_REGISTRY` and projects
    the resolved :class:`RoleSpec` via :func:`build_role_contract`,
    threading the wave's sandbox deny-list through. A wave without an
    ``agent_role`` returns ``None`` so the dispatch prompt stays
    byte-equivalent to the pre-W12 renderer.

    When *role_blocks* carries an entry for the wave's ``agent_role``, the
    matching body is injected into the contract's ``system_prompt`` (the
    role-tier dispatch block, FLEET-5); an absent role is a no-op. An injected
    block over *role_tier_token_cap* tokens raises
    :class:`~eawf.workflow.agents.specs.models.RoleTierBudgetError` (FLEET-6).

    Raises:
        RoleTierBudgetError: when the wave's role-tier block exceeds
            ``role_tier_token_cap`` tokens.
    """
    if wave.agent_role is None:
        return None
    role_value = _as_agent_session_role(wave.agent_role).value
    try:
        role_spec = get_role_spec(_as_agent_session_role(wave.agent_role))
    except KeyError:
        return None
    role_block_body = role_blocks.get(role_value) if role_blocks is not None else None
    return build_role_contract(
        role_spec,
        state=state,
        wave_id=wave.id,
        role_block_body=role_block_body,
        role_tier_token_cap=role_tier_token_cap,
    )


def _as_agent_session_role(value: AgentSessionRole | str) -> AgentSessionRole:
    """Coerce a wave's ``agent_role`` field into an :class:`AgentSessionRole`.

    The state model declares ``Wave.agent_role`` as the enum, but
    JSON-loaded snapshots may surface it as the string value; the
    coercion stays defensive so the role lookup never blows up on a
    valid registry value.
    """
    if isinstance(value, AgentSessionRole):
        return value
    return AgentSessionRole(value)


def render_wave_prompt(
    state: State,
    wave_id: str,
    *,
    repo_root: Path | None = None,
) -> str:
    """Return the Markdown prompt for *wave_id*.

    Builds a typed :class:`SubagentSpec` via :func:`build_subagent_spec`
    and renders it. The rendered bytes are identical to the pre-W14
    ad-hoc renderer.

    Args:
        state: Validated, read-only state snapshot.
        wave_id: Target wave id (must exist in ``state.waves``).
        repo_root: Optional repo root. When supplied, the renderer
            scans ``<repo_root>/.ea/local/`` and
            ``<repo_root>/.ea/local/research/`` for spike briefs
            (``*.md`` files whose filename mentions the wave id,
            iter id, or phase id) and emits them under a
            ``## References`` section. ``None`` (default) skips the
            scan and omits the section — preserves the v0.1 surface
            for callers that have not yet plumbed the repo path
            through. ``.ea/local/`` is gitignored, so the briefs
            stay local-only per the ``spike-workflow`` rule.
            Memory recall uses the same root for full-body lookup when
            supplied; without it, dispatch still renders cache summaries
            from ``state.memory_index`` and treats missing bodies as empty.

    Returns:
        A single string containing the full Markdown prompt. The
        return is intentionally unindented top-to-bottom so callers can
        emit it verbatim.

    Raises:
        KeyError: When the wave id is missing or the
            wave → iter → phase → scope chain has a broken link.
    """
    spec = build_subagent_spec(state, wave_id, repo_root=repo_root)
    return _render_spec_prompt(state, spec, wave_id=wave_id, repo_root=repo_root)


def _render_spec_prompt(
    state: State,
    spec: SubagentSpec,
    *,
    wave_id: str,
    repo_root: Path | None,
    headless: bool = False,
) -> str:
    """Render *spec* and splice in read-only dispatch context when present.

    *headless* forwards to :meth:`SubagentSpec.render` so the live-spawn
    path gets the trailing ``## Report output`` schema block; the
    interactive default leaves the prompt byte-equivalent.
    """
    prompt = spec.render(headless=headless)
    ceremony = _render_ceremony_section(state, wave_id=wave_id)
    if ceremony is not None:
        prompt = _insert_section_after_heading(prompt, ceremony, heading="## Wave tags")
    section = _render_memory_section(state, wave_id=wave_id, repo_root=repo_root)
    if section is None:
        return prompt
    return _insert_section_after_heading(prompt, section, heading="## Recent audits")


def _render_ceremony_section(state: State, *, wave_id: str) -> str | None:
    """Return the dispatch ``## Ceremony`` section when history exists."""
    recommendation = compute_ceremony(state, wave_id=wave_id)
    if recommendation.closed_wave_count == 0:
        return None
    latest = (
        recommendation.operator_confirmed_wave_ids[0]
        if recommendation.operator_confirmed_wave_ids
        else "none"
    )
    return "\n".join(
        [
            "## Ceremony",
            "",
            f"- recommendation: mode {recommendation.mode}",
            f"- operator_confirmed_counter: {recommendation.operator_confirmed_counter}",
            f"- closed_wave_count: {recommendation.closed_wave_count}",
            f"- latest_operator_confirmed_wave: {latest}",
            f"- basis: {recommendation.reason}",
        ]
    )


def _render_memory_section(
    state: State,
    *,
    wave_id: str,
    repo_root: Path | None,
) -> str | None:
    """Return the dispatch ``## Memory`` section, or ``None`` when empty."""
    if not state.memory_index:
        return None
    wave = state.waves.get(wave_id)
    if wave is None:
        return None
    memory_path = _memory_path_for_repo(repo_root)
    result = render_context(
        state=state,
        memory_path=memory_path,
        anchor_scope=wave.id,
        budget=DEFAULT_BUDGET,
        heading_level=3,
    )
    if not result.included_ids and not result.skipped_ids:
        return None
    lines = [
        "## Memory",
        "",
        f"Read-only recall for {wave.id}; budget={result.budget} tokens_used={result.tokens_used}.",
    ]
    if result.text:
        lines.extend(["", result.text.rstrip()])
    else:
        lines.extend(["", "No entries fit the token budget."])
    if result.skipped_ids:
        lines.extend(["", f"[skipped: {len(result.skipped_ids)}]"])
    return "\n".join(lines)


def _memory_path_for_repo(repo_root: Path | None) -> Path:
    """Return the canonical memory store path for *repo_root* or the current cwd."""
    root = repo_root if repo_root is not None else Path.cwd()
    return store_path(root / ".ea" / "state.json", StoreKind.MEMORY)


def _insert_section_after_heading(prompt: str, section: str, *, heading: str) -> str:
    """Insert *section* after the Markdown section headed by *heading*."""
    start = prompt.find(heading)
    if start == -1:
        return f"{prompt.rstrip()}\n\n{section.rstrip()}\n"
    next_section = prompt.find("\n\n## ", start + len(heading))
    if next_section == -1:
        return f"{prompt.rstrip()}\n\n{section.rstrip()}\n"
    return f"{prompt[:next_section]}\n\n{section.rstrip()}{prompt[next_section:]}"


# ---- Scope resolution -------------------------------------------------------


def _resolve_scope_for_wave(state: State, wave: Wave) -> str:
    """Walk wave → iter → phase → scope_id; raise on any broken link."""
    it = state.iters.get(wave.iter_id)
    if it is None:
        raise KeyError(
            f"wave {wave.id!r} references unknown iter {wave.iter_id!r}; cannot resolve scope"
        )
    phase = state.phases.get(it.phase_id)
    if phase is None:
        raise KeyError(
            f"iter {it.id!r} references unknown phase {it.phase_id!r}; cannot resolve scope"
        )
    return phase.scope_id


# ---- Section builders -------------------------------------------------------


def _build_dependencies(state: State, wave: Wave) -> list[SpecDependency]:
    """Project a wave's ``deps`` into typed :class:`SpecDependency` rows.

    A dep id absent from ``state.waves`` becomes a row with ``title`` /
    ``status`` left ``None`` so the rendered prompt surfaces the missing
    edge (``status=unknown``) rather than raising — the strict
    invariants in ``validate_state`` already catch dangling deps on the
    mutation seam.
    """
    rows: list[SpecDependency] = []
    for dep_id in wave.deps:
        dep = state.waves.get(dep_id)
        if dep is None:
            rows.append(SpecDependency(wave_id=dep_id))
        else:
            rows.append(SpecDependency(wave_id=dep_id, title=dep.title, status=dep.status.value))
    return rows


def _build_decisions(state: State, *, scope_id: str) -> list[SpecDecision]:
    """Project in-scope decisions into typed :class:`SpecDecision` rows."""
    return [
        SpecDecision(
            decision_id=decision.id,
            title=decision.title,
            rationale=decision.rationale,
        )
        for decision in _decisions_for_scope(state, scope_id=scope_id)
    ]


def _build_hypotheses(state: State, *, scope_id: str) -> list[SpecHypothesis]:
    """Project in-scope hypotheses into typed :class:`SpecHypothesis` rows."""
    return [
        SpecHypothesis(
            hypothesis_id=hyp.id,
            metric=hyp.metric,
            confirm=hyp.confirm,
            reject=hyp.reject,
            verdict=hyp.verdict.value if hyp.verdict is not None else None,
        )
        for hyp in _hypotheses_for_scope(state, scope_id=scope_id)
    ]


def _build_recent_audits(state: State, *, scope_id: str) -> list[SpecAudit]:
    """Project recent in-scope audits into typed :class:`SpecAudit` rows."""
    return [
        SpecAudit(
            audit_id=audit.id,
            kind=audit.kind.value,
            verdict=audit.verdict.value if audit.verdict is not None else None,
        )
        for audit in _recent_audits_for_scope(state, scope_id=scope_id, limit=5)
    ]


def _build_worktree(state: State, wave: Wave) -> SpecWorktree | None:
    """Project a wave's worktree record into a typed :class:`SpecWorktree`."""
    record = _worktree_record_for_wave(state, wave)
    if record is None:
        return None
    return SpecWorktree(
        branch=record.branch,
        path=record.path,
        base_branch=record.base_branch,
    )


def _find_spike_briefs(wave: Wave, *, repo_root: Path) -> list[str]:
    """Return repo-relative paths to spike briefs matching the wave.

    Scans ``<repo_root>/.ea/local/`` (non-recursive) and
    ``<repo_root>/.ea/local/research/`` (non-recursive). Match is a
    case-insensitive substring test of the filename against the wave id,
    iter id, and phase id. The returned list is sorted lexicographically
    for deterministic output across runs.

    Returns ``[]`` when ``.ea/local/`` does not exist or no file
    matches — callers treat that as "skip the section".
    """
    local_root = repo_root / ".ea" / "local"
    if not local_root.is_dir():
        logger.debug(f"_find_spike_briefs wave={wave.id!r} reason=local-dir-absent")
        return []

    phase_segment, _ = _phase_wave_commit_prefix(wave.id)
    tokens = {wave.id.lower(), wave.iter_id.lower(), phase_segment.lower()}

    candidates: list[Path] = []
    for sub in (local_root, local_root / "research"):
        if not sub.is_dir():
            continue
        for entry in sub.iterdir():
            if entry.is_file() and entry.suffix == ".md":
                candidates.append(entry)

    matched: list[str] = []
    for path in candidates:
        name_lower = path.name.lower()
        if any(tok in name_lower for tok in tokens):
            rel = path.relative_to(repo_root).as_posix()
            matched.append(rel)
    matched.sort()
    logger.debug(
        f"_find_spike_briefs wave={wave.id!r} matched={len(matched)} candidates={len(candidates)}"
    )
    return matched


# ---- Lookup helpers ---------------------------------------------------------


def _decisions_for_scope(state: State, *, scope_id: str) -> list[Decision]:
    """Return decisions attached to *scope_id*, sorted by id."""
    pool = state.decisions or {}
    out = [
        d
        for d in pool.values()
        if d.scope_id == scope_id and d.status not in _HIDDEN_DECISION_STATUSES
    ]
    out.sort(key=lambda d: d.id)
    return out


def _hypotheses_for_scope(state: State, *, scope_id: str) -> list[Hypothesis]:
    """Return hypotheses attached to *scope_id*, sorted by id."""
    pool = state.hypotheses or {}
    out = [h for h in pool.values() if h.scope_id == scope_id]
    out.sort(key=lambda h: h.id)
    return out


def _recent_audits_for_scope(
    state: State,
    *,
    scope_id: str,
    limit: int,
) -> list[Audit]:
    """Return the last *limit* audits in *scope_id*, sorted by created_at desc."""
    pool = state.audits or {}
    matching = [a for a in pool.values() if a.scope_id == scope_id]
    matching.sort(key=lambda a: a.created_at, reverse=True)
    return matching[:limit]


def _worktree_record_for_wave(state: State, wave: Wave) -> WorktreeRecord | None:
    """Return the wave's :class:`WorktreeRecord` when present, else ``None``."""
    if wave.worktree_id is None:
        return None
    pool = state.worktrees or {}
    return pool.get(wave.worktree_id)


def _phase_wave_commit_prefix(wave_id: str) -> tuple[str, str]:
    """Split a wave id ``Pxx-Iyy-Wzz`` into ``("Pxx", "Wzz")`` for commit prefix."""
    parts = wave_id.split("-")
    if len(parts) < 3:
        # IdStr regex on the model already enforces three segments, but the
        # renderer stays defensive so a hand-crafted state never crashes.
        return wave_id, "WXX"
    return parts[0], parts[2]


__all__ = [
    "DISPATCH_RUNTIMES",
    "DispatchEnvelope",
    "build_role_contract",
    "build_subagent_spec",
    "render_dispatch_envelope",
    "render_wave_prompt",
]
