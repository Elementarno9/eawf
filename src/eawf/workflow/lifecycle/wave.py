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
from datetime import UTC, datetime

from eawf.kernel.spec.intent import IntentBrief
from eawf.kernel.state.enums import (
    ActualStatus,
    AgentSessionRole,
    EffortBucket,
    IterStatus,
    WaveStatus,
)
from eawf.kernel.state.ids import natural_key
from eawf.kernel.state.models import ActualSummary, State, Wave
from eawf.workflow.estimation.buckets import default_estimate_summary
from eawf.workflow.lifecycle._errors import LifecycleError

logger = logging.getLogger(__name__)


def plan_wave(
    state: State,
    *,
    wave_id: str,
    iter_id: str,
    title: str,
    file_scopes: list[str],
    deps: list[str] | None = None,
    success_criteria: list[str] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
) -> Wave:
    """Insert a new wave with status ``pending``.

    Args:
        state: State to mutate in place.
        wave_id: Canonical wave id (e.g. ``P03-I02-W04``).
        iter_id: Parent iter id.
        title: Bounded ≤72-char wave title.
        file_scopes: File globs the wave is scoped to.
        deps: Optional list of prerequisite wave ids.
        success_criteria: Optional list of success-criterion strings.
        agent_role: Optional executor role.
        effort_bucket: Required XS/S/M/L/XL estimate bucket.
        description: Optional bounded ≤500-char long-form description;
            persisted on :attr:`Wave.description` for downstream renderers.
        intent: Optional typed :class:`IntentBrief` attaching the goal /
            motivation / success-signal + evidence + source-brief refs
            that motivated the wave. ``None`` (default) leaves the
            wave's intent unset; the field is additive + replay-safe so
            on-disk state without it re-validates.

    Raises:
        LifecycleError: if iter is missing/closed, wave id duplicates, any
            declared dep references a missing wave id, the dep set names
            the wave itself, ``effort_bucket`` is missing, or the
            resulting graph would contain a cycle.

    Side-effects on the reverse-index: for every ``dep`` in *deps*, the
    dep wave's ``blocks`` list is mutated in-place to include *wave_id*
    (idempotent — already-present ids are not duplicated).
    """
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
    for dep in deps_list:
        if dep not in state.waves:
            raise LifecycleError(f"unknown dep wave {dep!r}")
    # Cycle check: simulate the post-insert DAG over deps and topo-sort.
    # The new wave depends on every entry of ``deps_list``; existing
    # waves keep their current ``deps``. If toposort fails any node
    # remains unprocessed -> cycle. Self-dep is already rejected above.
    if _would_create_cycle(state, new_id=wave_id, new_deps=deps_list):
        raise LifecycleError(f"adding wave {wave_id!r} with deps={deps_list} would create a cycle")
    wave = Wave(
        id=wave_id,
        iter_id=iter_id,
        title=title,
        description=description,
        status=WaveStatus.PENDING,
        deps=deps_list,
        blocks=[],
        file_scopes=list(file_scopes),
        success_criteria=list(success_criteria or []),
        agent_role=agent_role,
        effort_bucket=effort_bucket,
        claim_session_id=None,
        worktree_id=None,
        outcome=None,
        opened_at=datetime.now(UTC),
        closed_at=None,
        intent=intent,
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
    success_criteria: list[str] | None = None,
    agent_role: AgentSessionRole | None = None,
    effort_bucket: EffortBucket | None = None,
    description: str | None = None,
    intent: IntentBrief | None = None,
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
        success_criteria: Optional replacement criteria; ``None`` leaves untouched.
        agent_role: Optional replacement role; ``None`` leaves untouched.
        effort_bucket: Optional replacement bucket; ``None`` leaves untouched.
        description: Optional replacement description (≤500 chars);
            ``None`` leaves the existing value untouched.
        intent: Optional replacement :class:`IntentBrief`; ``None``
            leaves the existing intent untouched (callers cannot clear
            an intent through this helper — the asymmetry mirrors the
            description / title API).

    Raises:
        LifecycleError: when *wave_id* is unknown or not PENDING.
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
    intent_goal = repr(intent.goal) if intent is not None else None
    logger.info(
        f"edit_wave_plan id={wave_id} title={title!r} file_scopes={file_scopes} "
        f"agent_role={agent_role} effort_bucket={effort_bucket} description={description!r} "
        f"intent_goal={intent_goal}"
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
) -> Wave:
    """Move a pending wave to ``claimed`` and bind it to *session_id*.

    Re-claiming an already-claimed wave with the *same* session is a no-op
    (idempotent). Re-claiming with a *different* session is rejected so the
    sibling-lock + status check delivers exactly-once semantics.

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

    Lifecycle guard: once every gate above passes, a PLANNED parent
    iter is activated as part of the same claim mutation so waves
    never run under a PLANNED iter. The activation is inlined rather
    than delegated to :func:`eawf.workflow.lifecycle.iter_.activate_iter`
    because that helper resets ``current.active_wave_ids`` — which
    would clobber sibling waves already on the active pointer. An
    already-ACTIVE iter is left untouched (idempotent); a terminal
    iter is unreachable here because the wave's own PENDING gate
    already rejects claims under a closed/abandoned iter.

    Raises:
        LifecycleError: when *wave_id* is unknown, wave is not
            PENDING, ``effort_bucket`` is ``None``, dep waves are not
            CLOSED, or a lower-numbered sibling-ready wave is still
            PENDING (without ``out_of_order``).
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status == WaveStatus.CLAIMED and wave.claim_session_id == session_id:
        logger.debug(f"claim_wave idempotent id={wave_id} session={session_id}")
        return wave
    if wave.status != WaveStatus.PENDING:
        raise LifecycleError(f"wave {wave_id!r} cannot be claimed (status={wave.status.value!r})")
    if wave.effort_bucket is None:
        raise LifecycleError(
            f"wave {wave_id!r} has no effort_bucket; set one via "
            f"`eawf roadmap revise --set-bucket` before claiming"
        )
    unmet_deps = [
        dep_id
        for dep_id in wave.deps
        if state.waves.get(dep_id) is None or state.waves[dep_id].status != WaveStatus.CLOSED
    ]
    if unmet_deps:
        raise LifecycleError(
            f"wave {wave_id!r} blocked on un-closed dep waves: "
            f"{sorted(unmet_deps, key=natural_key)}"
        )
    if not out_of_order:
        skipped = _lower_w_sibling_pending(state, wave)
        if skipped:
            raise LifecycleError(
                f"wave {wave_id!r} would skip lower-numbered ready siblings: "
                f"{sorted(skipped, key=natural_key)}; pass --out-of-order to claim regardless"
            )
    wave.status = WaveStatus.CLAIMED
    wave.claim_session_id = session_id
    if wave_id not in state.current.active_wave_ids:
        state.current.active_wave_ids.append(wave_id)
    # Lifecycle guard: a wave must never run under a PLANNED iter, so the
    # first claim activates the parent iter (status flip + current pointer)
    # atomically with the claim. ACTIVE iters are left as-is (idempotent);
    # terminal iters are unreachable because the PENDING gate above already
    # rejects claims under a closed iter's now-non-pending waves.
    it = state.iters.get(wave.iter_id)
    if it is not None and it.status == IterStatus.PLANNED:
        it.status = IterStatus.ACTIVE
        state.current.iter_id = it.id
        logger.info(f"claim_wave auto-activated iter={it.id} on first claim wave={wave_id}")
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
    if wave.status != WaveStatus.CLAIMED:
        raise LifecycleError(
            f"wave {wave_id!r} is not claimed (status={wave.status.value!r}); cannot start"
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
) -> Wave:
    """Close a claimed/in-progress wave with an outcome string.

    Pure status flip + outcome stamp; the wave's commit SHA stays on
    ``Wave.commit`` when it was pinned via ``eawf wave close --commit
    <ref>`` (P19-W17 re-introduced the field as ``ShaStr | None`` and
    normalises any ref shape via ``git rev-parse``). When ``commit`` is
    left ``None``, callers fall back to
    :func:`eawf.workflow.lifecycle.wave_sha.derive_wave_sha`, which walks
    ``git log --grep '[P##-W##]'`` against the active branch.

    Telemetry handoff (P28-I02-W03): the close path upserts
    :class:`ActualSummary` for *wave_id* in ``state.actuals`` carrying
    the wave's :attr:`Wave.tokens_consumed` tally on ``actual_tokens``.
    The auto-created actual leaves ``elapsed_eu=0.0`` — the open->close
    wall-clock span is not agent effort (it counts overnight,
    cross-session, and other-wave idle time, inflating consumed EU by
    ~10x per P27-I05 EU research); real elapsed-EU comes from measured
    ``eawf actual start/stop`` segments when an operator runs them.
    ``actual_cost_usd`` stays at ``0.0`` for v0.4 — the per-model rate
    table that turns tokens into dollars is not yet wired (the field
    exists so the post-mutation event envelope can publish a typed cost
    value once the rate table lands).

    Args:
        state: State to mutate in place.
        wave_id: Id of the claimed/in-progress wave to close.
        outcome: Human-readable outcome summary.
        tokens_consumed: Optional final token tally to persist on the
            wave before the close-time :class:`ActualSummary` upsert.
            ``None`` preserves the wave's existing accumulated tally.

    Raises:
        LifecycleError: when *wave_id* is unknown, the wave is not
            claimable for close, or *tokens_consumed* is negative.
    """
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise LifecycleError(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot close"
        )
    if tokens_consumed is not None:
        if tokens_consumed < 0:
            raise LifecycleError(f"tokens_consumed must be non-negative; got {tokens_consumed}")
        wave.tokens_consumed = tokens_consumed
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
    # refreshed; auto-created records leave elapsed_eu at 0.0 per the
    # docstring note above.
    if state.actuals is None:
        state.actuals = {}
    existing = state.actuals.get(wave_id)
    if existing is None:
        state.actuals[wave_id] = ActualSummary(
            id=f"ACT-{wave_id}",
            scope_id=wave_id,
            status=ActualStatus.DONE,
            elapsed_eu=0.0,
            actual_tokens=wave.tokens_consumed,
            actual_cost_usd=0.0,
            current_store_record_id=f"REC-{wave_id}",
            updated_at=now,
        )
    else:
        existing.status = ActualStatus.DONE
        existing.actual_tokens = wave.tokens_consumed
        existing.updated_at = now
    logger.info(
        f"close_wave id={wave_id} outcome={outcome!r} "
        f"actual_tokens={wave.tokens_consumed} actual_cost_usd=0.0"
    )
    return wave


def fail_wave(state: State, *, wave_id: str, reason: str) -> Wave:
    """Mark a claimed/in-progress wave as ``failed`` with *reason*."""
    wave = state.waves.get(wave_id)
    if wave is None:
        raise LifecycleError(f"unknown wave {wave_id!r}")
    if wave.status in {
        WaveStatus.CLOSED,
        WaveStatus.FAILED,
        WaveStatus.ABANDONED,
    }:
        raise LifecycleError(f"wave {wave_id!r} already terminal (status={wave.status.value!r})")
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
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise LifecycleError(
            f"wave {wave_id!r} is not claimed/in_progress "
            f"(status={wave.status.value!r}); cannot release"
        )
    wave.status = WaveStatus.PENDING
    wave.claim_session_id = None
    wave.worktree_id = None
    if wave_id in state.current.active_wave_ids:
        state.current.active_wave_ids.remove(wave_id)
    logger.info(f"release_wave wave={wave_id} reason={reason!r}")
    return wave
