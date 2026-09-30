"""The epoch-1 open-question writers and the research store readers that remain.

``research.add_question`` and ``research.resolve_question`` write
:class:`~eawf.kernel.state.models.OpenQuestion` rows through the canonical
state writer. The store helpers read and append the epoch-1
``research_campaign`` and ``research_round`` stores that ``eawf status``
summarises. Research Campaigns themselves are native records now, planned,
driven and closed by the ``runtime.campaign.*`` verbs.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Collection
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.spec.campaign_driver import (
    RoundFindings,
)
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    ClaimStatus,
    OpenQuestionDropReason,
    OpenQuestionStatus,
    StoreKind,
    Urgency,
)
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import ResearcherReportBody
from eawf.kernel.store.kinds.research_campaign import (
    ResearchCampaignPayload,
)
from eawf.kernel.store.kinds.research_round import ResearchRoundPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import MethodContext, register

if TYPE_CHECKING:
    from eawf.kernel.state.models import Claim, OpenQuestion, State

logger = logging.getLogger(__name__)


def persist_campaign(state_path: Path, payload: ResearchCampaignPayload) -> str:
    """Append *payload* as one row to ``research_campaign.jsonl`` and return its id.

    Wraps the typed :class:`ResearchCampaignPayload` in an :class:`Envelope`
    with ``kind=StoreKind.RESEARCH_CAMPAIGN`` and appends it via
    :func:`eawf.kernel.store.append.append_envelope` (per-file portalock +
    fsync). The on-disk row is the single source of truth; no projection runs
    because a campaign record is a non-state append.

    Args:
        state_path: Path to the scope's ``state.json``; the campaign store
            resolves under its sibling ``store/`` directory.
        payload: The validated campaign payload to persist.

    Returns:
        The appended envelope id (equal to ``payload.campaign_id``).

    Raises:
        StateConflict: When the campaign-store append lock cannot be acquired
            within the canonical timeout (``kind="LockConflict"``).
    """
    campaign_path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id=None,
        created_at=datetime.now(UTC),
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(campaign_path, envelope)
    logger.info(
        f"persist_campaign id={payload.campaign_id!r} "
        f"topic={payload.campaign.topic!r} dispatches={len(payload.campaign.dispatches)}"
    )
    return payload.campaign_id


def read_latest_campaign(state_path: Path, campaign_id: str) -> ResearchCampaignPayload | None:
    """Return the most-recent persisted payload for *campaign_id*, or ``None``.

    Walks the append-only ``research_campaign.jsonl`` store under *state_path*
    in record order, validating each envelope + payload, and returns the LAST
    row matching *campaign_id* so a campaign that has been re-appended (e.g. a
    cancel that stamps a fresh tombstoned row) resolves to its current state.
    Returns ``None`` when the store is absent or carries no row for the id.

    Args:
        state_path: Path to the scope's ``state.json``; the campaign store
            resolves under its sibling ``store/`` directory.
        campaign_id: The campaign id to resolve.

    Returns:
        The latest matching :class:`ResearchCampaignPayload`, or ``None``.
    """
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    if not path.exists():
        return None
    latest: ResearchCampaignPayload | None = None
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchCampaignPayload.model_validate(envelope.payload)
        if payload.campaign_id == campaign_id:
            latest = payload
    return latest


def _resolve_research_scope(state: State, scope_id: str | None) -> str:
    """Resolve the scope a research claim / question is bound to.

    An explicit *scope_id* wins; otherwise the active project's code anchors
    the row (the campaign is scoped to the project, like every other research
    entity). A scope-less state (no project) falls back to the literal
    ``"research"`` so the row still validates.

    Args:
        state: The loaded state the row is being added to.
        scope_id: An explicit caller-supplied scope id, or ``None``.

    Returns:
        The resolved non-empty scope id.
    """
    if scope_id:
        return scope_id
    if state.project is not None:
        return state.project.code
    return "research"


class AddQuestionParams(BaseModel):
    """Params for :func:`add_question`.

    The campaign control-plane ``add_question`` channel: the operator (via the
    TUI ``o`` key or the retired headless ``question add`` verb) injects a
    new :class:`~eawf.kernel.state.models.OpenQuestion` into the campaign ledger
    mid-run. The TUI sends only ``title``; the other fields default so the row
    lands as an ordinary advisory open question unless the operator escalates
    it.

    Attributes:
        title: The question text, an imperative noun-phrase bounded to 1..72
            chars to match the :class:`~eawf.kernel.state.models.OpenQuestion`
            model's title bound. An over-cap / empty title is rejected fail-fast
            at the params boundary (``-32602 invalid params``) before any side
            effect.
        description: Optional long-form framing for the question.
        blocking: Whether the question gates further campaign work (the
            balanced-autonomy interrupt). Defaults ``False`` (advisory).
        urgency: The shared :class:`~eawf.kernel.state.enums.Urgency` rung the
            question inherits. Defaults to ``NORMAL``.
        scope_id: Explicit scope the question binds to; ``None`` resolves to
            the active project's code.
        question_id: Optional caller-allocated id; ``None`` allocates a fresh
            ``OQ-<hex>`` id.
        repo_root: Optional per-request repo anchor for the worktree-aware
            state writer; ``None`` uses the daemon's boot-time state path.
    """

    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=72)
    description: str | None = Field(default=None, max_length=500)
    blocking: bool = False
    urgency: Urgency = Urgency.NORMAL
    scope_id: str | None = None
    question_id: str | None = Field(default=None, min_length=1)
    repo_root: str | None = None


class AddQuestionResult(BaseModel):
    """Result of :func:`add_question`.

    Attributes:
        question_id: The id of the open question just written.
        status: The question's lifecycle status (``"open"`` /
            ``"blocked"`` when the operator marked it blocking).
        scope_id: The resolved scope the question was bound to.
    """

    model_config = ConfigDict(extra="forbid")
    question_id: str
    status: str
    scope_id: str


def _apply_add_question(
    state: State, args: AddQuestionParams, *, question_id: str
) -> dict[str, Any]:
    """Write one :class:`OpenQuestion` row onto ``state.open_questions``.

    A blocking question lands ``BLOCKED`` (the interrupt status); an advisory
    one lands ``OPEN``. The row is constructed through the typed model, so an
    over-cap / empty title raises :class:`pydantic.ValidationError` -- the
    canonical writer maps it to ``-32002 validation_failed``.

    Args:
        state: Loaded :class:`State`. Mutated in place.
        args: Validated :class:`AddQuestionParams`.
        question_id: The resolved (caller or freshly allocated) question id.

    Returns:
        Result dict matching :class:`AddQuestionResult`.
    """
    from eawf.kernel.state.models import OpenQuestion

    status = OpenQuestionStatus.BLOCKED if args.blocking else OpenQuestionStatus.OPEN
    scope_id = _resolve_research_scope(state, args.scope_id)
    questions = dict(state.open_questions or {})
    questions[question_id] = OpenQuestion(
        id=question_id,
        scope_id=scope_id,
        title=args.title,
        description=args.description,
        status=status,
        blocking=args.blocking,
        urgency=args.urgency,
        created_at=datetime.now(UTC),
    )
    state.open_questions = questions
    logger.info(
        f"_apply_add_question question={question_id!r} scope_id={scope_id!r} "
        f"blocking={args.blocking} status={status.value}"
    )
    return AddQuestionResult(
        question_id=question_id,
        status=status.value,
        scope_id=scope_id,
    ).model_dump(mode="json")


@register("research.add_question")
async def add_question(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Write an :class:`OpenQuestion` row through the canonical state writer.

    The daemon-canonical mutator for ``state.open_questions`` (AGENTS rule 4);
    the TUI ``o`` key + the retired headless ``question add`` verb proxy
    here. The row lands through the same per-file portalock + WAL + event-append
    path every state mutator uses (:func:`commit_worktree_state`), so the
    single-writer invariant holds and the board re-renders the new question on
    its next refresh.

    Args:
        ctx: Server context -- must carry ``state_path`` (+ ``event_path`` /
            ``wal_dir`` for the canonical write).
        params: JSON-RPC params per :class:`AddQuestionParams`.

    Returns:
        Dict matching :class:`AddQuestionResult`.

    Raises:
        ValueError: When *params* does not validate against
            :class:`AddQuestionParams` (an unknown key, an empty / over-cap
            title). Mapped to ``-32602 invalid params``.
    """
    from eawf.runtime.daemon.methods.state_worktree import (
        commit_worktree_state as _commit_worktree_state,
    )

    args = AddQuestionParams.model_validate(params)
    repo_root = Path(args.repo_root) if args.repo_root else None
    question_id = args.question_id if args.question_id is not None else f"OQ-{uuid.uuid4().hex[:8]}"
    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=params,
        command="research.add_question",
        scope_id=args.scope_id,
        apply_func=lambda state: _apply_add_question(state, args, question_id=question_id),
    )


class ResolveQuestionParams(BaseModel):
    """Params for :func:`resolve_question`.

    Attributes:
        question_id: Id of the :class:`OpenQuestion` to resolve; the lookup key
            into ``state.open_questions``.
        drop: When ``True`` mark the question ``DROPPED``; when ``False``
            (default) mark it ``ANSWERED``. Either terminal status clears the
            ``blocking`` bit so a campaign halted on the question resumes.
        drop_reason: The disposition a drop records; ``None`` reads as
            ``SUPERSEDED`` when *superseded_by_question_ref* names a successor
            and ``OUT_OF_SCOPE`` otherwise. Only valid with *drop*.
        superseded_by_question_ref: The successor question that replaces this
            one; only valid with *drop*, and it must name another question in
            the ledger.
        scope_id: Explicit scope threaded to the canonical writer (mirrors
            :class:`AddQuestionParams`); the resolve itself keys off
            *question_id*, so this only anchors the write's scope tag.
        repo_root: Optional per-request repo anchor for the worktree-aware state
            writer; ``None`` uses the daemon's boot-time state path.
    """

    model_config = ConfigDict(extra="forbid")
    question_id: str = Field(min_length=1)
    drop: bool = False
    drop_reason: OpenQuestionDropReason | None = None
    superseded_by_question_ref: str | None = None
    scope_id: str | None = None
    repo_root: str | None = None

    @model_validator(mode="after")
    def _refuse_drop_fields_on_an_answer(self) -> ResolveQuestionParams:
        """Refuse a drop disposition on a resolve that does not drop."""
        if not self.drop and (
            self.drop_reason is not None or self.superseded_by_question_ref is not None
        ):
            raise ValueError("drop_reason and superseded_by_question_ref require drop")
        return self


class ResolveQuestionResult(BaseModel):
    """Result of :func:`resolve_question`.

    Attributes:
        question_id: The id of the question just resolved.
        status: The question's new terminal status (``"answered"`` /
            ``"dropped"``).
        scope_id: The scope the resolved question was bound to.
    """

    model_config = ConfigDict(extra="forbid")
    question_id: str
    status: str
    scope_id: str


def _apply_resolve_question(state: State, args: ResolveQuestionParams) -> dict[str, Any]:
    """Flip one :class:`OpenQuestion` to a terminal status and clear its blocking bit.

    An operator resolve: a ``BLOCKED`` (or still-``OPEN``) question moves to
    ``ANSWERED`` -- or ``DROPPED`` when *args.drop* -- and its ``blocking`` bit
    is cleared. Clearing ``blocking`` is load-bearing: the balanced-autonomy
    interrupt (the RISKS band + the ``BLOCKED_AWAIT_USER`` run phase) counts the
    ``blocking`` bool, not the status, so a resolve that left the bit set would
    never drop the count and the run would stay halted. ``answered_by_claim_id``
    stays ``None`` (an operator resolve has no answering claim, unlike the
    round-reconcile path). A ``DROPPED`` row always carries a reason, so an
    unreasoned drop is never left silent: the caller's choice, else
    ``SUPERSEDED`` when a successor is named, else ``OUT_OF_SCOPE``. The row
    is re-validated so a reason and successor that disagree are refused.

    Args:
        state: Loaded :class:`State`. ``state.open_questions`` is mutated in
            place (copy-then-assign, mirroring :func:`_apply_add_question`).
        args: Validated :class:`ResolveQuestionParams`.

    Returns:
        Result dict matching :class:`ResolveQuestionResult`.

    Raises:
        ValueError: When *args.question_id* names no row in
            ``state.open_questions``, the successor names no other row, or the
            reason and successor disagree. Mapped to ``-32002
            validation_failed`` by the canonical writer.
    """
    from eawf.kernel.state.models import OpenQuestion

    questions = dict(state.open_questions or {})
    question = questions.get(args.question_id)
    if question is None:
        raise ValueError(f"unknown question: {args.question_id!r}")
    successor = args.superseded_by_question_ref
    if successor is not None and (successor == args.question_id or successor not in questions):
        raise ValueError(f"unknown successor question: {successor!r}")
    status = OpenQuestionStatus.DROPPED if args.drop else OpenQuestionStatus.ANSWERED
    drop_reason = None
    if args.drop:
        drop_reason = args.drop_reason or (
            OpenQuestionDropReason.SUPERSEDED
            if successor is not None
            else OpenQuestionDropReason.OUT_OF_SCOPE
        )
    questions[args.question_id] = OpenQuestion.model_validate(
        {
            **question.model_dump(),
            "status": status,
            "blocking": False,
            "drop_reason": drop_reason,
            "superseded_by_question_ref": successor,
            "resolved_at": datetime.now(UTC),
        }
    )
    state.open_questions = questions
    logger.info(
        f"_apply_resolve_question question={args.question_id!r} "
        f"status={status.value} drop={args.drop}"
    )
    return ResolveQuestionResult(
        question_id=args.question_id,
        status=status.value,
        scope_id=question.scope_id,
    ).model_dump(mode="json")


@register("research.resolve_question")
async def resolve_question(ctx: MethodContext, params: dict[str, Any]) -> dict[str, Any]:
    """Resolve an :class:`OpenQuestion` through the canonical state writer.

    The daemon-canonical mutator that unblocks a campaign halted on a blocking
    question (AGENTS rule 4); the TUI ``a`` approve key on a surfaced blocking
    question + the retired headless ``question resolve`` verb proxy here.
    The row moves to a terminal status with its ``blocking`` bit cleared through
    the same per-file portalock + WAL + event-append path every state mutator
    uses (:func:`commit_worktree_state`), so the single-writer invariant holds
    and the board re-renders the run as resumed on its next refresh.

    Args:
        ctx: Server context -- must carry ``state_path`` (+ ``event_path`` /
            ``wal_dir`` for the canonical write).
        params: JSON-RPC params per :class:`ResolveQuestionParams`.

    Returns:
        Dict matching :class:`ResolveQuestionResult`.

    Raises:
        ValueError: When *params* does not validate against
            :class:`ResolveQuestionParams` (an unknown key), or when the id
            names no open-question row. Mapped to ``-32602 invalid params`` /
            ``-32002 validation_failed``.
    """
    from eawf.runtime.daemon.methods.state_worktree import (
        commit_worktree_state as _commit_worktree_state,
    )

    args = ResolveQuestionParams.model_validate(params)
    repo_root = Path(args.repo_root) if args.repo_root else None
    return _commit_worktree_state(
        ctx=ctx,
        repo_root=repo_root,
        params=params,
        command="research.resolve_question",
        scope_id=args.scope_id,
        apply_func=lambda state: _apply_resolve_question(state, args),
    )


def _normalize_claim_text(text: str) -> str:
    """Return a whitespace / case-normalized key for near-duplicate claim dedup.

    Casefolds + collapses runs of whitespace so trivially-different phrasings of
    the same finding (extra spaces, capitalization) compact to one claim.

    Args:
        text: The finding line / claim text to normalize.

    Returns:
        The normalized dedup key.
    """
    return " ".join(text.split()).casefold()


def _refute_named_claims(
    claims: dict[str, Claim], bodies: tuple[ResearcherReportBody, ...], resolved_scope: str
) -> list[str]:
    """Flip every LIVE claim a body names in ``refuted_claim_ids`` to REFUTED.

    A researcher names the prior claim its survey contradicts explicitly
    (never inferred from finding text) -- a stale or foreign-scope id, or one
    already REFUTED / SUPERSEDED, is silently skipped rather than raised: a
    round's contradiction signal must not abort the whole reconcile over one
    bad reference.

    Args:
        claims: The round's working claim dict (id -> row), mutated in place.
        bodies: The round's parsed researcher bodies.
        resolved_scope: The campaign's resolved scope id.

    Returns:
        The ids flipped to REFUTED this call, in body order.
    """
    refuted: list[str] = []
    for body in bodies:
        for target_id in body.refuted_claim_ids:
            target = claims.get(target_id)
            if (
                target is not None
                and target.scope_id == resolved_scope
                and target.status in (ClaimStatus.OPEN, ClaimStatus.SUPPORTED)
            ):
                claims[target_id] = target.model_copy(update={"status": ClaimStatus.REFUTED})
                refuted.append(target_id)
    return refuted


def _seal_stale_auto_resolutions(questions: dict[str, OpenQuestion], resolved_scope: str) -> int:
    """Seal every AUTO_RESOLVED question the scope carries from an earlier round.

    The override window is exactly one round wide: a policy pairing
    that survived to the NEXT round's reconcile has stood unchallenged and
    locks in as SEALED. A pairing THIS round's own elimination step makes is
    never passed here -- the caller runs this seal BEFORE that step, so it has
    not yet crossed a round boundary.

    Args:
        questions: The round's working question dict (id -> row), mutated in
            place.
        resolved_scope: The campaign's resolved scope id.

    Returns:
        The count of questions sealed this call.
    """
    sealed = 0
    for question_id, question in questions.items():
        is_carried_auto_resolve = (
            question.scope_id == resolved_scope
            and question.status is OpenQuestionStatus.AUTO_RESOLVED
        )
        if is_carried_auto_resolve:
            questions[question_id] = question.model_copy(
                update={"status": OpenQuestionStatus.SEALED}
            )
            sealed += 1
    return sealed


def reconcile_round_claims(
    state: State,
    findings: RoundFindings,
    *,
    scope_id: str | None,
    now: datetime,
) -> list[str]:
    """Fold one round's parsed findings into ``state.open`` Claim rows.

    The round-end reconcile: every finding line a round's spawned researchers
    returned (:attr:`~eawf.kernel.spec.campaign_driver.RoundFindings.finding_lines`)
    becomes one ``OPEN`` :class:`~eawf.kernel.state.models.Claim` row carrying
    the body's ``evidence_refs`` as the claim's evidence (so the EviBound
    resolver + the saturation reducer score real rows). The claim id is
    ``CLM-r<round>-<domain>-<n>`` so a re-run round does not collide. A finding
    line over the title bound is truncated to the 72-char title cap with its
    full text preserved in the description. A body naming a claim id in
    :attr:`~eawf.kernel.store.kinds.agent_report.ResearcherReportBody.refuted_claim_ids`
    flips that LIVE claim (scope-matched, ``OPEN``/``SUPPORTED``) to
    :attr:`~eawf.kernel.state.enums.ClaimStatus.REFUTED` -- the only place a
    claim ever reaches that status, feeding the campaign's contradiction stop
    rule. Also seals every ``AUTO_RESOLVED`` question the scope carries from a
    STRICTLY earlier round: the override window is exactly one round
    wide, so a policy pairing that survived to the next round's reconcile has
    stood unchallenged and locks in as ``SEALED``. A pairing this same round's
    elimination step makes (below) is untouched here since it has not yet
    crossed a round boundary.

    Pure with respect to its inputs aside from mutating *state* in place (the
    caller owns the canonical persist). Returns the ids of every Claim row this
    round touched -- newly written, then newly REFUTED -- so the caller carries
    a fresh copy of a flipped claim forward into its own ledger (never just the
    per-round claim count: a REFUTED status change is invisible to a
    round-to-round contradiction check unless the fresh row rides this return).

    Args:
        state: Loaded :class:`State`. ``state.claims`` / ``state.open_questions``
            are mutated in place.
        findings: The round's parsed findings.
        scope_id: Explicit scope for the claims; ``None`` resolves to the
            project code.
        now: The instant the claims are logged at.

    Returns:
        The ids of the Claim rows this round wrote (new rows, finding order)
        followed by the ids it flipped to REFUTED (pre-existing rows).
    """
    from eawf.kernel.state.models import Claim

    resolved_scope = _resolve_research_scope(state, scope_id)
    claims = dict(state.claims or {})
    written: list[str] = []
    # Compaction (dedup-only policy): a finding whose normalized full text
    # matches a LIVE claim already on the scope's ledger -- or one written
    # earlier this round -- is collapsed (not re-added), so a trivial question
    # that keeps re-surfacing the same finding across rounds does not grow the
    # ledger unboundedly. The dedup key is the FULL finding line (truncation is
    # display-only), so it matches a live claim's description when the row's
    # title was truncated. Synthesising per-domain clusters is a follow-up.
    seen_texts = {
        _normalize_claim_text(c.description if c.description is not None else c.title)
        for c in claims.values()
        if c.scope_id == resolved_scope and c.status in (ClaimStatus.OPEN, ClaimStatus.SUPPORTED)
    }
    compacted = 0
    for domain, body in zip(findings.domains, findings.bodies, strict=True):
        evidence = [ref.ref for ref in body.evidence_refs]
        for index, line in enumerate(body.findings):
            key = _normalize_claim_text(line)
            if key in seen_texts:
                compacted += 1
                continue
            seen_texts.add(key)
            claim_id = f"CLM-r{findings.round_number}-{domain}-{index}"
            title = line if len(line) <= 72 else f"{line[:69]}..."
            description = None if len(line) <= 72 else line[:500]
            claims[claim_id] = Claim(
                id=claim_id,
                scope_id=resolved_scope,
                title=title,
                description=description,
                status=ClaimStatus.OPEN,
                evidence_refs=evidence,
                created_at=now,
            )
            written.append(claim_id)
    refuted = _refute_named_claims(claims, findings.bodies, resolved_scope)
    # A finding carries no link to the question it addresses, so a claim can
    # only be paired by elimination: when the scope has exactly one OPEN,
    # non-blocking question, that question is every claim's single candidate
    # and the round's first claim pairs with it. With two or more candidates
    # the pairing would be a guess, so every question stays OPEN for the
    # operator. Blocking questions are operator checkpoints and never
    # auto-resolve. The pairing is a policy inference, not an operator
    # answering the question, so it lands AUTO_RESOLVED: folding
    # it into ANSWERED would attribute a machine decision to a person and
    # hide that no human looked at the question.
    questions = dict(state.open_questions or {})
    sealed = _seal_stale_auto_resolutions(questions, resolved_scope)
    auto_resolved = 0
    candidates = [
        q
        for q in questions.values()
        if q.scope_id == resolved_scope and q.status is OpenQuestionStatus.OPEN and not q.blocking
    ]
    if written and len(candidates) == 1:
        claim_id, question = written[0], candidates[0]
        claims[claim_id] = claims[claim_id].model_copy(update={"answers_question_id": question.id})
        questions[question.id] = question.model_copy(
            update={
                "status": OpenQuestionStatus.AUTO_RESOLVED,
                "answered_by_claim_id": claim_id,
                "resolved_at": now,
            }
        )
        auto_resolved = 1
    state.claims = claims
    state.open_questions = questions
    # A researcher that returns verdict=blocked with a clarification
    # question raises a BLOCKING OpenQuestion -- an operator checkpoint gating
    # its round -- through the shared add-question writer, so the operator
    # answers it via the existing approve/steer channel (the block
    # pauses only its round, not the whole campaign). Runs after the resolution
    # commit so _apply_add_question reads the updated open_questions.
    raised = 0
    for domain, body in zip(findings.domains, findings.bodies, strict=True):
        if body.verdict is AgentReportVerdict.BLOCKED and body.question:
            clarify_id = f"OQ-clarify-r{findings.round_number}-{domain}-{uuid.uuid4().hex[:8]}"
            _apply_add_question(
                state,
                AddQuestionParams(
                    title=body.question if len(body.question) <= 72 else f"{body.question[:69]}...",
                    description=body.question if len(body.question) > 72 else None,
                    blocking=True,
                    urgency=Urgency.HIGH,
                    scope_id=resolved_scope,
                    question_id=clarify_id,
                ),
                question_id=clarify_id,
            )
            raised += 1
    logger.info(
        f"reconcile_round_claims round={findings.round_number} scope_id={resolved_scope!r} "
        f"claims={len(written)} compacted={compacted} refuted={len(refuted)} sealed={sealed} "
        f"auto_resolved_questions={auto_resolved} raised_clarifications={raised}"
    )
    return written + refuted


def persist_round(state_path: Path, payload: ResearchRoundPayload) -> str:
    """Append one :class:`ResearchRoundPayload` to the ``research_round`` store.

    Wraps the payload in an :class:`Envelope` with
    ``kind=StoreKind.RESEARCH_ROUND`` and appends it via
    :func:`~eawf.kernel.store.append.append_envelope` (per-file portalock +
    fsync). The round store is append-only and non-state, the same as the
    campaign store; the board re-reads it to render the RUN / ROUND bands.

    Args:
        state_path: Path to the scope's ``state.json``; the round store
            resolves under its sibling ``store/`` directory.
        payload: The validated round payload to persist.

    Returns:
        The appended envelope id (``<campaign_id>-r<round_number>``).
    """
    envelope_id = f"{payload.campaign_id}-r{payload.round_number}"
    envelope = Envelope(
        id=envelope_id,
        kind=StoreKind.RESEARCH_ROUND,
        scope_id=None,
        created_at=payload.recorded_at,
        summary=f"round {payload.round_number} of {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, StoreKind.RESEARCH_ROUND), envelope)
    return envelope_id


def read_campaign_rounds(state_path: Path, campaign_id: str) -> list[ResearchRoundPayload]:
    """Return every persisted round for *campaign_id*, in round order.

    Walks the append-only ``research_round`` store under *state_path*,
    validating each envelope + payload, and returns the rows matching
    *campaign_id* sorted by round number. Empty when the store is absent or
    carries no row for the id (the common pre-run path).

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The campaign whose rounds to read.

    Returns:
        The campaign's round payloads in ascending round order.
    """
    path = store_path(state_path, StoreKind.RESEARCH_ROUND)
    if not path.exists():
        return []
    rounds: list[ResearchRoundPayload] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        payload = ResearchRoundPayload.model_validate(envelope.payload)
        if payload.campaign_id == campaign_id:
            rounds.append(payload)
    rounds.sort(key=lambda r: r.round_number)
    return rounds


def read_campaign_costs(state_path: Path, campaign_ids: Collection[str]) -> dict[str, Decimal]:
    """Return the researcher spend booked against each of *campaign_ids*.

    Sums the ``cost_usd`` of every ``dispatch_cost`` event scoped to a campaign
    -- the campaign's own cost centre, separate from any execution wave's
    counters. The event store is walked ONCE for the whole set, so totalling N
    campaigns costs one pass rather than N.

    Every id in *campaign_ids* appears in the result; one with no cost row maps
    to ``Decimal("0")``, so a caller never has to distinguish "absent" from
    "nothing spent".

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_ids: The campaigns whose researcher spend to total.

    Returns:
        A mapping of campaign id to its summed researcher cost in USD.
    """
    totals: dict[str, Decimal] = {campaign_id: Decimal("0") for campaign_id in campaign_ids}
    if not totals:
        return totals
    path = store_path(state_path, StoreKind.EVENT)
    if not path.exists():
        return totals
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        envelope = Envelope.model_validate_json(raw_line)
        if envelope.scope_id not in totals:
            continue
        payload = envelope.payload
        if isinstance(payload, dict) and payload.get("event_type") == "dispatch_cost":
            totals[envelope.scope_id] += Decimal(str(payload.get("cost_usd", "0")))
    return totals


def read_campaign_cost(state_path: Path, campaign_id: str) -> Decimal:
    """Return the total researcher spend booked against *campaign_id*.

    Args:
        state_path: Path to the scope's ``state.json``.
        campaign_id: The campaign whose researcher spend to total.

    Returns:
        The summed campaign researcher cost in USD, or ``Decimal("0")`` when
        the store is absent or carries no cost row for the campaign.
    """
    return read_campaign_costs(state_path, [campaign_id])[campaign_id]


__all__ = [
    "AddQuestionParams",
    "AddQuestionResult",
    "ResearchRoundPayload",
    "ResolveQuestionParams",
    "ResolveQuestionResult",
    "add_question",
    "persist_campaign",
    "persist_round",
    "read_campaign_cost",
    "read_campaign_costs",
    "read_campaign_rounds",
    "read_latest_campaign",
    "reconcile_round_claims",
    "resolve_question",
]
