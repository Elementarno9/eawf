"""Tests for :mod:`eawf.kernel.spec.operator_input` (P29-I01-W15).

Pins the hub-and-spoke operator-input channel + the campaign progress
projection:

- :class:`OperatorInput` accepts each of the four kinds
  (``override`` / ``notice-broadcast`` / ``add_question`` / ``steer``)
  with the matching kind-specific payload, and rejects a payload that
  does not match the declared kind (``extra="forbid"`` + the
  payload-matches-kind validator).
- **D-2 — blocking-only interrupt.** Only a blocking
  (:attr:`Urgency.URGENT`) input soft-pauses the round; a lower-urgency
  input is queued. :meth:`OperatorInputChannel.fold` partitions the log
  accordingly.
- **D-3 — override persists-locked.** A locked override stays in the
  channel's effective set across the fold until a later override on the
  same scope clears the lock (``locked=False``); a one-shot
  (``locked=False``) override never enters the effective set.
- :class:`CampaignProgressState.project` reduces a round counter + a
  per-domain table into one closed :class:`CampaignProgressKind` over the
  0 / 1 / N-round (and 0 / 1 / N-domain) boundary cases, by a fixed
  precedence, and rejects negative round / blocking counts and a
  non-terminal ``terminal`` argument.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.spec.operator_input import (
    BLOCKING_URGENCY,
    AddQuestionPayload,
    CampaignProgressKind,
    CampaignProgressState,
    ChannelFold,
    DomainProgress,
    DomainProgressStatus,
    EffectiveOverride,
    OperatorInput,
    OperatorInputChannel,
    OperatorInputKind,
    OverridePayload,
    SteerAction,
    SteerPayload,
)
from eawf.kernel.state.enums import Urgency

_AT = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_CAMPAIGN = "RC-001"


def _input(
    *,
    kind: OperatorInputKind,
    scope: str = "campaign",
    note: str = "operator note",
    urgency: Urgency = Urgency.NORMAL,
    payload: object | None = None,
    at: datetime = _AT,
) -> OperatorInput:
    """Build an OperatorInput on minimal valid defaults."""
    return OperatorInput(
        campaign_id=_CAMPAIGN,
        kind=kind,
        scope=scope,
        note=note,
        urgency=urgency,
        payload=payload,
        at=at,
    )


# --- The four kinds parse with the matching payload ----------------------


def test_override_input_parses_with_override_payload() -> None:
    """An ``override`` carries an OverridePayload (value + locked)."""
    inp = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope="topic:liquidity",
        payload=OverridePayload(value="claude-code", locked=True),
    )
    assert inp.kind is OperatorInputKind.OVERRIDE
    assert isinstance(inp.payload, OverridePayload)
    assert inp.payload.value == "claude-code"
    assert inp.payload.locked is True
    assert inp.author == "operator"


def test_notice_broadcast_input_parses_without_payload() -> None:
    """A ``notice-broadcast`` carries no payload; the note carries it."""
    inp = _input(
        kind=OperatorInputKind.NOTICE_BROADCAST,
        note="ABC shipped a v2 API, re-check integration claims",
    )
    assert inp.kind is OperatorInputKind.NOTICE_BROADCAST
    assert inp.payload is None
    # The canonical wire value carries the hyphen.
    assert inp.kind.value == "notice-broadcast"


def test_add_question_input_parses_with_add_question_payload() -> None:
    """An ``add_question`` carries an AddQuestionPayload (text + urgency)."""
    inp = _input(
        kind=OperatorInputKind.ADD_QUESTION,
        payload=AddQuestionPayload(text="Does the v2 API rate-limit?", urgency=Urgency.HIGH),
    )
    assert inp.kind is OperatorInputKind.ADD_QUESTION
    assert isinstance(inp.payload, AddQuestionPayload)
    assert inp.payload.text == "Does the v2 API rate-limit?"
    assert inp.payload.urgency is Urgency.HIGH


def test_steer_input_parses_with_steer_payload() -> None:
    """A ``steer`` carries a SteerPayload (narrow / widen / park)."""
    inp = _input(
        kind=OperatorInputKind.STEER,
        scope="topic:regulatory",
        payload=SteerPayload(action=SteerAction.PARK),
    )
    assert inp.kind is OperatorInputKind.STEER
    assert isinstance(inp.payload, SteerPayload)
    assert inp.payload.action is SteerAction.PARK


def test_add_question_payload_defaults_urgency_to_normal() -> None:
    """An injected question defaults to NORMAL urgency (additive)."""
    payload = AddQuestionPayload(text="open gap")
    assert payload.urgency is Urgency.NORMAL


# --- Payload-kind mismatch fails fast (error path) -----------------------


def test_override_without_payload_rejected() -> None:
    """An ``override`` missing its payload fails the kind-matches validator."""
    with pytest.raises(ValidationError, match="requires OverridePayload"):
        _input(kind=OperatorInputKind.OVERRIDE, payload=None)


def test_notice_broadcast_with_payload_rejected() -> None:
    """A ``notice-broadcast`` carrying a payload fails (it carries none)."""
    with pytest.raises(ValidationError, match="carries no payload"):
        _input(
            kind=OperatorInputKind.NOTICE_BROADCAST,
            payload=SteerPayload(action=SteerAction.WIDEN),
        )


def test_steer_with_wrong_payload_rejected() -> None:
    """A ``steer`` carrying an override payload fails the kind match."""
    with pytest.raises(ValidationError, match="requires SteerPayload"):
        _input(
            kind=OperatorInputKind.STEER,
            payload=OverridePayload(value="x"),
        )


def test_operator_input_rejects_unknown_field() -> None:
    """``extra='forbid'`` rejects a typo'd top-level key (AGENTS rule 2)."""
    with pytest.raises(ValidationError):
        OperatorInput.model_validate(
            {
                "campaign_id": _CAMPAIGN,
                "kind": "notice-broadcast",
                "scope": "campaign",
                "note": "n",
                "athor": "operator",  # typo
                "at": _AT,
            }
        )


def test_operator_input_rejects_non_operator_author() -> None:
    """``author`` is fixed ``"operator"`` — a forged author fails."""
    with pytest.raises(ValidationError):
        OperatorInput.model_validate(
            {
                "campaign_id": _CAMPAIGN,
                "kind": "notice-broadcast",
                "scope": "campaign",
                "note": "n",
                "author": "researcher",
                "at": _AT,
            }
        )


def test_operator_input_rejects_empty_note() -> None:
    """The note is always durable — an empty note fails min_length."""
    with pytest.raises(ValidationError):
        _input(kind=OperatorInputKind.NOTICE_BROADCAST, note="")


# --- D-2: blocking-only interrupt ----------------------------------------


def test_blocking_urgency_is_urgent() -> None:
    """The blocking threshold maps onto the canonical URGENT rung."""
    assert BLOCKING_URGENCY is Urgency.URGENT


@pytest.mark.parametrize(
    "urgency",
    [Urgency.LOW, Urgency.NORMAL, Urgency.HIGH],
)
def test_non_urgent_input_is_not_blocking(urgency: Urgency) -> None:
    """Every rung below URGENT is non-blocking (queued, not a pause) — D-2."""
    inp = _input(kind=OperatorInputKind.NOTICE_BROADCAST, urgency=urgency)
    assert inp.is_blocking is False


def test_urgent_input_is_blocking() -> None:
    """An URGENT input is the only kind that blocks — D-2."""
    inp = _input(kind=OperatorInputKind.NOTICE_BROADCAST, urgency=Urgency.URGENT)
    assert inp.is_blocking is True


def test_fold_partitions_blocking_vs_queued() -> None:
    """The fold routes URGENT inputs to ``blocking`` and the rest to ``queued`` — D-2."""
    blocking = _input(
        kind=OperatorInputKind.NOTICE_BROADCAST, note="invalidates work", urgency=Urgency.URGENT
    )
    queued_low = _input(kind=OperatorInputKind.NOTICE_BROADCAST, note="fyi", urgency=Urgency.LOW)
    queued_high = _input(
        kind=OperatorInputKind.STEER,
        scope="topic:x",
        note="narrow",
        urgency=Urgency.HIGH,
        payload=SteerPayload(action=SteerAction.NARROW),
    )
    fold = OperatorInputChannel.fold([queued_low, blocking, queued_high])
    assert fold.paused is True
    assert fold.blocking == (blocking,)
    assert fold.queued == (queued_low, queued_high)
    # The two partitions reconstruct the whole log (order preserved within each).
    assert len(fold.blocking) + len(fold.queued) == 3


def test_fold_no_blocking_input_is_not_paused() -> None:
    """A log with no URGENT input does not pause the round — D-2."""
    fold = OperatorInputChannel.fold(
        [_input(kind=OperatorInputKind.NOTICE_BROADCAST, urgency=Urgency.HIGH)]
    )
    assert fold.paused is False
    assert fold.blocking == ()


# --- D-3: override persists-locked ---------------------------------------


def test_locked_override_is_effective() -> None:
    """A locked override lands in the channel's effective set — D-3."""
    locked = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope="topic:liquidity",
        note="ship layout B",
        payload=OverridePayload(value="layout-B", locked=True),
    )
    fold = OperatorInputChannel.fold([locked])
    assert fold.effective_overrides == (
        EffectiveOverride(scope="topic:liquidity", value="layout-B", note="ship layout B"),
    )


def test_locks_override_property() -> None:
    """``locks_override`` is True only for a locked OVERRIDE — D-3."""
    locked = _input(
        kind=OperatorInputKind.OVERRIDE,
        payload=OverridePayload(value="v", locked=True),
    )
    unlocked = _input(
        kind=OperatorInputKind.OVERRIDE,
        payload=OverridePayload(value="v", locked=False),
    )
    notice = _input(kind=OperatorInputKind.NOTICE_BROADCAST)
    assert locked.locks_override is True
    assert unlocked.locks_override is False
    assert notice.locks_override is False


def test_unlocked_override_not_effective() -> None:
    """A one-shot (``locked=False``) override never enters the effective set — D-3."""
    one_shot = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope="topic:x",
        payload=OverridePayload(value="v", locked=False),
    )
    fold = OperatorInputChannel.fold([one_shot])
    assert fold.effective_overrides == ()


def test_locked_override_persists_until_explicit_clear() -> None:
    """A locked override stays effective until a later override on the same scope clears it.

    The append-only "explicit clear" is a later ``override`` on the same
    scope carrying ``locked=False``; last-write-wins per scope then drops
    the scope from the effective set — D-3.
    """
    scope = "topic:liquidity"
    earlier = datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC)
    later = datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC)
    lock = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope=scope,
        note="lock claude-code",
        payload=OverridePayload(value="claude-code", locked=True),
        at=earlier,
    )
    clear = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope=scope,
        note="unlock",
        payload=OverridePayload(value="claude-code", locked=False),
        at=later,
    )
    # Lock alone -> effective.
    assert OperatorInputChannel.fold([lock]).effective_overrides != ()
    # Lock then explicit clear -> no longer effective (last-write-wins).
    assert OperatorInputChannel.fold([lock, clear]).effective_overrides == ()


def test_later_locked_override_supersedes_earlier_on_same_scope() -> None:
    """Last-write-wins: a later locked override replaces the earlier value — D-3."""
    scope = "topic:liquidity"
    first = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope=scope,
        note="v1",
        payload=OverridePayload(value="codex", locked=True),
        at=datetime(2026, 6, 1, 9, 0, 0, tzinfo=UTC),
    )
    second = _input(
        kind=OperatorInputKind.OVERRIDE,
        scope=scope,
        note="v2",
        payload=OverridePayload(value="claude-code", locked=True),
        at=datetime(2026, 6, 1, 10, 0, 0, tzinfo=UTC),
    )
    effective = OperatorInputChannel.fold([first, second]).effective_overrides
    assert effective == (EffectiveOverride(scope=scope, value="claude-code", note="v2"),)


def test_effective_overrides_sorted_by_scope() -> None:
    """Effective overrides come out in deterministic sorted-scope order — D-3."""
    inputs = [
        _input(
            kind=OperatorInputKind.OVERRIDE,
            scope="topic:zeta",
            payload=OverridePayload(value="z", locked=True),
        ),
        _input(
            kind=OperatorInputKind.OVERRIDE,
            scope="topic:alpha",
            payload=OverridePayload(value="a", locked=True),
        ),
    ]
    scopes = [eo.scope for eo in OperatorInputChannel.fold(inputs).effective_overrides]
    assert scopes == ["topic:alpha", "topic:zeta"]


def test_fold_empty_log_is_empty_channel_fold() -> None:
    """Folding an empty log yields an empty, non-paused ChannelFold (boundary)."""
    fold = OperatorInputChannel.fold([])
    assert isinstance(fold, ChannelFold)
    assert fold.blocking == ()
    assert fold.queued == ()
    assert fold.effective_overrides == ()
    assert fold.paused is False


def test_fold_is_pure_same_log_same_result() -> None:
    """The fold is pure: the same log yields an equal ChannelFold."""
    log = [
        _input(kind=OperatorInputKind.NOTICE_BROADCAST, urgency=Urgency.URGENT),
        _input(
            kind=OperatorInputKind.OVERRIDE,
            scope="topic:x",
            payload=OverridePayload(value="v", locked=True),
        ),
    ]
    assert OperatorInputChannel.fold(log) == OperatorInputChannel.fold(log)


# --- CampaignProgressState.project: round + per-domain over 0/1/N --------


def _domain(
    name: str,
    status: DomainProgressStatus,
    *,
    claims_logged: int = 0,
    open_questions: int = 0,
) -> DomainProgress:
    return DomainProgress(
        domain=name,
        status=status,
        claims_logged=claims_logged,
        open_questions=open_questions,
    )


def test_project_zero_rounds_empty_table_is_runnable() -> None:
    """The pre-first-round (round 0, no domains) state projects RUNNABLE (boundary)."""
    state = CampaignProgressState.project(round_index=0, domains=[])
    assert state.kind is CampaignProgressKind.RUNNABLE
    assert state.round_index == 0
    assert state.domains == ()
    assert state.is_blocked is False
    assert state.ready_domains == ()


def test_project_single_domain_ready_is_runnable() -> None:
    """A single READY domain projects RUNNABLE (single-domain boundary)."""
    state = CampaignProgressState.project(
        round_index=1, domains=[_domain("only", DomainProgressStatus.READY, claims_logged=2)]
    )
    assert state.kind is CampaignProgressKind.RUNNABLE
    assert state.round_index == 1
    assert state.ready_domains == ("only",)
    assert state.domains[0].claims_logged == 2


def test_project_n_domains_sorted_deterministic() -> None:
    """N per-domain rows come out in sorted-domain order regardless of input order."""
    rows = [
        _domain("zeta", DomainProgressStatus.SATURATED),
        _domain("alpha", DomainProgressStatus.READY),
        _domain("mu", DomainProgressStatus.WAITING_DEPS),
    ]
    state = CampaignProgressState.project(round_index=3, domains=rows)
    assert [d.domain for d in state.domains] == ["alpha", "mu", "zeta"]
    # READY present -> RUNNABLE wins over WAITING_DEPS / SATURATED.
    assert state.kind is CampaignProgressKind.RUNNABLE


def test_project_all_domains_saturated_is_saturated() -> None:
    """A non-empty table with no READY / WAITING domain projects SATURATED."""
    rows = [
        _domain("a", DomainProgressStatus.SATURATED),
        _domain("b", DomainProgressStatus.DONE),
    ]
    state = CampaignProgressState.project(round_index=5, domains=rows)
    assert state.kind is CampaignProgressKind.SATURATED
    assert state.is_blocked is False


def test_project_waiting_deps_only_is_blocked_deps() -> None:
    """A table whose only non-terminal domains wait on deps projects BLOCKED_DEPS."""
    rows = [
        _domain("a", DomainProgressStatus.WAITING_DEPS),
        _domain("b", DomainProgressStatus.SATURATED),
    ]
    state = CampaignProgressState.project(round_index=2, domains=rows)
    assert state.kind is CampaignProgressKind.BLOCKED_DEPS
    assert state.is_blocked is True


def test_project_blocking_count_wins_over_ready() -> None:
    """A positive blocking_count forces BLOCKED_AWAIT_USER over a READY domain — D-2."""
    state = CampaignProgressState.project(
        round_index=2,
        domains=[_domain("a", DomainProgressStatus.READY)],
        blocking_count=1,
    )
    assert state.kind is CampaignProgressKind.BLOCKED_AWAIT_USER
    assert state.blocking_count == 1
    assert state.is_blocked is True


def test_project_budget_wins_over_ready_but_not_blocking() -> None:
    """Budget exhaustion blocks a READY frontier, but a blocking input outranks it."""
    ready = [_domain("a", DomainProgressStatus.READY)]
    budget_only = CampaignProgressState.project(round_index=2, domains=ready, budget_exhausted=True)
    assert budget_only.kind is CampaignProgressKind.BLOCKED_BUDGET
    # Blocking input outranks budget in the precedence ladder.
    both = CampaignProgressState.project(
        round_index=2, domains=ready, blocking_count=2, budget_exhausted=True
    )
    assert both.kind is CampaignProgressKind.BLOCKED_AWAIT_USER


def test_project_explicit_terminal_wins_over_everything() -> None:
    """An explicit terminal pins DONE / CANCELLED / FAILED over the derived state."""
    ready = [_domain("a", DomainProgressStatus.READY)]
    for term in (
        CampaignProgressKind.DONE,
        CampaignProgressKind.CANCELLED,
        CampaignProgressKind.FAILED,
    ):
        state = CampaignProgressState.project(
            round_index=9, domains=ready, blocking_count=5, terminal=term
        )
        assert state.kind is term
        assert state.is_blocked is False


def test_project_rejects_negative_round_index() -> None:
    """A negative round index fails at the projection boundary (error path)."""
    with pytest.raises(ValueError, match="round_index must be non-negative"):
        CampaignProgressState.project(round_index=-1, domains=[])


def test_project_rejects_negative_blocking_count() -> None:
    """A negative blocking count fails at the projection boundary (error path)."""
    with pytest.raises(ValueError, match="blocking_count must be non-negative"):
        CampaignProgressState.project(round_index=0, domains=[], blocking_count=-1)


def test_project_rejects_non_terminal_terminal_arg() -> None:
    """Passing a non-terminal kind as ``terminal`` fails (error path)."""
    with pytest.raises(ValueError, match="terminal must be one of"):
        CampaignProgressState.project(
            round_index=0, domains=[], terminal=CampaignProgressKind.RUNNABLE
        )


def test_project_is_pure_same_args_same_state() -> None:
    """The projection is pure: the same arguments yield an equal state."""
    rows = [_domain("a", DomainProgressStatus.READY)]
    assert CampaignProgressState.project(
        round_index=4, domains=rows
    ) == CampaignProgressState.project(round_index=4, domains=rows)


def test_domain_progress_rejects_negative_counts() -> None:
    """Per-domain progress counts are non-negative (boundary)."""
    with pytest.raises(ValidationError):
        DomainProgress(domain="a", status=DomainProgressStatus.READY, claims_logged=-1)


def test_domain_progress_rejects_unknown_field() -> None:
    """``extra='forbid'`` rejects a typo'd per-domain key (AGENTS rule 2)."""
    with pytest.raises(ValidationError):
        DomainProgress.model_validate({"domain": "a", "status": "ready", "clams_logged": 1})
