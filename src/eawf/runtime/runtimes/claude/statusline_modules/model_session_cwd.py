"""``model_session_cwd`` statusline module — model + session + cwd basename.

Reads three fields from the Claude stdin payload:

- ``model`` — model name (string or mapping with ``id``/``name``).
- ``session_id`` — Claude session id (used as cache key by the prewarm
  worker; rendered abbreviated to the first 8 chars).
- ``cwd`` — working directory (only the basename is shown to keep the
  segment compact).

Missing fields collapse to ``-``; a fully missing payload renders
``model:- ses:- cwd:-``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


_SESSION_PREFIX_LEN: int = 8
"""How many chars of the session id to render. 8 is enough to disambiguate
in practice and keeps the segment narrow.
"""


def _model_label(payload: dict[str, Any]) -> str:
    """Return the model identifier or ``-`` when absent.

    Accepts either ``model: "<name>"`` (string) or ``model: {"id":"..."}``
    / ``model: {"name":"..."}`` (mapping) — Claude has shipped both shapes
    historically.
    """
    raw = payload.get("model")
    if isinstance(raw, str) and raw:
        return raw
    if isinstance(raw, dict):
        for key in ("display_name", "id", "name"):
            value = raw.get(key)
            if isinstance(value, str) and value:
                return value
    return "-"


def _session_label(payload: dict[str, Any]) -> str:
    """Return ``ses:<8-char-prefix>`` from ``session_id`` or ``ses:-``."""
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw:
        return raw[:_SESSION_PREFIX_LEN]
    return "-"


def _cwd_label(payload: dict[str, Any]) -> str:
    """Return the basename of ``cwd`` or ``-`` on missing/empty input."""
    raw = payload.get("cwd")
    if isinstance(raw, str) and raw:
        try:
            base = Path(raw).name or raw
        except TypeError, ValueError:
            return "-"
        return base or "-"
    return "-"


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``model:.. ses:.. cwd:..`` segment.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — the module reads only from the Claude payload.
            Kept for the uniform module signature.

    Returns:
        A :class:`StatuslineSegment` with ``module="model_session_cwd"``
        and ``status="ok"`` when at least one field was present, ``missing``
        when every field collapsed to ``-``.
    """
    del state_path  # accepted for uniform signature
    model = _model_label(claude_payload)
    session = _session_label(claude_payload)
    cwd = _cwd_label(claude_payload)
    text = f"model:{model} ses:{session} cwd:{cwd}"
    if model == "-" and session == "-" and cwd == "-":
        return StatuslineSegment(module="model_session_cwd", text=text, status="missing")
    return StatuslineSegment(module="model_session_cwd", text=text, status="ok")


__all__ = ["build"]
