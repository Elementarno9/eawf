"""Post-execution verify gate for the daemon dispatch runner (P28-I03-W57).

The dispatch runner's pre-W57 close path accepts an executor report
unconditionally — the runner emits the typed ``agent_end`` envelope and
returns. That left an open seam: a report whose verdict is
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.FAIL` or
:attr:`~eawf.kernel.state.enums.AgentReportVerdict.BLOCKED` could still
drive a wave-close path forward, defeating the v0.4 verify spine.

This module owns the **deterministic** close-readiness check the
runner consults after the subagent returns and before the wave can
move toward CLOSED. The check is pure: it inspects only the typed
:class:`~eawf.kernel.store.kinds.agent_report.AgentReportBody` and
returns a :class:`VerifyResult`. The runner converts a
``VerifyResult(passed=False, ...)`` into a fail-fast
:class:`DispatchCloseBlockedError` so the close path stops at the
first verifiable failure rather than silently accepting an unverified
attempt.

The gate is intentionally narrow: it does **not** re-run the readiness
projection (:func:`eawf.workflow.verify.readiness.compute`); that is the
operator-facing wave-close advisory. This gate is the daemon-side
runtime invariant that an executor report MUST claim success before the
runner reports the dispatch as close-ready.
"""

from __future__ import annotations

import ast
import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, NoReturn

from eawf.kernel.state.enums import AgentReportVerdict
from eawf.kernel.store.kinds.agent_report import (
    AgentReportBody,
    ExecutorReportBody,
)

if TYPE_CHECKING:
    from eawf.kernel.state.models import State, Wave
    from eawf.kernel.store.kinds.evidence import EvidenceRecord

logger = logging.getLogger(__name__)

#: Verdicts the runner treats as close-ready. ``PASS`` is a clean
#: dispatch; ``PASS_WITH_FOLLOWUPS`` carries follow-ups the operator
#: tracks but does not block on (e.g. a V5 runtime fallback).
_CLOSE_READY_VERDICTS: frozenset[AgentReportVerdict] = frozenset(
    {
        AgentReportVerdict.PASS,
        AgentReportVerdict.PASS_WITH_FOLLOWUPS,
    }
)


@dataclass(frozen=True)
class VerifyResult:
    """Outcome of :func:`verify_close_readiness`.

    Attributes:
        passed: ``True`` iff every check fired clean. ``False`` blocks
            the close path; the runner raises
            :class:`DispatchCloseBlockedError` with the :attr:`reasons`
            so the operator sees the precise refusal.
        verdict: The report verdict the check inspected; surfaced so
            callers can log / report the precise failure mode without
            re-reading the report.
        reasons: One short string per failing check. Empty on a clean
            pass. Reasons stay terse so they fit in a single log line
            and a CLI error message.
    """

    passed: bool
    verdict: AgentReportVerdict
    reasons: tuple[str, ...] = field(default_factory=tuple)


class DispatchCloseBlockedError(RuntimeError):
    """Raised by the dispatch runner when the verify gate refuses close.

    Carries the :class:`VerifyResult` so callers (and tests) can
    inspect the structured refusal alongside the human-readable
    message. The ``wave_id`` attribute names the wave whose close was
    blocked.
    """

    def __init__(self, *, wave_id: str, result: VerifyResult) -> None:
        self.wave_id = wave_id
        self.result = result
        reasons = "; ".join(result.reasons) if result.reasons else "no reasons recorded"
        super().__init__(
            f"verify gate blocked dispatch close for wave {wave_id!r}: "
            f"verdict={result.verdict.value} reasons=[{reasons}]"
        )


def evidence_rung_inputs(state: object, wave_id: str, *, repo_root: Path) -> tuple[int, bool]:
    """Resolve rung-4 inputs for *wave_id*: (typed_criteria_count, teeth bit).

    The shared seam both production callers (the dispatch runner's
    post-execution gate and the fleet clean-close probe) thread through
    :func:`verify_close_readiness`, so the evidence-refs rung reads one
    definition of "typed criterion" (``kind != legacy``) and one teeth
    bit (the resolved verify block's ``enforce``). Resolution failures
    degrade to ``(0, False)`` — rung 4 stays dormant rather than
    inventing enforcement the profile did not grant.
    """
    waves = getattr(state, "waves", {}) or {}
    wave = waves.get(wave_id)
    if wave is None:
        return 0, False
    typed = sum(
        1
        for criterion in (wave.success_criteria or [])
        if getattr(criterion, "kind", "legacy") != "legacy"
    )
    if typed == 0:
        return 0, False
    try:
        from eawf.workflow.verify.readiness import (
            load_active_verify_block,
            resolve_wave_verify_block,
        )

        block = resolve_wave_verify_block(
            load_active_verify_block(wave_id, state, repo_root=repo_root),  # type: ignore[arg-type]
            wave,
        )
    except (OSError, ValueError, KeyError) as exc:
        logger.warning(f"evidence_rung_inputs status=skip wave={wave_id} err={exc!s}")
        return typed, False
    return typed, bool(block is not None and block.enforce)


def verify_close_readiness(
    wave_id: str,
    report: AgentReportBody,
    *,
    typed_criteria_count: int = 0,
    require_evidence_refs: bool = False,
) -> VerifyResult:
    """Return a :class:`VerifyResult` for *report* against *wave_id*.

    The deterministic check has four rungs, evaluated in order:

    1. The report verdict MUST be a member of
       :data:`_CLOSE_READY_VERDICTS` (``PASS`` or
       ``PASS_WITH_FOLLOWUPS``). A ``FAIL`` or ``BLOCKED`` verdict is
       the most common refusal and stops the gate immediately.
    2. The report body's ``summary`` MUST be non-empty after
       stripping whitespace — every typed report body inherits the
       1..4000-char summary contract from
       :class:`~eawf.kernel.store.kinds.agent_report.AgentReportCommonBody`,
       but a whitespace-only summary slips past the min-length check
       and renders as a blank executor report. The gate refuses it.
    3. For an :class:`ExecutorReportBody`, the body's ``wave_id``
       MUST equal the dispatched *wave_id*. A mismatch means the
       executor reported on a different wave than the runner served,
       which is a fail-fast inconsistency.

    The gate intentionally does **not** require a ``commit_sha``: the
    runner falls back to the serving attempt id when the executor
    omits one (see
    :func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`).

    4. When *require_evidence_refs* is set (the teeth-wave profile bit)
       and the wave carries at least one typed criterion, the report's
       ``evidence_refs`` MUST be non-empty — one entry per criterion is
       the contract the executor DoD demands; an empty list on a
       criteria-bearing wave refuses close-ready (W49).

    Args:
        wave_id: The wave the runner served. Compared against
            :attr:`ExecutorReportBody.wave_id` for the executor path.
        report: The typed ``AgentReportBody`` the runner persisted.
        typed_criteria_count: Number of typed (``kind != legacy``)
            success criteria on the dispatched wave; ``0`` (the
            default) keeps rung 4 dormant for zero-criteria waves.
        require_evidence_refs: The teeth-wave profile bit (the resolved
            verify block's ``enforce``); ``False`` keeps rung 4 off so
            advisory repos and legacy callers are unchanged.

    Returns:
        :class:`VerifyResult` with ``passed`` reflecting every check.
        A failure surfaces every reason that fired (not just the
        first), so the operator sees the full picture in one log line.
    """
    reasons: list[str] = []
    verdict = report.verdict

    if verdict not in _CLOSE_READY_VERDICTS:
        reasons.append(f"verdict={verdict.value} not in close-ready set")

    if not report.summary.strip():
        reasons.append("report summary is blank")

    if isinstance(report, ExecutorReportBody) and report.wave_id != wave_id:
        reasons.append(
            f"executor body wave_id={report.wave_id!r} disagrees with dispatched wave={wave_id!r}"
        )

    # Rung 4 (W49, tightened per the W35 review): a criteria-bearing wave
    # must carry ONE evidence_ref per typed criterion once the teeth bit
    # (verify.enforce) is on — the same contract the executor DoD states.
    # A wave with zero typed criteria stays exempt.
    if require_evidence_refs and typed_criteria_count >= 1:
        refs = list(getattr(report, "evidence_refs", None) or [])
        if len(refs) < typed_criteria_count:
            reasons.append(
                f"evidence_refs carries {len(refs)} entr(y/ies) for a wave "
                f"with {typed_criteria_count} typed criteria (one per criterion required)"
            )

    passed = not reasons
    result = VerifyResult(
        passed=passed,
        verdict=verdict,
        reasons=tuple(reasons),
    )
    logger.info(
        f"verify_close_readiness wave={wave_id} passed={passed} "
        f"verdict={verdict.value} reasons={len(reasons)}"
    )
    return result


@dataclass(frozen=True)
class CloseGateResult:
    """Outcome of running a wave's deterministic close gates -- P30-I23-W19.

    :func:`verify_close_readiness` decides whether an executor report is
    close-ready from the report alone; it never runs the wave's own gates.
    On the fleet clean-close path that let a wave carrying a
    ``command_exit_zero`` gate flip to CLOSED on the agent's self-report
    without the command ever running (the A5 critical). This result carries
    the verdict of actually running those deterministic gates so the caller
    can decide between closing the lane and routing it to the repair/fork
    ladder.

    Attributes:
        passed: ``True`` iff every required deterministic-gated criterion
            passed its deterministic gate (or the wave carries no
            deterministic gate at all). ``False`` routes the lane to the
            repair/fork ladder instead of closing.
        evidence: The ``deterministic`` / ``pass`` :class:`EvidenceRecord`
            rows minted on a clean pass, each bound to the wave. Empty on a
            gate refusal and on a gateless wave.
        failing_criterion_id: The criterion whose deterministic gate
            refused, or ``None`` on a pass.
        failing_detail: The grounded falsifier the refusing gate produced,
            fed to the repair ladder so a re-dispatch is grounded in the
            concrete check output. Empty string on a pass.
    """

    passed: bool
    evidence: list[EvidenceRecord] = field(default_factory=list)
    failing_criterion_id: str | None = None
    failing_detail: str = ""


class FloorFailureClass(StrEnum):
    """How a refused deterministic close-gate should be routed -- P30-I25-W04.

    The bounded grounded-repair ladder re-dispatches a lane on any refused
    close-gate. That is correct for a refusal the executor can FIX (a real
    assertion failure, a lint error in changed code, a failing test the agent
    wrote) but wrong for one it cannot: when a spawn runs in a bare smoke repo
    (a fresh ``eawf init`` with no ``.pre-commit-config.yaml``, no package dir,
    no pytest dependency) the deterministic floor gates fail for ENVIRONMENTAL
    reasons the executor cannot create in-scope, so a re-dispatch burns attempts
    on an unfixable lane. This class labels the two cases so the fleet close-gate
    seam routes an environmental refusal to close-with-followups and keeps the
    repair ladder for executor-fixable refusals.
    """

    ENVIRONMENTAL = "environmental"
    EXECUTOR_FIXABLE = "executor_fixable"


#: The repo config file the ``pre-commit`` floor gate needs; a bare smoke repo
#: has none and the executor cannot scaffold it in-scope.
_PRECOMMIT_CONFIG = ".pre-commit-config.yaml"

#: Regex that recovers the argv list a ``command_exit_zero`` check records in
#: its detail (``argv=['uv', 'run', 'pre-commit', ...] returncode=1``).
_ARGV_DETAIL_RE = re.compile(r"argv=(\[[^\]]*\])")


def _detail_argv(failing_detail: str) -> list[str]:
    """Return the argv vector recorded in a ``command_exit_zero`` failing detail.

    A deterministic gate records its detail as ``argv=[...] returncode=N``; this
    recovers the argv list so the classifier can key off which floor command
    refused. A detail with no recognizable argv (a timeout string, a jury
    detail) yields an empty list, which the classifier treats as
    executor-fixable -- an unrecognized falsifier keeps the repair ladder.

    Args:
        failing_detail: The grounded falsifier the refusing gate produced.

    Returns:
        The recorded argv tokens, or ``[]`` when the detail carries no
        parseable ``argv=[...]`` fragment.
    """
    match = _ARGV_DETAIL_RE.search(failing_detail)
    if match is None:
        return []
    try:
        parsed = ast.literal_eval(match.group(1))
    except ValueError, SyntaxError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(token) for token in parsed]


def _mypy_targets(argv: list[str]) -> list[str]:
    """Return the non-flag path targets handed to a ``mypy`` floor gate.

    The python-profile floor pack runs ``mypy src/`` so its target IS the
    package dir; a target absent from the repo means the executor has no package
    dir to type-check and cannot create one in-scope. A non-mypy argv yields
    ``[]``.
    """
    if "mypy" not in argv:
        return []
    idx = argv.index("mypy")
    return [token for token in argv[idx + 1 :] if not token.startswith("-")]


def _pytest_declared(repo_root: Path) -> bool:
    """Return whether *repo_root*'s ``pyproject.toml`` declares the pytest dependency.

    A bare smoke repo either has no ``pyproject.toml`` or does not declare
    pytest, so ``uv run pytest`` fails for a missing test dependency the
    executor cannot add in-scope. The substring probe is deliberately coarse --
    any pytest mention (dependency, optional-dependency, or config table) counts
    as declared.
    """
    pyproject = repo_root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    try:
        return "pytest" in pyproject.read_text(encoding="utf-8")
    except OSError:
        return False


def classify_floor_failure(
    *,
    failing_detail: str,
    repo_root: Path,
) -> FloorFailureClass:
    """Classify a refused deterministic close-gate -- P30-I25-W04.

    Pure function of the gate's grounded falsifier + the repo scaffolding: it
    reads *failing_detail* to learn which floor command refused, then checks
    *repo_root* for the scaffolding that command depends on. A floor failure is
    ENVIRONMENTAL when the failing command needs repo scaffolding the executor
    cannot create in-scope:

    - the ``pre-commit`` gate + no :data:`_PRECOMMIT_CONFIG` at *repo_root*;
    - the ``mypy`` gate + a target package dir absent from *repo_root*;
    - the ``pytest`` gate + no pytest dependency declared in ``pyproject.toml``.

    Everything else (a real assertion failure, a lint error in changed code, a
    failing test the agent wrote, an unrecognized falsifier) is
    EXECUTOR_FIXABLE, so the repair ladder keeps its existing behaviour.

    Args:
        failing_detail: The refusing gate's grounded falsifier
            (:attr:`CloseGateResult.failing_detail`); carries the ``argv=[...]``
            the deterministic check recorded.
        repo_root: The repository root the floor gates ran against, checked for
            the scaffolding each floor command depends on.

    Returns:
        :attr:`FloorFailureClass.ENVIRONMENTAL` when the refusal is caused by
        missing scaffolding the executor cannot create in-scope, else
        :attr:`FloorFailureClass.EXECUTOR_FIXABLE`.
    """
    argv = _detail_argv(failing_detail)
    tokens = set(argv)
    if "pre-commit" in tokens and not (repo_root / _PRECOMMIT_CONFIG).is_file():
        return FloorFailureClass.ENVIRONMENTAL
    for target in _mypy_targets(argv):
        if not (repo_root / target).exists():
            return FloorFailureClass.ENVIRONMENTAL
    if "pytest" in tokens and not _pytest_declared(repo_root):
        return FloorFailureClass.ENVIRONMENTAL
    return FloorFailureClass.EXECUTOR_FIXABLE


class _JuryUnreachableError(RuntimeError):
    """Signal that the deterministic-only clean-close runner reached the jury tier.

    :func:`run_close_gates` scores only the deterministic oracle tier; a
    criterion that escalates past it has no deterministic falsifier, so the
    runner must NOT spawn a jury on the fleet drain path. The injected spawn
    factory raises this so the runner catches the escalation and defers to
    the DL-5 risk gate rather than convening a jury off the drain loop.
    """


def _no_jury_spawn_factory(_runtime: str) -> NoReturn:
    """A jury spawn factory that refuses to spawn -- the clean-close gate is deterministic-only.

    Args:
        _runtime: The runtime family the jury tier would bind. Unused -- the
            deterministic-only clean-close gate never convenes a jury.

    Raises:
        _JuryUnreachableError: Always -- reaching the jury tier from the
            deterministic-only clean-close runner is a defer signal, not a
            spawn request.
    """
    raise _JuryUnreachableError(
        "fleet clean-close gate runs the deterministic tier only; jury escalation is not permitted"
    )


async def run_close_gates(
    wave: Wave,
    *,
    state: State,
    state_path: Path,
    events_path: Path,
    repo_root: Path,
) -> CloseGateResult:
    """Run *wave*'s DETERMINISTIC close gates before a fleet clean-close -- W19.

    The fleet clean-close path flips a wave to CLOSED on the agent's own
    close-ready report (:func:`verify_close_readiness` checks only the report
    verdict + summary + wave-id). That trusts the self-report without ever
    running the wave's deterministic gates, so a wave carrying a
    ``command_exit_zero`` gate auto-closes without the command running. This
    runner closes that seam: it scores each REQUIRED deterministic-gated
    criterion through the shared ordered oracle
    (:func:`eawf.workflow.verify.oracle.run_oracle`), which the daemon close
    gate delegates to as well, so gate execution is reused rather than
    re-implemented.

    The runner is UNCONDITIONAL -- it does not consult ``verify.enforce``. A
    lane only reaches the clean-close branch after the DL-5 auto-close gate
    cleared it (a MECH / MED wave), and for such a wave a deterministic pass
    is the complete ground truth (DL-5), so its deterministic gates always
    run. Criteria with no deterministic gate are skipped: a MECH gateless
    wave keeps the status-flip close, and a MED wave's auditor verdict (the
    human sign-off the lane already carries) is not re-run here. A criterion
    that escalates past the deterministic tier (its gates yield no pass and
    no required-block failure) is deferred to the DL-5 risk gate -- the
    deterministic-only runner never convenes a jury on the drain path.

    Args:
        wave: The wave whose clean close is gated. Read-only here.
        state: Loaded, validated state forwarded to the oracle. The
            deterministic-only path never reaches the jury tier that would
            mutate it.
        state_path: Path to ``state.json``; stores resolve under its sibling
            ``store/`` directory.
        events_path: Path to ``event.jsonl``, forwarded to the oracle.
        repo_root: Repository root the deterministic checks run against.

    Returns:
        A :class:`CloseGateResult`: ``passed=True`` with the minted
        deterministic-pass evidence rows on a clean pass (empty for a
        gateless wave), or ``passed=False`` with the refusing criterion id +
        grounded falsifier on a deterministic gate refusal.
    """
    from eawf.kernel.store.kinds.evidence import deterministic_pass_record
    from eawf.workflow.verify.oracle import run_oracle
    from eawf.workflow.verify.readiness import _load_gate_specs

    gate_specs = _load_gate_specs(wave.id, state)
    evidence: list[EvidenceRecord] = []
    for criterion in wave.success_criteria:
        if not criterion.required or criterion.evidence_kind != "deterministic":
            continue
        gates = [g for g in gate_specs if g.criterion_id == criterion.id]
        if not gates:
            continue
        try:
            result = await run_oracle(
                criterion,
                gates,
                wave=wave,
                state=state,
                state_path=state_path,
                events_path=events_path,
                repo_root=repo_root,
                spawn_factory=_no_jury_spawn_factory,
            )
        except _JuryUnreachableError:
            # The deterministic tier did not resolve this criterion and it
            # escalated to the jury tier. The DL-5 risk gate governs jury
            # waves on the clean-close path, so defer rather than spawn.
            logger.debug(
                f"run_close_gates wave={wave.id} criterion={criterion.id!r} "
                "jury_escalation_deferred"
            )
            continue
        if result.status != "pass":
            if result.gate_id is None:
                # A non-deterministic (verdict / jury) tier refusal is not
                # this runner's concern -- the DL-5 risk gate already governs
                # jury / auditor waves on the clean-close path. Defer.
                logger.debug(
                    f"run_close_gates wave={wave.id} criterion={criterion.id!r} "
                    f"nondeterministic_refusal_deferred tier={int(result.tier)}"
                )
                continue
            logger.warning(
                f"run_close_gates wave={wave.id} criterion={criterion.id!r} "
                f"gate={result.gate_id!r} tier={int(result.tier)} status={result.status} blocked"
            )
            return CloseGateResult(
                passed=False,
                failing_criterion_id=result.criterion_id,
                failing_detail=result.failing_detail(),
            )
        if result.gate_id is not None:
            evidence.append(
                deterministic_pass_record(
                    scope_id=wave.id,
                    criterion_id=result.criterion_id,
                    gate_id=result.gate_id,
                    tier=int(result.tier),
                    detail=result.detail,
                )
            )
    logger.info(
        f"run_close_gates wave={wave.id} passed=True deterministic_evidence={len(evidence)}"
    )
    return CloseGateResult(passed=True, evidence=evidence)


__all__ = [
    "CloseGateResult",
    "DispatchCloseBlockedError",
    "FloorFailureClass",
    "VerifyResult",
    "classify_floor_failure",
    "run_close_gates",
    "verify_close_readiness",
]
