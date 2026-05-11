"""Render a phase PR body (Markdown) from ``state.json``.

The output mirrors the convention captured in ``AGENTS.md`` (the project
PR template):

- ``## Summary`` — 3-6 bullets pulled from phase-scoped decisions and the
  titles of iters under the phase.
- ``## Phase deliverables`` — table of every wave under the phase with
  ``wave_id``, ``title``, the wave's commit short-SHA (first 7 chars), and
  ``outcome``.
- ``## Test plan`` — standard checklist anchored at the phase.

Public API: :func:`build_pr_body` returns the rendered Markdown string given
a validated :class:`State` and a phase id.
"""

from __future__ import annotations

import logging

from eawf.state.models import State

logger = logging.getLogger(__name__)


class PrBodyNotFound(LookupError):  # noqa: N818 — pairs with cli.errors.NotFound naming
    """Raised when the requested phase id is not present in ``state.phases``."""


def _short_sha(value: str | None) -> str:
    if not value:
        return "(pending)"
    return value[:7]


def _decisions_for_phase(state: State, phase_id: str) -> list[str]:
    """Return summaries of decisions whose ``scope_id`` mentions the phase.

    Decisions are EAWF-wide in v0.2; we surface only those whose summary
    contains the phase id (e.g. ``"P06 rescope: ..."``) so PR bodies are
    phase-relevant rather than dumping every cross-phase decision.
    """
    decisions = state.decisions or {}
    out: list[str] = []
    for d in sorted(decisions.values(), key=lambda r: r.id):
        if phase_id in d.summary:
            out.append(d.summary)
    return out


def _iters_for_phase(state: State, phase_id: str) -> list[tuple[str, str]]:
    """Return ``(iter_id, title)`` rows for iters under *phase_id* (id order)."""
    iters = state.iters or {}
    return [
        (it.id, it.title)
        for it in sorted(iters.values(), key=lambda r: r.id)
        if it.phase_id == phase_id
    ]


def _waves_for_phase(state: State, phase_id: str) -> list[tuple[str, str, str, str]]:
    """Return ``(wave_id, title, short_commit, outcome)`` rows for waves in the phase."""
    iter_ids = {iid for iid, _t in _iters_for_phase(state, phase_id)}
    waves = state.waves or {}
    rows: list[tuple[str, str, str, str]] = []
    for w in sorted(waves.values(), key=lambda r: r.id):
        if w.iter_id not in iter_ids:
            continue
        rows.append(
            (
                w.id,
                w.title,
                _short_sha(w.commit),
                w.outcome or "",
            )
        )
    return rows


def build_pr_body(state: State, phase_id: str) -> str:
    """Render the Markdown PR body for *phase_id*.

    Args:
        state: Loaded, validated :class:`State`.
        phase_id: Target phase id (e.g. ``"P11"``).

    Returns:
        The rendered Markdown body as a single string.

    Raises:
        PrBodyNotFound: When *phase_id* is not in ``state.phases``.
    """
    phases = state.phases or {}
    phase = phases.get(phase_id)
    if phase is None:
        raise PrBodyNotFound(f"phase not found: {phase_id!r}")

    decisions = _decisions_for_phase(state, phase_id)
    iters = _iters_for_phase(state, phase_id)
    waves = _waves_for_phase(state, phase_id)

    audit_line = ""
    if phase.audit_id:
        audits = state.audits or {}
        audit = audits.get(phase.audit_id)
        if audit is not None and audit.verdict is not None:
            audit_line = f"Audit `{phase.audit_id}` verdict: **{audit.verdict.value}**."

    lines: list[str] = [
        f"# {phase_id}: {phase.title}",
        "",
        "## Summary",
        "",
    ]
    if audit_line:
        lines.append(f"- {audit_line}")
    for summary in decisions:
        lines.append(f"- {summary}")
    for _iter_id, title in iters:
        lines.append(f"- {title}")
    if not decisions and not iters and not audit_line:
        lines.append("- (no decisions or iters recorded for this phase)")
    lines.extend(["", "## Phase deliverables", ""])
    if waves:
        lines.append("| Wave | Title | Commit | Outcome |")
        lines.append("|------|-------|--------|---------|")
        for wave_id, title, short, outcome in waves:
            outcome_cell = outcome.replace("|", "\\|") if outcome else ""
            title_cell = title.replace("|", "\\|")
            lines.append(f"| `{wave_id}` | {title_cell} | `{short}` | {outcome_cell} |")
    else:
        lines.append("_(no waves recorded for this phase)_")
    lines.extend(
        [
            "",
            "## Test plan",
            "",
            "- [ ] `uv run pytest -q`",
            "- [ ] `uv run pre-commit run --all-files`",
            "- [ ] `uv run mypy src/eawf`",
        ]
    )
    return "\n".join(lines) + "\n"


__all__ = [
    "PrBodyNotFound",
    "build_pr_body",
]
