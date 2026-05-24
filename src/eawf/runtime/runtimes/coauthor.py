"""Explicit-opt-in runtime resolver for the co-author trailer (KISS-001).

The pre-W12 :func:`eawf.runtime.vcs.coauthor._runtime_from_env` did **implicit
runtime detection**: it sniffed environment-variable name prefixes
(``CLAUDE*`` / ``CODEX*``) and inferred a runtime from whatever the
shell happened to export. That was fragile — a user who pasted a
Claude-related shell snippet (or had CLAUDE_HOME on PATH from any
prior install) would silently get a Claude trailer even when running
under Codex, and there was no way to disable the heuristic short of
unsetting unrelated env vars.

KISS-001 forbids implicit detection. The dispatch surface now accepts
exactly two opt-in inputs:

1. The ``EAWF_COAUTHOR_RUNTIME`` env var (canonical), or its alias
   ``EAWF_COAUTHOR_HARNESS`` (preserved for backward compatibility
   with the pre-W12 hook).
2. The explicit ``detected_runtime`` field on a dispatch JSON payload
   (the daemon-side surface).

When neither opt-in is present, the resolver returns ``None`` (no
runtime detected) — the caller falls back to ``CoauthorConfig.default_runtime``
or raises if its policy requires a trailer. **No env-var prefix
sniffing. No /etc/os-release parsing. No /proc/<pid> walk. No
parent-PID lineage scan.**

Public surface
--------------

* :func:`resolve_runtime_explicit` — pure resolver that takes
  ``env`` + ``detected_runtime`` and returns the canonical runtime
  id, or ``None`` when neither opt-in fires.
* :exc:`ImplicitDetectionRejected` — raised when a caller passes
  ``strict=True`` to :func:`resolve_runtime_explicit` and no opt-in
  is present (testing the rejection contract).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

logger = logging.getLogger(__name__)


COAUTHOR_RUNTIME_ENV_VAR: Final[str] = "EAWF_COAUTHOR_RUNTIME"
"""Canonical opt-in env var. Hook + dispatch payloads MUST set this
to a normalised runtime alias (``claude`` / ``codex`` / runtime id)."""

COAUTHOR_RUNTIME_LEGACY_ENV_VAR: Final[str] = "EAWF_COAUTHOR_HARNESS"
"""Backward-compatibility alias preserved for the pre-W12 hook.
Operators should migrate to :data:`COAUTHOR_RUNTIME_ENV_VAR`."""

_RUNTIME_ALIASES: Final[dict[str, str]] = {
    "anthropic": "claude",
    "claude": "claude",
    "claude-code": "claude",
    "codex": "codex",
    "codex-cli": "codex",
    "openai": "codex",
}
"""Closed map of operator-facing aliases to the canonical runtime key
used by :data:`~eawf.runtime.vcs.coauthor.CoauthorConfig.trailers`. Mirrors the
:data:`~eawf.runtime.vcs.coauthor._RUNTIME_ALIASES` constant so the two paths
stay in lock-step."""


class ImplicitDetectionRejected(ValueError):  # noqa: N818 — KISS-001 contract verb
    """Raised when ``strict=True`` and no explicit opt-in was supplied.

    The error message names the canonical env var so the caller's
    operator surface can point the user at the fix.
    """


def _normalise(value: str) -> str:
    """Lowercase + strip + map underscores to hyphens for alias lookup."""
    return value.strip().casefold().replace("_", "-")


def _canonical(value: str) -> str:
    """Map a normalised alias to its canonical runtime key.

    Unknown aliases pass through unchanged so the caller-side policy
    layer can reject with its own error (the resolver does not gate
    the value space; only the surface contract).
    """
    normalised = _normalise(value)
    return _RUNTIME_ALIASES.get(normalised, normalised)


def resolve_runtime_explicit(
    *,
    env: Mapping[str, str] | None = None,
    detected_runtime: str | None = None,
    strict: bool = False,
) -> str | None:
    """Resolve the runtime from explicit opt-in sources only.

    Args:
        env: Environment mapping consulted for
            :data:`COAUTHOR_RUNTIME_ENV_VAR` (and its legacy alias).
            Defaults to ``None`` (env-var path skipped).
        detected_runtime: Explicit runtime tag carried on the
            dispatch JSON payload. Takes precedence over the env-var
            path when both are present (caller-supplied data is more
            specific than process environment).
        strict: When ``True``, raise :exc:`ImplicitDetectionRejected`
            instead of returning ``None`` when neither opt-in fires.
            Used by callers that require explicit opt-in (the
            integration test uses this to assert the rejection path).

    Returns:
        Canonical runtime id (``"claude"`` / ``"codex"`` / etc.) when
        an explicit opt-in is present; ``None`` otherwise (and
        ``strict=False``).

    Raises:
        ImplicitDetectionRejected: ``strict=True`` and no opt-in
            was supplied.
    """
    if detected_runtime is not None and detected_runtime.strip():
        canonical = _canonical(detected_runtime)
        logger.info(
            f"resolve_runtime_explicit source=payload detected={detected_runtime!r} "
            f"canonical={canonical!r}"
        )
        return canonical

    if env is not None:
        override = env.get(COAUTHOR_RUNTIME_ENV_VAR) or env.get(COAUTHOR_RUNTIME_LEGACY_ENV_VAR)
        if override and override.strip():
            canonical = _canonical(override)
            logger.info(
                f"resolve_runtime_explicit source=env var={COAUTHOR_RUNTIME_ENV_VAR!r} "
                f"value={override!r} canonical={canonical!r}"
            )
            return canonical

    if strict:
        raise ImplicitDetectionRejected(
            f"runtime not detected; set {COAUTHOR_RUNTIME_ENV_VAR}=<runtime> "
            f"or pass detected_runtime explicitly on the dispatch payload "
            f"(implicit env-var sniffing was removed per KISS-001)"
        )

    logger.info("resolve_runtime_explicit source=none result=None")
    return None


__all__ = [
    "COAUTHOR_RUNTIME_ENV_VAR",
    "COAUTHOR_RUNTIME_LEGACY_ENV_VAR",
    "ImplicitDetectionRejected",
    "resolve_runtime_explicit",
]
