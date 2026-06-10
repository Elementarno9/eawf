"""Bridge a research-campaign clarify proposal into a needs_user pause.

A research campaign that hits a gap it cannot close on its own raises a
**clarify proposal**: a typed request for operator input carrying the
question to ask and an :class:`~eawf.kernel.state.enums.Urgency` ranking how
soon a human needs to look. Until this bridge, that proposal had nowhere to
land — the durable operator-facing pause surface
(:mod:`eawf.workflow.skills.needs_user`) only ever recorded pauses raised by
skills terminating with ``status=needs_user``, so a campaign's clarify
proposal never flowed into the same inbox the operator answers out of band.
That gap is the clarify->needs_user disconnect.

:func:`bridge_clarify_to_pause` closes the single-proposal half: it takes a
:class:`ClarifyProposal` and records a ``needs_user`` pause **carrying the
proposal's urgency**, so a blocking clarify and an ordinary skill pause sort
against each other on one comparable ladder. The bridge owns no new store
shape — it wraps the daemon-owned :func:`eawf.workflow.skills.needs_user.record_pause`
append path, threading ``urgency`` through to the persisted pause row.

A whole **clarify run** emits more than one question at once: the gap a
campaign cannot close usually fans out into several disambiguations. Before
:func:`bridge_clarify_run_to_ledger` that run's questions had nowhere to go
but the floor — they were produced and discarded, leaving the two live halves
of the research loop (the clarify producer and the needs_user / OpenQuestion
ledger the operator answers out of) disconnected. The run bridge closes that:
it seeds the ledger with one resolvable pause row per proposed question, so a
clarify run produces answerable questions instead of dropping them. It reuses
the same single-proposal append, so every seeded row resolves through the
ordinary :func:`eawf.workflow.skills.needs_user.resolve_pause` path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.enums import Urgency
from eawf.workflow.skills.bodies.user_question import UserQuestion
from eawf.workflow.skills.needs_user import record_pause

if TYPE_CHECKING:
    from eawf.kernel.store.envelope import Envelope

logger = logging.getLogger(__name__)


class ClarifyProposal(BaseModel):
    """A research-campaign request for operator input, awaiting a pause.

    The campaign emits one of these when it needs a human to disambiguate
    before it can continue. The bridge converts it into a durable
    ``needs_user`` pause; the proposal itself is the in-flight shape before
    that conversion.

    Attributes:
        question: The 2-4-option :class:`UserQuestion` the operator answers.
            Reuses the existing pause/question shape so the bridged pause is
            indistinguishable from a skill-raised one once recorded.
        urgency: Shared :class:`~eawf.kernel.state.enums.Urgency` ranking how
            soon the operator should look. Carried verbatim onto the pause so
            the attention feed can sort a clarify pause against an open
            research question on one scale. Defaults to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL`.
    """

    model_config = ConfigDict(extra="forbid")

    question: UserQuestion
    urgency: Urgency = Field(default=Urgency.NORMAL)


def bridge_clarify_to_pause(
    state_path: Path,
    proposal: ClarifyProposal,
    *,
    scope_id: str,
    session: str,
    publish: Callable[[Envelope], None] | None = None,
) -> str:
    """Record *proposal* as a needs_user pause and return its ``pause-urn``.

    Resolves the clarify->needs_user disconnect: the campaign's clarify
    proposal lands in the same durable pause store the operator answers out
    of band, **carrying the proposal's urgency** so the pause sorts on the
    shared attention ladder. The append rides the daemon-owned
    :func:`eawf.workflow.skills.needs_user.record_pause` path; this function adds
    no new store shape, it only threads ``proposal.urgency`` through.

    Args:
        state_path: Absolute path to ``state.json`` (the pause store lives at
            its sibling ``store/event.jsonl``).
        proposal: The validated :class:`ClarifyProposal` to bridge.
        scope_id: Campaign / scope the pause belongs to.
        session: Originating session URN.
        publish: Optional daemon bus publisher forwarded to
            :func:`~eawf.workflow.skills.needs_user.record_pause`; invoked after
            the durable append succeeds.

    Returns:
        The fresh ``pause-urn`` recorded for the bridged pause — the key a
        later :func:`~eawf.workflow.skills.needs_user.resolve_pause` references.
    """
    pause_urn = record_pause(
        state_path,
        scope_id=scope_id,
        session=session,
        question=proposal.question,
        urgency=proposal.urgency,
        publish=publish,
    )
    logger.info(
        f"bridge_clarify_to_pause scope={scope_id!r} pause_urn={pause_urn!r} "
        f"urgency={proposal.urgency.value}"
    )
    return pause_urn


def bridge_clarify_run_to_ledger(
    state_path: Path,
    proposals: list[ClarifyProposal],
    *,
    scope_id: str,
    session: str,
    publish: Callable[[Envelope], None] | None = None,
) -> list[str]:
    """Seed the needs_user ledger with one resolvable row per clarify question.

    Closes the clarify-run -> needs_user disconnect: a clarify run emits a
    whole batch of proposed questions, and before this bridge that batch was
    discarded. This seeds the durable ledger with one ``needs_user`` pause per
    proposal — each carrying its own urgency and each resolvable through the
    ordinary :func:`~eawf.workflow.skills.needs_user.resolve_pause` path — so the
    clarify producer and the operator's answer inbox stay connected. The empty
    run is a no-op: zero proposals seed zero rows.

    Args:
        state_path: Absolute path to ``state.json`` (the pause store lives at
            its sibling ``store/event.jsonl``).
        proposals: The clarify run's proposed questions, in emit order. Each
            seeds one ledger row; the returned urns share that order.
        scope_id: Campaign / scope the seeded pauses belong to.
        session: Originating session URN.
        publish: Optional daemon bus publisher forwarded to each
            :func:`~eawf.workflow.skills.needs_user.record_pause`; invoked once per
            seeded row after its durable append succeeds.

    Returns:
        The seeded ``pause-urn`` list, one per proposal in emit order — empty
        when *proposals* is empty.
    """
    pause_urns = [
        bridge_clarify_to_pause(
            state_path,
            proposal,
            scope_id=scope_id,
            session=session,
            publish=publish,
        )
        for proposal in proposals
    ]
    logger.info(f"bridge_clarify_run_to_ledger scope={scope_id!r} seeded={len(pause_urns)}")
    return pause_urns


__all__ = [
    "ClarifyProposal",
    "bridge_clarify_run_to_ledger",
    "bridge_clarify_to_pause",
]
