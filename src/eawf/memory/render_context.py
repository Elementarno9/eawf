"""Token-budgeted memory render-context walk.

Strategy:
1. Score every active memory entry by ``recency * confidence * scope-distance``.
2. Sort descending; emit one entry at a time as a Markdown block.
3. Stop emitting once the accumulated token count would exceed the budget; the
   tail is reported as ``skipped``.

Token estimate uses a lightweight word-count proxy:
``tokens ~= len(text.split()) * 1.3``. This is intentionally crude — exact
tokenisation is runtime-dependent and the budget is advisory, not contractual.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from eawf.memory.store import find_envelope
from eawf.state.enums import Confidence, MemoryStatus
from eawf.state.models import MemorySummary, State

logger = logging.getLogger(__name__)

DEFAULT_BUDGET: int = 4096

# tokens ~= words * 1.3 -- coarse heuristic; documented in module docstring above.
_WORDS_PER_TOKEN: float = 1.3


def estimate_tokens(text: str) -> int:
    """Return an integer estimate of the token cost of *text*."""
    words = len(text.split())
    return round(words * _WORDS_PER_TOKEN)


_CONFIDENCE_WEIGHT: dict[Confidence, float] = {
    Confidence.HIGH: 1.0,
    Confidence.MEDIUM: 0.66,
    Confidence.LOW: 0.33,
}


def _scope_distance(memory_scope: str, anchor_scope: str | None) -> float:
    """Return a 1.0 / (1 + distance) weight comparing scopes.

    Equal scopes return 1.0; mismatch returns a fixed 0.5 to keep ordering
    stable. ``anchor_scope is None`` returns 0.7 — neutral.
    """
    if anchor_scope is None:
        return 0.7
    if memory_scope == anchor_scope:
        return 1.0
    if anchor_scope.startswith(memory_scope + "-") or memory_scope.startswith(anchor_scope + "-"):
        return 0.75
    return 0.5


def _recency_weight(now: datetime, summary: MemorySummary) -> float:
    """Decay weight from the entry's ``review_due`` (or 1.0 if none)."""
    if summary.review_due is None:
        return 1.0
    age_days = (now - summary.review_due).total_seconds() / 86400.0
    if age_days <= 0:
        return 1.0
    return max(0.1, 1.0 / (1.0 + age_days / 30.0))


@dataclass(frozen=True)
class RenderContextResult:
    """Outcome of a render-context walk."""

    text: str
    included_ids: list[str]
    skipped_ids: list[str]
    tokens_used: int
    budget: int


def render_context(
    *,
    state: State,
    memory_path: Path,
    anchor_scope: str | None = None,
    budget: int = DEFAULT_BUDGET,
    now: datetime | None = None,
) -> RenderContextResult:
    """Walk memory entries, render until *budget* exhausted.

    Args:
        state: Loaded :class:`State` carrying ``memory_index``.
        memory_path: Path to ``memory.jsonl`` for full-body lookup.
        anchor_scope: Optional anchor scope ID; entries closer to it rank higher.
        budget: Token budget (advisory). Defaults to ``DEFAULT_BUDGET`` (4096).
        now: Override for the current time (for tests). Defaults to UTC now.

    Returns:
        :class:`RenderContextResult` with the rendered Markdown body and the
        IDs of included vs skipped entries.
    """
    moment = now if now is not None else datetime.now(UTC)
    index = state.memory_index or {}
    actives: list[MemorySummary] = [s for s in index.values() if s.status == MemoryStatus.ACTIVE]

    def score(s: MemorySummary) -> float:
        return (
            _recency_weight(moment, s)
            * _CONFIDENCE_WEIGHT[s.confidence]
            * _scope_distance(s.scope_id, anchor_scope)
        )

    actives.sort(key=lambda s: (-score(s), s.id))

    included_ids: list[str] = []
    skipped_ids: list[str] = []
    blocks: list[str] = []
    used = 0
    for summary in actives:
        env = find_envelope(memory_path, summary.id)
        body = ""
        if env is not None:
            body_payload = env.payload.get("body")
            body = str(body_payload) if body_payload is not None else ""
        block = (
            f"## {summary.id} ({summary.scope_id}, {summary.confidence.value})\n"
            f"{summary.summary}\n\n"
            f"{body}\n"
        )
        block_tokens = estimate_tokens(block)
        if used + block_tokens > budget and included_ids:
            skipped_ids.append(summary.id)
            continue
        if used + block_tokens > budget and not included_ids:
            # Budget too tight even for the first block — skip it too.
            skipped_ids.append(summary.id)
            continue
        blocks.append(block)
        included_ids.append(summary.id)
        used += block_tokens

    text = "\n".join(blocks).strip() + ("\n" if blocks else "")
    logger.info(
        f"render_context budget={budget} used={used} "
        f"included={len(included_ids)} skipped={len(skipped_ids)}"
    )
    return RenderContextResult(
        text=text,
        included_ids=included_ids,
        skipped_ids=skipped_ids,
        tokens_used=used,
        budget=budget,
    )
