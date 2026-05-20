"""KISS-004 shared helpers across the three per-runtime renderers.

Background
----------

The earlier per-runtime renderers (``eawf.runtimes.{claude,codex,
opencode}.plugin_install``) shipped with significant copy-paste:
``_classify`` / ``_ensure_dir`` / blake2b body-hash construction /
sidecar fingerprint (JSON minus ``generated_at`` + derived ``hash``) /
the ``_DEFAULT_TIMESTAMP`` constant. A KISS LOC budget caps the
consolidated helper module at **<300 LOC** so the duplication shrinks
without the helper itself ballooning into a mini-framework.

Public surface (all pure, no IO side-effects):

* :data:`DEFAULT_TIMESTAMP` — frozen 1970-01-01T00:00:00Z used by
  every renderer for byte stability when the caller does not pin a
  timestamp. Centralised here so the three renderers stay in
  lock-step without re-declaring the constant.
* :class:`FileDelta` — frozen dataclass describing one (path,
  action) tuple. Used to be redeclared in every per-runtime
  ``InstallResult``; now imported.
* :func:`classify_action` — return ``"created"`` / ``"updated"`` /
  ``"unchanged"`` for a payload at a path. Replaces the three
  ``_classify`` siblings.
* :func:`ensure_dir` — ``Path.mkdir(parents=True, exist_ok=True)``
  wrapper that names the operation for log readability.
* :func:`stable_json_bytes` — sorted-keys / 4-space-indent /
  trailing-newline canonical JSON bytes. Used by every renderer
  for ``settings.json`` / ``opencode.json`` / sidecar bodies.
* :func:`body_hash_with_self` — given a JSON-serialisable mapping,
  return the same mapping with a ``hash`` field set to the
  blake2b-64 hex of the canonical bytes (sorted-keys, no
  whitespace) of the *pre-hash* body. The three sidecar / managed
  renderers all need this exact recipe.
* :func:`sidecar_fingerprint` — semantic fingerprint of sidecar
  JSON bytes that ignores ``generated_at`` + the derived ``hash``
  field so a timestamp-only refresh does not look like managed-file
  drift.

LOC budget (KISS-004): the entire helper module — this file +
:mod:`~eawf.runtimes.helpers.sidecar` — stays under 300 lines per
the cluster brief. Adding a new helper requires reviewing the
budget; if the cap is exceeded, split out a per-runtime concern
back into the runtime package.
"""

from __future__ import annotations

from eawf.runtimes.helpers.fs import (
    DEFAULT_TIMESTAMP,
    FileDelta,
    classify_action,
    ensure_dir,
)
from eawf.runtimes.helpers.json_canonical import (
    body_hash_with_self,
    stable_json_bytes,
)
from eawf.runtimes.helpers.sidecar import sidecar_fingerprint

__all__ = [
    "DEFAULT_TIMESTAMP",
    "FileDelta",
    "body_hash_with_self",
    "classify_action",
    "ensure_dir",
    "sidecar_fingerprint",
    "stable_json_bytes",
]
