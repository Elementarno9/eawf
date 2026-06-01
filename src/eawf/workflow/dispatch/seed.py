"""Interim verdict seeder for the ``eawf hook dispatch agent_end`` surface.

The self-eval surface (``eawf.observability.eval.self_eval``) and the jury
reducer (``eawf.observability.eval.jury``) both read their cohort off the
per-role agent-report stores via
:func:`eawf.workflow.agent_report.rollup.iter_agent_reports`, deriving one
:class:`~eawf.kernel.state.enums.AgentReportVerdict` per row. Those surfaces
ship before the live per-wave verdict producer (a later iter), so until the
live producer lands the stores carry **zero** verdict rows and self-eval
honestly refuses to score.

This module is the **interim / manual** producer that primes the store: it
translates one ``agent_end`` hook event into a single seeded verdict row,
appended through the canonical agent-report writer
:func:`eawf.workflow.agent_report.store.append_agent_report`. The seeded row
is byte-for-byte indistinguishable from one the live producer will write —
same store kind, same envelope shape, same ``body.verdict`` — so the
self-eval + jury surfaces read a seeded cohort exactly as they will read a
live one. When the live producer lands it replaces this seam without any
downstream reader change.

The seeder is deliberately session-authority-bound: it reuses the existing
:class:`~eawf.kernel.state.models.AgentSession` named by the payload rather
than synthesising a session-less row. A session-less report would trip the
``INV.AGENT_REPORT.SESSION_MISSING`` state invariant
(:func:`eawf.kernel.validate.invariants.check_agent_report_invariants`), so
seeding through the real session keeps the primed store valid under the same
checks a live row passes. The append targets the append-only per-role store
JSONL, never ``state.json`` — the seeder does not mutate canonical state.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

from eawf.kernel.state.models import State
from eawf.workflow.agent_report.store import (
    AgentReportAppendResult,
    append_agent_report,
    parse_agent_report_body,
)

logger = logging.getLogger(__name__)


def seed_interim_verdict(
    *,
    state: State,
    state_path: Path,
    session_id: str,
    base_id: str,
    body: object,
    runtime: str | None = None,
    generated_at: datetime | None = None,
    artifact_ids: list[str] | None = None,
    blob_refs: list[str] | None = None,
) -> AgentReportAppendResult:
    """Seed one interim verdict row from an ``agent_end`` hook event.

    Validates *body* as one of the role-specific agent-report bodies, then
    appends it through the canonical writer using the session named by
    *session_id* as authority. The persisted row's
    :class:`~eawf.kernel.state.enums.AgentReportVerdict` (``body.verdict``)
    is the seeded verdict the self-eval + jury surfaces read; the row lands
    in the session-role's store (``executor_report.jsonl``,
    ``auditor_report.jsonl``, ...) per the writer's role -> store-kind map.

    This is the interim producer: it reuses an existing session rather than
    minting a session-less row, so the seeded store stays valid under
    :func:`eawf.kernel.validate.invariants.check_agent_report_invariants`
    (a session-less row would trip ``SESSION_MISSING``). The append is
    store-only — no ``state.json`` mutation.

    Args:
        state: Loaded, validated state — supplies the session authority.
        state_path: Path to ``state.json``; the role store resolves under
            its sibling ``store/`` directory.
        session_id: Id of the session that authored the seed; must exist in
            ``state.agent_sessions`` (the writer fails fast otherwise).
        base_id: Report ``base_id`` — typically the wave / scope id the
            verdict is about; groups monotonic attempts per ``(role,
            base_id)``.
        body: Raw role-report body mapping; validated into the typed
            :class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody`
            discriminated union before the append.
        runtime: Runtime label recorded on the report header; defaults to
            the session's runtime when ``None``.
        generated_at: Generation timestamp; defaults to ``now(UTC)``.
        artifact_ids: Optional artifact ids attached to the row.
        blob_refs: Optional blob refs attached to the row.

    Returns:
        The :class:`~eawf.workflow.agent_report.store.AgentReportAppendResult`
        for the seeded row — its ``urn`` is the seeded cohort's store URN and
        ``store_kind`` the per-role store the verdict lands in.

    Raises:
        pydantic.ValidationError: When *body* is not a valid role-report
            body.
        KeyError: When *session_id* is absent from ``state.agent_sessions``.
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When
            the body role disagrees with the session role.
        eawf.workflow.agent_report.store.AgentReportScrubError: When the body
            text carries local or sensitive tokens.
    """
    typed_body = parse_agent_report_body(body)
    moment = generated_at if generated_at is not None else datetime.now(UTC)
    result = append_agent_report(
        state=state,
        state_path=state_path,
        session_id=session_id,
        base_id=base_id,
        body=typed_body,
        runtime=runtime,
        generated_at=moment,
        artifact_ids=artifact_ids,
        blob_refs=blob_refs,
    )
    logger.info(
        f"seed_interim_verdict base_id={base_id!r} verdict={typed_body.verdict.value!r} "
        f"store_kind={result.store_kind!r} attempt={result.attempt}"
    )
    return result


__all__ = [
    "seed_interim_verdict",
]
