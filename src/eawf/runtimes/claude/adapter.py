"""Claude Code adapter — implements :class:`RuntimeAdapter` (C07a §5.1).

Per-runtime stderr parsing rules come from §5.5; subprocess spawn +
session-resume wiring lands in P26-SURFACES (this wave ships the
typed contract surface only, not the live subprocess invocation).

The :meth:`open_session` / :meth:`continue_session` methods return
fully-typed :class:`~eawf.state.models.SessionAttempt` rows. In
v0.3-v0.5 the live subprocess spawn lives in the daemon dispatch
router (C07a §5.8); the adapter's role here is to construct the
typed row + parse subprocess outcomes. P26 wires the actual
``asyncio.create_subprocess_exec`` call.
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

_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate_limit_error|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|unauthor[iz][sez]+ed)\b",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b(?:5\d\d|internal_server_error|overloaded_error)\b", re.IGNORECASE)
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")


class ClaudeAdapter:
    """Claude Code runtime adapter (``claude -p`` subprocess primary).

    Implements :class:`~eawf.runtimes.adapter.RuntimeAdapter`. ``id``
    + ``cli_binary`` follow the canonical naming per
    :class:`~eawf.state.models.SessionAttempt.runtime`.
    """

    id: str = "claude-code"
    cli_binary: str = "claude"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix (C07a §G9 + D8) via
    # :func:`eawf.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table per W13 success criterion 3.
    accepts_continue: bool = runtime_supports("claude-code", "session_resume")
    supports_cache_control: bool = runtime_supports("claude-code", "cache_control")
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

        Live subprocess spawn lands in P26-SURFACES; the v0.3 wave
        ships the typed contract. The returned row carries an
        adapter-allocated ``session_id`` UUID and the opaque
        ``session_log_handle`` per rule 16.

        Per C04d D-d2 the caller-side ``cache_prefix`` is routed through
        :func:`~eawf.runtimes.cache_control.inject_cache_control`, which
        appends the Claude ``<cache_control type="ephemeral" />``
        breakpoint (``claude-code`` is the only runtime that accepts a
        caller-side marker, §5.6). The injected prefix feeds the live
        subprocess spawn that lands in P26-SURFACES.
        """

        injected_prefix = inject_cache_control(
            runtime_id=self.id,
            cache_prefix=cache_prefix,
        )
        if injected_prefix is not None:
            logger.debug(f"open_session runtime={self.id!r} cache_control=injected")
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
        """Resume the session at ``session_id`` via ``claude --continue``.

        Raises:
            SessionResumeFailedError: ``session_id`` is empty or otherwise
                fails the daemon-internal resume probe; daemon falls
                back to :meth:`open_session` per §5.8.
        """

        if not session_id:
            raise SessionResumeFailedError(f"empty session id: {session_id!r}")
        # P26 wires the actual ``claude --continue <session_id>``
        # subprocess spawn + JSONL log lookup. v0.3 surfaces the
        # typed row only; the resume probe lives in the daemon.
        return SessionAttempt(
            attempt=1,
            runtime=self.id,
            session_id=session_id,
            session_log_handle=self.session_log_handle(session_id),
            started_at=datetime.now(UTC),
        )

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the session log.

        Per §5.4 the real on-disk path lives in the daemon's
        in-process map (rule 16). The handle is a URN-shaped string
        the daemon resolves via
        :func:`eawf.daemon.session.resolve_session_log` (lands in
        P26).
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class.

        Per §5.5 rules: auth and rate-limit on exit 2; server-error
        and api-error on exit 1; timeout on SIGTERM (-15) or the
        ``timeout`` / ``deadline_exceeded`` keyword. Order matters —
        auth + rate-limit checks run before the generic
        ``5xx`` / ``4xx`` HTTP fallbacks so an "auth 401 over a 500
        response" classifies as auth (the more actionable verdict).
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
        """Claude Code supports ``--continue <session-id>``."""

        return self.accepts_continue


# Module-level Protocol-conformance sanity check. The daemon's
# ``isinstance(adapter, RuntimeAdapter)`` load-time gate catches F1
# Protocol-mismatch (§6); checking at import keeps the failure mode
# at the right layer.
_ADAPTER_CHECK: RuntimeAdapter = ClaudeAdapter()

__all__ = ["ClaudeAdapter"]
