"""Read the store record a memory promotion copies from.

A promotion copies a record of an epoch-1 store -- research, audit,
decision, ... -- into a new memory note. The record is read, never
changed: promotion is a forward link, not a move. The note itself is
written by the daemon's ``memory.promote`` verb.
"""

from __future__ import annotations

import logging
from pathlib import Path

from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


class PromotionError(ValueError):
    """Raised when the source store or the source record is missing."""


def load_source(store_path: Path, source_id: str) -> Envelope:
    """Return the latest envelope for *source_id* in *store_path*.

    Raises:
        PromotionError: The store file or the record is missing.
    """
    if not store_path.exists():
        raise PromotionError(f"store file does not exist: {store_path.name}")
    latest: Envelope | None = None
    for line in store_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        env = Envelope.model_validate_json(line)
        if env.id == source_id:
            latest = env
    if latest is None:
        raise PromotionError(f"source record {source_id!r} not found in {store_path.name}")
    logger.debug(f"load_source source={source_id} store={store_path.name}")
    return latest


def extract_body(env: Envelope) -> str:
    """Choose the most informative payload field as the promoted body."""
    payload = env.payload
    for key in ("body", "text", "rationale", "findings", "summary"):
        if key in payload:
            value = payload[key]
            if isinstance(value, str):
                return value
            if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
                return "\n".join(value)
    return env.summary


__all__ = ["PromotionError", "extract_body", "load_source"]
