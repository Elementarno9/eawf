"""Pure state-derived standup digest for ``eawf memory digest``.

The digest answers one question a newcomer (or a returning operator) asks at
the start of a session: *what is in flight right now, what just closed, and
what was recently decided* — without replaying a store. It is a **pure
projection** of the native decisions the tree's selected generation holds,
newest first, rendered as a glance-clear standup. It writes nothing.

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
    build_native_digest(decisions) -> MemoryDigest
    render_digest_md(digest) -> str   # pure markdown, prose-lint clean
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field

from eawf.kernel.state.epoch2.decision import Decision

logger = logging.getLogger(__name__)

#: Default cap on the recently-closed-iter list. Five keeps the standup
#: scannable — the full history is one the retired ``roadmap show`` verb away.
_DEFAULT_CLOSED_LIMIT: int = 5

#: Default cap on the recent-decision list, matching the ``eawf status``
#: ``recent_decisions`` projection so the two surfaces agree on "recent".
_DEFAULT_DECISION_LIMIT: int = 5


@dataclass(frozen=True)
class DigestEntry:
    """One row in the digest: a glossed lifecycle id plus its detail.

    Attributes:
        ref_id: The lifecycle / decision id (e.g. ``"P29"`` or
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


def build_native_digest(decisions: Sequence[Decision]) -> MemoryDigest:
    """Build the standup digest of a tree born at epoch 2 (pure; no I/O).

    Such a tree has no phase or iter pointer, so only its native decisions
    are projected, newest first.

    Args:
        decisions: The native decisions the tree's selected generation holds.

    Returns:
        A :class:`MemoryDigest` with no current pointers and no closed iters.
    """
    ordered = sorted(decisions, key=lambda d: (d.created_at, d.key), reverse=True)
    digest = MemoryDigest(
        phase=None,
        iter=None,
        recent_decisions=[
            DigestEntry(ref_id=d.key, title=d.title, detail=d.status.value)
            for d in ordered[:_DEFAULT_DECISION_LIMIT]
        ],
    )
    logger.info(f"build_native_digest decisions={len(digest.recent_decisions)}")
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
    "build_native_digest",
    "render_digest_md",
]
