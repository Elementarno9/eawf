"""``token_saving`` statusline module — prompt-cache hit ratio fallback.

When Claude includes ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
in the payload (Claude Code surfaces this on session metadata) we render
``save:<pct>%``. Otherwise the segment falls back to ``save:-`` with
``status="missing"`` per the W06 acceptance contract.

This module is intentionally minimal in v0.1 — Phase 5+ may add a deeper
heuristic that cross-references actual rolling tokens spent vs an
estimated baseline. v0.1 only honors what the Claude payload already
carries.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


def _extract_cache_ratio(payload: dict[str, Any]) -> float | None:
    """Return the prompt-cache hit ratio as a float in ``[0.0, 1.0]`` or ``None``.

    Probes (in order):

    1. ``payload["token_usage"]`` mapping — primary location per Claude
       Code's session info hook.
    2. ``payload["usage"]`` mapping.
    3. The flat payload itself (legacy shape).

    Computes ``cache_read / (cache_read + cache_creation + input)`` so the
    ratio reflects "tokens not re-billed" — matching how Anthropic
    documents prompt caching savings.
    """
    candidates: list[dict[str, Any]] = []
    raw = payload.get("token_usage")
    if isinstance(raw, dict):
        candidates.append(raw)
    raw = payload.get("usage")
    if isinstance(raw, dict):
        candidates.append(raw)
    candidates.append(payload)

    for cand in candidates:
        cache_read = cand.get("cache_read_input_tokens")
        cache_create = cand.get("cache_creation_input_tokens")
        plain_input = cand.get("input_tokens")
        if not isinstance(cache_read, int) or cache_read < 0:
            continue
        cache_create_v = cache_create if isinstance(cache_create, int) and cache_create >= 0 else 0
        plain_v = plain_input if isinstance(plain_input, int) and plain_input >= 0 else 0
        total = cache_read + cache_create_v + plain_v
        if total == 0:
            continue
        return cache_read / total
    return None


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``save:<pct>%`` (or ``save:-``) segment.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — kept for the uniform module signature.

    Returns:
        A :class:`StatuslineSegment` with ``module="token_saving"``.
        ``status="ok"`` when a ratio was computed, ``missing`` on fallback.
    """
    del state_path  # accepted for uniform signature
    ratio = _extract_cache_ratio(claude_payload)
    if ratio is None:
        return StatuslineSegment(module="token_saving", text="save:-", status="missing")
    pct = round(ratio * 100)
    return StatuslineSegment(
        module="token_saving",
        text=f"save:{pct}%",
        status="ok",
    )


__all__ = ["build"]
