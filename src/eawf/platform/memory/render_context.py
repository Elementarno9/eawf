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

from eawf.kernel.state.enums import Confidence, MemoryStatus
from eawf.kernel.state.models import MemorySummary, State
from eawf.platform.memory.store import find_envelope

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
    include_superseded: bool = False,
    max_entries: int | None = None,
) -> RenderContextResult:
    """Walk memory entries, render until *budget* exhausted.

    Determinism contract (W03 hardening):

    - Given identical (``state``, ``memory.jsonl``, ``anchor_scope``,
      ``budget``, ``now``, ``include_superseded``, ``max_entries``) inputs the
      function returns byte-identical ``text`` and identical ``included_ids``
      ordering. Sort key is the tuple ``(-score, id)`` — score ties break on
      ascending memory ID.
    - ``tokens_used <= budget`` ALWAYS. When the very first block already
      exceeds the budget, the function emits zero blocks rather than overflow.
      :class:`~eawf.kernel.state.enums.MemoryStatus.PRUNED` entries are unconditionally
      excluded; :class:`~eawf.kernel.state.enums.MemoryStatus.SUPERSEDED` entries are
      excluded by default and admitted only when ``include_superseded=True``.
    - ``included_ids`` and ``skipped_ids`` are disjoint; their union covers
      every entry the active/superseded filter admitted.

    Args:
        state: Loaded :class:`State` carrying ``memory_index``.
        memory_path: Path to ``memory.jsonl`` for full-body lookup.
        anchor_scope: Optional anchor scope ID; entries closer to it rank higher.
        budget: Token budget (HARD; result never exceeds it). Defaults to
            ``DEFAULT_BUDGET`` (4096).
        now: Override for the current time (for tests). Defaults to UTC now.
        include_superseded: When ``True``, entries with
            :class:`~eawf.kernel.state.enums.MemoryStatus.SUPERSEDED` are also
            considered. ``PRUNED`` is never admitted.
        max_entries: Optional cap on the count of included entries. The
            budget still wins on either side: an entry that would exceed
            ``budget`` is skipped even when ``len(included_ids) < max_entries``.

    Returns:
        :class:`RenderContextResult` with the rendered Markdown body and the
        IDs of included vs skipped entries. ``tokens_used <= budget`` always.
    """
    moment = now if now is not None else datetime.now(UTC)
    index = state.memory_index or {}
    eligible_statuses: set[MemoryStatus] = {MemoryStatus.ACTIVE}
    if include_superseded:
        eligible_statuses.add(MemoryStatus.SUPERSEDED)
    actives: list[MemorySummary] = [s for s in index.values() if s.status in eligible_statuses]

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
        if max_entries is not None and len(included_ids) >= max_entries:
            skipped_ids.append(summary.id)
            continue
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
        # Budget is HARD: never include a block that would overflow it, even
        # when no entry has been included yet (the "first block too big" path
        # returns zero blocks rather than emit-then-overflow).
        if used + block_tokens > budget:
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
