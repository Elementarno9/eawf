"""OpenCode adapter — implements :class:`RuntimeAdapter`.

OpenCode CLI = ``opencode run``. There is no caller-side
``cache_control`` marker (the bundled ``@ai-sdk/anthropic`` provider
injects it internally + the live regression at
``anomalyco/opencode#17910`` strips the marker for the OAuth-Claude
auth path); :attr:`supports_cache_control` is ``False``.

OpenCode uses a two-store layout: a SQLite primary store + auxiliary
diff arrays. The :meth:`session_log_handle` returns an opaque URN; the
daemon resolves the SQLite path via its in-process map. "session-log
path nonexistent" is a documented v0.3 risk — :meth:`supports_continue`
therefore returns ``False`` until the documented path ships in v0.4.
"""

from __future__ import annotations

import logging
import re
import uuid
from datetime import UTC, datetime

from eawf.kernel.state.models import SessionAttempt, Wave
from eawf.runtimes.adapter import (
    ErrorClass,
    RuntimeAdapter,
    SessionResumeFailedError,
)
from eawf.runtimes.cache_control import inject_cache_control
from eawf.runtimes.selector import runtime_supports

logger = logging.getLogger(__name__)

_RATE_LIMIT_RE = re.compile(rb"\b(?:429|rate[_ -]?limit)\b", re.IGNORECASE)
_AUTH_RE = re.compile(
    rb"\b(?:401|403|invalid_api_key|oauth_expired|unauthor[iz][sez]+ed)\b",
    re.IGNORECASE,
)
_SERVER_RE = re.compile(rb"\b5\d\d\b")
_TIMEOUT_RE = re.compile(rb"\b(?:timeout|deadline_exceeded)\b", re.IGNORECASE)
_API_RE = re.compile(rb"\b4\d\d\b")


class OpenCodeAdapter:
    """OpenCode runtime adapter (``opencode run`` subprocess primary).

    :attr:`accepts_continue` ships ``False`` in v0.3 because the
    session-log path catalog is not yet fully verified; the daemon
    treats every dispatch as fresh under that branch and avoids the
    fallback churn that would otherwise fire on every retry. v0.4 flips
    :attr:`accepts_continue` to ``True`` once the documented path lands.
    """

    id: str = "opencode"
    cli_binary: str = "opencode"
    # ``accepts_continue`` + ``supports_cache_control`` derive from the
    # YAML-backed capability matrix via
    # :func:`eawf.runtimes.selector.runtime_supports` — no parallel
    # hard-coded table.
    accepts_continue: bool = runtime_supports("opencode", "session_resume")
    supports_cache_control: bool = runtime_supports("opencode", "cache_control")
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
        boundary parity, but OpenCode is a **no-op path**: the bundled
        ``@ai-sdk/anthropic`` provider injects ``cache_control``
        internally and the OAuth-Claude path strips any caller-side
        marker (upstream ``#17910``), so the eawf adapter has no
        caller-side knob and returns the prefix unchanged.
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
        """OpenCode v0.3: session resume not yet supported.

        Per :attr:`accepts_continue` = ``False`` the daemon does not
        normally invoke this path; if it does the adapter raises
        :class:`SessionResumeFailedError` so the caller falls back to
        :meth:`open_session`.

        Raises:
            SessionResumeFailedError: OpenCode adapter does not support
                session resume in v0.3 (path catalog pending).
        """

        raise SessionResumeFailedError(
            f"opencode adapter does not support continue_session in v0.3: {session_id!r}"
        )

    def session_log_handle(self, session_id: str) -> str:
        """Daemon-internal opaque handle for the SQLite session log.

        Resolves to ``data-dir/storage/session.db`` via the daemon's
        in-process map (§5.4). The URN format matches the other
        adapters so the daemon's resolver is adapter-agnostic.
        """

        return f"urn:eawf:v1:session-log:{self.id}:{session_id}"

    def parse_error(
        self,
        exit_status: int,
        stderr: bytes,
    ) -> ErrorClass:
        """Map ``(exit_status, stderr)`` to a canonical class per §5.5.

        OpenCode-via-OAuth-Claude inherits the upstream auth regex
        ladder; per-vendor auth-failure phrasing is multi-form so
        the auth check stays generic (HTTP code substring + token
        keywords).
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
        """Whether the adapter supports session resume.

        Returns ``False`` in v0.3; flips to ``True`` in v0.4 once the
        session-log path catalog ships.
        """

        return self.accepts_continue


_ADAPTER_CHECK: RuntimeAdapter = OpenCodeAdapter()

__all__ = ["OpenCodeAdapter"]
