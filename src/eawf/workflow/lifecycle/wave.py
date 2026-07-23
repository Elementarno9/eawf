"""Pure-functional wave lifecycle transitions.

Plan / edit / remove / set-deps / claim / close / fail for :class:`Wave`,
plus the DAG cycle check and the monotonic claim-order helper. Every helper
mutates the supplied :class:`State` in place. See
:mod:`eawf.workflow.lifecycle.transitions` for the shared design rules and the
re-export surface that keeps ``eawf.workflow.lifecycle.transitions`` import paths
working after the per-entity split.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from eawf.kernel.config.schema import EuBasis, VerifyWaiverMode
from eawf.kernel.spec.common import CriterionSpec
from eawf.kernel.spec.intent import IntentBrief, has_authoring_body
from eawf.kernel.state.enums import (
    ActualStatus,
    AgentSessionRole,
    EffortBucket,
    IterStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import (
    ActualSummary,
    CriteriaFloorWaiver,
    RuntimeBaseline,
    RuntimeCarry,
    RuntimeLatest,
    State,
    Wave,
)
from eawf.observability.telemetry.join import _duration_ms_to_eu, _tokens_to_eu
from eawf.workflow.estimation.buckets import default_estimate_summary
from eawf.workflow.lifecycle._capacity import DEFAULT_MAX_PARALLEL_WAVES
from eawf.workflow.lifecycle._claim_guards import (
    active_wave_ids,
    validate_claim_capacity,
    validate_claim_criteria,
    validate_claim_parent,
)
from eawf.workflow.lifecycle._claim_session import validate_claim_session as validate_claim_session
from eawf.workflow.lifecycle._errors import (
    LifecycleError,
    check_criteria_floor,
    check_criteria_measurability,
    check_disabled_waiver_policy,
    check_title_clarity,
)
from eawf.workflow.lifecycle.iter_ import _apply_iter_activation
from eawf.workflow.lifecycle.spec import (
    WAVE_TRANSITIONS,
    GuardContext,
    GuardName,
    validate_transition,
)

if TYPE_CHECKING:
    from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)

#: Every per-class token counter a runtime snapshot carries.
_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)

#: The token classes that count as WORK. Cache reads are excluded: they re-count
#: the same cached context on every request, so their volume tracks session
#: position and context size rather than effort. They remain billed (they land in
#: ``actual_cost_usd``) and stay visible per-class on the runtime snapshots.
_WORK_TOKEN_FIELDS: tuple[str, ...] = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
)


def _claim_session_counters(runtime_session_id: str) -> RuntimeCounters | None:
    """Return a vendor runtime session's cumulative counters, when readable.

    The transcript is the primary source. The statusline runtime-counter
    sidecar stays a fallback for an operator whose statusline is
    ``eawf statusline`` but whose transcript does not resolve. Callers must
    supply a vendor runtime session id explicitly; an EAWF
    :class:`AgentSession` id never enters this lookup.

    Args:
        runtime_session_id: Vendor session id used to resolve both the runtime
            transcript and its session-keyed statusline cache.

    Returns:
        The session's cumulative :class:`RuntimeCounters`, or ``None`` when
        neither the transcript nor the sidecar yields any.
    """
    from eawf.runtime.runtime_counter_sidecar import (
        RuntimeCounterSidecar,
        sidecar_path_for_statusline_cache,
    )
    from eawf.runtime.runtimes.claude.statusline import cache_path_for
    from eawf.runtime.runtimes.claude.transcript_counters import (
        aggregate_transcript_counters,
        transcript_path_for_session,
    )

    transcript = transcript_path_for_session(runtime_session_id, cwd=Path.cwd())
    counters = aggregate_transcript_counters(transcript)
    if counters is not None:
        return counters
    sidecar = RuntimeCounterSidecar(
        sidecar_path_for_statusline_cache(cache_path_for(runtime_session_id))
    )
    return sidecar.read()


def _capture_runtime_baseline(runtime_session_id: str) -> RuntimeBaseline | None:
    """Return a claim-time snapshot for an explicit vendor runtime session.

    Converts the session's cumulative counters (see
    :func:`_claim_session_counters`) into a baseline stamped at claim time.
    Returns ``None`` when the session exposes no counters at all -- neither a
    readable transcript nor a statusline sidecar -- so the close-time delta
    degrades to "no captured runtime" rather than subtracting against a phantom
    zero baseline.

    Args:
        runtime_session_id: Vendor runtime session id whose counters the
            baseline snapshots. This is never an EAWF ``AgentSession.id``.
    """
    counters = _claim_session_counters(runtime_session_id)
    if counters is None:
        return None
    return RuntimeBaseline(
        api_duration_ms=counters.api_duration_ms,
        total_duration_ms=counters.total_duration_ms,
        cost_usd=float(counters.cost_usd) if counters.cost_usd is not None else None,
        input_tokens=counters.input_tokens,
        output_tokens=counters.output_tokens,
        cache_creation_input_tokens=counters.cache_creation_input_tokens,
        cache_read_input_tokens=counters.cache_read_input_tokens,
        harness=counters.harness,
        model=counters.model,
        session_id=runtime_session_id,
        measure_version=counters.measure_version,
        captured_at=datetime.now(UTC),
    )


@dataclass(frozen=True)
class RuntimeDelta:
    """Close-time runtime delta derived from baseline and latest counters.

    ``actual_tokens`` deliberately EXCLUDES prompt-cache reads. A cache read
    re-counts the same cached context on every request, so its volume tracks how
    far into a session the wave sits (and how big the context has grown by then)
    rather than how much work the wave did: the same wave claimed late in a long
    session reads millions more cached tokens than it would have claimed first.
    Counting that as effort makes the figure useless for calibration. The cache
    reads are still real spend, so ``actual_cost_usd`` bills them, and the
    per-class tallies stay on the runtime snapshots for the cost surfaces.

    Every counter below is the wave's SHARE of the session it was captured in --
    the raw delta divided by :attr:`shared_wave_count`. See
    :func:`shared_wave_divisor`.

    Attributes:
        elapsed_eu: Measured effort units on the configured EU basis.
        agent_runtime_eu: The agent-runtime EU (equal to ``elapsed_eu``; see the
            note in :func:`compute_runtime_delta`).
        actual_tokens: New tokens the wave burned -- input + output + cache
            writes, excluding cache reads.
        actual_cost_usd: Priced spend for the wave, cache reads included.
        api_duration_ms: Measured agent runtime in milliseconds.
        input_tokens: Non-cached input-token delta.
        output_tokens: Output-token delta.
        cache_creation_input_tokens: Prompt-cache write delta.
        cache_read_input_tokens: Prompt-cache read delta (billed, not counted as
            work in ``actual_tokens``).
        shared_wave_count: The divisor applied -- how many waves shared the
            session these counters were captured in. ``1`` when the wave had the
            session to itself. Carried on the delta so the split is auditable
            rather than a silent halving.
    """

    elapsed_eu: float
    agent_runtime_eu: float
    actual_tokens: int
    actual_cost_usd: float
    api_duration_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    shared_wave_count: int = 1


def _counter_delta(
    field_name: str,
    baseline_value: int | float | None,
    latest_value: int | float | None,
) -> int | float | None:
    """Return a non-negative counter delta, or ``None`` when either side is absent.

    A backwards counter used to RAISE here, which stranded the wave: the baseline
    lives on disk, so every retry of the close compared against it and raised
    again, and no operator action could clear it. (P30-I25 hit this for real --
    W34 changed the duration basis while waves were claimed against baselines
    recorded under the old one, making them permanently unclosable.)

    The capture path re-origins on a regressed counter so this should not arise;
    when it does anyway, degrade to a zero delta and say so loudly. A wave that
    under-reports its runtime is a bad measurement. A wave that can never close is
    a broken workflow.
    """
    if baseline_value is None or latest_value is None:
        return None
    delta = latest_value - baseline_value
    if delta < 0:
        logger.warning(
            f"_counter_delta field={field_name} baseline={baseline_value!r} "
            f"latest={latest_value!r} delta=0 status='regressed'; "
            "counter source reset or basis changed -- recording no runtime for it"
        )
        return 0
    return delta


def shared_wave_divisor(
    baseline: RuntimeBaseline | None,
    latest: RuntimeLatest | None,
) -> int:
    """Return how many waves shared the session these snapshots were captured in.

    One ``runtime.capture`` writes the same session snapshot to EVERY active wave,
    and each wave then differences the whole session. Without a divisor, N
    concurrent waves each record the session's entire runtime and cost: the
    runtime is real, but it is one session's, counted N times. (P30-I25 recorded
    exactly that -- two fan-out waves both closed on 0.3769 EU / $3.10, four more
    all closed on 0.0419 EU.)

    The snapshots each carry the concurrency the capture saw, and this takes the
    LARGEST of them. When the concurrency changed mid-span the exact split is not
    recoverable -- the counters are cumulative, not per-interval -- so the choice
    is which way to err. The largest count under-credits a wave that spent part of
    its life alone; the smallest hands one session's runtime to several waves
    again. Under-crediting is the honest error, and a wave whose runtime was
    shared is marked excluded from calibration anyway.

    Args:
        baseline: The wave's claim-time (or re-originated) snapshot.
        latest: The wave's freshest capture.

    Returns:
        The divisor -- at least ``1``, which is what an unshared session, a
        headless single-wave spawn, and a pre-v1.18 snapshot all resolve to.
    """
    counts = [
        snapshot.shared_wave_count
        for snapshot in (baseline, latest)
        if snapshot is not None and snapshot.shared_wave_count is not None
    ]
    return max(counts) if counts else 1


def _shared_share(
    delta: int | float | None,
    divisor: int,
) -> int | float | None:
    """Return the wave's share of a counter *delta* captured across *divisor* waves."""
    if delta is None or divisor <= 1:
        return delta
    return delta / divisor


def _with_carry(
    delta: int | float | None,
    carried: int | float,
) -> int | float | None:
    """Fold prior-session *carried* runtime into this session's *delta*.

    A wave that spanned earlier sessions carries their finished totals in
    :class:`~eawf.kernel.state.models.RuntimeCarry`; the close-time figure is the
    current session's delta plus that carry. A counter this session never
    reported (``None``) still resolves to the carry when there is one, so a wave
    whose work happened entirely in a previous session does not lose it.
    """
    if delta is None:
        return carried if carried else None
    return delta + carried


def compute_runtime_delta(
    baseline: RuntimeBaseline | None,
    latest: RuntimeLatest | None,
    *,
    carry: RuntimeCarry | None = None,
    eu_minutes: float,
    eu_basis: EuBasis = EuBasis.API_DURATION,
) -> RuntimeDelta | None:
    """Return captured runtime delta between claim baseline and latest counters.

    ``eu_basis`` selects which captured quantity derives elapsed EU. Missing
    baseline / latest / chosen-basis counters mean there is no captured runtime
    to apply, so the delta is ``None``. Counters that moved backwards do NOT
    reject the close: :func:`_counter_delta` clamps each to a zero delta and
    warns, so a wave whose counter source reset stays closable (the capture path
    re-origins the baseline on a regressed counter, so it should not arise here).

    ``carry`` folds in the runtime a multi-session wave already spent in sessions
    that have ended (the daemon rebases the baseline onto each new session's
    origin and accumulates the finished session's total there), so the close-time
    figure is *this* session's delta plus every earlier session's total.

    This session's delta is the wave's SHARE of the session: divided by however
    many waves were active when its counters were captured
    (:func:`shared_wave_divisor`), because one capture is written to every active
    wave and each would otherwise difference -- and record -- the whole session.
    The carry needs no division here: the daemon divides each finished session's
    total as it folds it, so the carry already holds this wave's share of it.

    Args:
        baseline: Claim-time counter snapshot; ``None`` means nothing was
            captured at claim, so there is no origin to difference against.
        latest: Freshest counter snapshot captured while the wave was active.
        carry: Totals already folded in from the wave's finished sessions, or
            ``None`` when the wave never spanned one.
        eu_minutes: Minutes represented by one effort unit.
        eu_basis: Which captured quantity derives ``elapsed_eu``.

    Returns:
        The :class:`RuntimeDelta`, or ``None`` when no runtime was captured --
        an absent baseline / latest, or no counter on the chosen basis.

    Raises:
        LifecycleError: When *eu_minutes* is not positive, or *eu_basis* is not
            a known :class:`~eawf.kernel.config.schema.EuBasis` member.
    """
    if baseline is None or latest is None:
        return None
    if eu_minutes <= 0.0:
        raise LifecycleError(f"eu_minutes must be positive: {eu_minutes!r}")

    divisor = shared_wave_divisor(baseline, latest)
    api_duration_ms = _with_carry(
        _shared_share(
            _counter_delta("api_duration_ms", baseline.api_duration_ms, latest.api_duration_ms),
            divisor,
        ),
        carry.api_duration_ms if carry is not None else 0,
    )
    total_duration_ms = _with_carry(
        _shared_share(
            _counter_delta(
                "total_duration_ms", baseline.total_duration_ms, latest.total_duration_ms
            ),
            divisor,
        ),
        carry.total_duration_ms if carry is not None else 0,
    )
    cost_usd_delta = _with_carry(
        _shared_share(_counter_delta("cost_usd", baseline.cost_usd, latest.cost_usd), divisor),
        carry.cost_usd if carry is not None else 0.0,
    )
    tokens: dict[str, int] = {}
    for field_name in _TOKEN_FIELDS:
        value = _with_carry(
            _shared_share(
                _counter_delta(
                    field_name,
                    getattr(baseline, field_name),
                    getattr(latest, field_name),
                ),
                divisor,
            ),
            getattr(carry, field_name) if carry is not None else 0,
        )
        if value is not None:
            tokens[field_name] = int(value)

    # Cache reads are billed but are NOT work: the same context is re-read on
    # every request, so the tally scales with session position, not effort.
    token_delta = sum(tokens.get(field, 0) for field in _WORK_TOKEN_FIELDS)

    elapsed_eu = _runtime_basis_eu(
        eu_basis,
        api_duration_ms=int(api_duration_ms) if api_duration_ms is not None else None,
        total_duration_ms=int(total_duration_ms) if total_duration_ms is not None else None,
        token_delta=token_delta if tokens else None,
        eu_minutes=eu_minutes,
    )
    if elapsed_eu is None:
        return None
    # elapsed_eu is the AGENT-RUNTIME basis (the model's api/total duration or a
    # token-derived estimate via _runtime_basis_eu), NOT the claim->close
    # wall-clock. This is intentional: a wave's effort is the agent's work, not
    # the operator's elapsed time, so elapsed_eu deliberately equals
    # agent_runtime_eu here -- do not "fix" it to a wall-clock delta.
    return RuntimeDelta(
        elapsed_eu=elapsed_eu,
        agent_runtime_eu=elapsed_eu,
        actual_tokens=token_delta,
        actual_cost_usd=float(cost_usd_delta) if cost_usd_delta is not None else 0.0,
        api_duration_ms=int(api_duration_ms) if api_duration_ms is not None else 0,
        input_tokens=tokens.get("input_tokens", 0),
        output_tokens=tokens.get("output_tokens", 0),
        cache_creation_input_tokens=tokens.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=tokens.get("cache_read_input_tokens", 0),
        shared_wave_count=divisor,
    )


def runtime_is_calibration_excluded(wave: Wave) -> bool:
    """Return whether this wave's measured runtime disqualifies it as calibration data.

    The row is still an honest record of what was captured -- it is just not a
    reference class, and a consumer must be able to see that from state rather
    than from a document. Two things disqualify it:

    * **The counters were re-originated.** A counter reset (a truncated
      transcript, a changed measure) drops the runtime measured before it, and it
      cannot be re-derived. What the wave closes on is a floor, not a measure.
    * **The session was shared.** One capture is written to every active wave, so
      a wave that shared its session closes on a SPLIT of one session's counters
      (:func:`shared_wave_divisor`), and the split is an approximation whenever
      the concurrency moved mid-span.

    Args:
        wave: The wave being closed.

    Returns:
        ``True`` when the wave's runtime was re-originated or shared.
    """
    resets = wave.runtime_carry.counter_resets if wave.runtime_carry is not None else 0
    shared = shared_wave_divisor(wave.runtime_baseline, wave.runtime_latest)
    return resets > 0 or shared > 1


def _runtime_basis_eu(
    eu_basis: EuBasis,
    *,
    api_duration_ms: int | None,
    total_duration_ms: int | None,
    token_delta: int | None,
    eu_minutes: float,
) -> float | None:
    """Convert the selected runtime basis into effort units.

    Returns:
        The basis quantity in EU, or ``None`` when the counter backing the
        selected basis was never captured.

    Raises:
        LifecycleError: When *eu_basis* is not a known :class:`EuBasis` member.
    """
    if eu_basis is EuBasis.API_DURATION:
        return _duration_ms_to_eu(api_duration_ms, eu_minutes=eu_minutes)
    if eu_basis is EuBasis.TOKENS:
        return _tokens_to_eu(token_delta)
    if eu_basis is EuBasis.WALL_CLOCK:
        return _duration_ms_to_eu(total_duration_ms, eu_minutes=eu_minutes)
    raise LifecycleError(f"unknown eu_basis: {eu_basis!r}")


def plan_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    title: str,
    file_scopes: list[str],
    deps: list[str] | None = None,
    success_criteria: list[CriterionSpec] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
    criteria_floor_waiver: CriteriaFloorWaiver | None = None,
    waiver_mode: VerifyWaiverMode = "B",
) -> Wave:
    """Insert a new wave with status ``pending``.

    Args:
        state: State to mutate in place.
        wave_id: Canonical wave id (e.g. ``P03-I02-W04``).
        iter_id: Parent iter id.
        title: Bounded ≤72-char wave title.
        file_scopes: File globs the wave is scoped to.
        deps: Optional list of prerequisite wave ids.
        success_criteria: Optional list of typed
            :class:`~eawf.kernel.spec.common.CriterionSpec` rows. Callers that
            hold free-form operator strings wrap each via
            :func:`~eawf.kernel.spec.common.grandfather_criterion` first.
        agent_role: Optional executor role.
        effort_bucket: Required XS/S/M/L/XL estimate bucket.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Wave.description` for downstream renderers.
        intent: Required typed :class:`IntentBrief` attaching the goal /
            motivation / success-signal + evidence + source-brief refs
            that motivated the wave. The signature default stays ``None``
            so a caller that omits it hits the authoring guard below; the
            persisted field is additive + replay-safe so on-disk state
            written before the field existed re-validates.

    Raises:
        LifecycleError: if iter is missing/closed, wave id duplicates, any
            declared dep references a missing wave id, the dep set names
            the wave itself, ``effort_bucket`` is missing, ``intent`` is
            missing, ``intent`` has an empty body (blank
            ``priority_rationale`` plus empty ``planned_steps``,
            ``risks``, and ``source_brief_ids``), the resulting graph
            would contain a cycle, or any non-legacy success criterion is
            unmeasurable (EAWF021).

    Side-effects on the reverse-index: for every ``dep`` in *deps*, the
    dep wave's ``blocks`` list is mutated in-place to include *wave_id*
    (idempotent — already-present ids are not duplicated).
    """
    candidate_criteria = list(success_criteria or [])
    check_disabled_waiver_policy(
        waiver_mode=waiver_mode,
        scope_id=wave_id,
        criteria=candidate_criteria,
        criteria_floor_waiver=criteria_floor_waiver,
    )
    it = state.iters.get(iter_id)
    if it is None:
        raise LifecycleError(f"unknown iter {iter_id!r}")
    if it.status not in {IterStatus.PLANNED, IterStatus.ACTIVE}:
        raise LifecycleError(f"iter {iter_id!r} is not open (status={it.status.value!r})")
    if wave_id in state.waves:
        raise LifecycleError(f"wave {wave_id!r} already exists")
    deps_list = list(deps or [])
    if wave_id in deps_list:
        raise LifecycleError(f"wave {wave_id!r} cannot depend on itself")
    if effort_bucket is None:
        raise LifecycleError(
            f"wave {wave_id!r} has no effort_bucket; set --effort-bucket before planning"
        )
    if intent is None:
        raise LifecycleError(
            f"wave {wave_id!r} has no intent; attach an IntentBrief before planning"
        )
    # Body-completeness is an authoring-only guard: a freshly authored brief
    # carrying only problem + desired_outcome (blank rationale, no steps, no
    # risks, no source brief) is rejected here so the planner says why the wave
    # earned its slot. A source-brief-derived brief answers by reference, so it
    # passes. The model stays permissive (no validator) so legacy on-disk briefs
    # with empty body fields still re-validate at load / replay.
    if not has_authoring_body(intent):
        raise LifecycleError(
            f"wave {wave_id!r} intent is empty; give it a priority_rationale, "
            "planned steps, or risks"
        )
    for dep in deps_list:
        if dep not in state.waves:
            raise LifecycleError(f"unknown dep wave {dep!r}")
    # Cycle check: simulate the post-insert DAG over deps and topo-sort.
    # The new wave depends on every entry of ``deps_list``; existing
    # waves keep their current ``deps``. If toposort fails any node
    # remains unprocessed -> cycle. Self-dep is already rejected above.
    if _would_create_cycle(state, new_id=wave_id, new_deps=deps_list):
        raise LifecycleError(f"adding wave {wave_id!r} with deps={deps_list} would create a cycle")
    # Title-clarity is the final author-facing gate before persistence, so the
    # structural DAG guards (dup id, self-dep, unknown dep, cycle) report first.
    check_title_clarity(title, entity_kind="wave", entity_id=wave_id)
    # Measurability runs before the wave is inserted so an unmeasurable typed
    # criterion is rejected at author time rather than slipping onto the row and
    # failing only at the close gate. Grandfathered legacy rows are exempt.
    check_criteria_measurability(candidate_criteria, entity_kind="wave", entity_id=wave_id)
    # The typed-criteria floor rejects legacy-string rows and gateless
    # deterministic claims at author time; a typed waiver bypasses it and
    # is persisted on the wave row so the bypass stays visible.
    check_criteria_floor(
        candidate_criteria,
        entity_kind="wave",
        entity_id=wave_id,
        waiver=criteria_floor_waiver,
    )
    wave = Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title,
        description=description,
        status=WaveStatus.PENDING,
        deps=deps_list,
        blocks=[],
        file_scopes=list(file_scopes),
        success_criteria=candidate_criteria,
        agent_role=agent_role,
        effort_bucket=effort_bucket,
        claim_session_id=None,
        worktree_id=None,
        outcome=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
        intent=intent,
        criteria_floor_waiver=criteria_floor_waiver,
    )
    state.waves[wave_id] = wave
    if wave_id not in it.wave_ids:
        it.wave_ids.append(wave_id)
    # Maintain the reverse "blocks" index: every dep gains the new wave
    # in its blocks list (idempotent).
    for dep in deps_list:
        dep_wave = state.waves[dep]
        if wave_id not in dep_wave.blocks:
            dep_wave.blocks.append(wave_id)
    logger.info(f"plan_wave id={wave_id} iter={iter_id} files={file_scopes} deps={deps_list}")
    return wave


def _would_create_cycle(
    state: State,
    *,
    new_id: str,
    new_deps: list[str],
) -> bool:
    """Return True iff inserting *new_id* with *new_deps* yields a cycle.

    Kahn-style topo sort over the union {existing waves' deps,
    new_id -> new_deps}. If any node remains unprocessed after the
    sweep there is at least one cycle reachable from / containing it.
    """
    # adjacency: child_id -> set of parent dep ids (edges parent -> child)
    deps_by_node: dict[str, set[str]] = {wid: set(w.deps) for wid, w in state.waves.items()}
    deps_by_node[new_id] = set(new_deps)
    # in_degree: incoming-edge count per node (an edge dep -> wave for each dep)
    in_degree: dict[str, int] = {node: len(parents) for node, parents in deps_by_node.items()}
    # children index: parent_id -> list of waves that list it as a dep
    children: dict[str, list[str]] = {node: [] for node in deps_by_node}
    for node, parents in deps_by_node.items():
        for parent in parents:
            # Skip dangling dep ids that were already rejected by the
            # caller's "unknown dep" guard — guard against KeyError if a
            # call site sneaks past that check.
            if parent in children:
                children[parent].append(node)
    ready = [node for node, count in in_degree.items() if count == 0]
    visited = 0
    while ready:
        node = ready.pop()
        visited += 1
        for child in children[node]:
            in_degree[child] -= 1
            if in_degree[child] == 0:
                ready.append(child)
    return visited != len(deps_by_node)


def edit_wave_plan(
    state: State,
    *,
    wave_id: str,
    title: str | None = None,
    file_scopes: list[str] | None = None,
    success_criteria: list[CriterionSpec] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
    criteria_floor_waiver: CriteriaFloorWaiver | None = None,
    waiver_mode: VerifyWaiverMode = "B",
) -> Wave:
    """Mutate a PENDING wave's plan-time fields. Rejects non-PENDING waves.

    Editable surface: title, file_scopes, success_criteria, agent_role,
    effort_bucket, description, intent. Dep mutations go through
    :func:`set_wave_deps` (it maintains the reverse ``blocks`` index and
    re-runs the cycle check). The description field is routed through the
    model's assignment validator so the ≤500-character bound is re-checked
    on edit; an over-cap value raises :class:`pydantic.ValidationError`.

    The PENDING gate is also the load-bearing invariant for the
    ACTIVE-phase revise path (P19-W12): the CLI gate accepts an ACTIVE
    parent phase, and this guard ensures only PENDING waves under it
    actually mutate while CLOSED/CLAIMED/IN_PROGRESS waves stay frozen.

    Args:
        state: State to mutate in place.
        wave_id: Canonical wave id.
        title: Optional replacement title; ``None`` leaves it untouched.
        file_scopes: Optional replacement file globs; ``None`` leaves untouched.
        success_criteria: Optional replacement typed
            :class:`~eawf.kernel.spec.common.CriterionSpec` rows; ``None``
            leaves untouched.
        agent_role: Optional replacement role; ``None`` leaves untouched.
        effort_bucket: Optional replacement bucket; ``None`` leaves untouched.
        description: Optional replacement description (≤500 chars);
            ``None`` leaves the existing value untouched.
        intent: Optional replacement :class:`IntentBrief`; ``None``
            leaves the existing intent untouched (callers cannot clear
            an intent through this helper — the asymmetry mirrors the
            description / title API).

    Raises:
        LifecycleError: when *wave_id* is unknown, not PENDING, or a
            replacement non-legacy success criterion is unmeasurable
            (EAWF021).
        pydantic.ValidationError: when *description* exceeds 500 chars
            or *intent* violates an :class:`IntentBrief` bound.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot edit plan"
        )
    candidate_criteria = list(success_criteria) if success_criteria is not None else []
    if success_criteria is not None or criteria_floor_waiver is not None:
        check_disabled_waiver_policy(
            waiver_mode=waiver_mode,
            scope_id=wave_id,
            criteria=candidate_criteria,
            criteria_floor_waiver=(
                criteria_floor_waiver
                if criteria_floor_waiver is not None
                else wave.criteria_floor_waiver
            ),
        )
    if success_criteria is not None:
        # Gate the new criteria before any field mutates so a rejected edit
        # leaves the wave untouched. Grandfathered legacy rows are exempt
        # from measurability but NOT from the typed-criteria floor.
        check_criteria_measurability(list(success_criteria), entity_kind="wave", entity_id=wave_id)
        check_criteria_floor(
            list(success_criteria),
            entity_kind="wave",
            entity_id=wave_id,
            waiver=criteria_floor_waiver
            if criteria_floor_waiver is not None
            else wave.criteria_floor_waiver,
        )
    if title is not None:
        wave.title = title
    if file_scopes is not None:
        wave.file_scopes = list(file_scopes)
    if success_criteria is not None:
        wave.success_criteria = list(success_criteria)
    if agent_role is not None:
        wave.agent_role = agent_role
    if effort_bucket is not None:
        wave.effort_bucket = effort_bucket
    if description is not None:
        wave.__pydantic_validator__.validate_assignment(wave, "description", description)
    if intent is not None:
        wave.__pydantic_validator__.validate_assignment(wave, "intent", intent)
    if criteria_floor_waiver is not None:
        wave.criteria_floor_waiver = criteria_floor_waiver
    intent_problem = repr(intent.problem) if intent is not None else None
    logger.info(
        f"edit_wave_plan id={wave_id} title={title!r} file_scopes={file_scopes} "
        f"agent_role={agent_role} effort_bucket={effort_bucket} description={description!r} "
        f"intent_problem={intent_problem}"
    )
    return wave


def remove_wave_plan(state: State, *, wave_id: str) -> None:
    """Delete a PENDING wave from state. Also strips reverse-index entries.

    Like :func:`edit_wave_plan`, the PENDING guard here is what makes
    ``eawf roadmap revise --remove-wave`` safe under an ACTIVE parent
    phase (P19-W12): CLOSED/CLAIMED/IN_PROGRESS waves never get
    removed regardless of the parent phase's status.

    Raises:
        LifecycleError: when *wave_id* is unknown or not PENDING.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot remove"
        )
    if wave.blocks:
        raise LifecycleError(
            f"wave {wave_id!r} blocks other waves {sorted(wave.blocks, key=natural_key)}; "
            "remove those first or break the dep"
        )
    for dep_id in wave.deps:
        dep_wave = state.waves.get(dep_id)
        if dep_wave is not None and wave_id in dep_wave.blocks:
            dep_wave.blocks.remove(wave_id)
    parent_iter = state.iters.get(wave.iter_id)
    if parent_iter is not None and wave_id in parent_iter.wave_ids:
        parent_iter.wave_ids.remove(wave_id)
    del state.waves[wave_id]
    logger.info(f"remove_wave_plan id={wave_id}")


def set_wave_deps(state: State, *, wave_id: str, deps: list[str]) -> Wave:
    """Replace a PENDING wave's deps. Maintains the reverse ``blocks`` index.

    Like the other ``*_wave_plan`` helpers, the PENDING guard is what
    keeps the ACTIVE-phase revise path (P19-W12) safe — only PENDING
    waves under an ACTIVE parent can have their dep set rewritten.

    Raises:
        LifecycleError: when *wave_id* is unknown, not PENDING, any dep
            id is unknown, self-dep, or the new graph would cycle.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(
            f"wave {wave_id!r} is not pending (status={wave.status.value!r}); cannot set deps"
        )
    new_deps = list(deps)
    if wave_id in new_deps:
        raise LifecycleError(f"wave {wave_id!r} cannot depend on itself")
    for dep in new_deps:
        if dep not in state.waves:
            raise LifecycleError(f"unknown dep wave {dep!r}")
    old_deps = list(wave.deps)
    wave.deps = new_deps
    try:
        if _would_create_cycle(state, new_id=wave_id, new_deps=new_deps):
            raise LifecycleError(f"set_wave_deps on {wave_id!r} with deps={new_deps} would cycle")
    except LifecycleError:
        wave.deps = old_deps
        raise
    for dep in old_deps:
        dep_wave = state.waves.get(dep)
        if dep_wave is not None and wave_id in dep_wave.blocks:
            dep_wave.blocks.remove(wave_id)
    for dep in new_deps:
        dep_wave = state.waves[dep]
        if wave_id not in dep_wave.blocks:
            dep_wave.blocks.append(wave_id)
    logger.info(f"set_wave_deps id={wave_id} deps={new_deps}")
    return wave


def claim_wave(
    state: State,
    *,
    wave_id: str,
    session_id: str,
    out_of_order: bool = False,
    max_parallel_waves: int = DEFAULT_MAX_PARALLEL_WAVES,
    waiver_mode: VerifyWaiverMode = "B",
) -> Wave:
    """Move a pending wave to ``claimed`` and bind it to *session_id*.

    Re-claiming an already-claimed wave with the *same* session is a no-op
    (idempotent). Re-claiming with a *different* session is rejected so the
    sibling-lock + status check delivers exactly-once semantics.

    *session_id* must name a live :class:`~eawf.kernel.state.models.AgentSession`
    row: :func:`validate_claim_session` runs immediately before the first
    mutation, so an unknown / non-ACTIVE / wrong-role / wrong-scope session
    rejects with a stable guard code and leaves the state untouched. A
    successful claim adds the wave to the session's ``claimed_wave_ids`` in
    the same mutation, so wave binding and session index are written together.

    On the first claim the wave's ``claimed_at`` work-start fact is
    stamped (``datetime.now(UTC)``). ``claimed_at`` is the
    anchor the elapsed-clock consumers use instead of ``opened_at``
    (plan/creation time), so a wave planned hours before it is claimed does
    not inflate its elapsed clock. An existing ``claimed_at`` is preserved on
    re-entry.

    P19-W02 dep + monotonic gates:

    - Reject the claim when any wave in ``wave.deps`` is not in
      :data:`WaveStatus.CLOSED`. Dependencies must land before a
      downstream wave can start.
    - Reject the claim when a sibling wave with a numerically lower
      ``W##`` is still PENDING under the same iter AND its own deps
      are already satisfied — this enforces the monotonic claim
      order that prevents parallel runtimes from skipping ahead.
    - ``out_of_order=True`` (CLI ``--out-of-order``) is the
      operator-blessed escape hatch for parallel-worktree dispatch
      where multiple waves of the same dep-frontier are intentionally
      claimed at once.

    Parent rows, not ``state.current`` pointers, define executability. The
    parent phase must be ACTIVE and the parent iter must be PLANNED or ACTIVE.
    A PLANNED iter runs the shared activation validator and is activated in the
    same mutation while preserving status-derived active-wave rows. Empty
    success criteria are rejected. Finally, the repository-wide count of
    CLAIMED + IN_PROGRESS waves must remain below *max_parallel_waves*;
    ``out_of_order`` relaxes sibling ordering only and never bypasses these
    lifecycle, criteria, session, pause, dependency, or capacity guards.

    Args:
        state: Mutable typed repository state.
        wave_id: Pending wave to claim.
        session_id: Active compatible session to bind.
        out_of_order: Whether to relax lower-ready-sibling ordering only.
        max_parallel_waves: Repository-wide CLAIMED + IN_PROGRESS hard cap.
        waiver_mode: Effective policy for persisted waiver mechanisms.

    Returns:
        The claimed wave row.

    Raises:
        LifecycleError: when *wave_id* is unknown, wave is not
            PENDING, ``effort_bucket`` is ``None``, dep waves are not
            CLOSED, a lower-numbered sibling-ready wave is still
            PENDING (without ``out_of_order``), or dispatch is paused
            (``state.dispatch_paused`` is ``True``) — the pause gate
            blocks regardless of ``out_of_order``.
        LifecycleGuardError: when a parent row is missing or not executable,
            another sibling iter conflicts with PLANNED autoactivation,
            criteria are empty, capacity is exhausted, or the claiming session
            is missing, inactive, role-incompatible, or scoped outside the
            wave's own scope chain.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    check_disabled_waiver_policy(
        waiver_mode=waiver_mode,
        scope_id=wave_id,
        criteria=list(wave.success_criteria),
        criteria_floor_waiver=wave.criteria_floor_waiver,
    )
    parent_iter = validate_claim_parent(state, wave)
    validate_claim_criteria(wave)
    if wave.status == WaveStatus.CLAIMED and wave.claim_session_id == session_id:
        # Idempotent re-entry still re-checks the binding: a wave claimed
        # hours ago whose session has since gone STALE must not be re-entered
        # (and then dispatched) on a dead session -- reuse is allowed only
        # while the bound session is still ACTIVE.
        validate_claim_session(state, wave, session_id)
        logger.debug(f"claim_wave idempotent id={wave_id} session={session_id}")
        return wave
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(f"wave {wave_id!r} cannot be claimed (status={wave.status.value!r})")
    if wave.effort_bucket is None:
        raise LifecycleError(
            f"wave {wave_id!r} has no effort_bucket; set one via "
            f"`eawf roadmap revise --set-bucket` before claiming"
        )
    # The pending -> claimed status move plus its named guards (deps-closed,
    # sibling-ordering, dispatch-not-paused) live in WAVE_TRANSITIONS now; the
    # booleans + the operator-facing failure messages are computed here (where
    # the wave id + sorted lists live) and evaluated by validate_transition in
    # the legacy deps -> sibling -> pause order. The pause gate is
    # unconditional: out_of_order satisfies sibling-ordering, not the pause.
    unmet_deps = [
        dep_id
        for dep_id in wave.deps
        if state.waves.get(dep_id) is None or state.waves[dep_id].status != WaveStatus.CLOSED
    ]
    skipped = _lower_w_sibling_pending(state, wave)
    guard_ctx = GuardContext(
        deps_closed=not unmet_deps,
        sibling_ordered=not skipped,
        out_of_order=out_of_order,
        not_paused=not state.dispatch_paused,
        messages={
            GuardName.DEPS_CLOSED: (
                f"wave {wave_id!r} blocked on un-closed dep waves: "
                f"{sorted(unmet_deps, key=natural_key)}"
            ),
            GuardName.SIBLING_ORDERED: (
                f"wave {wave_id!r} would skip lower-numbered ready siblings: "
                f"{sorted(skipped, key=natural_key)}; pass --out-of-order to claim regardless"
            ),
            GuardName.NOT_PAUSED: f"dispatch paused: resume before claiming {wave_id!r}",
        },
    )
    validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, guard_ctx)
    # Identity gate — the last check before the first mutation, so a rejected
    # claim leaves the state byte-identical (no status flip, no claimed_at
    # stamp, no active-wave pointer, no session index entry, no estimate row).
    session = validate_claim_session(state, wave, session_id)
    validate_claim_capacity(state, wave, max_parallel_waves=max_parallel_waves)
    if parent_iter.status is IterStatus.PLANNED:
        _apply_iter_activation(state, parent_iter, preserve_active_wave_ids=True)
        logger.info(
            f"claim_wave auto-activated iter={parent_iter.id} on first claim wave={wave_id}"
        )
    wave.status = WaveStatus.CLAIMED
    wave.claim_session_id = session_id
    # The session's claimed-wave index moves with the wave's own binding, in
    # the same in-memory mutation the caller persists in one write: the two
    # halves of the claim can never disagree on disk.
    if wave_id not in session.claimed_wave_ids:
        session.claimed_wave_ids = [*session.claimed_wave_ids, wave_id]
    # Stamp the work-start fact on the first claim only. opened_at is
    # plan/creation time; claimed_at is when work actually begins, so the
    # elapsed-clock consumers can anchor on it instead of inflating from
    # creation under plan-all-then-execute. Preserve an existing value so
    # a re-entry never re-bases the clock to a later wall-clock.
    if wave.claimed_at is None:
        wave.claimed_at = datetime.now(UTC)
    # No claim-time vendor-counter capture. ``session_id`` names an EAWF
    # AgentSession, NOT a Claude / Codex / OpenCode session, so resolving a
    # transcript or statusline sidecar by it read a foreign namespace and
    # stamped whatever happened to collide as this wave's origin. Until the
    # v0.7 schema adds ``runtime_session_id`` there is no honest mapping, and
    # absence is honest: the live-spawn path stamps a matched
    # baseline/latest pair of its own, and the interactive capture path
    # re-origins a missing baseline on its first capture.
    # Rebuild the advisory pointer from authoritative statuses on every
    # successful claim. This both records the new claimant and repairs stale
    # pointer rows without letting a stale pointer weaken the repo-wide cap.
    state.current.active_wave_ids = active_wave_ids(state)
    # Seed a default estimate from the wave's effort bucket so the
    # estimate-vs-actual variance metric has a baseline to compare the
    # close-time actual against. Skipped (no estimate) when the wave
    # carries no bucket — there is no centroid to derive from.
    estimate = default_estimate_summary(wave, now=datetime.now(UTC))
    if estimate is not None:
        if state.estimates is None:
            state.estimates = {}
        state.estimates[wave_id] = estimate
        logger.info(f"claim_wave seeded default estimate wave={wave_id} eu={estimate.expected_eu}")
    logger.info(f"claim_wave id={wave_id} session={session_id} out_of_order={out_of_order}")
    return wave


def _lower_w_sibling_pending(state: State, wave: Wave) -> list[str]:
    """Return PENDING sibling wave ids with a lower ``W##`` whose deps are CLOSED.

    A sibling shares ``wave.iter_id``. ``W##`` ordering uses the
    trailing two digits of the wave id. Returns an empty list when no
    such "ready and unclaimed" lower-numbered wave exists.
    """
    suffix = wave.id.split("-")[-1]
    if not (suffix.startswith("W") and suffix[1:].isdigit()):
        return []
    my_index = int(suffix[1:])
    skipped: list[str] = []
    for other_id, other in state.waves.items():
        if other.iter_id != wave.iter_id:
            continue
        other_suffix = other_id.split("-")[-1]
        if not (other_suffix.startswith("W") and other_suffix[1:].isdigit()):
            continue
        if int(other_suffix[1:]) >= my_index:
            continue
        if other.status != WaveStatus.PENDING:
            continue
        deps_met = all(
            state.waves.get(d) is not None and state.waves[d].status == WaveStatus.CLOSED
            for d in other.deps
        )
        if deps_met:
            skipped.append(other_id)
    return skipped


def start_wave(state: State, *, wave_id: str) -> Wave:
    """Move a claimed wave to ``in_progress`` at implementation start.

    The inline counterpart to the dispatch runner's head transition
    (:func:`eawf.runtime.daemon.dispatch_runner.run_dispatch`): a non-dispatched
    wave whose executor begins work flips from :data:`WaveStatus.CLAIMED`
    to :data:`WaveStatus.IN_PROGRESS` so the wave's status reflects that
    the claim has been picked up and code is being written. The wave stays
    on ``current.active_wave_ids`` (it was placed there by
    :func:`claim_wave`); only the status field changes.

    Re-starting an already-``in_progress`` wave is a no-op (idempotent) so
    a dispatched wave whose runner already flipped the status, and a retry
    that re-enters the start path, do not fault.

    Args:
        state: State to mutate in place.
        wave_id: Id of the claimed wave to move to ``in_progress``.

    Returns:
        The mutated :class:`~eawf.kernel.state.models.Wave`.

    Raises:
        LifecycleError: when *wave_id* is unknown, or the wave is in any
            status other than ``claimed``/``in_progress`` (a pending wave
            must be claimed first; a terminal wave cannot be re-started).
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status == WaveStatus.IN_PROGRESS:
        logger.debug(f"start_wave idempotent wave={wave_id} already in_progress")
        return wave
    # claimed -> in_progress is the only legal source for a start (the table
    # has no pending/terminal -> in_progress edge), so any non-claimed source
    # is rejected with the legacy "not claimed" message.
    validate_transition(
        WAVE_TRANSITIONS,
        wave.status,
        WaveStatus.IN_PROGRESS,
        illegal_message=(
            f"wave {wave_id!r} is not claimed (status={wave.status.value!r}); cannot start"
        ),
    )
    wave.status = WaveStatus.IN_PROGRESS
    logger.info(f"start_wave wave={wave_id}")
    return wave


def close_wave(
    state: State,
    *,
    wave_id: str,
    outcome: str,
    tokens_consumed: int | None = None,
    actual_attention_eu: float | None = None,
    actual_agent_runtime_eu: float | None = None,
    actual_elapsed_eu: float | None = None,
    actual_cost_usd: float | None = None,
) -> Wave:
    """Close a claimed/in-progress wave with an outcome string.

    Pure status flip + outcome stamp; the wave's commit SHA stays on
    ``Wave.commit`` when it was pinned via ``eawf wave close --commit
    <ref>`` (P19-W17 re-introduced the field as ``ShaStr | None`` and
    normalises any ref shape via ``git rev-parse``). When ``commit`` is
    left ``None``, callers fall back to
    :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`, which walks
    ``git log --grep '[P##-W##]'`` against the active branch.

    Telemetry handoff: the close path upserts :class:`ActualSummary` for
    *wave_id* in ``state.actuals`` carrying the wave's
    :attr:`Wave.tokens_consumed` tally on ``actual_tokens``. Existing
    summaries are operator-authored actuals; their effort fields are
    preserved and only token / cost / status fields refresh.
    The auto-created actual takes its measured figures from the caller: the
    daemon close path derives ``agent_runtime_eu``, ``elapsed_eu``, and
    ``actual_cost_usd`` from the wave's runtime baseline-to-latest delta
    (:func:`compute_runtime_delta`, whose token classes are priced through the
    per-model rate table), and falls back to the telemetry rollup's session
    ``duration_ms`` for ``elapsed_eu`` when no runtime snapshot was captured;
    ``attention_eu`` comes from that same rollup. The measured agent runtime is
    bounded effort — distinct from the open->close wall-clock span, which is
    NOT agent effort (it counts overnight, cross-session, and other-wave idle
    time, inflating consumed EU roughly tenfold) and is never substituted here.
    When the caller supplies no measured value the auto-created actual keeps the
    honest zero (``elapsed_eu=0.0``, ``actual_cost_usd=0.0``) rather than
    inventing one.
    The auto-created actual also carries the ``harness`` + ``model``
    attribution off the wave's latest runtime snapshot
    (:attr:`Wave.runtime_latest`, stamped by the daemon runtime-capture writer)
    so a recorded actual is calibratable by harness+model; both stay ``None``
    when no runtime was captured.

    The close path also marks the actual ``calibration_excluded`` when the wave's
    runtime was re-originated by a counter reset or shared across concurrent waves
    (:func:`runtime_is_calibration_excluded`). Such a row is an honest record of
    what was captured but is not a reference class, and the flag is what lets a
    consumer skip it reading state alone.

    Args:
        state: State to mutate in place.
        wave_id: Id of the claimed/in-progress wave to close.
        outcome: Human-readable outcome summary.
        tokens_consumed: Optional final token tally to persist on the
            wave before the close-time :class:`ActualSummary` upsert.
            ``None`` preserves the wave's existing accumulated tally.
        actual_attention_eu: Optional telemetry-derived attention effort
            for an auto-created :class:`ActualSummary`.
        actual_agent_runtime_eu: Optional telemetry-derived runtime effort
            for an auto-created :class:`ActualSummary`.
        actual_elapsed_eu: Optional telemetry-derived elapsed effort (the
            measured session runtime in EU) for an auto-created
            :class:`ActualSummary`. ``None`` leaves the auto-created
            ``elapsed_eu`` at ``0.0``.
        actual_cost_usd: Optional captured cost delta for an auto-created or
            refreshed :class:`ActualSummary`. ``None`` leaves the cost at the
            historical ``0.0`` default for new records and preserves existing
            cost on operator-authored records.

    Raises:
        LifecycleError: when *wave_id* is unknown, the wave is not
            claimable for close, or an actual value is negative.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    # claimed/in_progress -> closed are the only legal close edges; the table
    # has no pending/terminal -> closed edge, so a non-closable source raises
    # the legacy "not claimed/in_progress" message.
    validate_transition(
        WAVE_TRANSITIONS,
        wave.status,
        WaveStatus.CLOSED,
        illegal_message=(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot close"
        ),
    )
    if tokens_consumed is not None:
        if tokens_consumed < 0:
            raise LifecycleError(f"tokens_consumed must be non-negative; got {tokens_consumed}")
        wave.tokens_consumed = tokens_consumed
    if actual_attention_eu is not None and actual_attention_eu < 0.0:
        raise LifecycleError(f"actual_attention_eu must be non-negative; got {actual_attention_eu}")
    if actual_agent_runtime_eu is not None and actual_agent_runtime_eu < 0.0:
        raise LifecycleError(
            f"actual_agent_runtime_eu must be non-negative; got {actual_agent_runtime_eu}"
        )
    if actual_elapsed_eu is not None and actual_elapsed_eu < 0.0:
        raise LifecycleError(f"actual_elapsed_eu must be non-negative; got {actual_elapsed_eu}")
    if actual_cost_usd is not None and actual_cost_usd < 0.0:
        raise LifecycleError(f"actual_cost_usd must be non-negative; got {actual_cost_usd}")
    wave.status = WaveStatus.CLOSED
    wave.outcome = outcome
    now = datetime.now(UTC)
    wave.closed_at = now
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    # Upsert the ActualSummary so M26 + the wave_closed event payload
    # carry the close-time token tally. Existing records (e.g. seeded
    # by a manual ``eawf actual stop``) keep their elapsed_eu /
    # attention_eu / runtime_eu fields and only the token rollup is
    # refreshed; auto-created records take the telemetry-derived
    # elapsed_eu when one is supplied (else 0.0 per the docstring note).
    if state.actuals is None:
        state.actuals = {}
    existing = state.actuals.get(wave_id)
    auto_elapsed_eu = actual_elapsed_eu if actual_elapsed_eu is not None else 0.0
    auto_cost_usd = actual_cost_usd if actual_cost_usd is not None else 0.0
    # Thread the captured harness+model attribution off the wave's latest
    # runtime snapshot (stamped by the daemon runtime-capture writer) onto the
    # auto-created actual so a recorded actual is calibratable by harness+model.
    # Both stay nullable when no runtime was captured.
    actual_harness = wave.runtime_latest.harness if wave.runtime_latest is not None else None
    actual_model = wave.runtime_latest.model if wave.runtime_latest is not None else None
    # A wave whose counters were re-originated or shared closes on an honest
    # figure that is nonetheless not a reference class. Mark it on the row so a
    # calibration consumer skips it from state alone.
    calibration_excluded = runtime_is_calibration_excluded(wave)
    if existing is None:
        state.actuals[wave_id] = ActualSummary(
            id=f"ACT-{wave_id}",
            scope_id=wave_id,
            status=ActualStatus.DONE,
            elapsed_eu=auto_elapsed_eu,
            attention_eu=actual_attention_eu,
            agent_runtime_eu=actual_agent_runtime_eu,
            actual_tokens=wave.tokens_consumed,
            actual_cost_usd=auto_cost_usd,
            harness=actual_harness,
            model=actual_model,
            calibration_excluded=calibration_excluded,
            current_store_record_id=f"REC-{wave_id}",
            updated_at=now,
        )
    else:
        existing.status = ActualStatus.DONE
        existing.actual_tokens = wave.tokens_consumed
        if actual_cost_usd is not None:
            existing.actual_cost_usd = actual_cost_usd
        if actual_harness is not None:
            existing.harness = actual_harness
        if actual_model is not None:
            existing.model = actual_model
        # An operator-authored actual still has its token + cost fields refreshed
        # from the same disqualified capture, so the exclusion applies to it too.
        # Only ever set, never clear: nothing about a close makes a re-originated
        # or shared row calibratable again.
        if calibration_excluded:
            existing.calibration_excluded = True
        existing.updated_at = now
    logger.info(
        f"close_wave id={wave_id} outcome={outcome!r} "
        f"actual_tokens={wave.tokens_consumed} actual_cost_usd={auto_cost_usd} "
        f"actual_attention_eu={actual_attention_eu} elapsed_eu={auto_elapsed_eu} "
        f"calibration_excluded={calibration_excluded}"
    )
    return wave


def fail_wave(state: State, *, wave_id: str, reason: str) -> Wave:
    """Mark a claimed/in-progress wave as ``failed`` with *reason*."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    # pending/claimed/in_progress -> failed are the only legal fail edges; the
    # table has no terminal -> failed edge, so an already-terminal source
    # raises the legacy "already terminal" message.
    validate_transition(
        WAVE_TRANSITIONS,
        wave.status,
        WaveStatus.FAILED,
        illegal_message=f"wave {wave_id!r} already terminal (status={wave.status.value!r})",
    )
    wave.status = WaveStatus.FAILED
    wave.outcome = reason
    wave.closed_at = datetime.now(UTC)
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"fail_wave id={wave_id} reason={reason!r}")
    return wave


def release_wave(state: State, *, wave_id: str, reason: str | None = None) -> Wave:
    """Release a claimed/in-progress wave back to ``pending`` (un-claim).

    The inverse of :func:`claim_wave`: a runtime that claimed a wave but
    cannot finish it relinquishes the claim so another runtime can pick it
    up. Clears the claim binding (``claim_session_id``, ``worktree_id``)
    and drops the wave from ``current.active_wave_ids``. The parent iter's
    status is left untouched — releasing one wave does not de-activate an
    iter that other waves may still be running under.

    Re-releasing an already-PENDING wave is a no-op (idempotent) so a
    double-release across runtimes does not fault.

    Args:
        state: State to mutate in place.
        wave_id: Id of the wave to release.
        reason: Optional human-readable reason recorded in the log line;
            not persisted on the wave (the wave returns to a clean
            PENDING state with no outcome stamp).

    Raises:
        LifecycleError: when *wave_id* is unknown or the wave is in a
            terminal status (closed/failed/abandoned) that cannot be
            un-claimed.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status == WaveStatus.PENDING:
        logger.debug(f"release_wave idempotent wave={wave_id} already pending")
        return wave
    # claimed/in_progress -> pending are the only legal release edges; the
    # table has no terminal -> pending edge, so a terminal source raises the
    # legacy "not claimed/in_progress" message.
    validate_transition(
        WAVE_TRANSITIONS,
        wave.status,
        WaveStatus.PENDING,
        illegal_message=(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot release"
        ),
    )
    wave.status = WaveStatus.PENDING
    wave.claim_session_id = None
    wave.worktree_id = None
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"release_wave wave={wave_id} reason={reason!r}")
    return wave
