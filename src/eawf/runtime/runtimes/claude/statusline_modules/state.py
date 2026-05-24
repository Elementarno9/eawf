"""``state`` statusline module — phase / iter / wave from ``.ea/state.json``.

Reads the resolved state file and emits a compact ``state:<active-wave>``
or ``state:<phase>`` segment. Degrades to ``state:?`` when the state file
is missing, malformed, or empty (no active pointers).

The module reads the state file directly with :mod:`orjson` — no Pydantic
validation — because the statusline must remain fast (target <100 ms cold)
and a malformed payload should not crash the segment. Any read or decode
failure short-circuits to ``state:?`` with ``status="missing"``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import orjson

from eawf.surfaces.render.statusline import StatuslineSegment

logger = logging.getLogger(__name__)


def _label_from_payload(payload: dict[str, Any]) -> str:
    """Pick the most-specific lifecycle label from *payload*.

    Preference order: first active wave id → iter_id → phase_id → ``?``.
    All paths are best-effort string lookups — a missing key returns the
    next-most-specific value or ``?``.
    """
    current = payload.get("current") or {}
    if not isinstance(current, dict):
        return "?"
    active_waves = current.get("active_wave_ids") or []
    if isinstance(active_waves, list) and active_waves:
        first = active_waves[0]
        if isinstance(first, str) and first:
            return first
    iter_id = current.get("iter_id")
    if isinstance(iter_id, str) and iter_id:
        return iter_id
    phase_id = current.get("phase_id")
    if isinstance(phase_id, str) and phase_id:
        return phase_id
    return "?"


def build(claude_payload: dict[str, Any], state_path: Path | None) -> StatuslineSegment:
    """Return the ``state:<label>`` segment.

    Args:
        claude_payload: Decoded Claude stdin JSON. Currently unused — the
            module reads from disk — but kept as part of the uniform module
            signature so the orchestrator can call every module with the
            same args.
        state_path: Resolved ``.ea/state.json`` path or ``None`` when the
            resolver could not locate one.

    Returns:
        A :class:`StatuslineSegment` with ``module="state"``. Status is
        ``ok`` on a parsed payload with a usable label, ``missing`` on any
        decode/read failure or absent file.
    """
    del claude_payload  # accepted for uniform signature
    if state_path is None or not state_path.exists():
        return StatuslineSegment(module="state", text="state:?", status="missing")
    try:
        raw = state_path.read_bytes()
        payload = orjson.loads(raw)
    except (OSError, orjson.JSONDecodeError) as exc:
        logger.debug(f"build state-read-decode-failed error={exc}")
        return StatuslineSegment(module="state", text="state:?", status="missing")
    if not isinstance(payload, dict):
        return StatuslineSegment(module="state", text="state:?", status="missing")
    label = _label_from_payload(payload)
    if label == "?":
        return StatuslineSegment(module="state", text="state:?", status="missing")
    return StatuslineSegment(module="state", text=f"state:{label}", status="ok")


__all__ = ["build"]
