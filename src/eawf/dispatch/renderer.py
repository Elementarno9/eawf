"""Subagent prompt renderer (B025) + dispatch envelope (P10 W03).

The renderer takes a validated :class:`~eawf.state.models.State` and a
target wave id and returns a self-contained Markdown prompt suitable
for handing to an executor subagent. The prompt mirrors the agent-side
context: the wave's scope, its file_scopes, its blocking deps, any
decisions / hypotheses recorded under the same scope, the most recent
audits in scope, and the working-tree wire-up.

P27-I03-W14 reshapes the render path around a typed spec: instead of
concatenating section strings straight out of ``State``,
:func:`build_subagent_spec` projects ``State`` into a typed
:class:`~eawf.agents.specs.models.SubagentSpec`, and
:func:`render_wave_prompt` renders that spec. The rendered bytes are
unchanged — the spec's section renderers reproduce the legacy output —
but the prompt is now produced from a typed, individually-testable
structure rather than an ad-hoc string blob.

The renderer is a pure function: it takes a snapshot of state plus an
optional repo_root and returns a string. The CLI handlers own all
stdout / file writes (see :mod:`eawf.cli.commands.lifecycle`).

Scope resolution: a wave's effective scope is its phase's ``scope_id``.
The chain is ``wave.iter_id`` → :class:`~eawf.state.models.Iter` →
``iter.phase_id`` → :class:`~eawf.state.models.Phase` → ``phase.scope_id``.
A broken link surfaces as a :class:`KeyError` so callers can map the
missing edge to the canonical NOT_FOUND exit code.

P10 W03 adds :func:`render_dispatch_envelope`, a pure dispatch adapter
that wraps the wave prompt in a typed :class:`DispatchEnvelope` for
either the ``claude-code`` runtime (single-string prompt) or the
``claude-agent-sdk`` runtime (SDK invocation block with ``mcp_servers``
and ``allowed_tools`` projected from :attr:`State.mcp_grants`). No new
runtime dependency on the SDK package — render-only.

P20 W14 adds spike-brief surfacing: when ``repo_root`` is supplied, the
renderer scans ``<repo_root>/.ea/local/`` and
``<repo_root>/.ea/local/research/`` for ``*.md`` files whose filename
references the wave id, iter id, or phase id (case-insensitive
substring match). Matched briefs render under a ``## References``
section so the dispatched subagent reads them before starting work.
``.ea/local/`` is gitignored — briefs stay local-only per the
``spike-workflow`` AGENTS.md rule. When no brief exists (or
``repo_root`` is ``None``) the section is omitted.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from eawf.agents.specs.models import (
    SpecAudit,
    SpecDecision,
    SpecDependency,
    SpecHypothesis,
    SpecWorktree,
    SubagentSpec,
)
from eawf.state.models import (
    Audit,
    Decision,
    Hypothesis,
    State,
    Wave,
    WorktreeRecord,
)

logger = logging.getLogger(__name__)


# CLI-facing runtime names. The installer's ``_SUPPORTED_RUNTIMES`` uses
# the internal canonical name ``"claude"`` for back-compat with v0.1
# settings.json writes; the dispatch adapter exposes ``"claude-code"``
# at the CLI surface so the two SDK-vs-CLI cousins read symmetrically.
# This tuple is the source of truth — the CLI layer imports it directly
# rather than re-declaring the allow-list.
_CLI_RUNTIME_CLAUDE_CODE: str = "claude-code"
_CLI_RUNTIME_CLAUDE_AGENT_SDK: str = "claude-agent-sdk"
DISPATCH_RUNTIMES: tuple[str, ...] = (
    _CLI_RUNTIME_CLAUDE_CODE,
    _CLI_RUNTIME_CLAUDE_AGENT_SDK,
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
      :attr:`State.mcp_grants` (W02) — empty when ``mcp_grants`` is
      absent or has no grant for the dispatched wave.

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
            ``"mcp__<server_id>__*"`` glob string.
    """

    runtime: str
    wave_id: str
    prompt: str
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    allowed_tools: list[str] = field(default_factory=list)


def render_dispatch_envelope(
    state: State,
    wave_id: str,
    runtime: str,
    *,
    repo_root: Path | None = None,
) -> DispatchEnvelope:
    """Return a typed :class:`DispatchEnvelope` for *wave_id* and *runtime*.

    Pure function — no I/O, no logging side-effects beyond the module
    logger. The CLI handler owns stdout/file writes; this function only
    produces the typed envelope.

    Args:
        state: Validated, read-only state snapshot.
        wave_id: Target wave id (must exist in ``state.waves``).
        runtime: CLI-facing runtime name. Must be one of
            ``"claude-code"`` or ``"claude-agent-sdk"``; anything else
            raises :class:`ValueError` with the canonical
            ``unknown runtime ...; expected one of [...]`` format that
            matches :func:`eawf.mcp.installer._validate_runtime`.
        repo_root: Forwarded to :func:`render_wave_prompt` for API
            symmetry; not consumed in v0.1.

    Returns:
        :class:`DispatchEnvelope` with ``runtime``, ``wave_id``,
        ``prompt`` populated for every runtime, plus ``mcp_servers`` and
        ``allowed_tools`` populated on the ``claude-agent-sdk`` branch.

    Raises:
        ValueError: ``runtime`` is unsupported.
        KeyError: ``wave_id`` is missing or the wave → iter → phase →
            scope chain has a broken link (propagated from
            :func:`render_wave_prompt`).
    """
    if runtime not in DISPATCH_RUNTIMES:
        raise ValueError(f"unknown runtime {runtime!r}; expected one of {list(DISPATCH_RUNTIMES)}")
    prompt = render_wave_prompt(state, wave_id, repo_root=repo_root)
    if runtime == _CLI_RUNTIME_CLAUDE_CODE:
        return DispatchEnvelope(runtime=runtime, wave_id=wave_id, prompt=prompt)
    # claude-agent-sdk branch: project MCP servers + grant-derived tools.
    mcp_servers = _project_mcp_servers(state)
    allowed_tools = _project_allowed_tools(state, wave_id=wave_id)
    return DispatchEnvelope(
        runtime=runtime,
        wave_id=wave_id,
        prompt=prompt,
        mcp_servers=mcp_servers,
        allowed_tools=allowed_tools,
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
    """
    grants = state.mcp_grants or {}
    tools: set[str] = set()
    for grant in grants.values():
        if grant.scope_kind == "wave" and grant.scope_id == wave_id:
            tools.add(f"mcp__{grant.server_id}__*")
    return sorted(tools)


def build_subagent_spec(
    state: State,
    wave_id: str,
    *,
    repo_root: Path | None = None,
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

    Returns:
        A fully-populated :class:`SubagentSpec`.

    Raises:
        KeyError: When the wave id is missing or the
            wave → iter → phase → scope chain has a broken link.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id!r}")

    scope_id = _resolve_scope_for_wave(state, wave)

    spec = SubagentSpec(
        wave_id=wave.id,
        iter_id=wave.iter_id,
        title=wave.title,
        scope_id=scope_id,
        agent_role=wave.agent_role.value if wave.agent_role else None,
        effort_bucket=wave.effort_bucket.value if wave.effort_bucket else None,
        success_criteria=list(wave.success_criteria),
        file_scopes=list(wave.file_scopes),
        dependencies=_build_dependencies(state, wave),
        decisions=_build_decisions(state, scope_id=scope_id),
        hypotheses=_build_hypotheses(state, scope_id=scope_id),
        recent_audits=_build_recent_audits(state, scope_id=scope_id),
        references=_find_spike_briefs(wave, repo_root=repo_root) if repo_root is not None else [],
        worktree=_build_worktree(state, wave),
    )
    logger.debug(
        f"build_subagent_spec wave={wave.id!r} scope={scope_id!r} "
        f"deps={len(spec.dependencies)} decisions={len(spec.decisions)} "
        f"refs={len(spec.references)}"
    )
    return spec


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

    Returns:
        A single string containing the full Markdown prompt. The
        return is intentionally unindented top-to-bottom so callers can
        emit it verbatim.

    Raises:
        KeyError: When the wave id is missing or the
            wave → iter → phase → scope chain has a broken link.
    """
    spec = build_subagent_spec(state, wave_id, repo_root=repo_root)
    return spec.render()


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
    out = [d for d in pool.values() if d.scope_id == scope_id]
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
    "build_subagent_spec",
    "render_dispatch_envelope",
    "render_wave_prompt",
]
