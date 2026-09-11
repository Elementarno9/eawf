"""Unit + property tests for the lifecycle FSM transition tables (FS07).

The wave / phase / iter status guards used to be scattered across
:mod:`eawf.workflow.lifecycle.wave`, :mod:`~eawf.workflow.lifecycle.phase`, and
:mod:`~eawf.workflow.lifecycle.iter_` as inline ``if status != ...`` checks. FS07
extracted them into named guarded tables in
:mod:`eawf.workflow.lifecycle.spec` consulted via :func:`validate_transition`.

This module pins the extracted tables three ways:

- **Boundary** -- every legal edge in each table is accepted by
  :func:`validate_transition` (with satisfied guards); terminal statuses carry
  empty out-edge sets.
- **Error-path** -- an absent edge raises :class:`LifecycleError`; a guarded
  edge with an unmet predicate raises with the contract message substring.
- **CR-3 differential** -- a frozen reference edge-set encodes the legal
  transitions independently derived from the pre-refactor guard logic; the
  live tables must match it exactly (a regression pin), and a Hypothesis
  ``@given`` sweep over ``(frm, to)`` wave-status pairs asserts
  :func:`validate_transition` accepts/rejects identically to that reference.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

import pytest
from hypothesis import given
from hypothesis import strategies as st

from eawf.kernel.spec.release import (
    Release,
    ReleaseChannel,
    ReleaseStatus,
)
from eawf.kernel.spec.release_config import load_release_config
from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    SpecStatus,
    WaveStatus,
)
from eawf.workflow.lifecycle._errors import LifecycleError
from eawf.workflow.lifecycle.spec import (
    ITER_TRANSITIONS,
    PHASE_TRANSITIONS,
    SPEC_TRANSITIONS,
    WAVE_TRANSITIONS,
    GuardContext,
    GuardName,
    validate_transition,
)
from eawf.workflow.release.lifecycle import (
    RELEASE_DENIALS,
    RELEASE_TRANSITIONS,
    TERMINAL_RELEASE_STATUSES,
    ReleaseDenialCode,
    ReleaseGuardContext,
    ReleaseGuardName,
    ReleaseTransitionError,
    advance_release,
    next_release_statuses,
    validate_release_transition,
)
from eawf.workflow.release.preflight import approve_release, record_preflight_result
from eawf.workflow.release.train import DEV1_RELEASE_CONFIG_YAML, V07_TRAIN
from eawf.workflow.verify.release_readiness import (
    ReleaseReadiness,
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalStatus,
    compute_readiness,
)

# ---------------------------------------------------------------------------
# Reference edge-sets: legal (frm, to) -> guards derived INDEPENDENTLY from
# reading the pre-refactor inline guard logic. These are the CR-3 regression
# pins; the live tables MUST match them exactly.
# ---------------------------------------------------------------------------

#: claim_wave (pending -> claimed, deps + sibling + pause guards), start_wave
#: (claimed -> in_progress), close_wave (claimed/in_progress -> closed),
#: fail_wave (pending/claimed/in_progress -> failed), release_wave
#: (claimed/in_progress -> pending).
_WAVE_REFERENCE: dict[tuple[WaveStatus, WaveStatus], frozenset[GuardName]] = {
    (WaveStatus.PENDING, WaveStatus.CLAIMED): frozenset(
        {GuardName.DEPS_CLOSED, GuardName.SIBLING_ORDERED, GuardName.NOT_PAUSED}
    ),
    (WaveStatus.PENDING, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.CLOSED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.CLAIMED, WaveStatus.PENDING): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.CLOSED): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.FAILED): frozenset({GuardName.NONE}),
    (WaveStatus.IN_PROGRESS, WaveStatus.PENDING): frozenset({GuardName.NONE}),
}

#: activate_phase (planned -> active), close_phase (planned/active -> closed),
#: archive_phase (planned -> archived), reopen_phase (closed -> active).
_PHASE_REFERENCE: dict[tuple[PhaseStatus, PhaseStatus], frozenset[GuardName]] = {
    (PhaseStatus.PLANNED, PhaseStatus.ACTIVE): frozenset({GuardName.NONE}),
    (PhaseStatus.PLANNED, PhaseStatus.CLOSED): frozenset({GuardName.NONE}),
    (PhaseStatus.PLANNED, PhaseStatus.ARCHIVED): frozenset({GuardName.NONE}),
    (PhaseStatus.ACTIVE, PhaseStatus.CLOSED): frozenset({GuardName.NONE}),
    (PhaseStatus.CLOSED, PhaseStatus.ACTIVE): frozenset({GuardName.NONE}),
}

#: activate_iter (planned -> active), close_iter (planned/active -> closed),
#: archive cascade (planned/active -> abandoned).
_ITER_REFERENCE: dict[tuple[IterStatus, IterStatus], frozenset[GuardName]] = {
    (IterStatus.PLANNED, IterStatus.ACTIVE): frozenset({GuardName.NONE}),
    (IterStatus.PLANNED, IterStatus.CLOSED): frozenset({GuardName.NONE}),
    (IterStatus.PLANNED, IterStatus.ABANDONED): frozenset({GuardName.NONE}),
    (IterStatus.ACTIVE, IterStatus.CLOSED): frozenset({GuardName.NONE}),
    (IterStatus.ACTIVE, IterStatus.ABANDONED): frozenset({GuardName.NONE}),
}


def _table_as_reference(
    table: dict[object, frozenset[tuple[object, GuardName]]],
) -> dict[tuple[object, object], frozenset[GuardName]]:
    """Collapse a guarded table into a ``(frm, to) -> guards`` mapping."""
    out: dict[tuple[object, object], frozenset[GuardName]] = {}
    for frm, edges in table.items():
        for to, guard in edges:
            out.setdefault((frm, to), frozenset())
            out[(frm, to)] = out[(frm, to)] | {guard}
    return out


# ---------------------------------------------------------------------------
# CR-3 differential: live tables match the independently-derived reference.
# ---------------------------------------------------------------------------


def test_wave_table_matches_reference_edge_set() -> None:
    """WAVE_TRANSITIONS encodes exactly the pre-refactor legal wave edges."""
    assert _table_as_reference(WAVE_TRANSITIONS) == _WAVE_REFERENCE


def test_phase_table_matches_reference_edge_set() -> None:
    """PHASE_TRANSITIONS encodes exactly the pre-refactor legal phase edges."""
    assert _table_as_reference(PHASE_TRANSITIONS) == _PHASE_REFERENCE


def test_iter_table_matches_reference_edge_set() -> None:
    """ITER_TRANSITIONS encodes exactly the pre-refactor legal iter edges."""
    assert _table_as_reference(ITER_TRANSITIONS) == _ITER_REFERENCE


# ---------------------------------------------------------------------------
# Boundary: every legal edge is accepted; terminal statuses have no out-edges.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("frm_to", sorted(_WAVE_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_wave_edges(
    frm_to: tuple[WaveStatus, WaveStatus],
) -> None:
    """Every legal wave edge passes with an all-satisfied guard context."""
    frm, to = frm_to
    validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())


@pytest.mark.parametrize("frm_to", sorted(_PHASE_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_phase_edges(
    frm_to: tuple[PhaseStatus, PhaseStatus],
) -> None:
    frm, to = frm_to
    validate_transition(PHASE_TRANSITIONS, frm, to)


@pytest.mark.parametrize("frm_to", sorted(_ITER_REFERENCE, key=lambda p: (p[0].value, p[1].value)))
def test_validate_transition_accepts_legal_iter_edges(
    frm_to: tuple[IterStatus, IterStatus],
) -> None:
    frm, to = frm_to
    validate_transition(ITER_TRANSITIONS, frm, to)


@pytest.mark.parametrize(
    ("table", "terminal"),
    [
        (WAVE_TRANSITIONS, WaveStatus.CLOSED),
        (WAVE_TRANSITIONS, WaveStatus.FAILED),
        (WAVE_TRANSITIONS, WaveStatus.ABANDONED),
        (PHASE_TRANSITIONS, PhaseStatus.ARCHIVED),
        (ITER_TRANSITIONS, IterStatus.CLOSED),
        (ITER_TRANSITIONS, IterStatus.ABANDONED),
    ],
)
def test_terminal_statuses_have_no_out_edges(
    table: dict[object, frozenset[tuple[object, GuardName]]],
    terminal: object,
) -> None:
    """Terminal statuses carry an empty out-edge set in their table."""
    assert table[terminal] == frozenset()


def test_spec_table_legal_edges_still_validate() -> None:
    """The unguarded SPEC table is untouched and still maps the linear DAG."""
    assert SPEC_TRANSITIONS[SpecStatus.DRAFT] == frozenset({SpecStatus.READY})
    assert SPEC_TRANSITIONS[SpecStatus.READY] == frozenset({SpecStatus.IMPLEMENTED})
    assert SPEC_TRANSITIONS[SpecStatus.IMPLEMENTED] == frozenset({SpecStatus.ARCHIVED})
    assert SPEC_TRANSITIONS[SpecStatus.ARCHIVED] == frozenset()


# ---------------------------------------------------------------------------
# Error-path: illegal edges + unmet guards raise LifecycleError.
# ---------------------------------------------------------------------------


def test_validate_transition_rejects_absent_edge() -> None:
    """An edge absent from the table raises LifecycleError (CR-1)."""
    with pytest.raises(LifecycleError, match="illegal transition"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.CLOSED, WaveStatus.CLAIMED)


def test_validate_transition_absent_edge_uses_caller_message() -> None:
    """The caller-supplied illegal_message is surfaced verbatim."""
    with pytest.raises(LifecycleError, match="cannot start"):
        validate_transition(
            WAVE_TRANSITIONS,
            WaveStatus.PENDING,
            WaveStatus.IN_PROGRESS,
            illegal_message="wave 'W' is not claimed (status='pending'); cannot start",
        )


def test_validate_transition_rejects_unmet_deps_closed_guard() -> None:
    """A pending -> claimed edge with deps not closed raises with the message."""
    ctx = GuardContext(
        deps_closed=False,
        messages={GuardName.DEPS_CLOSED: "wave 'W2' blocked on un-closed dep waves: ['W1']"},
    )
    with pytest.raises(LifecycleError, match="un-closed dep waves"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_rejects_unmet_sibling_ordered_guard() -> None:
    """A claim skipping a lower-W## ready sibling raises with the message."""
    ctx = GuardContext(
        sibling_ordered=False,
        messages={
            GuardName.SIBLING_ORDERED: (
                "wave 'W2' would skip lower-numbered ready siblings: ['W1']; "
                "pass --out-of-order to claim regardless"
            )
        },
    )
    with pytest.raises(LifecycleError, match="lower-numbered ready siblings"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_out_of_order_satisfies_sibling_guard() -> None:
    """The out-of-order override satisfies the sibling-ordering guard."""
    ctx = GuardContext(sibling_ordered=False, out_of_order=True)
    validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_pause_gate_blocks_even_out_of_order() -> None:
    """The not-paused guard is unconditional: out_of_order does not bypass it."""
    ctx = GuardContext(
        not_paused=False,
        out_of_order=True,
        messages={GuardName.NOT_PAUSED: "dispatch paused: resume before claiming 'W1'"},
    )
    with pytest.raises(LifecycleError, match="dispatch paused: resume before claiming"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_deps_guard_surfaces_before_sibling() -> None:
    """Multiple unmet guards surface in the legacy deps -> sibling order."""
    ctx = GuardContext(
        deps_closed=False,
        sibling_ordered=False,
        messages={
            GuardName.DEPS_CLOSED: "deps marker",
            GuardName.SIBLING_ORDERED: "sibling marker",
        },
    )
    with pytest.raises(LifecycleError, match="deps marker"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


def test_validate_transition_unmet_guard_without_message_uses_fallback() -> None:
    """A guard with no supplied message falls back to a generic phrasing."""
    ctx = GuardContext(not_paused=False)
    with pytest.raises(LifecycleError, match="blocked by guard"):
        validate_transition(WAVE_TRANSITIONS, WaveStatus.PENDING, WaveStatus.CLAIMED, ctx)


# ---------------------------------------------------------------------------
# CR-3 Hypothesis sweep: accept/reject identical to the reference for every
# wave (frm, to) status pair, with all guards satisfied.
# ---------------------------------------------------------------------------


@pytest.mark.property
@given(
    frm=st.sampled_from(list(WaveStatus)),
    to=st.sampled_from(list(WaveStatus)),
)
def test_validate_transition_matches_reference_for_all_wave_pairs(
    frm: WaveStatus, to: WaveStatus
) -> None:
    """For every wave status pair, accept iff the reference marks it legal.

    Guards are all satisfied (default context) so a legal edge always passes;
    an absent edge always raises. This is the holds-for-all differential
    between the new table-driven path and the independently-derived reference.
    """
    legal = (frm, to) in _WAVE_REFERENCE
    if legal:
        validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())
    else:
        with pytest.raises(LifecycleError):
            validate_transition(WAVE_TRANSITIONS, frm, to, GuardContext())


# ---------------------------------------------------------------------------
# Release status machine (P31): the guarded table, its named denials, and the
# two production call sites that walk it -- the preflight result edge and the
# approval guard.
# ---------------------------------------------------------------------------


#: Reference edge-set for the Release table, derived independently from the
#: release lifecycle diagram: pin, the deterministic preflight result, the
#: three pre-effect returns to DRAFT, the three cancellations, approval,
#: publication, timeout, verification, observation, recovery and the two
#: burns -- the exhausted one out of RECOVERING and the operationless one
#: out of DRAFT, which is where an adopted out-of-band publication stops.
_RELEASE_REFERENCE: dict[tuple[ReleaseStatus, ReleaseStatus], ReleaseGuardName] = {
    (ReleaseStatus.DRAFT, ReleaseStatus.CANDIDATE): ReleaseGuardName.MANIFEST_COMPLETE,
    (ReleaseStatus.DRAFT, ReleaseStatus.CANCELLED): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (
        ReleaseStatus.DRAFT,
        ReleaseStatus.PARTIALLY_RELEASED,
    ): ReleaseGuardName.RECOVERY_EXHAUSTED,
    (ReleaseStatus.CANDIDATE, ReleaseStatus.PREFLIGHT_FAILED): ReleaseGuardName.NONE,
    (ReleaseStatus.CANDIDATE, ReleaseStatus.DRAFT): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (ReleaseStatus.CANDIDATE, ReleaseStatus.CANCELLED): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (ReleaseStatus.CANDIDATE, ReleaseStatus.APPROVED): ReleaseGuardName.GATES_GREEN,
    (ReleaseStatus.PREFLIGHT_FAILED, ReleaseStatus.DRAFT): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (
        ReleaseStatus.PREFLIGHT_FAILED,
        ReleaseStatus.CANCELLED,
    ): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (ReleaseStatus.APPROVED, ReleaseStatus.DRAFT): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (ReleaseStatus.APPROVED, ReleaseStatus.CANCELLED): ReleaseGuardName.NO_EXTERNAL_EFFECT,
    (ReleaseStatus.APPROVED, ReleaseStatus.PUBLISHING): ReleaseGuardName.APPROVAL_FRESH,
    (ReleaseStatus.PUBLISHING, ReleaseStatus.VERIFYING): ReleaseGuardName.TARGET_RESULTS_COMPLETE,
    (ReleaseStatus.PUBLISHING, ReleaseStatus.PUBLISH_TIMEOUT): ReleaseGuardName.NONE,
    (ReleaseStatus.PUBLISHING, ReleaseStatus.RECOVERING): ReleaseGuardName.NONE,
    (ReleaseStatus.PUBLISH_TIMEOUT, ReleaseStatus.PUBLISHING): ReleaseGuardName.IDEMPOTENT_RETRY,
    (ReleaseStatus.PUBLISH_TIMEOUT, ReleaseStatus.RECOVERING): ReleaseGuardName.NONE,
    (ReleaseStatus.VERIFYING, ReleaseStatus.BAKED): ReleaseGuardName.OBSERVED_PRERELEASE,
    (ReleaseStatus.VERIFYING, ReleaseStatus.RELEASED): ReleaseGuardName.OBSERVED_STABLE,
    (ReleaseStatus.VERIFYING, ReleaseStatus.RECOVERING): ReleaseGuardName.NONE,
    (ReleaseStatus.RECOVERING, ReleaseStatus.PUBLISHING): ReleaseGuardName.IDEMPOTENT_RETRY,
    (
        ReleaseStatus.RECOVERING,
        ReleaseStatus.PARTIALLY_RELEASED,
    ): ReleaseGuardName.RECOVERY_EXHAUSTED,
}

_RELEASE_SHA = "a" * 40
_RELEASE_TREE_SHA = "b" * 40
_RELEASE_DIGEST = f"sha256:{'c' * 64}"
_RELEASE_NOW = datetime(2026, 9, 4, 12, 0, tzinfo=UTC)


def _release_record(**overrides: Any) -> Release:
    """Build a pinned ``0.7.0.dev1`` candidate with *overrides* applied."""
    payload: dict[str, Any] = {
        "uid": UUID(int=7),
        "key": "REL-0.7.0.dev1",
        "version": "0.7.0.dev1",
        "channel": ReleaseChannel.DEV,
        "authority_epoch": 1,
        "status": ReleaseStatus.CANDIDATE,
        "source_sha": _RELEASE_SHA,
        "source_tree_sha": _RELEASE_TREE_SHA,
        "manifest_ref": "artifact://release/manifest",
        "manifest_digest": _RELEASE_DIGEST,
    }
    payload.update(overrides)
    return Release(**payload)


def _release_readiness(*, ready: bool) -> ReleaseReadiness:
    """Compute a dev1 sweep whose rows all pass (or all fail)."""
    status = ReleaseSignalStatus.PASS if ready else ReleaseSignalStatus.FAIL
    remediation = "" if ready else "repair the release inputs"

    def probe(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
        return ReleaseSignalOutcome(status=status, remediation=remediation)

    config = load_release_config(DEV1_RELEASE_CONFIG_YAML, train=V07_TRAIN)
    return compute_readiness(
        config,
        probes=dict.fromkeys(ReleaseSignalName, probe),
        computed_at=_RELEASE_NOW,
    )


def test_release_table_matches_reference_edge_set() -> None:
    """Every legal release edge, and only those, appear in the live table."""
    live = {
        (frm, target): guard
        for frm, edges in RELEASE_TRANSITIONS.items()
        for target, guard in edges
    }
    assert live == _RELEASE_REFERENCE


@pytest.mark.parametrize(("frm", "to"), sorted(_RELEASE_REFERENCE, key=repr))
def test_validate_release_transition_accepts_every_legal_edge(
    frm: ReleaseStatus, to: ReleaseStatus
) -> None:
    """A satisfied guard context admits every edge in the reference set."""
    validate_release_transition(frm, to, ReleaseGuardContext())


def test_release_terminal_statuses_have_no_out_edges() -> None:
    """The four terminal statuses never leave; correction is a new version."""
    assert (
        frozenset(
            {
                ReleaseStatus.CANCELLED,
                ReleaseStatus.BAKED,
                ReleaseStatus.RELEASED,
                ReleaseStatus.PARTIALLY_RELEASED,
            }
        )
        == TERMINAL_RELEASE_STATUSES
    )
    for status in TERMINAL_RELEASE_STATUSES:
        assert next_release_statuses(status) == frozenset()


@pytest.mark.parametrize(
    ("frm", "to"),
    [
        (ReleaseStatus.DRAFT, ReleaseStatus.APPROVED),
        (ReleaseStatus.BAKED, ReleaseStatus.DRAFT),
        (ReleaseStatus.PARTIALLY_RELEASED, ReleaseStatus.DRAFT),
        (ReleaseStatus.PARTIALLY_RELEASED, ReleaseStatus.CANCELLED),
        (ReleaseStatus.CANCELLED, ReleaseStatus.DRAFT),
        (ReleaseStatus.VERIFYING, ReleaseStatus.PUBLISHING),
    ],
)
def test_validate_release_transition_rejects_an_absent_edge(
    frm: ReleaseStatus, to: ReleaseStatus
) -> None:
    """An edge outside the table raises the illegal-transition code."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        validate_release_transition(frm, to)
    assert excinfo.value.code is ReleaseDenialCode.ILLEGAL_RELEASE_TRANSITION
    assert excinfo.value.frm is frm
    assert excinfo.value.to is to


@pytest.mark.parametrize(
    ("frm", "to", "ctx", "code"),
    [
        (
            ReleaseStatus.DRAFT,
            ReleaseStatus.CANDIDATE,
            ReleaseGuardContext(manifest_complete=False),
            ReleaseDenialCode.RELEASE_MANIFEST_INCOMPLETE,
        ),
        (
            ReleaseStatus.CANDIDATE,
            ReleaseStatus.APPROVED,
            ReleaseGuardContext(gates_green=False),
            ReleaseDenialCode.RELEASE_NOT_READY,
        ),
        (
            ReleaseStatus.APPROVED,
            ReleaseStatus.PUBLISHING,
            ReleaseGuardContext(approval_fresh=False),
            ReleaseDenialCode.APPROVAL_STALE,
        ),
        (
            ReleaseStatus.VERIFYING,
            ReleaseStatus.BAKED,
            ReleaseGuardContext(observed_prerelease=False),
            ReleaseDenialCode.PUBLICATION_NOT_OBSERVED,
        ),
        (
            ReleaseStatus.VERIFYING,
            ReleaseStatus.RELEASED,
            ReleaseGuardContext(observed_stable=False),
            ReleaseDenialCode.PUBLICATION_NOT_OBSERVED,
        ),
        (
            ReleaseStatus.APPROVED,
            ReleaseStatus.CANCELLED,
            ReleaseGuardContext(external_effect_started=True),
            ReleaseDenialCode.RELEASE_EFFECT_ALREADY_STARTED,
        ),
        (
            ReleaseStatus.PUBLISHING,
            ReleaseStatus.VERIFYING,
            ReleaseGuardContext(target_results_complete=False),
            ReleaseDenialCode.TARGET_RESULTS_INCOMPLETE,
        ),
        (
            ReleaseStatus.RECOVERING,
            ReleaseStatus.PUBLISHING,
            ReleaseGuardContext(idempotent_retry=False),
            ReleaseDenialCode.UNSAFE_RELEASE_RETRY,
        ),
        (
            ReleaseStatus.RECOVERING,
            ReleaseStatus.PARTIALLY_RELEASED,
            ReleaseGuardContext(recovery_exhausted=False),
            ReleaseDenialCode.RECOVERY_BUDGET_AVAILABLE,
        ),
    ],
)
def test_validate_release_transition_denies_with_its_named_error(
    frm: ReleaseStatus,
    to: ReleaseStatus,
    ctx: ReleaseGuardContext,
    code: ReleaseDenialCode,
) -> None:
    """Each guarded edge raises exactly the error its table row declares."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        validate_release_transition(frm, to, ctx)
    assert excinfo.value.code is code
    assert code.value in str(excinfo.value)


def test_release_denials_cover_exactly_the_guarded_edges() -> None:
    """Every guarded edge declares a denial; no unguarded edge does."""
    guarded = {
        (frm, target)
        for frm, edges in RELEASE_TRANSITIONS.items()
        for target, guard in edges
        if guard is not ReleaseGuardName.NONE
    }
    assert set(RELEASE_DENIALS) == guarded


def test_advance_release_returns_a_successor_with_a_bumped_revision() -> None:
    """A legal advance produces a new frozen record, leaving the old intact."""
    candidate = _release_record()
    failed = advance_release(candidate, ReleaseStatus.PREFLIGHT_FAILED)
    assert failed.status is ReleaseStatus.PREFLIGHT_FAILED
    assert failed.revision == candidate.revision + 1
    assert candidate.status is ReleaseStatus.CANDIDATE


def test_advance_release_rejects_a_successor_that_breaks_a_record_invariant() -> None:
    """Advancing an unpinned draft to CANDIDATE fails the pin invariant."""
    draft = _release_record(
        status=ReleaseStatus.DRAFT,
        source_sha=None,
        source_tree_sha=None,
        manifest_ref=None,
        manifest_digest=None,
    )
    with pytest.raises(ValueError, match="requires pinned fields"):
        advance_release(draft, ReleaseStatus.CANDIDATE)


def test_release_preflight_result_leaves_a_green_candidate_alone() -> None:
    """A green sweep keeps the candidate approvable rather than moving it."""
    candidate = _release_record()
    assert record_preflight_result(candidate, _release_readiness(ready=True)) is candidate


def test_release_preflight_result_moves_a_red_candidate_to_preflight_failed() -> None:
    """A non-pass required row is a deterministic result, not a denial."""
    candidate = _release_record()
    outcome = record_preflight_result(candidate, _release_readiness(ready=False))
    assert outcome.status is ReleaseStatus.PREFLIGHT_FAILED


def test_release_preflight_result_rejects_a_sweep_for_another_release() -> None:
    """A readiness object cannot be applied to a record it was not computed for."""
    other = _release_record(
        key="REL-0.7.0.dev3",
        version="0.7.0.dev3",
        authority_epoch=2,
        membership_refs=("eawf://bundle/ACB-0001",),
    )
    with pytest.raises(ValueError, match="cannot be applied to release"):
        record_preflight_result(other, _release_readiness(ready=True))


def test_release_approval_denies_release_not_ready_on_a_red_sweep() -> None:
    """The approval guard reads the same derived required set as preflight."""
    with pytest.raises(ReleaseTransitionError) as excinfo:
        approve_release(
            _release_record(),
            _release_readiness(ready=False),
            approval_ref="receipt://approval/0001",
            approved_at=_RELEASE_NOW,
        )
    assert excinfo.value.code is ReleaseDenialCode.RELEASE_NOT_READY


def test_release_approval_binds_the_receipt_on_a_green_sweep() -> None:
    """A green sweep approves and the successor carries the approval reference."""
    approved = approve_release(
        _release_record(),
        _release_readiness(ready=True),
        approval_ref="receipt://approval/0001",
        approved_at=_RELEASE_NOW,
    )
    assert approved.status is ReleaseStatus.APPROVED
    assert approved.approval_ref == "receipt://approval/0001"
    assert approved.revision == 1


def test_release_approval_rejects_a_naive_approval_timestamp() -> None:
    """An approval instant without a timezone is refused at the boundary."""
    with pytest.raises(ValueError, match="must be timezone-aware"):
        approve_release(
            _release_record(),
            _release_readiness(ready=True),
            approval_ref="receipt://approval/0001",
            approved_at=datetime(2026, 9, 4, 12, 0),
        )
