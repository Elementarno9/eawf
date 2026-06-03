"""Pure state-derived standup digest for ``eawf memory digest``.

The digest answers one question a newcomer (or a returning operator) asks at
the start of a session: *what is in flight right now, what just closed, and
what was recently decided* — without opening ``state.json`` or replaying a
store. It is a **pure projection** of the loaded :class:`State`: it reads the
current pointers, the most-recently-closed iters, and the most-recent
decisions, and renders them as a glance-clear standup. It writes nothing, so
``state.json``, ``memory.jsonl``, and ``event.jsonl`` are byte-equal before
and after a digest call.

Why a derived digest rather than a remembered one: status is *queried*, not
memorized (see the memory-hygiene convention in ``AGENTS.md``). A digest that
reads from state can never drift from reality the way a hand-maintained
standup note does, and it costs nothing to keep current.

Newcomer-clarity contract: every lifecycle id this digest emits (a phase
``P<NN>``, an iter ``P<NN>-I<NN>``, a decision ``D<NN>``) is glossed on first
use by pairing the id with its bounded title via :func:`_gloss`, so the
rendered markdown passes the doc-clarity prose lints (jargon defined on first
use). The output is one line per paragraph (no manual wrap), headings + short
bullets (scannable), and carries no inline path/link soup.

Public API::

    DigestEntry          # one glossed id + title + optional detail row
    MemoryDigest         # typed projection (current / recently_closed / decisions)
    build_digest(state) -> MemoryDigest
    render_digest_md(digest) -> str   # pure markdown, prose-lint clean
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from eawf.kernel.state.enums import IterStatus
from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)

#: Default cap on the recently-closed-iter list. Five keeps the standup
#: scannable — the full history is one ``eawf roadmap show`` away.
_DEFAULT_CLOSED_LIMIT: int = 5

#: Default cap on the recent-decision list, matching the ``eawf status``
#: ``recent_decisions`` projection so the two surfaces agree on "recent".
_DEFAULT_DECISION_LIMIT: int = 5


@dataclass(frozen=True)
class DigestEntry:
    """One row in the digest: a glossed lifecycle id plus its detail.

    Attributes:
        ref_id: The lifecycle / decision id (e.g. ``"P29"``, ``"P29-I08"``,
            ``"D01"``). Kept raw so the JSON surface stays machine-joinable
            against ``state.json``.
        title: The entity's bounded title — the gloss that makes the id
            newcomer-legible in the rendered markdown.
        detail: Optional one-line trailing fact (a verdict, a status, a
            close timestamp). Empty string when the row carries no trailer.
    """

    ref_id: str
    title: str
    detail: str = ""


@dataclass(frozen=True)
class MemoryDigest:
    """Typed, state-derived standup projection.

    Attributes:
        phase: The current phase entry, or ``None`` when no phase is active.
        iter: The current iter entry, or ``None`` when no iter is active.
        recently_closed: Most-recently-closed iters, newest first.
        recent_decisions: Most-recent decisions, newest first.
    """

    phase: DigestEntry | None
    iter: DigestEntry | None
    recently_closed: list[DigestEntry] = field(default_factory=list)
    recent_decisions: list[DigestEntry] = field(default_factory=list)


def _current_phase_entry(state: State) -> DigestEntry | None:
    """Project the current phase pointer into a glossed entry, or ``None``."""
    phase_id = state.current.phase_id
    if phase_id is None:
        return None
    phase = state.phases.get(phase_id)
    if phase is None:
        return DigestEntry(ref_id=phase_id, title="(phase record missing)")
    return DigestEntry(ref_id=phase.id, title=phase.title, detail=phase.status.value)


def _current_iter_entry(state: State) -> DigestEntry | None:
    """Project the current iter pointer into a glossed entry, or ``None``."""
    iter_id = state.current.iter_id
    if iter_id is None:
        return None
    iter_rec = state.iters.get(iter_id)
    if iter_rec is None:
        return DigestEntry(ref_id=iter_id, title="(iter record missing)")
    return DigestEntry(ref_id=iter_rec.id, title=iter_rec.title, detail=iter_rec.status.value)


def _verdict_for(state: State, audit_id: str | None) -> str:
    """Return the audit verdict string for *audit_id*, or ``""`` when absent."""
    if audit_id is None or state.audits is None:
        return ""
    audit = state.audits.get(audit_id)
    if audit is None or audit.verdict is None:
        return ""
    return f"audit {audit.verdict.value}"


def _recently_closed_iters(state: State, limit: int) -> list[DigestEntry]:
    """Return up to *limit* most-recently-closed iters, newest first.

    Sorted by ``closed_at`` descending with the id as a stable tiebreaker so
    two iters closed in the same instant order deterministically. Each entry's
    detail carries the linked audit verdict when one is recorded.
    """
    pairs = [
        (iter_rec.closed_at, iter_rec.id, iter_rec)
        for iter_rec in state.iters.values()
        if iter_rec.status == IterStatus.CLOSED and iter_rec.closed_at is not None
    ]
    pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return [
        DigestEntry(
            ref_id=iter_rec.id,
            title=iter_rec.title,
            detail=_verdict_for(state, iter_rec.audit_id),
        )
        for _, _, iter_rec in pairs[:limit]
    ]


def _recent_decisions(state: State, limit: int) -> list[DigestEntry]:
    """Return up to *limit* most-recent decisions, newest first.

    Sorted by ``created_at`` descending with the id as a stable tiebreaker.
    Each entry's detail carries the lifecycle status so a superseded decision
    reads as historical at a glance.
    """
    pairs = [(d.created_at, d.id, d) for d in state.decisions.values()]
    pairs.sort(key=lambda p: (p[0], p[1]), reverse=True)
    return [
        DigestEntry(ref_id=d.id, title=d.title, detail=d.status.value) for _, _, d in pairs[:limit]
    ]


def build_digest(
    state: State,
    *,
    closed_limit: int = _DEFAULT_CLOSED_LIMIT,
    decision_limit: int = _DEFAULT_DECISION_LIMIT,
) -> MemoryDigest:
    """Build the typed standup digest from *state* (pure; no I/O, no mutation).

    Args:
        state: The loaded, already-validated :class:`State`.
        closed_limit: Cap on the recently-closed-iter list (must be >= 0).
        decision_limit: Cap on the recent-decision list (must be >= 0).

    Returns:
        A :class:`MemoryDigest` projecting the current pointers, the most-
        recently-closed iters, and the most-recent decisions.

    Raises:
        ValueError: when *closed_limit* or *decision_limit* is negative.
    """
    if closed_limit < 0:
        raise ValueError(f"closed_limit must be >= 0; got {closed_limit!r}")
    if decision_limit < 0:
        raise ValueError(f"decision_limit must be >= 0; got {decision_limit!r}")
    digest = MemoryDigest(
        phase=_current_phase_entry(state),
        iter=_current_iter_entry(state),
        recently_closed=_recently_closed_iters(state, closed_limit),
        recent_decisions=_recent_decisions(state, decision_limit),
    )
    logger.info(
        f"build_digest phase={digest.phase.ref_id if digest.phase else None} "
        f"iter={digest.iter.ref_id if digest.iter else None} "
        f"closed={len(digest.recently_closed)} decisions={len(digest.recent_decisions)}"
    )
    return digest


def _gloss(entry: DigestEntry) -> str:
    """Return ``id (title)`` so the lifecycle id is glossed on first use.

    The gloss is what keeps the rendered markdown newcomer-legible: a bare
    ``P29`` is an internal code the prose lints flag, but ``P29 (...)`` defines
    it inline. The optional trailing detail is appended in brackets when set.
    """
    base = f"{entry.ref_id} ({entry.title})"
    if entry.detail:
        return f"{base} — {entry.detail}"
    return base


def render_digest_md(digest: MemoryDigest) -> str:
    """Render *digest* as a glance-clear standup in Markdown.

    The output is one line per paragraph (no manual wrap), uses headings and
    short bullets so it stays scannable, and glosses every lifecycle id on
    first use via :func:`_gloss` so it passes the doc-clarity prose lints. The
    function is pure and deterministic: identical input yields byte-identical
    output across calls.

    Returns:
        The rendered Markdown with no trailing newline (the caller frames it).
    """
    lines: list[str] = ["# Standup digest", ""]

    lines.append("## Current focus")
    lines.append("")
    if digest.phase is None and digest.iter is None:
        lines.append("No phase or iter is active right now.")
    else:
        if digest.phase is not None:
            lines.append(f"- Phase: {_gloss(digest.phase)}")
        if digest.iter is not None:
            lines.append(f"- Iter: {_gloss(digest.iter)}")
    lines.append("")

    lines.append("## Recently closed")
    lines.append("")
    if not digest.recently_closed:
        lines.append("No iter has closed yet.")
    else:
        lines.extend(f"- {_gloss(entry)}" for entry in digest.recently_closed)
    lines.append("")

    lines.append("## Recent decisions")
    lines.append("")
    if not digest.recent_decisions:
        lines.append("No decision has been recorded yet.")
    else:
        lines.extend(f"- {_gloss(entry)}" for entry in digest.recent_decisions)

    return "\n".join(lines)


__all__ = [
    "DigestEntry",
    "MemoryDigest",
    "build_digest",
    "render_digest_md",
]
