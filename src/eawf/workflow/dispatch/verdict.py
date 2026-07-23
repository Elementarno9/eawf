"""Live per-wave fresh-context auditor verdict producer (P29-I04-W07).

Until this wave the self-eval surface
(:func:`eawf.observability.eval.self_eval.compute_self_eval`), the jury
reducer (:func:`eawf.observability.eval.jury.aggregate_jury`), and the
trust / cost-A-B surfaces that consume verdicts read **zero** verdict
rows -- the role-report stores were primed only by the interim / manual
seeder :func:`eawf.workflow.dispatch.seed.seed_interim_verdict`. This
module is the **live producer**: at wave close a fresh-context auditor
session re-reads the wave's diff against its ``success_criteria`` and
writes an :class:`~eawf.kernel.store.kinds.agent_report.AuditorReportBody`
verdict, append-only, at ``base_id=wave_id``. The interim seeder stays the
manual path; this producer runs alongside it.

Three layers, kept separate so the gate + policy are testable without a
spawn:

- :func:`verdict_requirement` -- the **pure risk-weighted policy**. It
  maps a wave to ``"always"`` (a fresh auditor verdict is required and
  blocks close), ``"sampled"`` (mechanical waves selected by a
  deterministic sampler), or ``"skip"`` (mechanical waves not selected).
  High-risk signals -- a large effort bucket, a judgment-heavy role, or a
  security-scoped wave -- force ``"always"``.
- :func:`verify_wave_verdict_gate` -- the **pure close gate**. It reads
  the persisted AUDITOR rows for the wave and returns a
  :class:`WaveVerdictGate`. The gate blocks close ONLY for the required
  subset: an ``"always"`` (or sampled-and-selected) wave whose freshest
  auditor verdict is absent or not close-ready blocks; a ``"skip"`` wave
  never blocks.
- :func:`produce_wave_verdict` -- the **live producer**. It registers a
  fresh-context AUDITOR :class:`~eawf.kernel.state.models.AgentSession`
  (distinct from the wave's executor session), drives the bounded re-ask
  loop (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`) with
  an injected ``spawn`` over an auditor-only prompt carrying solely the
  diff base + the wave's ``success_criteria`` (NOT the executor's working
  context), and appends the validated body through the canonical writer
  :func:`eawf.workflow.agent_report.store.append_agent_report` at
  ``base_id=wave_id``.

Fresh-context invariant. The verdict author MUST be a new AUDITOR
session, never the executor's. :func:`produce_wave_verdict` registers its
own auditor session; if the resolved author session turns out to be the
executor's (role EXECUTOR) the append is refused with
:class:`ExecutorSelfReportError` -- a self-report is a fail-fast typed
error, not a silently-accepted verdict. Fresh context is structural: the
author runs in its own AUDITOR session over a diff-only prompt -- the
executor's narrative is never threaded in -- so the verdict re-reads the
diff cold.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import TypeAdapter

from eawf.kernel.spec.wave import WaveBehavior
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    AgentSessionRole,
    AgentSessionStatus,
    Confidence,
    EffortBucket,
)
from eawf.kernel.state.models import AgentSession, State, Wave
from eawf.kernel.store.kinds.agent_report import AgentReportBody, AuditorReportBody
from eawf.runtime.session.store import SessionConflict, start_session, terminalize_session
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.agent_report.store import (
    AgentReportAppendResult,
    append_agent_report,
)
from eawf.workflow.dispatch.llm_assist import (
    DEFAULT_MAX_ATTEMPTS,
    LLMAssistResult,
    SpawnFn,
    assist_with_schema,
)
from eawf.workflow.lifecycle.wave_sha import derive_diff_base

logger = logging.getLogger(__name__)

#: Forced schema for the auditor verdict. Validating a spawn's JSON against
#: this adapter narrows the agent-report union to the auditor body: a
#: non-auditor body (e.g. an executor self-report shape) fails the
#: ``role: Literal["auditor"]`` discriminator and raises
#: :class:`pydantic.ValidationError`, which the bounded re-ask loop
#: classifies as a schema mismatch and re-asks (rather than escaping as an
#: uncaught error).
_AUDITOR_BODY_ADAPTER: TypeAdapter[AuditorReportBody] = TypeAdapter(AuditorReportBody)

#: A verdict requirement classification for one wave.
#:
#: - ``"always"`` -- a fresh auditor verdict is required; its absence or a
#:   FAIL / BLOCKED verdict blocks close. High-risk waves land here.
#: - ``"sampled"`` -- a mechanical wave the deterministic sampler selected;
#:   it is treated like ``"always"`` for the close gate so a sampled wave
#:   still blocks on a missing / failed verdict.
#: - ``"skip"`` -- a mechanical wave the sampler did not select; close is
#:   never blocked on a verdict for it.
VerdictRequirement = Literal["always", "sampled", "skip"]

#: Effort buckets that force an ``"always"`` verdict requirement. A large
#: wave carries enough blast radius that a fresh auditor pass is mandatory.
_HIGH_RISK_EFFORT: frozenset[EffortBucket] = frozenset({EffortBucket.L, EffortBucket.XL})

#: Roles whose output is judgment-heavy rather than mechanical. A wave run
#: by one of these roles forces an ``"always"`` verdict requirement: the
#: deliverable is a design / assessment artifact whose correctness a fresh
#: auditor pass must confirm, not a mechanical edit a sampler may skip.
_JUDGMENT_ROLES: frozenset[AgentSessionRole] = frozenset(
    {
        AgentSessionRole.AUDITOR,
        AgentSessionRole.REVIEWER,
        AgentSessionRole.PLANNER,
        AgentSessionRole.RESEARCHER,
        AgentSessionRole.DOMAIN_SPECIALIST,
        AgentSessionRole.OPERATOR,
    }
)

#: Security-relevant keywords. A wave whose title or success criteria name
#: any of these is security-scoped and forces an ``"always"`` requirement
#: -- a sandbox / auth / egress regression is exactly the failure mode a
#: fresh auditor pass exists to catch, so it is never sampled away.
_SECURITY_KEYWORDS: tuple[str, ...] = (
    "security",
    "sandbox",
    "auth",
    "egress",
    "secret",
    "jail",
    "scrub",
)

#: Deterministic sampling rate for mechanical (low-risk) waves: 1 in N is
#: sampled for a fresh auditor verdict. The sampler is a stable hash of the
#: wave id so the same wave always lands the same way (no flakiness, no
#: per-run drift) while roughly one in :data:`_SAMPLE_EVERY` mechanical
#: waves still gets an independent check.
_SAMPLE_EVERY: int = 4

#: Verdicts the close gate treats as close-ready (mirrors
#: :data:`eawf.workflow.verify.dispatch_close._CLOSE_READY_VERDICTS`). A
#: ``PASS`` is clean; ``PASS_WITH_FOLLOWUPS`` carries follow-ups the
#: operator tracks but does not block on.
_CLOSE_READY_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS_WITH_FOLLOWUPS,
    }
)


class ExecutorSelfReportError(ValueError):
    """Raised when the would-be verdict author is the wave's executor.

    The fresh-context invariant: the per-wave verdict MUST be authored by a
    new AUDITOR session, never the executor's. A session whose role is
    EXECUTOR cannot author the auditor verdict for its own wave -- that is a
    self-report, which this error refuses fail-fast so a non-independent
    verdict never reaches the store.
    """


@dataclass(frozen=True)
class WaveVerdictGate:
    """Outcome of :func:`verify_wave_verdict_gate`.

    Attributes:
        wave_id: The wave whose verdict gate was evaluated.
        requirement: The wave's :data:`VerdictRequirement` classification.
        passed: ``True`` iff the gate does not block close. Always ``True``
            for a ``"skip"`` wave; for an ``"always"`` / ``"sampled"`` wave
            it is ``True`` only when a fresh AUDITOR verdict exists and is
            close-ready.
        verdict: The freshest AUDITOR verdict found for the wave, or
            ``None`` when no auditor verdict has been written yet.
        reasons: One short string per blocking signal. Empty when the gate
            passes.
    """

    wave_id: str
    requirement: VerdictRequirement
    passed: bool
    verdict: AgentReportVerdict | None = None
    reasons: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class WaveVerdictResult:
    """Result of :func:`produce_wave_verdict`.

    Attributes:
        append_result: The canonical-writer append result for the persisted
            auditor verdict (carries the store URN + the monotonic attempt
            number).
        assist_result: The schema-forced
            :class:`~eawf.workflow.dispatch.llm_assist.LLMAssistResult`
            wrapping the validated auditor body + the spawn provenance.
        auditor_session_id: Id of the fresh AUDITOR session that authored
            the verdict (distinct from the wave's executor session).
        verdict: The persisted verdict value.
    """

    append_result: AgentReportAppendResult
    assist_result: LLMAssistResult
    auditor_session_id: str
    verdict: AgentReportVerdict


def _wave_text_corpus(wave: Wave) -> str:
    """Return the lowercased title + success-criteria text for keyword scans."""
    parts = [wave.title, *(c.text for c in wave.success_criteria)]
    return "\n".join(parts).lower()


def _is_security_scoped(wave: Wave) -> bool:
    """Return whether *wave* names a security-relevant keyword.

    Scans the wave title and success criteria for any
    :data:`_SECURITY_KEYWORDS` as a **whole word**. A hyphen is treated as
    part of the token so a wave-code shape such as ``"AUTH-3"`` does NOT
    arm the ``"auth"`` keyword, and a substring embedded in a larger word
    (``"authority"``, ``"regression"`` for ``"egress"``) is not a match --
    only a standalone ``"auth"`` / ``"egress"`` classifies. A
    security-scoped wave forces an ``"always"`` verdict requirement so a
    sandbox / auth / egress regression cannot be sampled past the close
    gate.
    """
    corpus = _wave_text_corpus(wave)
    return any(
        re.search(rf"(?<![\w-]){re.escape(keyword)}(?![\w-])", corpus)
        for keyword in _SECURITY_KEYWORDS
    )


def _is_sampled(wave_id: str, *, sample_every: int = _SAMPLE_EVERY) -> bool:
    """Return whether a mechanical wave is sampled for a fresh verdict.

    Deterministic: a stable hash of *wave_id* modulo *sample_every* decides,
    so the same wave always lands the same way across runs (no flakiness).
    Roughly one in *sample_every* mechanical waves is selected.

    Args:
        wave_id: The wave id to test.
        sample_every: The 1-in-N sampling denominator. Must be at least 1.

    Returns:
        ``True`` when the wave is in the sampled subset.
    """
    if sample_every <= 1:
        return True
    digest = sum(ord(ch) for ch in wave_id)
    return digest % sample_every == 0


def verdict_requirement(
    wave: Wave,
    *,
    sample_every: int = _SAMPLE_EVERY,
) -> VerdictRequirement:
    """Return the risk-weighted verdict requirement for *wave*.

    Pure function -- no I/O, no store access. The policy:

    1. ``"always"`` when the wave is high-risk on any signal:

       - a large effort bucket (``L`` / ``XL``);
       - a judgment-heavy ``agent_role`` (auditor / reviewer / planner /
         researcher / domain-specialist / operator);
       - a security-scoped wave (title or success criteria name a
         :data:`_SECURITY_KEYWORDS` keyword).

    2. Otherwise the wave is mechanical (a small / unspecified-effort
       executor-style wave). A deterministic sampler (:func:`_is_sampled`)
       selects roughly one in *sample_every* mechanical waves for a fresh
       verdict -- those return ``"sampled"``; the rest return ``"skip"``.

    Args:
        wave: The wave to classify. Read-only.
        sample_every: The 1-in-N mechanical-wave sampling denominator.

    Returns:
        The :data:`VerdictRequirement` for *wave*.
    """
    if wave.effort_bucket is not None and wave.effort_bucket in _HIGH_RISK_EFFORT:
        return "always"
    if wave.agent_role is not None and wave.agent_role in _JUDGMENT_ROLES:
        return "always"
    if _is_security_scoped(wave):
        return "always"
    if _is_sampled(wave.id, sample_every=sample_every):
        return "sampled"
    return "skip"


def _latest_auditor_verdict(
    state_path: Path,
    wave_id: str,
) -> AgentReportVerdict | None:
    """Return the freshest AUDITOR verdict for *wave_id*, or ``None``.

    Reads the auditor report store via
    :func:`eawf.workflow.agent_report.rollup.iter_agent_reports` filtered to
    role AUDITOR + ``base_id == wave_id``. The rows arrive sorted by
    ``(created_at, id)`` ascending, so the last row is the freshest attempt
    -- the verdict the gate honours.
    """
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=wave_id)
    if not rows:
        return None
    return rows[-1].payload.body.verdict


def verify_wave_verdict_gate(
    wave: Wave,
    *,
    state_path: Path,
    sample_every: int = _SAMPLE_EVERY,
) -> WaveVerdictGate:
    """Return the close-gate outcome for *wave*'s fresh-auditor verdict.

    The gate blocks close ONLY on the required subset. For a wave whose
    :func:`verdict_requirement` is ``"always"`` or ``"sampled"``, close is
    blocked when the freshest persisted AUDITOR verdict is absent or is not
    close-ready (a ``FAIL`` / ``BLOCKED`` verdict). A ``"skip"`` wave never
    blocks -- the gate passes unconditionally so a mechanical wave the
    sampler did not select does not require a verdict to close.

    Pure read: this inspects only the persisted auditor rows and the wave's
    typed fields; it never spawns. The daemon close path calls it and maps a
    blocking outcome onto its lifecycle rejection.

    Args:
        wave: The wave being closed. Read-only.
        state_path: Path to ``state.json``; the auditor report store
            resolves under its sibling ``store/`` directory.
        sample_every: The 1-in-N mechanical-wave sampling denominator,
            forwarded to :func:`verdict_requirement`.

    Returns:
        A :class:`WaveVerdictGate` whose ``passed`` reflects whether close
        is permitted.
    """
    requirement = verdict_requirement(wave, sample_every=sample_every)
    if requirement == "skip":
        logger.debug(f"verify_wave_verdict_gate wave={wave.id} requirement=skip passed=True")
        return WaveVerdictGate(wave_id=wave.id, requirement=requirement, passed=True)

    verdict = _latest_auditor_verdict(state_path, wave.id)
    reasons: list[str] = []
    if verdict is None:
        reasons.append("no fresh auditor verdict written for wave")
    elif verdict not in _CLOSE_READY_VERDICTS:
        reasons.append(f"auditor verdict={verdict.value} not in close-ready set")

    passed = not reasons
    logger.info(
        f"verify_wave_verdict_gate wave={wave.id} requirement={requirement} "
        f"verdict={verdict.value if verdict is not None else 'none'} passed={passed}"
    )
    return WaveVerdictGate(
        wave_id=wave.id,
        requirement=requirement,
        passed=passed,
        verdict=verdict,
        reasons=tuple(reasons),
    )


def _criteria_block(success_criteria: Iterable[str]) -> str:
    """Render the wave success criteria as a numbered prompt block."""
    rows = [f"{idx}. {criterion}" for idx, criterion in enumerate(success_criteria, start=1)]
    if not rows:
        return "(the wave declared no explicit success criteria)"
    return "\n".join(rows)


def _rubric_block(rubric: Sequence[WaveBehavior]) -> str:
    """Render a refute-first instruction line per rubric item.

    Each line is keyed by the behaviour ``id`` so a downstream ballot can
    match a refutation per item, names the ``quality_dimension`` the score
    is taken on, and instructs the auditor to actively try to DISPROVE the
    item -- defaulting to fail when the evidence does not positively
    support it. The disprove-first stance is the whole point of the block:
    a verdict that cannot refute an item from the evidence is the only one
    that passes it.

    Args:
        rubric: The ordered jury-scorable behaviours to score. Each is
            expected to carry a ``quality_dimension`` (jury-scorable
            behaviours always do, per the WaveBehavior validator); a
            missing dimension renders as ``unspecified``.

    Returns:
        The newline-joined per-item instruction block.
    """
    rows: list[str] = []
    for behavior in rubric:
        dimension = (
            behavior.quality_dimension.value if behavior.quality_dimension else "unspecified"
        )
        rows.append(
            f"- {behavior.id} (quality dimension: {dimension}): actively try to "
            f"DISPROVE this item. Cite the evidence above; default to FAIL unless "
            f"the evidence positively supports it. Item under test: {behavior.text}"
        )
    return "\n".join(rows)


#: The auditor runs inside the operator's LIVE working tree, so a verification
#: command that mutates that tree is not a read: ``pre-commit run --all-files``
#: stashes uncommitted changes and, when a hook auto-fix conflicts with the
#: stash, rolls the whole thing back -- silently discarding work the operator
#: has not committed yet. (It cost a set of uncommitted test edits during
#: P30-I25.) A verdict never needs to re-run the gates: the executor's run is
#: recorded, and re-running one buys nothing but risk.
WORKING_TREE_RULE: str = (
    "## Working-tree rule\n"
    "\n"
    "You are auditing the operator's LIVE working tree. Read it; never mutate\n"
    "it. Do NOT run `pre-commit`, `git stash`, `git checkout`, `git reset`, or\n"
    "any formatter / fixer in write mode -- `pre-commit run --all-files` stashes\n"
    "uncommitted changes and can roll them back on a hook conflict, destroying\n"
    "work the operator has not committed. When a criterion asserts that a gate\n"
    "passes (tests, lint, pre-commit, CI), verify it from the RECORDED evidence\n"
    "-- the wave's evidence block, the commit, the stored gate output -- and\n"
    "mark it unverified if that evidence is absent. Never re-run the gate to\n"
    "check."
)


def build_auditor_prompt(
    wave: Wave,
    *,
    diff_base: str,
    rubric: Sequence[WaveBehavior] | None = None,
    evidence_block: str | None = None,
    include_diff: bool = True,
) -> str:
    """Return the fresh-context auditor prompt for *wave*.

    The prompt is deliberately fresh-context: it carries the diff range and
    the wave's ``success_criteria`` -- never the executor's prior report or
    working narrative. The auditor re-reads the diff cold and produces an
    :class:`~eawf.kernel.store.kinds.agent_report.AuditorReportBody` verdict
    with one
    :class:`~eawf.kernel.store.kinds.agent_report.CriterionVerdict` row per
    criterion plus any refutations.

    When *rubric* is non-empty the prompt grows a refute-first block: one
    instruction line per jury-scorable behaviour, keyed by its ``id`` and
    naming its ``quality_dimension``, telling the auditor to actively
    DISPROVE the item and default to fail unless the evidence supports it.
    When *evidence_block* is supplied the prompt carries it under an
    ``## Evidence`` heading so the verdict grounds in provenance-pinned
    evidence. Setting *include_diff* to ``False`` omits the diff section
    entirely, which forces an evidence-grounded verdict (the diff is no
    longer available to lean on).

    Args:
        wave: The wave under audit. Supplies the id + success criteria.
        diff_base: The git diff base ref the auditor diffs against
            (``git diff <diff_base>...HEAD`` scopes to the wave's delta).
        rubric: Optional ordered jury-scorable behaviours to score
            refute-first. ``None`` or empty omits the rubric block.
        evidence_block: Optional provenance-pinned evidence text the
            verdict must ground in. ``None`` omits the evidence section.
        include_diff: When ``False`` the diff section is omitted entirely
            (an evidence-grounded verdict cannot lean on the diff).

    Returns:
        The rendered Markdown auditor prompt.
    """
    criteria = _criteria_block(c.text for c in wave.success_criteria)
    sections: list[str] = [
        f"# Fresh-context audit: wave {wave.id}\n"
        "\n"
        "You are a fresh-context AUDITOR. You did not write this code. Re-read the\n"
        "wave's diff against its success criteria and produce an independent\n"
        "verdict -- do not assume the implementation is correct."
    ]
    if include_diff:
        sections.append(
            "## Diff under audit\n"
            "\n"
            f"Diff the wave's delta with `git diff {diff_base}...HEAD`. Read every\n"
            "changed file before judging."
        )
    if evidence_block is not None:
        sections.append("## Evidence\n\n" + evidence_block)
    if rubric:
        sections.append(
            "## Rubric (refute-first)\n"
            "\n"
            "Score each rubric item below by trying to DISPROVE it. A passing\n"
            "verdict is the one you could NOT refute from the evidence; when the\n"
            "evidence does not positively support an item, default to fail.\n"
            "\n"
            f"{_rubric_block(rubric)}"
        )
    sections.append(f"## Success criteria\n\n{criteria}")
    sections.append(WORKING_TREE_RULE)
    sections.append(
        "## Output contract\n"
        "\n"
        "Respond with ONLY a single JSON object that validates against the\n"
        "auditor `agent_end` report body (`role` = `auditor`). It MUST carry a\n"
        "`verdict` (one of pass / pass-with-followups / fail / blocked), a\n"
        "`confidence` -- the STRING `high`, `medium`, or `low`, never a number --\n"
        "a `summary`, a `target_id` equal to the wave id, one\n"
        "`criteria` entry (a CriterionVerdict with `criterion` + `passed`) per\n"
        "success criterion above, and any `refutations` you found. No prose, no\n"
        "code fences."
    )
    return "\n\n".join(sections)


def parse_auditor_report_body(raw: object) -> AuditorReportBody:
    """Validate *raw* as an :class:`AuditorReportBody`.

    The forced-schema validator the producer hands the bounded re-ask loop
    (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`). It
    validates *raw* directly against :data:`_AUDITOR_BODY_ADAPTER`, so a
    spawn that returns a non-auditor body (e.g. an executor self-report
    shape) fails the ``role: Literal["auditor"]`` discriminator and raises
    :class:`pydantic.ValidationError` -- which the loop catches, classifies
    as a schema mismatch, and re-asks (then exhausts typed). Raising a
    ``ValidationError`` rather than a plain ``ValueError`` is load-bearing:
    the loop only catches ``json.JSONDecodeError`` + ``ValidationError``, so
    a plain ``ValueError`` would escape the bounded retry uncaught.

    Args:
        raw: The JSON-decoded spawn output.

    Returns:
        The validated :class:`AuditorReportBody`.

    Raises:
        pydantic.ValidationError: When *raw* is not a valid auditor report
            body (wrong role, missing fields, or a schema mismatch).
    """
    return _AUDITOR_BODY_ADAPTER.validate_python(_coerce_confidence(raw))


#: Cutoffs for reading a numeric confidence as an enum bucket. A model asked for
#: a "confidence" reaches for a probability far more readily than for one of
#: three words, so a bare float is the single most common way an otherwise
#: correct auditor report fails the schema -- and because the re-ask loop is
#: bounded, three of them in a row kill the close outright with no verdict
#: written (P30-I25-W29). The cutoffs are the obvious thirds; the point is to
#: accept an answer that is already unambiguous, not to invent precision.
_CONFIDENCE_HIGH: float = 0.75
_CONFIDENCE_MEDIUM: float = 0.4


def _coerce_confidence(raw: object) -> object:
    """Return *raw* with a numeric ``confidence`` read as its enum bucket.

    A ``0.9`` means ``high`` in any reading of the word, and refusing it costs a
    whole audit. Anything that is not a plain number in ``[0, 1]`` is passed
    through untouched, so a genuinely unparseable body still raises
    :class:`pydantic.ValidationError` and the bounded re-ask still runs.
    """
    if not isinstance(raw, dict):
        return raw
    value = raw.get("confidence")
    if isinstance(value, bool) or not isinstance(value, int | float):
        return raw
    if not 0.0 <= float(value) <= 1.0:
        return raw
    if value >= _CONFIDENCE_HIGH:
        bucket = Confidence.HIGH
    elif value >= _CONFIDENCE_MEDIUM:
        bucket = Confidence.MEDIUM
    else:
        bucket = Confidence.LOW
    logger.info(f"_coerce_confidence raw={value!r} bucket={bucket.value!r}")
    return {**raw, "confidence": bucket.value}


#: Suffix appended to the wave id to scope the fresh auditor session. The
#: session store enforces one ACTIVE session per ``(scope_id, runtime)``;
#: the wave-close producer may run while the executor session is still
#: ACTIVE on ``(wave_id, runtime)``, so the auditor session scopes to a
#: distinct ``"{wave_id}::audit"`` to coexist with the executor lane. The
#: verdict's load-bearing join key stays the bare ``wave_id`` (passed as the
#: report ``base_id``), which the close gate, self-eval, and retro digest
#: all key on -- only the session-store uniqueness key is qualified.
_AUDITOR_SCOPE_SUFFIX: str = "::audit"


def _auditor_scope_id(wave_id: str) -> str:
    """Return the verdict-qualified scope id for *wave_id*'s auditor session."""
    return f"{wave_id}{_AUDITOR_SCOPE_SUFFIX}"


def _find_active_auditor(state: State, *, scope_id: str, runtime: str) -> AgentSession | None:
    """Return an ACTIVE AUDITOR session for the (scope, runtime) pair, or ``None``.

    Mirrors the ``(scope_id, runtime)`` uniqueness key the session store
    enforces, narrowed to the auditor role so the retry path resolves the
    exact session the store's
    :class:`~eawf.runtime.session.store.SessionConflict` refers to. The
    *scope_id* is the verdict-qualified auditor scope (:func:`_auditor_scope_id`).
    """
    for session in state.agent_sessions.values():
        if (
            session.role is AgentSessionRole.AUDITOR
            and session.scope_id == scope_id
            and session.runtime == runtime
            and session.status is AgentSessionStatus.ACTIVE
        ):
            return session
    return None


def _find_active_at_slot(state: State, *, scope_id: str, runtime: str) -> AgentSession | None:
    """Return the ACTIVE session occupying the (scope, runtime) slot, role-agnostic.

    Mirrors the exact ``(scope_id, runtime)`` ACTIVE-uniqueness key the session
    store keys :class:`~eawf.runtime.session.store.SessionConflict` on, without
    narrowing to a role. Used to diagnose which session caused a conflict so a
    non-auditor occupant (an executor) is rejected as a self-report rather than
    re-raised as an opaque conflict.
    """
    for session in state.agent_sessions.values():
        if (
            session.scope_id == scope_id
            and session.runtime == runtime
            and session.status is AgentSessionStatus.ACTIVE
        ):
            return session
    return None


def _resolve_auditor_session(
    *,
    state: State,
    events_path: Path,
    wave: Wave,
    runtime: str,
    now: datetime | None,
) -> AgentSession:
    """Register (or reuse) the fresh-context AUDITOR session for *wave*.

    Opens a new AUDITOR :class:`~eawf.kernel.state.models.AgentSession`
    scoped to the verdict-qualified :func:`_auditor_scope_id` via
    :func:`eawf.runtime.session.store.start_session`. The qualified scope
    keeps the auditor lane distinct from the executor's wave-scoped session
    so both can be ACTIVE at close time (the store keys ACTIVE-uniqueness on
    ``(scope_id, runtime)``).

    On the append-only **retry** path the ACTIVE auditor session already
    exists, so the store raises
    :class:`~eawf.runtime.session.store.SessionConflict`; this helper catches
    it and reuses the existing auditor session as the author -- mirroring
    active-session reuse in
    :func:`eawf.runtime.daemon.methods.agent._claim_live_session`.
    Reusing the auditor session keeps the verdict author fresh-context
    relative to the executor (it is never the executor's session) while the
    report layer still records attempt 2 as a distinct append.

    Args:
        state: Validated state -- mutated in place when a new session is
            registered.
        events_path: Path to ``event.jsonl`` for the session-start event.
        wave: The wave under audit.
        runtime: Runtime adapter id recorded on the session.
        now: Optional fixed timestamp for the session start.

    Returns:
        The fresh-context AUDITOR :class:`~eawf.kernel.state.models.AgentSession`.

    Raises:
        ExecutorSelfReportError: When the resolved session is not an AUDITOR
            session (a self-report would otherwise slip through).
    """
    scope_id = _auditor_scope_id(wave.id)
    try:
        session = start_session(
            state=state,
            events_path=events_path,
            role=AgentSessionRole.AUDITOR,
            scope_id=scope_id,
            runtime=runtime,
            now=now,
        ).session
    except SessionConflict:
        existing = _find_active_auditor(state, scope_id=scope_id, runtime=runtime)
        if existing is None:
            # The slot is occupied by a non-auditor session (the executor's
            # lane). Re-using it would make the executor the verdict author,
            # so refuse the self-report fail-fast rather than re-raise the
            # opaque conflict -- a non-independent verdict never reaches the
            # store.
            occupant = _find_active_at_slot(state, scope_id=scope_id, runtime=runtime)
            if occupant is not None:
                raise ExecutorSelfReportError(
                    f"verdict author must be a fresh auditor session, got role "
                    f"{occupant.role.value!r} for wave: {wave.id!r}"
                ) from None
            raise
        logger.info(f"_resolve_auditor_session reuse wave={wave.id} session={existing.id!r}")
        session = existing
    # Fresh-context invariant: the verdict author MUST be a new AUDITOR
    # session. A session whose role is EXECUTOR cannot author the verdict
    # for its own wave -- refuse the self-report fail-fast.
    if session.role is not AgentSessionRole.AUDITOR:
        raise ExecutorSelfReportError(
            f"verdict author must be a fresh auditor session, got role "
            f"{session.role.value!r} for wave: {wave.id!r}"
        )
    return session


async def produce_wave_verdict(
    *,
    state: State,
    state_path: Path,
    events_path: Path,
    wave: Wave,
    spawn: SpawnFn,
    runtime: str = "claude-code",
    repo_root: Path | None = None,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    now: datetime | None = None,
    on_session_registered: Callable[[State], None] | None = None,
    on_session_terminalized: Callable[[State], None] | None = None,
) -> WaveVerdictResult:
    """Produce + persist a fresh-context auditor verdict for *wave*.

    The ordered steps, each a single-responsibility seam:

    1. Resolve the fresh AUDITOR
       :class:`~eawf.kernel.state.models.AgentSession`
       (:func:`_resolve_auditor_session`) scoped to the wave -- a NEW
       session distinct from the wave's executor session on the first
       produce, reused on the append-only retry path. Either way the author
       is fresh-context (never the executor's session); the helper refuses a
       non-auditor author fail-fast.
    2. Resolve the diff base via
       :func:`eawf.workflow.lifecycle.wave_sha.derive_diff_base` and render
       the auditor-only prompt (:func:`build_auditor_prompt`) carrying solely
       the diff range + the wave's success criteria.
    3. Drive the bounded re-ask loop
       (:func:`eawf.workflow.dispatch.llm_assist.assist_with_schema`) with the
       injected *spawn* and the narrowing :func:`parse_auditor_report_body`
       validator, so the loop only accepts an auditor body.
    4. Append the validated body through the canonical writer
       (:func:`eawf.workflow.agent_report.store.append_agent_report`) at
       ``base_id=wave.id``. The writer computes the monotonic attempt, so a
       second call appends attempt 2 rather than overwriting attempt 1
       (append-only retry).

    The injected *spawn* is the testability seam: production binds the
    resolved adapter's
    :meth:`~eawf.runtime.runtimes.adapter.RuntimeAdapter.spawn_session`; a
    test binds a recording stub returning a canned auditor
    :class:`~eawf.runtime.runtimes.adapter.SpawnResult`, so no real
    subprocess runs. Fresh context is structural: the auditor runs in its
    own session over a diff-only prompt, never the executor's narrative.

    Args:
        state: Loaded, validated state -- mutated in place by the session
            registration (the caller persists it through the canonical
            writer). Supplies the executor session for the self-report
            check.
        state_path: Path to ``state.json``; the auditor report store
            resolves under its sibling ``store/`` directory.
        events_path: Path to ``event.jsonl`` for the session-start event.
        wave: The wave under audit.
        spawn: Injected async spawn callable performing one spawn per
            prompt. Never a real subprocess under test.
        runtime: Runtime adapter id recorded on the auditor session +
            report header. Defaults to ``"claude-code"``.
        repo_root: Repository root forwarded to
            :func:`derive_diff_base` so the diff base resolves in the right
            git tree. ``None`` lets the derive fall back to the process cwd.
        max_attempts: Bounded re-ask ceiling forwarded to
            :func:`assist_with_schema`.
        now: Optional fixed timestamp for the session start (tests pin it).
        on_session_registered: Optional callback invoked with *state* right
            after the auditor session is registered and before the spawn runs.
            The close path binds it to a state persist so the running auditor is
            visible in the Watch roster while it works, rather than only once the
            close completes (and never, when the close fails).
        on_session_terminalized: Optional best-effort callback invoked after the
            auditor session reaches CLOSED or FAILED. The daemon close path
            persists this terminal state immediately so a later close guard
            rejection cannot leave the durable row ACTIVE.

    Returns:
        A :class:`WaveVerdictResult` carrying the append result, the
        schema-forced assist result, the fresh auditor session id, and the
        persisted verdict.

    Raises:
        ExecutorSelfReportError: When the registered author session
            resolves to an EXECUTOR session (a self-report).
        eawf.workflow.dispatch.llm_assist.LLMAssistError: When the bounded
            re-ask loop exhausts without a schema-valid auditor body.
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When
            the persisted body role disagrees with the author session role.
    """
    auditor_session: AgentSession | None = None
    report_appended = False
    try:
        auditor_session = _resolve_auditor_session(
            state=state,
            events_path=events_path,
            wave=wave,
            runtime=runtime,
            now=now,
        )
        # The session lands in `state` in memory, and the CALLER persists state --
        # which, on the close path, happens only when the close finishes. An audit
        # runs for minutes and can fail, so the operator's Watch roster (which reads
        # state) showed no running agent at all while the auditor worked, and none
        # afterwards when the close failed. Persist the registration now so the live
        # auditor is visible for as long as it runs.
        if on_session_registered is not None:
            on_session_registered(state)

        diff_base = derive_diff_base(wave.id, repo_root=repo_root)
        prompt = build_auditor_prompt(wave, diff_base=diff_base)
        assist_result = await assist_with_schema(
            prompt,
            spawn=spawn,
            validator=parse_auditor_report_body,
            max_attempts=max_attempts,
        )
        body: AgentReportBody = assist_result.body
        append_result = append_agent_report(
            state=state,
            state_path=state_path,
            session_id=auditor_session.id,
            base_id=wave.id,
            body=body,
            runtime=runtime,
        )
        report_appended = True
        logger.info(
            f"produce_wave_verdict wave={wave.id} session={auditor_session.id!r} "
            f"verdict={body.verdict.value!r} attempt={append_result.attempt}"
        )
        return WaveVerdictResult(
            append_result=append_result,
            assist_result=assist_result,
            auditor_session_id=auditor_session.id,
            verdict=body.verdict,
        )
    finally:
        if auditor_session is not None:
            terminal_status = (
                AgentSessionStatus.CLOSED if report_appended else AgentSessionStatus.FAILED
            )
            terminalize_session(
                state=state,
                events_path=events_path,
                session_id=auditor_session.id,
                status=terminal_status,
                summary=(
                    "auditor report appended"
                    if report_appended
                    else "auditor attempt failed before report append"
                ),
                now=now,
            )
            if on_session_terminalized is not None:
                try:
                    on_session_terminalized(state)
                except Exception as exc:
                    logger.warning(
                        f"produce_wave_verdict wave={wave.id} session={auditor_session.id!r} "
                        f"terminal_persist=failed error={exc!r}"
                    )


def assert_not_executor_self_report(state: State, *, wave_id: str, author_session_id: str) -> None:
    """Refuse an executor self-report fail-fast.

    The standalone fresh-context guard the daemon close path calls before
    accepting any externally-supplied verdict author: it looks the author
    session up in *state* and raises when that session's role is EXECUTOR.
    This makes the self-report rejection reusable by callers that did not go
    through :func:`produce_wave_verdict` (which registers its own auditor
    session and so cannot hit this case).

    Args:
        state: Validated state carrying ``agent_sessions``.
        wave_id: The wave the verdict is about (for the error message).
        author_session_id: The id of the would-be verdict author session.

    Raises:
        KeyError: When *author_session_id* is absent from
            ``state.agent_sessions``.
        ExecutorSelfReportError: When the author session role is EXECUTOR.
    """
    session = state.agent_sessions.get(author_session_id)
    if session is None:
        raise KeyError(f"unknown agent session: {author_session_id!r}")
    if session.role is AgentSessionRole.EXECUTOR:
        raise ExecutorSelfReportError(
            f"executor session {author_session_id!r} cannot author its own "
            f"verdict for wave: {wave_id!r}"
        )


__all__ = [
    "ExecutorSelfReportError",
    "VerdictRequirement",
    "WaveVerdictGate",
    "WaveVerdictResult",
    "assert_not_executor_self_report",
    "build_auditor_prompt",
    "parse_auditor_report_body",
    "produce_wave_verdict",
    "verdict_requirement",
    "verify_wave_verdict_gate",
]
