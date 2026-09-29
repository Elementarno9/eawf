"""``model_session_cwd`` statusline module — model + session + cwd basename.

Reads three fields from the Claude stdin payload:

- ``model`` — model name (string or mapping with ``id``/``name``).
- ``session_id`` — Claude session id (used as cache key by the prewarm
  worker; rendered abbreviated to the first 8 chars).
- ``cwd`` — working directory (only the basename is shown to keep the
  segment compact).

A missing field renders its own ``n/a(absent)`` marker; a payload carrying
none of the three renders ``model:n/a(no-host-payload)``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.statusline_modules._host import host_source
from eawf.surfaces.render.statusline import (
    UNAVAILABLE_MARK,
    StatuslineSegment,
    sourced_segment,
    unavailable_segment,
)

logger = logging.getLogger(__name__)

_MODULE = "model_session_cwd"
_SOURCE = host_source("model,session_id,cwd")

#: What a field the host payload omits renders as, inside an otherwise present segment.
_ABSENT = f"{UNAVAILABLE_MARK}(absent)"


_SESSION_PREFIX_LEN: int = 8
"""How many chars of the session id to render. 8 is enough to disambiguate
in practice and keeps the segment narrow.
"""


def _model_label(payload: dict[str, Any]) -> str | None:
    """Return the model identifier, or ``None`` when absent.

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
    return None


def _session_label(payload: dict[str, Any]) -> str | None:
    """Return the 8-char prefix of ``session_id``, or ``None`` when absent."""
    raw = payload.get("session_id")
    if isinstance(raw, str) and raw:
        return raw[:_SESSION_PREFIX_LEN]
    return None


def _cwd_label(payload: dict[str, Any]) -> str | None:
    """Return the basename of ``cwd``, or ``None`` on missing/empty input."""
    raw = payload.get("cwd")
    if isinstance(raw, str) and raw:
        return Path(raw).name or raw
    return None


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``model:.. ses:.. cwd:..`` segment.

    Args:
        claude_payload: Decoded Claude stdin JSON.
        state_path: Unused — the module reads only from the Claude payload.
            Kept for the uniform module signature.

    Returns:
        A :class:`StatuslineSegment` with ``module="model_session_cwd"``
        and ``status="ok"`` when at least one field was present, or the
        ``model:n/a(no-host-payload)`` marker when none was.
    """
    del state_path  # accepted for uniform signature
    model = _model_label(claude_payload)
    session = _session_label(claude_payload)
    cwd = _cwd_label(claude_payload)
    if model is None and session is None and cwd is None:
        return unavailable_segment(_MODULE, "model", "no-host-payload", _SOURCE)
    value = f"{model or _ABSENT} ses:{session or _ABSENT} cwd:{cwd or _ABSENT}"
    return sourced_segment(_MODULE, "model", value, _SOURCE)


__all__ = ["build"]
