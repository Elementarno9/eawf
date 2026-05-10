"""Subagent prompt renderer (B025).

The renderer takes a validated :class:`~eawf.state.models.State` and a
target wave id and returns a self-contained Markdown prompt suitable
for handing to an executor subagent. The prompt mirrors the agent-side
context: the wave's scope, its file_scopes, its blocking deps, any
decisions / hypotheses recorded under the same scope, the most recent
audits in scope, and the working-tree wire-up.

The renderer is a pure function: it takes a snapshot of state plus an
optional repo_root and returns a string. The CLI handlers own all
stdout / file writes (see :mod:`eawf.cli.commands.lifecycle`).

Scope resolution: a wave's effective scope is its phase's ``scope_id``.
The chain is ``wave.iter_id`` → :class:`~eawf.state.models.Iter` →
``iter.phase_id`` → :class:`~eawf.state.models.Phase` → ``phase.scope_id``.
A broken link surfaces as a :class:`KeyError` so callers can map the
missing edge to the canonical NOT_FOUND exit code.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.state.models import (
    Audit,
    Decision,
    Hypothesis,
    State,
    Wave,
    WorktreeRecord,
)

logger = logging.getLogger(__name__)


# ---- Public API -------------------------------------------------------------


def render_wave_prompt(
    state: State,
    wave_id: str,
    *,
    repo_root: Path | None = None,
) -> str:
    """Return the Markdown prompt for *wave_id*.

    Args:
        state: Validated, read-only state snapshot.
        wave_id: Target wave id (must exist in ``state.waves``).
        repo_root: Optional repo root. Reserved for future expansions
            that read git refs; the v0.1 surface ignores it but accepts
            it so the call signature stays stable for callers that
            already plumb the repo path through.

    Returns:
        A single string containing the full Markdown prompt. The
        return is intentionally unindented top-to-bottom so callers can
        emit it verbatim.

    Raises:
        KeyError: When the wave id is missing or the
            wave → iter → phase → scope chain has a broken link.
    """
    _ = repo_root  # accepted for API symmetry; not consumed in v0.1
    wave = state.waves.get(wave_id)
    if wave is None:
        raise KeyError(f"unknown wave: {wave_id}")

    scope_id = _resolve_scope_for_wave(state, wave)

    sections: list[str] = []
    sections.append(_render_header(wave))
    sections.append(_render_scope(wave, scope_id=scope_id))
    sections.append(_render_dependencies(state, wave))
    sections.append(_render_decisions(state, scope_id=scope_id))
    sections.append(_render_hypotheses(state, scope_id=scope_id))
    sections.append(_render_recent_audits(state, scope_id=scope_id))
    sections.append(_render_working_tree(state, wave))
    sections.append(_render_workflow(wave))
    sections.append(_render_out_of_scope())

    return "\n\n".join(sections).rstrip() + "\n"


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


# ---- Section renderers ------------------------------------------------------


def _render_header(wave: Wave) -> str:
    return f"# Wave {wave.id}: {wave.title}"


def _render_scope(wave: Wave, *, scope_id: str) -> str:
    files = ", ".join(wave.file_scopes) if wave.file_scopes else "(none)"
    rationale = (
        f"Scope is anchored on iter {wave.iter_id} under scope {scope_id}. "
        f"Stay inside the listed file_scopes — any change outside this list "
        f"is out of scope for this wave."
    )
    return f"## Scope\n\n{files}\n\n{rationale}"


def _render_dependencies(state: State, wave: Wave) -> str:
    lines = ["## Dependencies", ""]
    if not wave.deps:
        lines.append("None.")
        return "\n".join(lines)
    for dep_id in wave.deps:
        dep = state.waves.get(dep_id)
        if dep is None:
            # Surface the missing edge in the prompt rather than raising; the
            # renderer is best-effort for non-fatal gaps. The strict
            # invariants in ``validate_state`` already catch dangling deps
            # on the mutation seam.
            lines.append(f"- {dep_id}: (missing from state) (status=unknown)")
        else:
            lines.append(f"- {dep_id}: {dep.title} (status={dep.status.value})")
    return "\n".join(lines)


def _render_decisions(state: State, *, scope_id: str) -> str:
    matching = _decisions_for_scope(state, scope_id=scope_id)
    if not matching:
        return "## Decisions\n\nNone."
    parts = ["## Decisions"]
    for decision in matching:
        parts.append("")
        parts.append(f"### {decision.id}: {decision.summary}")
        parts.append("")
        parts.append(decision.rationale.rstrip())
    return "\n".join(parts)


def _render_hypotheses(state: State, *, scope_id: str) -> str:
    matching = _hypotheses_for_scope(state, scope_id=scope_id)
    if not matching:
        return "## Hypotheses\n\nNone."
    lines = ["## Hypotheses", ""]
    for hyp in matching:
        verdict = hyp.verdict.value if hyp.verdict is not None else "open"
        lines.append(f"- {hyp.id}: metric={hyp.metric!r}")
        lines.append(f"    confirm: {hyp.confirm}")
        lines.append(f"    reject:  {hyp.reject}")
        lines.append(f"    verdict: {verdict}")
    return "\n".join(lines)


def _render_recent_audits(state: State, *, scope_id: str) -> str:
    matching = _recent_audits_for_scope(state, scope_id=scope_id, limit=5)
    if not matching:
        return "## Recent audits\n\nNone."
    lines = ["## Recent audits", ""]
    for audit in matching:
        verdict = audit.verdict.value if audit.verdict is not None else "pending"
        lines.append(f"- {audit.id}: {audit.kind.value} verdict={verdict}")
    return "\n".join(lines)


def _render_working_tree(state: State, wave: Wave) -> str:
    record = _worktree_record_for_wave(state, wave)
    lines = ["## Working tree", ""]
    if record is not None:
        lines.append(f"Branch: {record.branch}")
        lines.append(f"Worktree path: {record.path}")
        lines.append(f"Base commit: {record.base_branch}")
    else:
        lines.append("Worktree path: inline")
    return "\n".join(lines)


def _render_workflow(wave: Wave) -> str:
    phase_segment, wave_segment = _phase_wave_commit_prefix(wave.id)
    commit_prefix = f"[{phase_segment}-{wave_segment}]"
    body = (
        "## Workflow\n"
        "\n"
        "1. cd into the wave's worktree (see `## Working tree` above).\n"
        "2. Implement edits in dependency order: schemas → logic → CLI → tests.\n"
        "3. Run the local gauntlet:\n"
        "   - `uv run pre-commit run --all-files`\n"
        "   - `uv run mypy src/`\n"
        "   - `uv run pytest tests/ -q`\n"
        "4. Commit with prefix `"
        + commit_prefix
        + " <type>: <summary>` (3-6 bullet body) and the\n"
        "   `Co-Authored-By: Claude <noreply@anthropic.com>` trailer."
    )
    return body


def _render_out_of_scope() -> str:
    return (
        "## Out of scope\n"
        "\n"
        "- Do **not** push the branch.\n"
        "- Do **not** open a PR.\n"
        "- Do **not** edit `.ea/state.json` or `.ea/store/event.jsonl` "
        "directly — every mutation goes through `uv run eawf state ...`.\n"
        "- Never `git commit --no-verify`; root-cause the hook instead."
    )


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
    "render_wave_prompt",
]
