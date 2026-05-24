"""Canonical-JSON helpers shared across per-runtime renderers.

Two renderer-side recipes need to agree byte-for-byte across
runtimes:

* **Pretty-form output** — sorted keys, four-space indent, trailing
  newline. Used for ``settings.json`` / ``opencode.json`` / the
  Codex marketplace ``plugin.json`` / each sidecar body. Two
  invocations on the same input must yield identical bytes.
* **Hash-form input** — sorted keys, no whitespace separators
  (``","`` / ``":"``), no trailing newline. Used as the
  pre-image for the blake2b-64 body hash that the doctor walks at
  drift-detection time.

Centralising both recipes here keeps the per-runtime renderers
free of the canonicalisation logic; each renderer hashes a payload
the same way as the next renderer.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any


def stable_json_bytes(body: Mapping[str, Any]) -> bytes:
    """Render *body* as canonical pretty-form JSON bytes.

    Recipe: sorted keys + 4-space indent + trailing newline. Two
    invocations on the same input return identical bytes — every
    on-disk artifact the renderer writes is byte-stable across runs.

    Args:
        body: JSON-serialisable mapping.

    Returns:
        UTF-8 encoded bytes ready to write atomically.
    """
    return (json.dumps(dict(body), sort_keys=True, indent=2) + "\n").encode("utf-8")


def _canonical_hash_bytes(body: Mapping[str, Any]) -> bytes:
    """Render *body* in the hash-form recipe (no whitespace separators).

    Internal helper for :func:`body_hash_with_self`; not exported
    because the only public consumer is the body-hash recipe.
    """
    return json.dumps(dict(body), sort_keys=True, separators=(",", ":")).encode("utf-8")


def body_hash_with_self(body: Mapping[str, Any]) -> dict[str, Any]:
    """Return *body* augmented with its own blake2b-64 hash.

    The recipe matches every existing per-runtime sidecar / managed
    body construction:

    1. Compute the blake2b-64 hex digest of the canonical hash-form
       bytes of *body* (sorted keys, no whitespace).
    2. Insert that digest into the result under the ``"hash"`` key.

    The hand-recorded hash flips when any other field in the body
    changes; doctors compare ``recorded_hash`` against
    ``recompute(body - {"hash"})`` to detect drift.

    Args:
        body: JSON-serialisable mapping. MUST NOT carry a
            pre-existing ``"hash"`` key (callers either build a
            fresh body or pop the prior key before re-hashing).

    Returns:
        A new dict containing every key from *body* plus the
        derived ``hash`` field.
    """
    result = dict(body)
    canonical = _canonical_hash_bytes(result)
    result["hash"] = hashlib.blake2b(canonical, digest_size=8).hexdigest()
    return result


__all__ = [
    "body_hash_with_self",
    "stable_json_bytes",
]
