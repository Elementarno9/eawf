"""Sidecar-fingerprint helper shared across per-runtime renderers.

The Codex + OpenCode renderers each maintain a ``.eawf-managed.json``
sidecar that carries the renderer's record of the on-disk plugin
tree. Sidecar drift detection must be tolerant of timestamp-only
refreshes: a renderer run with an updated ``generated_at`` but
unchanged contributions should NOT trip drift.

The original implementations in
``eawf.runtime.runtimes.{codex,opencode}.plugin_install`` shipped identical
``_sidecar_fingerprint`` helpers. KISS-004 consolidates them here.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def sidecar_fingerprint(payload: bytes) -> str:
    """Return a semantic fingerprint for sidecar JSON bytes.

    ``generated_at`` and the derived ``hash`` field are intentionally
    ignored so an install minutes apart that only refreshes the
    timestamp does not look like managed-file drift to the doctor.

    Recipe:

    1. Decode + parse the JSON payload. Malformed UTF-8 / non-JSON /
       non-object payloads fall back to raw-bytes hashing — the
       doctor still detects drift, just without semantic
       normalisation.
    2. Drop ``generated_at`` + ``hash`` from the comparable view.
    3. Hash the remaining keys in canonical (sorted-keys, no
       whitespace) form.

    Args:
        payload: Bytes read from the on-disk sidecar.

    Returns:
        16-character blake2b-64 hex digest stable across
        timestamp-only refreshes.
    """
    try:
        parsed: Any = json.loads(payload.decode("utf-8"))
    except UnicodeDecodeError, json.JSONDecodeError:
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    if not isinstance(parsed, dict):
        return hashlib.blake2b(b"raw:" + payload, digest_size=8).hexdigest()
    comparable = dict(parsed)
    comparable.pop("generated_at", None)
    comparable.pop("hash", None)
    body = json.dumps(comparable, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.blake2b(body, digest_size=8).hexdigest()


__all__ = [
    "sidecar_fingerprint",
]
