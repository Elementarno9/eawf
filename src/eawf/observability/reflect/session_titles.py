"""Titles for history sessions, written once from a structural digest and cached.

A history session is titled the way a Run is: its structural facts go into a
:class:`~eawf.observability.reflect.titles.SessionDigest`, the digest's hash
keys the shared local title cache, and an uncached digest is sent to the title
provider once. The digest carries counts and labels only, never a prompt, a
tool payload or any transcript text, because the session index keeps none.

An answer is held to the title contract before it is stored: quotes and a
``Title:`` lead are stripped, and an answer that is absent, over-long, under
three characters, ``UNKNOWN`` or a refusal is rejected. A rejected answer is
asked once more; a second rejection leaves the digest uncached, so the session
falls back to its structural title rather than to a wrong one. Every call spends
the run's :class:`~eawf.observability.reflect.titles.CallBudget`; once it is spent,
the remaining digests keep their structural titles for a later run to fill.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import Final

from eawf.observability.reflect.analysis import HistorySession, Metric, SessionTitle
from eawf.observability.reflect.titles import (
    TITLE_MAX_CHARS,
    UNKNOWN_ANSWER,
    CachedTitle,
    CallBudget,
    FillAccount,
    SessionDigest,
    TitleCache,
    TitleProvider,
    TitleSource,
    model_title,
)

logger = logging.getLogger(__name__)

#: How many times one digest is asked before its title falls back.
PROVIDER_ATTEMPTS: Final = 2

_TITLE_LEAD: Final = re.compile(r"(?i)^title\s*[:-]\s*")
_NOT_A_TITLE: Final = re.compile(r"(?i)^(?:i |sorry|as an|here is|here's|the digest)")
_MIN_CHARS: Final = 3
_MS_PER_HOUR: Final = Decimal(3_600_000)
_TENTH: Final = Decimal("0.1")


def history_digest(session: HistorySession) -> SessionDigest:
    """Return the structural digest one history session's title is asked for.

    Args:
        session: The session.

    Returns:
        The digest; a history session records no terminal state, so its
        observation is ``unknown``.
    """
    calendar_ms = session.metrics.get(Metric.CALENDAR_MS)
    return SessionDigest(
        project_label=session.project_label,
        runtime=session.runtime,
        model_family=session.model_family,
        terminal_observation="unknown",
        model_steps=int(session.metrics.get(Metric.MODEL_STEPS, 0.0)),
        subagents=int(session.metrics.get(Metric.CHILD_COUNT, 0.0)),
        tool_work=int(session.metrics.get(Metric.TOOL_CALLS, 0.0)),
        wall_hours=(
            None
            if calendar_ms is None
            else (Decimal(int(calendar_ms)) / _MS_PER_HOUR).quantize(_TENTH)
        ),
    )


def normalize_title(answer: str | None) -> str | None:
    """Return the contract-satisfying title in a provider answer, or ``None``.

    Formatting noise is stripped rather than rejected, because asking again
    buys nothing a strip does; what is rejected is a title that is absent,
    over-long, too short, ``UNKNOWN`` or not a title at all.

    Args:
        answer: The provider's raw answer.

    Returns:
        The scrubbed title, at most :data:`TITLE_MAX_CHARS` characters.
    """
    if answer is None:
        return None
    candidate = " ".join(answer.split())
    candidate = _TITLE_LEAD.sub("", candidate).strip().strip("\"'").strip().rstrip(".").strip()
    if not candidate or candidate.upper() == UNKNOWN_ANSWER:
        return None
    if len(candidate) > TITLE_MAX_CHARS or len(candidate) < _MIN_CHARS:
        return None
    if _NOT_A_TITLE.match(candidate):
        return None
    return model_title(candidate)


def _structural(session: HistorySession, digest: SessionDigest) -> str | None:
    """Return the title the index alone supports, or ``None`` without a start time."""
    if session.started_at is None:
        return None
    title = (
        f"Review the {digest.project_label} {digest.runtime} session of"
        f" {session.started_at.date().isoformat()}, {digest.model_steps} steps"
    )
    return title[:TITLE_MAX_CHARS].rstrip(". ")


def _ask(
    provider: TitleProvider, digest: SessionDigest, account: FillAccount, budget: CallBudget
) -> str | None:
    """Ask for one digest's title, once more after a rejected answer, within the budget."""
    if not budget.spend():
        account.digests_skipped += 1
        return None
    for attempt in range(PROVIDER_ATTEMPTS):
        if attempt and not budget.spend():
            return None
        answer = provider.title_for(digest.text())
        account.digests_sent += 1
        title = normalize_title(answer)
        if title is not None:
            return title
        if answer is not None and answer.strip() == UNKNOWN_ANSWER:
            account.unknown_uncached += 1
            return None
    return None


def fill_session_titles(
    sessions: Sequence[HistorySession],
    *,
    cache: TitleCache,
    provider: TitleProvider | None,
    local_only: bool,
    now: datetime,
    budget: CallBudget | None = None,
) -> tuple[dict[str, SessionTitle], FillAccount, TitleCache]:
    """Return every session's title, the fill's account and the cache after it.

    Args:
        sessions: The sessions to title.
        cache: The local title cache as read.
        provider: The provider to ask for an uncached digest; ``None`` when none
            is available or ``--local-only`` is in force.
        local_only: Whether ``--local-only`` is in force.
        now: When a new answer is stored.
        budget: The provider calls the run may still make; a fresh
            :data:`~eawf.observability.reflect.titles.TITLE_CALLS_PER_RUN`
            budget when omitted.

    Returns:
        A title per session ref, the account, and the cache with every new
        answer added under its digest key.
    """
    budget = CallBudget() if budget is None else budget
    account = FillAccount(
        provider=None if provider is None else provider.name, local_only=local_only
    )
    entries: dict[str, CachedTitle] = dict(cache.entries)
    titles: dict[str, SessionTitle] = {}
    for session in sessions:
        digest = history_digest(session)
        key = digest.key()
        title: str | None = None
        if key in entries:
            account.cache_hits += 1
            title = entries[key].title
        elif provider is not None:
            title = _ask(provider, digest, account, budget)
            if title is not None:
                entries[key] = CachedTitle(title=title, stored_at=now)
        source = TitleSource.MODEL_WRITTEN
        if title is None:
            title = _structural(session, digest)
            source = TitleSource.UNAVAILABLE if title is None else TitleSource.STRUCTURAL
        account.by_source[source.value] += 1
        titles[session.session_ref] = SessionTitle(
            session_ref=session.session_ref, title=title, source=source
        )
    logger.debug(
        f"fill_session_titles sessions={len(titles)} sent={account.digests_sent} "
        f"hits={account.cache_hits} local_only={local_only}"
    )
    return titles, account, TitleCache(entries=entries)


__all__ = [
    "PROVIDER_ATTEMPTS",
    "fill_session_titles",
    "history_digest",
    "normalize_title",
]
