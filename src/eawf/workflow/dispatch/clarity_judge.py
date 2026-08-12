"""Layer-3 LLM clarity-judge contract — spawn-free.

The doc-clarity enforcement stack
(``.ea/local/research/2026-05-29-doc-clarity.md``) layers four checks of
rising cost. Layers 0-2 are deterministic — a rendered standard, the
EAWF016 title lint, the description floor, and the Vale prose pass. They
catch *form*: a commit-type prefix on a title, an empty description, inline
path/link soup. They cannot judge whether a newcomer can actually follow
the prose — audience-fit and genuine motivation are semantic, not regex.
**Layer 3 is the LLM-as-judge clarity gate**: a model scores an artifact
against a fixed six-dimension rubric and the panel's votes reduce to a
single pass/fail.

This module ships the Layer-3 *contract* spawn-free. The live multi-judge
rung — actually convening model jurors — is deferred behind the agent-spawn
floor (the same dependency the cross-vendor jury,
:mod:`eawf.observability.eval.cross_vendor_jury`, carries). Shipping the
contract first means the criterion-set, the judge prompt, the rollup, and
the calibration anchors all exist and are tested *before* a single token is
spent, and the gate sits behind the W02 deterministic floor so the judge
never runs on an empty description.

Zero new wire types — the reused machinery
-------------------------------------------
The judge IS the auditor role; the panel IS the minority-veto jury; the
aggregate IS an evidence row. Nothing here defines a new Pydantic model or
touches ``state.json``:

* **criterion set** — the six
  :data:`~eawf.platform.profiles.clarity.NEWCOMER_TEST_DIMENSIONS`
  (audience-fit, jargon-defined, why-present, scannable, reference-hygiene,
  not-a-title-duplicate), shared verbatim with the deterministic lints so a
  dimension means the same thing whether a regex or the judge scores it.
* **per-juror ballot** — one
  :class:`~eawf.kernel.store.kinds.agent_report.AuditorReportBody`, with one
  :class:`~eawf.kernel.store.kinds.agent_report.CriterionVerdict` per
  dimension (pointwise scoring: each dimension is judged on its own, never
  ranked pairwise against another artifact, which removes position bias).
  :func:`parse_clarity_judge_body` is the forced-schema validator the
  bounded re-ask loop hands a live juror.
* **multi-judge reduction** — the existing minority-veto reducer
  :func:`eawf.observability.eval.jury.aggregate_jury` over one
  :class:`~eawf.observability.eval.jury.JurorBallot` per juror. The panel is
  three cheap jurors because an LLM judge misses problems more than it
  false-alarms, so a single dissent must veto.
* **aggregate** — one
  :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`
  (``evidence_kind="jury"``, ``produced_by="agent"``), exactly as the
  EviBound rungs (:mod:`eawf.workflow.evidence.rung2`) land theirs.

The blocking-dimension rule
---------------------------
``why_present`` and ``not_a_title_duplicate`` are blocking *for the
entity-description surface* (the
:attr:`~eawf.platform.profiles.clarity.ClarityDimension.blocking_for_description`
flag). A juror's per-dimension verdicts reduce to that juror's overall
ballot through :func:`juror_verdict_from_criteria`: a failed blocking
dimension sinks the juror to ``fail`` regardless of the other five; any
other failed dimension is a ``pass-with-followups`` (a tracked nit, not a
block); all-pass is a clean ``pass``. This is the worked judgment the brief
specifies — a ``description == title`` artifact scores ``why = 0`` and
``not_a_title_duplicate = 0`` and so each juror votes ``fail``, and the
panel reduces to ``fail``.

Spawn-free
----------
Every function here is pure or injected: the prompt is a string, the rollup
is a reducer over already-collected ballots, and the live-juror seam is a
:data:`ClarityBallotFn` callback (mirroring
:data:`eawf.workflow.evidence.rung3.BallotFn`) the deferred live rung will
bind to a real spawn. Nothing in this module spawns a subprocess.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from pydantic import TypeAdapter

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.store.kinds.agent_report import AuditorReportBody, CriterionVerdict
from eawf.kernel.store.kinds.evidence import EvidenceRecord, EvidenceStatus, mint_evidence_id
from eawf.observability.eval.jury import (
    JurorBallot,
    JuryAggregate,
    JuryAggregateOutcome,
    aggregate_jury,
)
from eawf.platform.lint.clarity_anchors import (
    ANCHOR_SCORE_MAX,
    CALIBRATION_ANCHORS,
    ClarityAnchor,
)
from eawf.platform.profiles.clarity import (
    NEWCOMER_TEST,
    NEWCOMER_TEST_DIMENSIONS,
    ClarityDimension,
)

logger = logging.getLogger(__name__)

#: The surface whose blocking dimensions bite. The
#: :attr:`~eawf.platform.profiles.clarity.ClarityDimension.blocking_for_description`
#: flag is scoped to the entity-description surface (the worst class per the
#: doc-clarity findings); the judge takes this as a parameter so the same
#: contract can score a PR body or a promoted brief with the blocking rule
#: relaxed.
CLARITY_DESCRIPTION_SURFACE: str = "entity_description"

#: Default panel size for the clarity jury. Three cheap jurors with
#: minority-veto: an LLM judge misses problems more than it false-alarms, so
#: a single dissent vetoes. Mirrors
#: :data:`eawf.workflow.evidence.rung3.DEFAULT_JUROR_COUNT`.
DEFAULT_CLARITY_JUROR_COUNT: int = 3

#: A dimension scored at or above this on the anchored ``0..ANCHOR_SCORE_MAX``
#: scale passes that dimension; a score below it (i.e. ``0``) fails it. The
#: floor is ``1`` so a "partially clear" dimension (score ``1``) is a pass
#: while only an outright-absent dimension (score ``0``) fails — matching the
#: worked anchors, where the blocking dimensions fail precisely at ``0``.
PASS_DIMENSION_SCORE: int = 1

#: Map the reduced jury outcome onto the evidence-row status. A ``PASS``
#: aggregate certifies the artifact is newcomer-clear; a ``FAIL`` aggregate
#: (a veto) records a clarity failure; a ``NEEDS_USER`` aggregate (a split
#: with no veto) is ``blocked`` — the panel could not resolve, so the
#: operator adjudicates rather than the row silently passing.
_OUTCOME_STATUS: dict[JuryAggregateOutcome, EvidenceStatus] = {
    JuryAggregateOutcome.PASS: "pass",
    JuryAggregateOutcome.FAIL: "fail",
    JuryAggregateOutcome.NEEDS_USER: "blocked",
}

#: Forced-schema adapter for one juror's clarity ballot. Validating a
#: spawn's JSON-decoded output through this narrows it to an
#: :class:`AuditorReportBody`: a non-auditor body (a wrong-role shape) fails
#: the ``role: Literal["auditor"]`` discriminator and raises
#: :class:`pydantic.ValidationError`, which the bounded re-ask loop
#: classifies as a schema mismatch and re-asks rather than letting escape.
_JUDGE_BODY_ADAPTER: TypeAdapter[AuditorReportBody] = TypeAdapter(AuditorReportBody)

#: A callable that convenes one independent clarity juror and returns its
#: scored :class:`AuditorReportBody`. Injected (never imported) so this
#: module spawns nothing: the deferred live rung binds a spawn-then-validate
#: adapter (drive the resolved runtime's ``spawn_session`` through the
#: bounded re-ask loop with :func:`parse_clarity_judge_body` as the
#: forced-schema validator); a test binds a recording stub returning a
#: canned body. The single ``str`` argument is the per-juror prompt from
#: :func:`build_clarity_judge_prompt`.
type ClarityBallotFn = Callable[[str], Awaitable[AuditorReportBody]]


def clarity_criteria() -> tuple[str, ...]:
    """Return the six clarity criterion labels, in rendered order.

    The criterion set IS the shared
    :data:`~eawf.platform.profiles.clarity.NEWCOMER_TEST_DIMENSIONS`; this
    helper surfaces each dimension's human ``label`` as the criterion-name
    slot a juror stamps on its per-dimension
    :class:`~eawf.kernel.store.kinds.agent_report.CriterionVerdict`.

    Returns:
        One label per dimension, in the canonical order.
    """
    return tuple(dim.label for dim in NEWCOMER_TEST_DIMENSIONS)


def _blocking_dimension_keys(surface: str) -> frozenset[str]:
    """Return the dimension keys that are blocking for *surface*.

    Only the entity-description surface has blocking dimensions today
    (``why_present`` / ``not_a_title_duplicate``); any other surface has
    none, so a failed dimension there is a tracked nit rather than a block.
    """
    if surface != CLARITY_DESCRIPTION_SURFACE:
        return frozenset()
    return frozenset(dim.key for dim in NEWCOMER_TEST_DIMENSIONS if dim.blocking_for_description)


def _criterion_label_to_key() -> dict[str, str]:
    """Return a label -> dimension-key map for the six clarity dimensions."""
    return {dim.label: dim.key for dim in NEWCOMER_TEST_DIMENSIONS}


def _dimension_line(idx: int, dim: ClarityDimension, *, surface: str) -> str:
    """Render one dimension as a numbered prompt line, marking it when blocking.

    A dimension that is blocking for the description surface and is being
    scored on that surface gets a ``BLOCKING`` marker so the juror knows a
    zero there sinks the whole vote.
    """
    suffix = ""
    if dim.blocking_for_description and surface == CLARITY_DESCRIPTION_SURFACE:
        suffix = " — BLOCKING for this surface"
    return f"{idx}. {dim.label} (key `{dim.key}`){suffix}"


def _anchor_block(anchor: ClarityAnchor) -> str:
    """Render one calibration anchor as a prompt block.

    Shows the anchor's surface, polarity, verbatim sample, the expected
    per-dimension scores, and the one-line rationale so a juror anchors its
    own scoring on the worked example.
    """
    score_pairs = ", ".join(f"{key}={anchor.scores[key]}" for key in sorted(anchor.scores))
    return (
        f"### {anchor.anchor_id} ({anchor.polarity}, surface={anchor.surface})\n"
        "\n"
        f"> {anchor.sample}\n"
        "\n"
        f"Expected scores: {score_pairs}\n"
        f"Why: {anchor.rationale}\n"
    )


def build_clarity_judge_prompt(
    artifact_text: str,
    *,
    surface: str,
    anchors: tuple[ClarityAnchor, ...] = CALIBRATION_ANCHORS,
) -> str:
    """Return the pointwise clarity-judge prompt for one artifact.

    The prompt is **pointwise**: the juror scores *this* artifact against
    each dimension on its own, never ranked pairwise against another
    artifact, which eliminates the position bias a pairwise comparison
    introduces. It embeds the calibration *anchors* (the worked positive +
    negative examples) so every juror anchors its ``0..ANCHOR_SCORE_MAX``
    scale on the same cases, and it asks for the verdict as an auditor
    ``agent_end`` body with one criterion per dimension — the same shape the
    fresh-context wave auditor emits, so the rollup reuses the auditor body
    unchanged.

    Args:
        artifact_text: The verbatim prose under judgment (a docstring, a PR
            bullet, an entity description).
        surface: Which prose surface the artifact is from — one of the
            anchor surfaces. ``CLARITY_DESCRIPTION_SURFACE`` activates the
            blocking-dimension rule in the rollup; the prompt names which
            dimensions block so the juror knows the stakes.
        anchors: The calibration anchors to embed. Defaults to the full
            :data:`~eawf.platform.lint.clarity_anchors.CALIBRATION_ANCHORS`
            set.

    Returns:
        The rendered Markdown clarity-judge prompt.

    Raises:
        ValueError: When *artifact_text* is empty / whitespace-only — there
            is nothing to judge.
    """
    if not artifact_text.strip():
        raise ValueError("artifact_text must be non-empty")

    dimension_lines = "\n".join(
        _dimension_line(idx, dim, surface=surface)
        for idx, dim in enumerate(NEWCOMER_TEST_DIMENSIONS, start=1)
    )
    anchor_blocks = "\n".join(_anchor_block(anchor) for anchor in anchors)
    return (
        "# Clarity judge\n"
        "\n"
        "You are an independent clarity juror. You judge ONE artifact in\n"
        "isolation against the newcomer test, and you have not seen any other\n"
        "juror's vote. The single gate is:\n"
        "\n"
        f"> {NEWCOMER_TEST}\n"
        "\n"
        "## Dimensions\n"
        "\n"
        f"Score each dimension on a 0..{ANCHOR_SCORE_MAX} scale (0 = absent, "
        f"{ANCHOR_SCORE_MAX} = fully satisfied). Score each dimension on its own "
        "(pointwise); do not compare this artifact against another.\n"
        "\n"
        f"{dimension_lines}\n"
        "\n"
        "## Calibration anchors\n"
        "\n"
        "These worked examples fix the scale. A real artifact should score like\n"
        "the anchor it most resembles.\n"
        "\n"
        f"{anchor_blocks}\n"
        "## Artifact under judgment\n"
        "\n"
        f"surface: {surface}\n"
        "\n"
        f"{artifact_text}\n"
        "\n"
        "## Output contract\n"
        "\n"
        "Respond with ONLY a single JSON object that validates against the\n"
        "auditor `agent_end` report body (`role` = `auditor`). It MUST carry a\n"
        "`verdict` (pass / pass-with-followups / fail / blocked), a `confidence`,\n"
        "a `summary`, a `target_id` for the artifact, and exactly one `criteria`\n"
        "entry per dimension above. Each criteria entry is a CriterionVerdict\n"
        "whose `criterion` is the dimension label and whose `passed` is true when\n"
        f"you scored that dimension at or above {PASS_DIMENSION_SCORE}, false when "
        "you scored it 0. No prose, no code fences."
    )


def parse_clarity_judge_body(raw: object) -> AuditorReportBody:
    """Validate *raw* as a clarity-judge :class:`AuditorReportBody`.

    The forced-schema validator a live convener hands the bounded re-ask
    loop (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`). It
    validates *raw* through :data:`_JUDGE_BODY_ADAPTER`, so a spawn that
    returns a non-auditor body fails the ``role: Literal["auditor"]``
    discriminator and raises :class:`pydantic.ValidationError`. Raising a
    ``ValidationError`` (not a plain ``ValueError``) is load-bearing: the
    loop catches only ``json.JSONDecodeError`` + ``ValidationError``, so a
    plain ``ValueError`` would escape the bounded retry uncaught.

    Args:
        raw: The JSON-decoded spawn output.

    Returns:
        The validated :class:`AuditorReportBody`.

    Raises:
        pydantic.ValidationError: When *raw* is not a valid auditor body.
    """
    return _JUDGE_BODY_ADAPTER.validate_python(raw)


def juror_verdict_from_criteria(
    criteria: tuple[CriterionVerdict, ...] | list[CriterionVerdict],
    *,
    surface: str,
) -> AgentReportVerdict:
    """Reduce one juror's per-dimension verdicts to its overall ballot.

    The per-juror reduction the brief specifies:

    - a failed **blocking** dimension (``why_present`` /
      ``not_a_title_duplicate`` on the entity-description surface) sinks the
      juror to :attr:`~eawf.kernel.state.enums.AgentReportVerdict.FAIL`
      regardless of the other dimensions — the motivation or the
      not-a-duplicate signal is the load-bearing one for that surface;
    - any other failed dimension is a
      :attr:`~eawf.kernel.state.enums.AgentReportVerdict.PASS_WITH_FOLLOWUPS`
      (a tracked nit the operator sees but does not block on);
    - all dimensions passing is a clean
      :attr:`~eawf.kernel.state.enums.AgentReportVerdict.PASS`.

    Args:
        criteria: The juror's per-dimension
            :class:`~eawf.kernel.store.kinds.agent_report.CriterionVerdict`
            rows. The ``criterion`` slot is matched back to a dimension key
            so the blocking rule can be applied; an unrecognized criterion
            label is treated as non-blocking.
        surface: The artifact's prose surface — selects which dimensions are
            blocking (only ``CLARITY_DESCRIPTION_SURFACE`` has any).

    Returns:
        The juror's overall :class:`~eawf.kernel.state.enums.AgentReportVerdict`.

    Raises:
        ValueError: When *criteria* is empty — a juror must score at least
            one dimension to cast a ballot.
    """
    if not criteria:
        raise ValueError("clarity juror must score at least one dimension")

    blocking = _blocking_dimension_keys(surface)
    label_to_key = _criterion_label_to_key()
    blocking_failed = False
    any_failed = False
    for verdict in criteria:
        if verdict.passed:
            continue
        any_failed = True
        key = label_to_key.get(verdict.criterion)
        if key is not None and key in blocking:
            blocking_failed = True

    if blocking_failed:
        return AgentReportVerdict.FAIL
    if any_failed:
        return AgentReportVerdict.PASS_WITH_FOLLOWUPS
    return AgentReportVerdict.PASS


def _juror_ballot(body: AuditorReportBody, *, juror_id: str, surface: str) -> JurorBallot:
    """Build a binary :class:`JurorBallot` from one judge body.

    The juror's six per-dimension verdicts reduce to a single binary verdict
    (:func:`juror_verdict_from_criteria`) that becomes the ballot the
    minority-veto reducer consumes.
    """
    verdict = juror_verdict_from_criteria(tuple(body.criteria), surface=surface)
    return JurorBallot(juror_id=juror_id, acceptance_style="binary", verdict=verdict)


@dataclass(frozen=True)
class ClarityJudgeResult:
    """Typed outcome of a reduced clarity-judge panel.

    A frozen dataclass — local plumbing, not a wire type. It pairs the
    minority-veto aggregate with the evidence row the aggregate lands as, so
    a caller gets both the reduction detail (per-signal reasons, veto count)
    and the durable :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`
    in one return.

    Attributes:
        scope_id: URN of the scope (wave / iter / phase / decision /
            artifact) the judged artifact belongs to.
        surface: The prose surface judged.
        outcome: The reduced
            :class:`~eawf.observability.eval.jury.JuryAggregateOutcome`.
        aggregate: The :class:`~eawf.observability.eval.jury.JuryAggregate`
            the minority-veto reducer produced over the juror ballots.
        evidence: The :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`
            the aggregate lands as (``evidence_kind="jury"``,
            ``produced_by="agent"``), ready to append to the evidence store.
        juror_verdicts: One overall verdict per juror, in convene order — the
            ballots the aggregate reduced.
    """

    scope_id: str
    surface: str
    outcome: JuryAggregateOutcome
    aggregate: JuryAggregate
    evidence: EvidenceRecord
    juror_verdicts: tuple[AgentReportVerdict, ...] = field(default_factory=tuple)

    @property
    def needs_user(self) -> bool:
        """Return whether the panel routed to the operator (a split, no veto)."""
        return self.outcome is JuryAggregateOutcome.NEEDS_USER


def _aggregate_to_evidence(
    aggregate: JuryAggregate,
    *,
    scope_id: str,
    surface: str,
    refs: tuple[str, ...],
    now: datetime | None,
) -> EvidenceRecord:
    """Mint the :class:`EvidenceRecord` for a reduced clarity aggregate.

    The aggregate lands as a ``jury`` evidence row exactly as the EviBound
    rungs land theirs (:func:`eawf.workflow.evidence.rung2.run_rung2_gate`):
    ``produced_by="agent"`` (the panel is agent jurors), the status mapped
    from the outcome, and the veto / ballot counts carried as metrics so a
    downstream reader sees how the panel split without re-reading the
    ballots.
    """
    status = _OUTCOME_STATUS[aggregate.outcome]
    summary = (
        f"clarity judge ({surface}) outcome={aggregate.outcome.value} "
        f"over {aggregate.ballot_count} jurors (veto={aggregate.veto_count})"
    )
    return EvidenceRecord(
        id=mint_evidence_id(),
        scope_id=scope_id,
        produced_by="agent",
        evidence_kind="jury",
        status=status,
        summary=summary[:500],
        refs=list(refs),
        metrics={
            "juror_count": aggregate.ballot_count,
            "veto_count": aggregate.veto_count,
        },
        created_at=now if now is not None else datetime.now(UTC),
    )


def rollup_clarity_judges(
    bodies: tuple[AuditorReportBody, ...] | list[AuditorReportBody],
    *,
    scope_id: str,
    surface: str,
    refs: tuple[str, ...] = (),
    now: datetime | None = None,
) -> ClarityJudgeResult:
    """Reduce a panel of clarity-judge bodies into a typed result + evidence row.

    The rollup wiring — the spawn-free heart of the contract. Each juror
    body's six per-dimension
    :class:`~eawf.kernel.store.kinds.agent_report.CriterionVerdict` rows
    reduce to one binary ballot (:func:`juror_verdict_from_criteria`, which
    applies the blocking-dimension rule for the surface); the ballots reduce
    through the existing minority-veto reducer
    (:func:`eawf.observability.eval.jury.aggregate_jury`); and the aggregate
    lands as a ``jury`` :class:`~eawf.kernel.store.kinds.evidence.EvidenceRecord`.

    A single juror voting ``fail`` (because a blocking dimension scored 0)
    vetoes the panel to ``fail`` under minority-veto — the worked judgment
    the brief specifies for a ``description == title`` artifact. A split with
    no veto routes to ``NEEDS_USER`` (the evidence status is ``blocked``), so
    an unresolvable panel is an operator decision, never a silent pass.

    Args:
        bodies: One :class:`AuditorReportBody` per juror, each carrying one
            criterion per dimension. Must be non-empty.
        scope_id: URN of the scope the judged artifact belongs to, stamped
            on the evidence row.
        surface: The artifact's prose surface — selects the blocking
            dimensions in the per-juror reduction.
        refs: Optional typed references (decision / artifact / audit ids)
            the evidence row substantiates.
        now: Optional fixed UTC timestamp for the evidence row (tests pin
            it); defaults to ``datetime.now(UTC)``.

    Returns:
        A :class:`ClarityJudgeResult` carrying the outcome, the aggregate,
        the evidence row, and the per-juror verdicts.

    Raises:
        ValueError: When *bodies* is empty (no panel to reduce) or any
            juror body scored no dimensions (propagated from
            :func:`juror_verdict_from_criteria`).
    """
    if not bodies:
        raise ValueError("clarity rollup requires at least one juror body")

    juror_verdicts = tuple(
        juror_verdict_from_criteria(tuple(body.criteria), surface=surface) for body in bodies
    )
    ballots = tuple(
        _juror_ballot(body, juror_id=f"clarity-juror-{idx + 1}", surface=surface)
        for idx, body in enumerate(bodies)
    )
    aggregate = aggregate_jury(ballots)
    evidence = _aggregate_to_evidence(
        aggregate,
        scope_id=scope_id,
        surface=surface,
        refs=refs,
        now=now,
    )
    logger.info(
        f"rollup_clarity_judges scope={scope_id!r} surface={surface!r} "
        f"outcome={aggregate.outcome.value} jurors={aggregate.ballot_count} "
        f"veto={aggregate.veto_count} status={evidence.status}"
    )
    return ClarityJudgeResult(
        scope_id=scope_id,
        surface=surface,
        outcome=aggregate.outcome,
        aggregate=aggregate,
        evidence=evidence,
        juror_verdicts=juror_verdicts,
    )


__all__ = [
    "CLARITY_DESCRIPTION_SURFACE",
    "DEFAULT_CLARITY_JUROR_COUNT",
    "PASS_DIMENSION_SCORE",
    "ClarityBallotFn",
    "ClarityJudgeResult",
    "build_clarity_judge_prompt",
    "clarity_criteria",
    "juror_verdict_from_criteria",
    "parse_clarity_judge_body",
    "rollup_clarity_judges",
]
