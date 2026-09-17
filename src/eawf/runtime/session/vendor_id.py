"""Vendor session ids as state stores them: a digest, never the raw id.

A runtime's own session id (a Claude Code session UUID, a Codex provider
session id) names local artefacts such as the session transcript file, so a raw
id in committed state keeps pointing at a file on the operator's machine long
after the session ended. State keeps a one-way digest instead. The digest is
still an exact key: a later capture that carries the raw id hashes to the same
value, so session matching keeps working without the raw id being written.

Hashing is idempotent. An already-hashed id passes through unchanged, which is
what lets a row that still carries a raw id compare equal to a hashed capture
through :func:`same_vendor_session`.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Final

logger = logging.getLogger(__name__)

_PREFIX: Final[str] = "vsid-"

# 128 bits of SHA-256 keep ids unique across every session a state file will
# ever hold while staying short enough to read in a session-log handle.
_DIGEST_HEX_CHARS: Final[int] = 32

_HASHED_ID: Final[re.Pattern[str]] = re.compile(rf"{_PREFIX}[0-9a-f]{{{_DIGEST_HEX_CHARS}}}")


def hash_vendor_session_id(session_id: str) -> str:
    """Return the digest form of a vendor session id.

    Args:
        session_id: A raw vendor session id, or one this function already
            hashed.

    Returns:
        ``vsid-`` followed by 32 lowercase hex characters of the id's SHA-256.
        An id already in that form is returned unchanged.

    Raises:
        ValueError: When ``session_id`` is empty, since an empty id names no
            session and would hash to a key every empty id shares.
    """
    if not session_id:
        raise ValueError("vendor session id must be non-empty")
    if _HASHED_ID.fullmatch(session_id) is not None:
        return session_id
    digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
    return f"{_PREFIX}{digest[:_DIGEST_HEX_CHARS]}"


def same_vendor_session(stored: str | None, incoming: str | None) -> bool:
    """Report whether two vendor session ids name the same session.

    Either side may be raw or hashed, so a row that still carries a raw id
    matches a capture that arrives hashed.

    Args:
        stored: The id a state row carries, or ``None`` when it has none.
        incoming: The id a capture or lifecycle event carries, or ``None``.

    Returns:
        ``True`` only when both ids are non-empty and hash to the same digest.
        A missing id matches nothing, not even another missing id.
    """
    if not stored or not incoming:
        return False
    return hash_vendor_session_id(stored) == hash_vendor_session_id(incoming)


__all__ = ["hash_vendor_session_id", "same_vendor_session"]
