"""``context_tokens`` statusline module — input/output token usage.

Claude's hook payload may carry a ``token_usage`` (or ``usage``) sub-object
with ``input_tokens`` and ``output_tokens`` keys. When present, the segment
renders ``ctx:<in>/<out>``; when absent, the segment degrades to ``ctx:-``
with ``status="missing"``.

The module reads only from the Claude payload — no on-disk lookup — so
the segment is cheap (microseconds) and always safe.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.surfaces.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


def _extract_usage(payload: dict[str, Any]) -> tuple[int, int] | None:
    """Pull ``(input, output)`` token counts from *payload* if present.

    Recognised shapes (try in order):

    1. ``payload["token_usage"] = {"input_tokens": int, "output_tokens": int}``.
    2. ``payload["usage"] = {"input_tokens": int, "output_tokens": int}``.
    3. ``payload["input_tokens"] = int`` and ``payload["output_tokens"] = int``
       (flat shape, used by some early Claude payload versions).

    Returns ``None`` if no recognised shape is found, or any of the values
    is the wrong type (negative integers and floats are coerced via ``int``
    when finite, otherwise dropped to ``None``).
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
        in_val = cand.get("input_tokens")
        out_val = cand.get("output_tokens")
        if isinstance(in_val, int) and isinstance(out_val, int) and in_val >= 0 and out_val >= 0:
            return (in_val, out_val)
    return None


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``ctx:<in>/<out>`` (or ``ctx:-``) segment.

    Args:
        claude_payload: Decoded Claude stdin JSON. Read for ``token_usage``
            / ``usage`` / flat token fields.
        state_path: Unused — the module reads only from the Claude payload.

    Returns:
        A :class:`StatuslineSegment` with ``module="context_tokens"`` and
        ``status="ok"`` when usage is present, ``missing`` otherwise.
    """
    del state_path  # accepted for uniform signature
    usage = _extract_usage(claude_payload)
    if usage is None:
        return StatuslineSegment(module="context_tokens", text="ctx:-", status="missing")
    in_tok, out_tok = usage
    return StatuslineSegment(
        module="context_tokens",
        text=f"ctx:{in_tok}/{out_tok}",
        status="ok",
    )


__all__ = ["build"]
