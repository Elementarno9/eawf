"""Codex CLI adapter — implements :class:`RuntimeAdapter` (C07a §5.1).

Codex CLI = ``codex exec``. Per §5.2 + §5.6 there is no caller-side
``cache_control`` marker (OpenAI prompt caching is automatic at the
≥1024-token threshold); :attr:`supports_cache_control` is therefore
``False``.

Per §5.4 Codex session logs live under ``$CODEX_HOME`` (date-sharded
JSONL); the adapter caches ``(session_id → path)`` at dispatch time so
subsequent retries skip the tree walk. v0.3 ships the typed surface
only; the live walk lands in P26-SURFACES.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from eawf.runtimes.adapter import (
    ErrorClass,
    RuntimeAdapter,
    SessionResumeFailedError,
)
from eawf.runtimes.cache_control import inject_cache_control
from eawf.runtimes.selector import runtime_supports
from eawf.state.models import SessionAttempt, Wave

logger = logging.getLogger(__name__)

_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|chatgpt subscription expired"
    rb"|unauthor[iz][sez]+ed)\b",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b5\d\d\b")
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")


class CodexAdapter:
    """Codex CLI runtime adapter (``codex exec`` subprocess primary)."""

    id: str = "codex"
    cli_binary: str = "codex"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix (C07a §G9 + D8) via
    # :func:`eawf.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table per W13 success criterion 3.
    accepts_continue: bool = runtime_supports("codex", "session_resume")
    supports_cache_control: bool = runtime_supports("codex", "cache_control")
    error_classes_emitted: tuple[ErrorClass, ...] = (
        "RUNTIME_RATE_LIMIT",
        "RUNTIME_SERVER_ERROR",
        "RUNTIME_TIMEOUT",
        "RUNTIME_API_ERROR",
        "RUNTIME_AUTH_ERROR",
    )

    async def open_session(
        self,
        wave: Wave,
        prompt: str,
        *,
        cache_prefix: str | None = None,
        model_hint: str | None = None,
    ) -> SessionAttempt:
        """Construct a fresh-session :class:`SessionAttempt` row.

        ``cache_prefix`` is routed through
        :func:`~eawf.runtimes.cache_control.inject_cache_control` for
        boundary parity, but Codex is a **no-op path** (C04d D-d2 / §5.6):
        OpenAI prompt caching is automatic at the ≥1024-token threshold
        and there is no caller-side marker surface, so the prefix is
        returned unchanged.
        """

        injected_prefix = inject_cache_control(
            runtime_id=self.id,
            cache_prefix=cache_prefix,
        )
        if injected_prefix is not None:
            logger.debug(f"open_session runtime={self.id!r} cache_control=no_op")
        session_id = str(uuid.uuid4())
        attempts = sorted(wave.sessions)
        next_attempt = (max(attempts) + 1) if attempts else 1
        return SessionAttempt(
            attempt=next_attempt,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    async def continue_session(
        self,
        session_id: str,
        prompt: str,
    ) -> SessionAttempt:
        """Resume the session via ``codex exec resume <session-id>``.

        Raises:
            SessionResumeFailedError: ``session_id`` is empty or otherwise
                fails the daemon-side resume probe.
        """

        if not session_id:
            raise SessionResumeFailedError(f"empty session id: {session_id!r}")
        return SessionAttempt(
            attempt=1,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the session log.

        The handle abstracts over Codex's date-sharded JSONL layout
        under ``$CODEX_HOME`` (resolved via blitz brief; §5.4); the
        daemon walks the tree once + caches the result.
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class per §5.5.

        Codex follows the same regex ladder as Claude except for the
        Codex-specific "chatgpt subscription expired" auth phrasing.
        """

        if _AUTH_RE.search(stderr):
            return "RUNTIME_AUTH_ERROR"
        if _RATE_LIMIT_RE.search(stderr):
            return "RUNTIME_RATE_LIMIT"
        if _TIMEOUT_RE.search(stderr) or exit_status in (-15, 124):
            return "RUNTIME_TIMEOUT"
        if _SERVER_RE.search(stderr):
            return "RUNTIME_SERVER_ERROR"
        if _API_RE.search(stderr):
            return "RUNTIME_API_ERROR"
        return "RUNTIME_API_ERROR"

    def supports_continue(self) -> bool:
        """Codex supports ``codex exec resume <session-id>``."""

        return self.accepts_continue


_ADAPTER_CHECK: RuntimeAdapter = CodexAdapter()

__all__ = ["CodexAdapter"]
