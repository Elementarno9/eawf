"""Spec-jury close-gate flavour for UI/UX-banded waves (P29-I08-W05).

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

Idle-contract (mirrors the W08 clarity judge + the W04 EviBound rung-3
ballot seam). The live multi-juror rung is DEFERRED behind an INJECTED
:data:`PerItemBallotFn` callback: the producer is wired + tested, but the
live model is bound at runtime, not spawned here. When the callback is
``None`` the producer is **idle** -- it returns a typed
:class:`SpecJuryResult` with ``status="skipped"`` and writes nothing, so a
banded close with no ballot fn proceeds exactly as it does today. When the
callback is supplied the producer convenes the jury and the close gate
consults the reduced verdict. Nothing in this module spawns a subprocess.

Band-scoped enforcement. The predicate :func:`wave_in_uiux_band` decides
whether a wave routes through the producer; it reads the active profile's
:attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands` token list and
returns ``False`` when the list is empty (the v0.5 default until the
band-population wave ships the real tokens). The predicate takes the band
list as a parameter so it is overridable -- an integration test forces a
band wave by passing an explicit token.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal

from eawf.kernel.spec.heuristics import is_ui_scope
from eawf.kernel.spec.rubric import rubric_items
from eawf.kernel.spec.wave import WaveSpec
from eawf.kernel.state.enums import (
    AgentReportVerdict,
    Confidence,
)
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.kinds.agent_report import AuditorReportBody, CriterionVerdict
from eawf.observability.eval.cross_vendor_jury import (
    PerItemJurorBallot,
    PerItemJuryResult,
    reduce_per_item_ballots,
)
from eawf.observability.eval.jury import JuryAggregateOutcome
from eawf.workflow.agent_report.store import (
    AgentReportAppendResult,
    append_agent_report,
)
from eawf.workflow.dispatch.verdict import build_auditor_prompt
from eawf.workflow.lifecycle.wave_sha import derive_diff_base

logger = logging.getLogger(__name__)

#: An injected async callback that convenes the per-item jury for one wave
#: and returns one :class:`~eawf.observability.eval.cross_vendor_jury.PerItemJurorBallot`
#: per juror. Injected (never imported) so this module spawns nothing: the
#: deferred live rung binds a spawn-then-validate adapter (drive each juror's
#: runtime through the bounded re-ask loop and parse a per-item ballot from
#: its output); a test binds a recording stub returning canned ballots. The
#: single ``str`` argument is the refute-first per-item auditor prompt from
#: :func:`build_auditor_prompt`.
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
        reason: Short human reason a run was skipped, or ``None`` when scored.
    """

    wave_id: str
    status: SpecJuryStatus
    verdict: AgentReportVerdict | None = None
    result: PerItemJuryResult | None = None
    append_result: AgentReportAppendResult | None = None
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
    * **Token** -- any token in *bands* appears (case-insensitively) as a
      substring of the wave id or title. The band list is the active
      profile's :attr:`~eawf.platform.profiles.models.VerifyBlock.uiux_bands`;
      an empty or ``None`` list disables this arm. The list is a parameter
      (not read from config here) so the predicate stays pure + overridable:
      an integration test forces a band wave by passing an explicit token.

    Args:
        wave: The wave being closed. Read-only.
        bands: The UI/UX band tokens for the token arm. ``None`` or empty
            disables the token arm; the structural ``file_scopes`` arm still
            applies.

    Returns:
        ``True`` when the wave's file_scopes are UI surface OR a band token
        matches the wave id or title.
    """
    if is_ui_scope(wave.file_scopes):
        return True
    if not bands:
        return False
    corpus = f"{wave.id}\n{wave.title}".lower()
    return any(token.lower() in corpus for token in bands if token)


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


async def produce_spec_jury_verdict(
    *,
    state: State,
    state_path: Path,
    wave: Wave,
    spec: WaveSpec | None,
    auditor_session_id: str,
    per_item_ballot_fn: PerItemBallotFn | None = None,
    evidence_block: str | None = None,
    runtime: str = "claude-code",
    repo_root: Path | None = None,
    now: datetime | None = None,
) -> SpecJuryResult:
    """Produce + persist a per-rubric-item spec-jury verdict for *wave*.

    The ordered steps, each a single-responsibility seam:

    1. **Idle guard.** When *per_item_ballot_fn* is ``None`` the producer is
       idle: it returns a ``"skipped"`` :class:`SpecJuryResult` and writes
       nothing (the close proceeds unchanged). This is the deferred
       live-rung contract -- the rung is wired + tested here; the live model
       is injected at runtime.
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
            the producer idle (the deferred live-rung contract).
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
        with the reduced verdict when the jury convened.

    Raises:
        eawf.workflow.agent_report.store.AgentReportRoleMismatchError: When
            *auditor_session_id* does not resolve to an AUDITOR session.
        ValueError: When a ballot votes on a rubric item id absent from the
            rubric (propagated from
            :func:`reduce_per_item_ballots` -- a malformed ballot).
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
    return SpecJuryResult(
        wave_id=wave.id,
        status="scored",
        verdict=verdict,
        result=result,
        append_result=append_result,
    )


__all__ = [
    "PerItemBallotFn",
    "SpecJuryResult",
    "SpecJuryStatus",
    "produce_spec_jury_verdict",
    "wave_in_uiux_band",
]
