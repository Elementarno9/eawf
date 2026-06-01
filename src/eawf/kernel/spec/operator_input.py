"""OperatorInput channel + CampaignProgressState — the hub-and-spoke control plane.

A research campaign runs as a sequence of *rounds* fanned out across
several *domains* (the W14 :class:`~eawf.kernel.spec.research_campaign.StagedCampaign`
shape). This module owns the two operator-facing halves of the
balanced-autonomy control plane that ride on top of that campaign:

- the **operator-input channel** — :class:`OperatorInput` is one
  append-only, typed input the operator pushes *while a campaign runs*.
  It carries one of four kinds (:class:`OperatorInputKind`:
  ``override`` / ``notice-broadcast`` / ``add_question`` / ``steer``),
  a free-text ``note``, a kind-specific ``payload``, and the shared
  :class:`~eawf.kernel.state.enums.Urgency` ladder. The channel is
  hub-and-spoke (AGENTS rule 4 + the a2a verdict): the operator never
  messages an agent directly — every input lands on the daemon-owned
  blackboard and the orchestrator distributes it. :class:`OperatorInputChannel`
  is the **pure reducer** that folds an append-log of inputs into the
  two decisions a round loop needs — which inputs *pause* the round
  (D-2: blocking-only interrupt) and which locked overrides are
  *effective* this round (D-3: an override persists-locked until
  explicitly cleared); and
- the **progress projection** — :class:`CampaignProgressState` is a pure
  projection (same shape as the saturation reducer
  :class:`~eawf.kernel.spec.saturation.SaturationReport`) that answers
  "can this campaign go further, or is it blocked?" by reducing a round
  counter + a per-domain progress table into a single closed
  :class:`CampaignProgressKind` state plus the per-domain detail.

Both halves are pure: they perform no I/O, mutate nothing, and never
raise on the read path (a paused input, a locked override, a blocked
state are all *data* on the returned value, not exceptions). The round
loop / orchestrator / ``eawf research status`` surface consumes the
reduced values; this module never spawns, opens a session, or writes
state.

Decision provenance
-------------------
The two operator-decisions this module encodes (named in the campaign
control-plane brief, recorded as typed Decision rows in ``state.json``):

- **D-2 — blocking-only interrupt.** Only a blocking input soft-pauses
  the running round; a non-blocking input is queued for an agent's next
  task rather than interrupting work already in flight. The blocking
  threshold maps onto the canonical :class:`~eawf.kernel.state.enums.Urgency`
  ladder: :attr:`~eawf.kernel.state.enums.Urgency.URGENT` is the only
  rung that blocks (the ladder's own docstring fixes ``URGENT`` as
  "blocks progress; raise to the operator now"), mirroring the W19
  coupling where a blocking :class:`~eawf.kernel.state.models.OpenQuestion`
  carries urgency.
- **D-3 — override persists-locked.** A locked override
  (``OverridePayload.locked`` is ``True``) is treated as fixed by the
  decomposer/router and survives subsequent rounds; it stays active
  until a later override on the same scope explicitly clears the lock.
  Because the channel is append-only the clear is itself an input — a
  later ``override`` on the same scope carrying ``locked=False``.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import Urgency
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: The :class:`Urgency` rung at (or above) which an operator input
#: soft-pauses the running round — the D-2 blocking-only threshold. The
#: canonical ladder fixes :attr:`Urgency.URGENT` as "blocks progress;
#: raise to the operator now", so it is the single blocking rung; every
#: lower rung (``LOW`` / ``NORMAL`` / ``HIGH``) is queued for an agent's
#: next task instead of interrupting in-flight work.
BLOCKING_URGENCY: Urgency = Urgency.URGENT


class OperatorInputKind(StrEnum):
    """Closed vocabulary for :attr:`OperatorInput.kind`.

    Every operator-initiated mid-run input is exactly one of these four
    kinds. The values are stable wire identifiers; the names follow the
    canonical control-plane spelling (note the hyphen in
    ``notice-broadcast``).

    Values:
        OVERRIDE: A forced choice. Pins a value (a run-param / qualify /
            claim) via :class:`OverridePayload`; when
            :attr:`OverridePayload.locked` is ``True`` the decomposer /
            router treats it as fixed and never re-derives it, so it
            persists across rounds (D-3) until a later override on the
            same scope clears the lock.
        NOTICE_BROADCAST: A free-text input not tied to any decision,
            addressed to the whole campaign, a topic, or a role (the
            operator's "notify all research agents about new inputs").
            The :attr:`OperatorInput.note` carries the broadcast; there
            is no kind-specific payload.
        ADD_QUESTION: Inject a new open question into the campaign ledger
            mid-run via :class:`AddQuestionPayload`; the next round (or a
            freeing slot in the current one) picks it up.
        STEER: Narrow / widen / park a topic without blocking the run,
            via :class:`SteerPayload` — the between-rounds feedback loop.
    """

    OVERRIDE = "override"
    NOTICE_BROADCAST = "notice-broadcast"
    ADD_QUESTION = "add_question"
    STEER = "steer"


class SteerAction(StrEnum):
    """Closed set of steer directions for :attr:`SteerPayload.action`.

    Values:
        NARROW: Tighten a topic's scope (drop sub-questions / sources).
        WIDEN: Broaden a topic's scope (admit more sub-questions).
        PARK: Pause a topic without dropping it — it can be un-parked
            by a later steer.
    """

    NARROW = "narrow"
    WIDEN = "widen"
    PARK = "park"


class OverridePayload(BaseModel):
    """Kind-specific payload for an :attr:`OperatorInputKind.OVERRIDE` input.

    Attributes:
        value: The pinned choice — a run-param value, a qualify verdict,
            or a claim id, as a free-text token the decomposer / router
            consumes. Non-empty.
        locked: Whether the override persists-locked (D-3). ``True`` makes
            the decomposer / router treat the value as fixed and never
            re-derive it across rounds; the lock holds until a later
            override on the same scope sets ``locked=False``. Defaults to
            ``False`` (a one-shot override that does not survive the
            round it lands in).
    """

    model_config = ConfigDict(extra="forbid")

    value: str = Field(min_length=1)
    locked: bool = False


class AddQuestionPayload(BaseModel):
    """Kind-specific payload for an :attr:`OperatorInputKind.ADD_QUESTION` input.

    Attributes:
        text: The question text injected into the campaign ledger.
            Non-empty, bounded at 280 characters (one scannable line).
        urgency: The :class:`~eawf.kernel.state.enums.Urgency` the new
            question inherits when it lands in the ledger. Defaults to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL` so an injected
            question ranks like an ordinarily-surfaced one unless the
            operator escalates it.
    """

    model_config = ConfigDict(extra="forbid")

    text: str = Field(min_length=1, max_length=280)
    urgency: Urgency = Urgency.NORMAL


class SteerPayload(BaseModel):
    """Kind-specific payload for an :attr:`OperatorInputKind.STEER` input.

    Attributes:
        action: The steer direction — narrow / widen / park the topic the
            input's :attr:`OperatorInput.scope` names.
    """

    model_config = ConfigDict(extra="forbid")

    action: SteerAction


class OperatorInput(BaseModel):
    """One append-only, daemon-owned operator input pushed mid-run.

    Models every operator-initiated input as a single typed record so the
    orchestrator can prepend the active inputs for an agent's scope to its
    dispatch context (hub-and-spoke distribution — the operator never
    messages an agent directly). The record is durable, so every later
    round sees the input and the final brief's provenance lists what the
    operator injected and why.

    The :attr:`payload` is a discriminated union keyed by :attr:`kind`:
    an ``override`` carries an :class:`OverridePayload`, an
    ``add_question`` an :class:`AddQuestionPayload`, a ``steer`` a
    :class:`SteerPayload`, and a ``notice-broadcast`` carries no payload
    (``None`` — the :attr:`note` carries the broadcast text). The
    :meth:`_payload_matches_kind` validator rejects a payload that does
    not match the declared kind, so a malformed input fails fast at the
    ingestion boundary (AGENTS rule 2).

    Attributes:
        campaign_id: Id of the running campaign this input targets.
        kind: Which of the four :class:`OperatorInputKind` inputs this is.
        scope: The blackboard scope the input addresses — the whole
            campaign, a topic, a role, a choice, or a question (a
            free-text scope token such as ``"campaign"`` / ``"topic:liquidity"``
            / ``"role:researcher"``). Non-empty.
        note: Free-text WHY / the input itself — always durable so the
            provenance trail records the operator's reason. Non-empty.
        urgency: The shared :class:`~eawf.kernel.state.enums.Urgency`
            ladder ranking how soon the operator needs the input acted on.
            :data:`BLOCKING_URGENCY` (``URGENT``) is the only rung that
            soft-pauses the round (D-2); every lower rung is queued.
            Defaults to :attr:`~eawf.kernel.state.enums.Urgency.NORMAL`.
        payload: Kind-specific payload (see the discriminated union
            above); ``None`` for a ``notice-broadcast``.
        author: Who pushed the input. Fixed ``"operator"`` — the channel
            is operator-driven by construction.
        at: When the input was pushed (timezone-aware UTC).
    """

    model_config = ConfigDict(extra="forbid")

    campaign_id: str = Field(min_length=1)
    kind: OperatorInputKind
    scope: str = Field(min_length=1)
    note: str = Field(min_length=1)
    urgency: Urgency = Urgency.NORMAL
    payload: OverridePayload | AddQuestionPayload | SteerPayload | None = None
    author: Literal["operator"] = "operator"
    at: UtcDatetime

    @property
    def is_blocking(self) -> bool:
        """Whether this input soft-pauses the running round (D-2).

        Returns:
            ``True`` iff :attr:`urgency` is at the blocking rung
            (:data:`BLOCKING_URGENCY`); a lower rung is queued for an
            agent's next task instead of interrupting in-flight work.
        """
        return self.urgency is BLOCKING_URGENCY

    @property
    def locks_override(self) -> bool:
        """Whether this input is a locked override that persists (D-3).

        Returns:
            ``True`` iff this is an :attr:`OperatorInputKind.OVERRIDE`
            carrying an :class:`OverridePayload` with ``locked`` set; such
            an override stays fixed across rounds until a later override
            on the same :attr:`scope` clears the lock.
        """
        return (
            self.kind is OperatorInputKind.OVERRIDE
            and isinstance(self.payload, OverridePayload)
            and self.payload.locked
        )

    @model_validator(mode="after")
    def _payload_matches_kind(self) -> OperatorInput:
        """Reject a payload that does not match the declared :attr:`kind`.

        Returns:
            The validated input (unchanged).

        Raises:
            ValueError: when the payload type does not match
                :attr:`kind` — e.g. an ``override`` without an
                :class:`OverridePayload`, or a ``notice-broadcast`` that
                carries a payload at all.
        """
        expected: dict[OperatorInputKind, type[BaseModel] | None] = {
            OperatorInputKind.OVERRIDE: OverridePayload,
            OperatorInputKind.ADD_QUESTION: AddQuestionPayload,
            OperatorInputKind.STEER: SteerPayload,
            OperatorInputKind.NOTICE_BROADCAST: None,
        }
        want = expected[self.kind]
        if want is None:
            if self.payload is not None:
                raise ValueError(
                    f"kind {self.kind.value!r} carries no payload, "
                    f"got {type(self.payload).__name__}"
                )
        elif not isinstance(self.payload, want):
            got = type(self.payload).__name__ if self.payload is not None else "None"
            raise ValueError(
                f"kind {self.kind.value!r} requires {want.__name__} payload, got {got}"
            )
        return self


@dataclass(frozen=True)
class EffectiveOverride:
    """One locked override that is still in force, folded by the channel.

    Attributes:
        scope: The blackboard scope the override pins (the
            :attr:`OperatorInput.scope` of the winning input).
        value: The pinned :attr:`OverridePayload.value`.
        note: The operator's WHY from the winning override input.
    """

    scope: str
    value: str
    note: str


@dataclass(frozen=True)
class ChannelFold:
    """Typed result of folding an :class:`OperatorInput` append-log.

    Produced only by :meth:`OperatorInputChannel.fold` — never
    hand-constructed on the call path. Carries the two decisions a round
    loop needs from the channel: which inputs pause the round (D-2) and
    which locked overrides are effective (D-3).

    Attributes:
        blocking: The inputs that soft-pause the round, in append order —
            every input whose :attr:`OperatorInput.is_blocking` is
            ``True`` (D-2). Empty when no input blocks.
        queued: The non-blocking inputs, in append order — distributed to
            agents on their next task rather than interrupting in-flight
            work. The union of :attr:`blocking` + :attr:`queued` is the
            whole log in order.
        effective_overrides: The locked overrides still in force, one per
            scope, in sorted-scope order for deterministic output (D-3).
            A locked override on a scope stays here until a later override
            on the same scope clears the lock (``locked=False``).
    """

    blocking: tuple[OperatorInput, ...] = ()
    queued: tuple[OperatorInput, ...] = ()
    effective_overrides: tuple[EffectiveOverride, ...] = ()

    @property
    def paused(self) -> bool:
        """Whether the round is soft-paused — any blocking input present (D-2)."""
        return bool(self.blocking)


class OperatorInputChannel:
    """Pure reducer folding an :class:`OperatorInput` append-log.

    The channel is not a container the caller mutates — it is a stateless
    reducer (a single :meth:`fold` classmethod) over the daemon-owned
    append-log of inputs. Folding is pure: the same log always yields the
    same :class:`ChannelFold`; it performs no I/O, mutates nothing, and
    never raises on the read path.
    """

    @classmethod
    def fold(cls, inputs: Iterable[OperatorInput]) -> ChannelFold:
        """Fold an append-log of inputs into the round-loop decisions.

        Partitions the log into the inputs that pause the round (D-2:
        blocking-only) and the inputs that are queued, and folds the
        override stream into the set of locked overrides still in force
        (D-3: an override persists-locked until a later override on the
        same scope clears the lock).

        The override fold is *last-write-wins per scope*: each
        ``override`` input replaces any prior override on the same scope.
        A later override carrying ``locked=False`` therefore *clears* a
        previously locked one (the append-only "explicit clear"); only the
        scopes whose latest override is locked survive into
        :attr:`ChannelFold.effective_overrides`.

        Args:
            inputs: The operator-input append-log, in append (chronological)
                order. The caller is responsible for ordering; the fold
                preserves input order for the blocking / queued partitions
                and resolves the override stream by last-write-wins.

        Returns:
            A :class:`ChannelFold` carrying the blocking / queued
            partitions and the effective locked overrides.
        """
        input_list = list(inputs)
        blocking: list[OperatorInput] = []
        queued: list[OperatorInput] = []
        # Last-write-wins per scope: a later override on a scope replaces
        # the earlier one. We track the latest OverridePayload per scope
        # (with its note) and read `locked` off the winner at the end, so
        # a later locked=False clears a prior locked=True (D-3 clear).
        latest_override: dict[str, tuple[OverridePayload, str]] = {}

        for item in input_list:
            if item.is_blocking:
                blocking.append(item)
            else:
                queued.append(item)
            if item.kind is OperatorInputKind.OVERRIDE and isinstance(
                item.payload, OverridePayload
            ):
                latest_override[item.scope] = (item.payload, item.note)

        effective = tuple(
            EffectiveOverride(scope=scope, value=payload.value, note=note)
            for scope, (payload, note) in sorted(latest_override.items())
            if payload.locked
        )
        logger.debug(
            f"fold inputs={len(input_list)} blocking={len(blocking)} "
            f"queued={len(queued)} effective_overrides={len(effective)}"
        )
        return ChannelFold(
            blocking=tuple(blocking),
            queued=tuple(queued),
            effective_overrides=effective,
        )


class CampaignProgressKind(StrEnum):
    """Closed vocabulary for :attr:`CampaignProgressState.kind`.

    Answers "can the campaign go further, or is it blocked?" with one
    state. The blocked / terminal states are mutually exclusive; the
    projection resolves exactly one (see :meth:`CampaignProgressState.project`).

    Values:
        RUNNABLE: The frontier has ready domain work — the campaign can
            run another round.
        BLOCKED_AWAIT_USER: A blocking operator input or blocking question
            is open; the round is soft-paused awaiting the operator (D-2).
        BLOCKED_BUDGET: A token / cost / rate-limit-window cap is hit.
        BLOCKED_DEPS: Every active domain waits on an unresolved
            dependency.
        SATURATED: The loop-until-dry saturation gates all passed — a
            *good* terminal, not a block (see
            :class:`~eawf.kernel.spec.saturation.SaturationReport`).
        DONE: The campaign finished normally.
        CANCELLED: The campaign was cancelled by the operator.
        FAILED: The campaign terminated on an error.
    """

    RUNNABLE = "runnable"
    BLOCKED_AWAIT_USER = "blocked_await_user"
    BLOCKED_BUDGET = "blocked_budget"
    BLOCKED_DEPS = "blocked_deps"
    SATURATED = "saturated"
    DONE = "done"
    CANCELLED = "cancelled"
    FAILED = "failed"


class DomainProgressStatus(StrEnum):
    """Per-domain progress status for one domain in a campaign round.

    Values:
        READY: The domain has frontier work it can run this round.
        WAITING_DEPS: The domain's next work waits on an unresolved
            dependency.
        BLOCKED: The domain is paused awaiting the operator (a blocking
            input / question touches its scope).
        SATURATED: The domain has converged — no new claims arriving.
        DONE: The domain finished its survey.
    """

    READY = "ready"
    WAITING_DEPS = "waiting_deps"
    BLOCKED = "blocked"
    SATURATED = "saturated"
    DONE = "done"


class DomainProgress(BaseModel):
    """Per-domain progress row consumed by :meth:`CampaignProgressState.project`.

    One row per research domain in the campaign. The projection reduces
    the per-domain table into the campaign-wide
    :class:`CampaignProgressKind`; this row is the per-domain detail the
    ``eawf research status`` surface renders under the campaign state.

    Attributes:
        domain: The research-domain name (matches the
            :class:`~eawf.kernel.spec.research_campaign.StagedDispatch`
            domain key). Non-empty.
        status: The domain's :class:`DomainProgressStatus` this round.
        claims_logged: Count of claims the domain has logged so far
            (>= 0). A scannable per-domain progress number.
        open_questions: Count of still-open questions in the domain
            (>= 0).
    """

    model_config = ConfigDict(extra="forbid")

    domain: str = Field(min_length=1)
    status: DomainProgressStatus
    claims_logged: Annotated[int, Field(ge=0)] = 0
    open_questions: Annotated[int, Field(ge=0)] = 0


@dataclass(frozen=True)
class CampaignProgressState:
    """Pure projection of a campaign's round + per-domain progress.

    Produced only by :meth:`project` — never hand-constructed on the call
    path. Mirrors the saturation reducer
    :class:`~eawf.kernel.spec.saturation.SaturationReport`: a pure
    projection (no I/O, no mutation, no raise on the read path) that folds
    the inputs into a single closed state plus the explaining detail. The
    ``eawf research status`` feed and the cockpit right-pane bands consume
    the reduced value.

    Attributes:
        kind: The single campaign-wide :class:`CampaignProgressKind` the
            projection resolved.
        round_index: The current round number (>= 0; 0 is the pre-first-round
            staging state). Carried through onto the projection so the
            status feed renders "round N".
        domains: The per-domain :class:`DomainProgress` rows, in
            sorted-domain order for deterministic output.
        blocking_count: Number of open blocking operator inputs / questions
            driving a :attr:`CampaignProgressKind.BLOCKED_AWAIT_USER`
            state; 0 otherwise.

    """

    kind: CampaignProgressKind
    round_index: int
    domains: tuple[DomainProgress, ...] = ()
    blocking_count: int = 0

    @property
    def is_blocked(self) -> bool:
        """Whether the campaign is in any of the three blocked states."""
        return self.kind in _BLOCKED_KINDS

    @property
    def ready_domains(self) -> tuple[str, ...]:
        """Names of the domains with frontier work this round, in order."""
        return tuple(d.domain for d in self.domains if d.status is DomainProgressStatus.READY)

    @classmethod
    def project(
        cls,
        *,
        round_index: int,
        domains: Sequence[DomainProgress],
        blocking_count: int = 0,
        budget_exhausted: bool = False,
        terminal: CampaignProgressKind | None = None,
    ) -> CampaignProgressState:
        """Project a round + per-domain table into a single progress state.

        Pure: the same arguments always yield the same state; no I/O, no
        mutation, no raise on the read path. The state is resolved by a
        fixed precedence so exactly one :class:`CampaignProgressKind`
        comes out:

        1. an explicit *terminal* (``DONE`` / ``CANCELLED`` / ``FAILED``)
           wins over everything — the campaign is over;
        2. else a positive ``blocking_count`` -> ``BLOCKED_AWAIT_USER``
           (D-2: a blocking input / question pauses the round);
        3. else ``budget_exhausted`` -> ``BLOCKED_BUDGET``;
        4. else any domain ``READY`` -> ``RUNNABLE``;
        5. else any domain ``WAITING_DEPS`` -> ``BLOCKED_DEPS``;
        6. else (every domain ``SATURATED`` / ``DONE``, or no domains at
           all) -> ``SATURATED`` when at least one domain converged, else
           ``RUNNABLE`` for an empty pre-staging table.

        Args:
            round_index: The current round number (must be >= 0).
            domains: The per-domain progress rows. May be empty (the
                pre-first-round staging state).
            blocking_count: Number of open blocking operator inputs /
                questions; a positive count forces ``BLOCKED_AWAIT_USER``.
                Must be >= 0.
            budget_exhausted: Whether a token / cost / rate-limit cap is
                hit; forces ``BLOCKED_BUDGET`` when no blocking input is
                open.
            terminal: An explicit terminal state to pin (``DONE`` /
                ``CANCELLED`` / ``FAILED``); when given it overrides the
                derived state. ``None`` derives the state from the table.

        Returns:
            A :class:`CampaignProgressState` carrying the resolved
            :attr:`kind`, the round index, the sorted per-domain rows, and
            the blocking count.

        Raises:
            ValueError: when ``round_index`` or ``blocking_count`` is
                negative, or when ``terminal`` is given but is not one of
                the three terminal kinds.
        """
        if round_index < 0:
            raise ValueError(f"round_index must be non-negative, got {round_index}")
        if blocking_count < 0:
            raise ValueError(f"blocking_count must be non-negative, got {blocking_count}")
        if terminal is not None and terminal not in _TERMINAL_KINDS:
            raise ValueError(
                f"terminal must be one of {sorted(k.value for k in _TERMINAL_KINDS)}, "
                f"got {terminal.value!r}"
            )

        sorted_domains = tuple(sorted(domains, key=lambda d: d.domain))
        kind = cls._resolve_kind(
            domains=sorted_domains,
            blocking_count=blocking_count,
            budget_exhausted=budget_exhausted,
            terminal=terminal,
        )
        logger.debug(
            f"project round={round_index} domains={len(sorted_domains)} "
            f"blocking={blocking_count} budget_exhausted={budget_exhausted} kind={kind.value}"
        )
        return cls(
            kind=kind,
            round_index=round_index,
            domains=sorted_domains,
            blocking_count=blocking_count,
        )

    @staticmethod
    def _resolve_kind(
        *,
        domains: tuple[DomainProgress, ...],
        blocking_count: int,
        budget_exhausted: bool,
        terminal: CampaignProgressKind | None,
    ) -> CampaignProgressKind:
        """Resolve the single progress kind by the fixed precedence.

        See :meth:`project` for the precedence ladder; this helper holds
        the branch logic so the public method stays a thin validate +
        delegate.
        """
        if terminal is not None:
            return terminal
        if blocking_count > 0:
            return CampaignProgressKind.BLOCKED_AWAIT_USER
        if budget_exhausted:
            return CampaignProgressKind.BLOCKED_BUDGET
        statuses = {d.status for d in domains}
        if DomainProgressStatus.READY in statuses:
            return CampaignProgressKind.RUNNABLE
        if DomainProgressStatus.WAITING_DEPS in statuses:
            return CampaignProgressKind.BLOCKED_DEPS
        # No ready / waiting domain left: every domain has converged or
        # finished. An empty table is the pre-staging state (runnable);
        # a non-empty all-converged table is saturated.
        if not domains:
            return CampaignProgressKind.RUNNABLE
        return CampaignProgressKind.SATURATED


#: The terminal :class:`CampaignProgressKind` values — a campaign in one of
#: these is over (no further round). ``SATURATED`` is deliberately NOT here:
#: it is a *good* stop the loop reaches, but the projection treats it as a
#: derived state, not an operator-pinned terminal.
_TERMINAL_KINDS: frozenset[CampaignProgressKind] = frozenset(
    {
        CampaignProgressKind.DONE,
        CampaignProgressKind.CANCELLED,
        CampaignProgressKind.FAILED,
    }
)

#: The :class:`CampaignProgressKind` values that mean the campaign cannot
#: currently advance (the three blocked states). Drives
#: :attr:`CampaignProgressState.is_blocked`.
_BLOCKED_KINDS: frozenset[CampaignProgressKind] = frozenset(
    {
        CampaignProgressKind.BLOCKED_AWAIT_USER,
        CampaignProgressKind.BLOCKED_BUDGET,
        CampaignProgressKind.BLOCKED_DEPS,
    }
)


__all__ = [
    "BLOCKING_URGENCY",
    "AddQuestionPayload",
    "CampaignProgressKind",
    "CampaignProgressState",
    "ChannelFold",
    "DomainProgress",
    "DomainProgressStatus",
    "EffectiveOverride",
    "OperatorInput",
    "OperatorInputChannel",
    "OperatorInputKind",
    "OverridePayload",
    "SteerAction",
    "SteerPayload",
]
