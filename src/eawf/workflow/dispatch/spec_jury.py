"""Spec-jury close-gate flavour for UI/UX-banded waves.

The default wave-close verdict gate
(:func:`eawf.workflow.dispatch.verdict.verify_wave_verdict_gate`) and its
cross-vendor upgrade
(:func:`eawf.observability.eval.cross_vendor_jury.convene_cross_vendor_jury`)
score a wave **holistically** -- one verdict over the whole diff. A UI/UX
wave carries a richer contract: its :class:`~eawf.kernel.spec.wave.WaveSpec`
flags a subset of behaviours ``jury_scorable=True``, each tagged with the
ISO-25010 ``quality_dimension`` it is measured on. Those flagged behaviours
ARE the rubric (:func:`eawf.kernel.spec.rubric.rubric_items`). This module is
the **spec-jury producer**: for a banded wave it loads the rubric, renders a
refute-first per-item auditor prompt
(:func:`eawf.workflow.dispatch.verdict.build_auditor_prompt`), collects one
per-item ballot per juror, reduces them per rubric item
(:func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`),
and writes ONE per-item :class:`~eawf.kernel.store.kinds.agent_report.AuditorReportBody`
at ``base_id=wave_id`` through the canonical agent-report writer.

Live multi-juror rung (TRUST-5). The producer drives an INJECTED
:data:`PerItemBallotFn` callback: :func:`live_per_item_ballot_fn` binds the
live rung -- it drives each disjoint juror runtime through a bounded re-ask
loop and parses one :class:`PerItemJurorBallot` per juror from its output,
reusing the per-runtime :data:`~eawf.observability.eval.cross_vendor_jury.SpawnFactory`
the cross-vendor jury already binds. The callback stays injected (never
imported here) so the module spawns nothing of its own: production binds the
live fn, a test binds a recording stub returning canned ballots. When the
callback is ``None`` the producer is **idle** -- it returns a typed
:class:`SpecJuryResult` with ``status="skipped"`` and writes nothing, so a
banded close with no ballot fn proceeds exactly as it does today. When the
callback is supplied the producer convenes the jury and the close gate
consults the reduced verdict.

Advisory-until-blocking. A per-item FAIL is held ADVISORY by default: the
producer writes the FAIL verdict for the operator but does NOT raise, so an
uncalibrated spec jury never blocks a close. Only once the jury has EARNED
:attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`
authority (the TRUST-4 earned-authority computation threaded in by the
daemon close path) does a per-item FAIL raise :class:`LifecycleError` and
block the close -- mirroring the same staged gate the cross-vendor oracle
path applies.

Band-scoped enforcement. The predicate :func:`wave_in_uiux_band` decides
whether a wave routes through the producer; it reads the active profile's
:attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands` token list and
returns ``False`` when the list is empty (the v0.5 default until the
band-population wave ships the real tokens). The predicate takes the band
list as a parameter so it is overridable -- an integration test forces a
band wave by passing an explicit token.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.spec.rubric import rubric_items
from eawf.kernel.spec.wave import WaveBehavior, WaveSpec
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    Confidence,
    StoreKind,
)
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.append import append_envelope
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.agent_report import AuditorReportBody, CriterionVerdict
from eawf.kernel.store.kinds.event import EventPayload
from eawf.kernel.store.kinds.evidence import mint_evidence_id
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import (
    JURY_RUNTIME_FAMILIES,
    PerItemJurorBallot,
    PerItemJuryResult,
    SpawnFactory,
    parse_per_item_ballot,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.observability.eval.jury_validation import BlockAuthority
from eawf.workflow.agent_report.store import (
    AgentReportAppendResult,
    append_agent_report,
)
from eawf.workflow.dispatch.llm_assist import (
    DEFAULT_MAX_ATTEMPTS,
    SchemaAttemptFailure,
)
from eawf.workflow.dispatch.verdict import build_auditor_prompt
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.wave_sha import derive_diff_base

logger = logging.getLogger(__name__)

#: An injected async callback that convenes the per-item jury for one wave
#: and returns one :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot`
#: per juror. Injected (never imported by the producer) so the producer spawns
#: nothing of its own: the live rung (:func:`live_per_item_ballot_fn`) binds a
#: spawn-then-validate adapter that drives each juror's runtime through the
#: bounded re-ask loop and parses a per-item ballot from its output; a test
#: binds a recording stub returning canned ballots. The single ``str`` argument
#: is the refute-first per-item auditor prompt from :func:`build_auditor_prompt`.
type PerItemBallotFn = Callable[[str], Awaitable[tuple[PerItemJurorBallot, ...]]]

#: Map the reduced per-item jury outcome onto the auditor report verdict the
#: close gate consults. A ``PASS`` aggregate clears the wave; a ``FAIL``
#: (one credible refutation vetoed a rubric item) is a ``FAIL`` verdict; a
#: ``NEEDS_USER`` (a split with no veto, or an unscored item) is ``BLOCKED``
#: so the operator adjudicates rather than the close silently passing.
_OUTCOME_VERDICT: dict[JuryAggregateOutcome, AgentReportVerdict] = {
    JuryAggregateOutcome.PASS: AgentReportVerdict.PASS,
    JuryAggregateOutcome.FAIL: AgentReportVerdict.FAIL,
    JuryAggregateOutcome.NEEDS_USER: AgentReportVerdict.BLOCKED,
}

#: ``event_type`` marking a raw-juror-FindingSet row in the evidence store.
#: The reduced AUDITOR verdict is persisted separately (the gated close
#: signal); this row preserves the UN-reduced per-juror ballots so a later
#: Plan-Jury calibration sweep has the granular vote data the reduction
#: collapses away.
SPEC_JURY_FINDINGSET_EVENT_TYPE = "spec_jury_findingset"

#: ``extras`` key holding the JSON-encoded array of raw per-juror ballots.
_FINDINGSETS_KEY = "findingsets"

#: ``extras`` key holding the wave id the FindingSets were collected for.
_WAVE_KEY = "wave_id"

#: ``extras`` key holding the count of jurors that cast a ballot.
_JUROR_COUNT_KEY = "juror_count"


def persist_juror_findingsets(
    state_path: Path,
    *,
    wave: Wave,
    ballots: tuple[PerItemJurorBallot, ...],
    now: datetime | None = None,
) -> str:
    """Persist the raw per-juror ballots (FindingSets) and return their row urn.

    The spec-jury close gate reduces the per-juror ballots into ONE auditor
    verdict (:func:`reduce_per_item_ballots`) and persists only that reduction.
    The reduction is lossy: the per-juror, per-item votes + refutations it folds
    away are exactly the calibration signal a Plan-Jury sweep needs (Fleiss-kappa
    over juror agreement, cross-vendor co-error rate). This appends ONE evidence
    store row carrying the UN-reduced ballots as JSON so that signal survives.

    The append rides the daemon-owned canonical store path
    (:func:`eawf.kernel.store.append.append_envelope`), the same writer the
    close-gate evidence ledger uses, so an on-disk FindingSet row is
    indistinguishable from any other daemon-written evidence row.

    Args:
        state_path: Absolute path to ``state.json`` (the store lives at its
            sibling ``store/evidence.jsonl``).
        wave: The banded wave the jury was convened for.
        ballots: The raw per-juror ballots collected from the jury fire. May be
            empty (every juror abstained); an empty FindingSet row is still
            persisted so a calibration sweep sees the all-abstain fire rather
            than inferring it from a gap.
        now: Optional fixed timestamp for the row (tests pin it).

    Returns:
        The fresh ``EV-<12 hex>`` evidence-record id the FindingSets were
        persisted under (the same id-namespace every evidence-store row uses).
    """
    stamp = now or datetime.now(UTC)
    row_id = mint_evidence_id()
    findingsets_json = json.dumps([ballot.model_dump(mode="json") for ballot in ballots])
    payload = EventPayload(
        timestamp=stamp,
        event_type=SPEC_JURY_FINDINGSET_EVENT_TYPE,
        actor="spec_jury",
        command="spec jury findingsets",
        args_hash="",
        status="ok",
        message=f"raw juror findingsets wave={wave.id} jurors={len(ballots)}",
        extras={
            _WAVE_KEY: wave.id,
            _JUROR_COUNT_KEY: len(ballots),
            _FINDINGSETS_KEY: findingsets_json,
        },
    )
    envelope = Envelope(
        id=row_id,
        kind=StoreKind.EVIDENCE,
        scope_id=wave.id,
        created_at=stamp,
        updated_at=None,
        summary=f"spec-jury findingsets {wave.id} jurors={len(ballots)}",
        payload=payload.model_dump(mode="json"),
    )
    append_envelope(store_path(state_path, StoreKind.EVIDENCE), envelope)
    logger.info(f"persist_juror_findingsets wave={wave.id} jurors={len(ballots)} row_id={row_id!r}")
    return row_id


#: Outcome of a spec-jury produce attempt.
#:
#: - ``"scored"`` -- a ballot fn was supplied, the jury convened, and the
#:   reduced verdict was written as an AUDITOR report.
#: - ``"skipped"`` -- the producer was idle (no ballot fn injected) OR the
#:   wave carries no jury-scorable behaviour (an empty rubric has nothing to
#:   veto); nothing was written and close proceeds.
SpecJuryStatus = Literal["scored", "skipped"]


@dataclass(frozen=True)
class SpecJuryResult:
    """Typed outcome of :func:`produce_spec_jury_verdict`.

    A frozen dataclass -- local plumbing, not a wire type. The close gate
    reads :attr:`verdict` (``None`` when the producer was idle / skipped) to
    decide whether to block.

    Attributes:
        wave_id: The wave the producer was run for.
        status: ``"scored"`` when the jury convened and a verdict was
            written, ``"skipped"`` when the producer was idle or the rubric
            was empty.
        verdict: The reduced AUDITOR verdict mapped from the per-item jury
            outcome, or ``None`` for a skipped run.
        result: The underlying
            :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJuryResult`,
            or ``None`` for a skipped run.
        append_result: The canonical-writer append result for the persisted
            auditor verdict, or ``None`` for a skipped run.
        findingset_id: The ``EV-<hex>`` evidence-store row id the raw per-juror
            ballots (FindingSets) were persisted under on a scored run, or
            ``None`` for a skipped run. The reduced verdict is lossy; this row
            preserves the un-reduced ballots for Plan-Jury calibration.
        reason: Short human reason a run was skipped, or ``None`` when scored.
    """

    wave_id: str
    status: SpecJuryStatus
    verdict: AgentReportVerdict | None = None
    result: PerItemJuryResult | None = None
    append_result: AgentReportAppendResult | None = None
    findingset_id: str | None = None
    reason: str | None = None

    @property
    def scored(self) -> bool:
        """Return whether the jury convened and wrote a verdict."""
        return self.status == "scored"


def wave_in_uiux_band(wave: Wave, *, bands: list[str] | tuple[str, ...] | None) -> bool:
    """Return whether *wave* is UI/UX-banded for the spec-jury gate.

    A wave is banded when EITHER arm fires (the band is the UNION):

    * **Structural** -- the wave's ``file_scopes`` are UI surface per
      :func:`eawf.kernel.spec.heuristics.is_ui_scope` (any scope under
      ``src/eawf/surfaces/tui/`` or ``src/eawf/surfaces/render/``). A wave
      that touches the UI tree is UI/UX-risky regardless of its title, so
      this arm bands UI waves with no per-profile config.
    * **Token** -- any token in *bands* matches (case-insensitively) on a
      WORD BOUNDARY of the wave id or title. The match is whole-word, not a
      bare substring: a token like ``"ui"`` arms only when ``ui`` stands as
      its own word (``"ui polish"``), never when it is embedded in a larger
      word (``"build pipeline"`` / ``"quiz"`` do NOT arm). The band list is
      the active profile's
      :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`; an empty
      or ``None`` list disables this arm. The list is a parameter (not read
      from config here) so the predicate stays pure + overridable: an
      integration test forces a band wave by passing an explicit token.

    Args:
        wave: The wave being closed. Read-only.
        bands: The UI/UX band tokens for the token arm. ``None`` or empty
            disables the token arm; the structural ``file_scopes`` arm still
            applies.

    Returns:
        ``True`` when the wave's file_scopes are UI surface OR a band token
        matches a whole word of the wave id or title.
    """
    if is_ui_scope(wave.file_scopes):
        return True
    if not bands:
        return False
    corpus = f"{wave.id}\n{wave.title}".lower()
    for token in bands:
        if not token:
            continue
        # Word-boundary match so a token cannot arm on a substring embedded
        # in a larger word (``"ui"`` must not match ``"build"`` / ``"quiz"``).
        if re.search(rf"\b{re.escape(token.lower())}\b", corpus):
            return True
    return False


def _per_item_criteria(result: PerItemJuryResult) -> list[CriterionVerdict]:
    """Project per-item verdicts onto auditor :class:`CriterionVerdict` rows.

    One row per rubric item, keyed by the item id, ``passed`` true only when
    the item's reduced outcome is ``PASS``. A ``FAIL`` (veto) or
    ``NEEDS_USER`` (unresolved split) item is a non-pass criterion so the
    written report names exactly which rubric item the jury could not clear.
    """
    rows: list[CriterionVerdict] = []
    for item in result.items:
        rows.append(
            CriterionVerdict(
                criterion=item.item_id,
                passed=item.outcome is JuryAggregateOutcome.PASS,
            )
        )
    return rows


def _summary_for(wave_id: str, result: PerItemJuryResult) -> str:
    """Render the one-line spec-jury summary stamped on the auditor body."""
    failed = result.failed_item_ids
    detail = f" failed=[{', '.join(failed)}]" if failed else ""
    return (
        f"spec-jury wave={wave_id} outcome={result.outcome.value} items={len(result.items)}{detail}"
    )[:4000]


def _build_body(wave_id: str, result: PerItemJuryResult) -> AuditorReportBody:
    """Build the per-item :class:`AuditorReportBody` from a reduced result.

    The verdict is mapped from the wave-level fold (:data:`_OUTCOME_VERDICT`);
    the per-item verdicts become the body's ``criteria`` rows; the cited
    refutations across all vetoed items become the body's ``refutations`` so
    the operator sees why a rubric item failed without re-reading the
    ballots.
    """
    verdict = _OUTCOME_VERDICT[result.outcome]
    refutations = [ref for item in result.items for ref in item.refutations]
    return AuditorReportBody(
        verdict=verdict,
        confidence=Confidence.HIGH,
        summary=_summary_for(wave_id, result),
        target_id=wave_id,
        criteria=_per_item_criteria(result),
        refutations=refutations,
    )


def _per_item_output_contract(juror: str, rubric: tuple[WaveBehavior, ...]) -> str:
    """Render the per-item ballot output contract appended to the juror prompt.

    The shared :func:`build_auditor_prompt` already carries the refute-first
    rubric block; this contract narrows the OUTPUT to a single
    :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot` keyed
    to *juror*, with exactly one ``votes`` row per rubric item id. A vote names
    the item id, a boolean ``passed``, and -- on a ``passed=False`` vote -- a
    ``refutation`` so the veto is cited.

    Args:
        juror: The runtime family id stamped on the ballot's ``juror`` field.
        rubric: The ordered jury-scorable behaviours the ballot must vote on.

    Returns:
        The ``## Output contract`` block to append to the per-item prompt.
    """
    item_ids = ", ".join(behavior.id for behavior in rubric)
    return (
        "## Output contract\n"
        "\n"
        "Respond with ONLY a single JSON object that is a per-item ballot. It MUST\n"
        f'carry `"juror": "{juror}"` and a `"votes"` array with exactly one entry\n'
        f"per rubric item id ({item_ids}). Each vote MUST carry an `item_id`, a\n"
        "boolean `passed`, and -- when `passed` is false -- a `refutation` string\n"
        "citing why the item fails. No prose, no code fences."
    )


async def _drive_one_juror_ballot(
    *,
    base_prompt: str,
    juror: str,
    rubric: tuple[WaveBehavior, ...],
    spawn_factory: SpawnFactory,
    max_attempts: int,
) -> PerItemJurorBallot | None:
    """Drive one disjoint juror runtime to a parsed per-item ballot, or abstain.

    Binds *juror*'s spawn via *spawn_factory* and drives it through a bounded
    re-ask loop: each spawn's ``text`` is JSON-decoded and validated against
    :func:`~eawf.observability.eval.cross_vendor_jury.parse_per_item_ballot`. On
    a clean parse the validated ballot is returned. On a schema mismatch
    (unparseable JSON or a :class:`pydantic.ValidationError`) the loop records a
    typed failure, re-prompts with a correction notice naming it, and spawns
    again -- up to *max_attempts* total spawns. Any failure (the bound spawn
    raising, or the loop exhausting without a schema-valid ballot) converts into
    an abstention (``None``) so one vendor's outage does not crash the producer,
    mirroring the cross-vendor convener's abstention contract.

    Args:
        base_prompt: The refute-first per-item auditor prompt (already carrying
            the rubric block) shared across jurors.
        juror: The runtime family this juror spawns on (one of
            :data:`~eawf.observability.eval.cross_vendor_jury.JURY_RUNTIME_FAMILIES`).
        rubric: The ordered jury-scorable behaviours the ballot votes on.
        spawn_factory: Per-runtime spawn factory; called once to bind *juror*'s
            own vendor spawn.
        max_attempts: Bounded re-ask ceiling for this juror.

    Returns:
        The validated :class:`PerItemJurorBallot`, or ``None`` when the juror
        abstained (spawn raised or the loop exhausted).
    """
    spawn = spawn_factory(juror)
    prompt = f"{base_prompt}\n\n{_per_item_output_contract(juror, rubric)}"
    failures: list[SchemaAttemptFailure] = []
    current_prompt = prompt
    for attempt in range(1, max_attempts + 1):
        try:
            result = await spawn(current_prompt)
        except Exception as exc:
            logger.warning(
                f"_drive_one_juror_ballot juror={juror!r} attempt={attempt} status=abstained "
                f"reason={type(exc).__name__}"
            )
            return None
        try:
            decoded = json.loads(result.text)
            ballot = parse_per_item_ballot(decoded)
        except (json.JSONDecodeError, ValidationError) as exc:
            reason = "invalid_json" if isinstance(exc, json.JSONDecodeError) else "schema_mismatch"
            failures.append(
                SchemaAttemptFailure(attempt=attempt, reason=reason, detail=f"{exc}"[:2000])
            )
            current_prompt = (
                f"{prompt}\n\n## Output correction required\n\n"
                f"Your previous response (attempt {attempt}) was rejected: {reason}. "
                "Respond with ONLY a single JSON object that validates against the "
                "per-item ballot schema. No prose, no code fences."
            )
            continue
        logger.info(
            f"_drive_one_juror_ballot juror={juror!r} attempt={attempt} status=voted "
            f"votes={len(ballot.votes)}"
        )
        return ballot
    logger.warning(
        f"_drive_one_juror_ballot juror={juror!r} status=exhausted attempts={max_attempts}"
    )
    return None


def live_per_item_ballot_fn(
    *,
    spawn_factory: SpawnFactory,
    rubric: tuple[WaveBehavior, ...],
    runtimes: tuple[str, ...] = JURY_RUNTIME_FAMILIES,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
) -> PerItemBallotFn:
    """Bind the LIVE per-item ballot fn for the spec jury.

    Returns a :data:`PerItemBallotFn` the producer drives: when invoked with the
    refute-first per-item prompt it convenes one disjoint juror per family in
    *runtimes*, driving each through the bounded re-ask loop
    (:func:`_drive_one_juror_ballot`) to parse one :class:`PerItemJurorBallot`.
    Jurors are convened one at a time and each reads only the shared prompt --
    never another juror's ballot (independence by construction; no peer channel).
    An abstaining juror (runtime unavailable or its loop exhausted) contributes
    no ballot, so the reduction runs over the jurors that did vote, exactly as
    the cross-vendor convener's quorum contract intends.

    The *spawn_factory* is the testability seam: production binds each runtime's
    adapter spawn (reusing the daemon's ``_jury_spawn_factory``); a test binds a
    recording stub returning canned ballot text, so no real subprocess runs.

    Args:
        spawn_factory: Per-runtime spawn factory bound once per juror. Production
            passes the same factory the cross-vendor jury uses.
        rubric: The ordered jury-scorable behaviours each juror votes on. The
            output contract is keyed to these item ids.
        runtimes: The runtime families to convene one juror from each of.
            Defaults to the three disjoint families.
        max_attempts: Bounded re-ask ceiling forwarded to each juror.

    Returns:
        The live :data:`PerItemBallotFn` the producer awaits with the per-item
        auditor prompt.
    """

    async def _fn(prompt: str) -> tuple[PerItemJurorBallot, ...]:
        ballots: list[PerItemJurorBallot] = []
        for juror in runtimes:
            ballot = await _drive_one_juror_ballot(
                base_prompt=prompt,
                juror=juror,
                rubric=rubric,
                spawn_factory=spawn_factory,
                max_attempts=max_attempts,
            )
            if ballot is not None:
                ballots.append(ballot)
        logger.info(
            f"live_per_item_ballot_fn runtimes={len(runtimes)} voted={len(ballots)} "
            f"abstained={len(runtimes) - len(ballots)}"
        )
        return tuple(ballots)

    return _fn


async def produce_spec_jury_verdict(
    *,
    state: State,
    state_path: Path,
    wave: Wave,
    spec: WaveSpec | None,
    auditor_session_id: str,
    per_item_ballot_fn: PerItemBallotFn | None = None,
    block_authority: BlockAuthority = BlockAuthority.ADVISORY,
    evidence_block: str | None = None,
    runtime: str = "claude-code",
    repo_root: Path | None = None,
    now: datetime | None = None,
) -> SpecJuryResult:
    """Produce + persist a per-rubric-item spec-jury verdict for *wave*.

    The ordered steps, each a single-responsibility seam:

    1. **Idle guard.** When *per_item_ballot_fn* is ``None`` the producer is
       idle: it returns a ``"skipped"`` :class:`SpecJuryResult` and writes
       nothing (the close proceeds unchanged). The live rung binds the ballot
       fn (:func:`live_per_item_ballot_fn`); a test binds a recording stub.
    2. **Rubric load.** Project the wave's :class:`~eawf.kernel.spec.wave.WaveSpec`
       to its jury-scorable behaviours
       (:func:`eawf.kernel.spec.rubric.rubric_items`). A missing spec or an
       empty rubric is the safe-skip path: a wave with nothing to score has
       nothing to veto, so the producer returns ``"skipped"`` rather than
       blocking a banded close on an authoring gap.
    3. **Prompt + ballots.** Render the refute-first per-item auditor prompt
       (:func:`eawf.workflow.dispatch.verdict.build_auditor_prompt` with
       ``include_diff=False`` so the verdict grounds in the supplied
       *evidence_block*, not the raw diff), then invoke the injected ballot
       fn to collect one
       :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot`
       per juror.
    4. **Reduce.** Reduce the ballots per rubric item
       (:func:`eawf.observability.eval.cross_vendor_jury.reduce_per_item_ballots`)
       scoped to the rubric's item ids.
    5. **Write.** Map the reduced wave-level outcome onto an
       :class:`~eawf.kernel.store.kinds.agent_report.AuditorReportBody`
       (verdict from the fold, one ``criteria`` row per item, cited
       refutations) and append it through the canonical writer
       (:func:`eawf.workflow.agent_report.store.append_agent_report`) at
       ``base_id=wave.id``.
    6. **Advisory-until-blocking gate.** A non-close-ready verdict (FAIL /
       BLOCKED) is held ADVISORY by default -- the verdict is written for the
       operator but the producer does NOT raise. Only when *block_authority*
       is :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`
       (the jury has earned blocking authority on eawf's own distribution via
       the TRUST-4 computation the daemon threads in) does a non-close-ready
       verdict raise :class:`LifecycleError` and block the close.

    Args:
        state: Loaded, validated state -- supplies the author session for the
            canonical writer. Not mutated here.
        state_path: Path to ``state.json``; the auditor report store resolves
            under its sibling ``store/`` directory.
        wave: The banded wave under audit.
        spec: The wave's deliverable spec, or ``None`` when no spec is
            attached (a safe-skip).
        auditor_session_id: Id of a fresh AUDITOR session that authors the
            verdict. The canonical writer rejects a role mismatch, so the
            caller is responsible for resolving an auditor session.
        per_item_ballot_fn: Injected async ballot callback. ``None`` keeps
            the producer idle (the live rung binds it via
            :func:`live_per_item_ballot_fn`).
        block_authority: Whether a non-close-ready per-item verdict may BLOCK
            the close (:attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`,
            the verdict raises :class:`LifecycleError`) or is held merely
            advisory (:attr:`~eawf.observability.eval.jury_validation.BlockAuthority.ADVISORY`,
            the default, the verdict is written but the close proceeds). The
            daemon close path computes the earned authority and passes it in.
        evidence_block: Optional provenance-pinned evidence text the prompt
            carries under an ``## Evidence`` heading. ``None`` omits the
            section.
        runtime: Runtime adapter id recorded on the written report. Defaults
            to ``"claude-code"``.
        repo_root: Repository root forwarded to the diff-base derivation for
            the prompt header. ``None`` falls back to the process cwd.
        now: Optional fixed timestamp for the written report (tests pin it).

    Returns:
        A :class:`SpecJuryResult`. ``status="skipped"`` (verdict ``None``)
        when the producer is idle or the rubric is empty; ``status="scored"``
        with the reduced verdict when the jury convened. A scored
        non-close-ready verdict still returns ``"scored"`` under ADVISORY
        authority (the close proceeds); under BLOCKING authority it raises
        instead of returning.

    Raises:
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When
            *auditor_session_id* does not resolve to an AUDITOR session.
        ValueError: When a ballot votes on a rubric item id absent from the
            rubric (propagated from
            :func:`reduce_per_item_ballots` -- a malformed ballot).
        LifecycleError: When the reduced verdict is non-close-ready (FAIL /
            BLOCKED) AND *block_authority* is
            :attr:`~eawf.observability.eval.jury_validation.BlockAuthority.BLOCKING`
            -- the calibrated jury blocks the close.
    """
    if per_item_ballot_fn is None:
        logger.info(f"produce_spec_jury_verdict wave={wave.id} status=skipped reason=idle")
        return SpecJuryResult(
            wave_id=wave.id,
            status="skipped",
            reason="no per-item ballot fn injected (idle contract)",
        )

    rubric = rubric_items(spec) if spec is not None else ()
    if not rubric:
        reason = "no wave spec" if spec is None else "no jury-scorable behaviour in spec"
        logger.info(f"produce_spec_jury_verdict wave={wave.id} status=skipped reason={reason!r}")
        return SpecJuryResult(wave_id=wave.id, status="skipped", reason=reason)

    diff_base = derive_diff_base(wave.id, repo_root=repo_root)
    prompt = build_auditor_prompt(
        wave,
        diff_base=diff_base,
        rubric=rubric,
        evidence_block=evidence_block,
        include_diff=False,
    )
    ballots = tuple(await per_item_ballot_fn(prompt))
    # Persist the UN-reduced ballots BEFORE the lossy reduction so the raw
    # FindingSets survive on every jury fire for Plan-Jury calibration.
    findingset_id = persist_juror_findingsets(state_path, wave=wave, ballots=ballots, now=now)
    rubric_item_ids = tuple(behavior.id for behavior in rubric)
    result = reduce_per_item_ballots(ballots, rubric_item_ids)

    body = _build_body(wave.id, result)
    append_result = append_agent_report(
        state=state,
        state_path=state_path,
        session_id=auditor_session_id,
        base_id=wave.id,
        body=body,
        runtime=runtime,
        generated_at=now,
    )
    verdict = _OUTCOME_VERDICT[result.outcome]
    logger.info(
        f"produce_spec_jury_verdict wave={wave.id} status=scored "
        f"outcome={result.outcome.value} verdict={verdict.value!r} "
        f"items={len(result.items)} attempt={append_result.attempt}"
    )
    # The staged advisory-to-block gate (TRUST-4 / TRUST-5). A non-close-ready
    # verdict (FAIL veto, or BLOCKED split / unscored item) BLOCKS the close
    # only when the caller passes BLOCKING authority -- a calibrated jury that
    # has cleared its trust floors raises LifecycleError so the close is refused.
    # Under ADVISORY authority (the default, an uncalibrated jury) the verdict
    # is written for the operator and the close proceeds; the veto is preserved
    # in the persisted report so an audit still sees it.
    close_ready = {AgentReportVerdict.PASS, AgentReportVerdict.PASS_WITH_FOLLOWUPS}
    if verdict not in close_ready and block_authority is BlockAuthority.BLOCKING:
        failed = result.failed_item_ids
        detail = f"failed items=[{', '.join(failed)}]" if failed else "no failed items recorded"
        logger.warning(
            f"produce_spec_jury_verdict wave={wave.id} jury_veto_blocking "
            f"verdict={verdict.value!r} authority=blocking close_blocked=True"
        )
        raise LifecycleError(
            f"spec jury vetoed close: wave={wave.id!r} verdict={verdict.value} ({detail})"
        )
    return SpecJuryResult(
        wave_id=wave.id,
        status="scored",
        verdict=verdict,
        result=result,
        append_result=append_result,
        findingset_id=findingset_id,
    )


__all__ = [
    "SPEC_JURY_FINDINGSET_EVENT_TYPE",
    "PerItemBallotFn",
    "SpecJuryResult",
    "SpecJuryStatus",
    "live_per_item_ballot_fn",
    "persist_juror_findingsets",
    "produce_spec_jury_verdict",
    "wave_in_uiux_band",
]
