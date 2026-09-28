"""CON-051, CON-053, CON-054: the consequence of every lifecycle verb, from the transition table.

The verb table is spelled beside the daemon's rather than imported from it, so the two
are held equal here: a verb renamed, re-edged or given an approval on one side reds. The
guard set the preview leaves to the daemon is held equal to the daemon's own table too.
"""

from __future__ import annotations

import pytest

from eawf.kernel.state.epoch2.consequence import (
    CANONICAL_MUTATIONS,
    DAEMON_GUARDS,
    GUARD_CONDITIONS,
    MUTATIONS_BY_METHOD,
    CanonicalMutation,
    consequence,
)
from eawf.kernel.state.epoch2.transitions import TransitionGuard
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_VERBS
from eawf.runtime.daemon.methods.domain_guards import GUARD_COMPUTERS


def _shape(
    method: str, kind: str, froms: tuple[object, ...], to: object, approve: bool, bind: bool
) -> tuple[object, ...]:
    return (method, kind, tuple(str(s) for s in froms), str(to), approve, bind)


def test_con_051_the_verb_table_is_the_daemons_own() -> None:
    ours = [
        _shape(
            m.method,
            m.entity.value,
            m.from_statuses,
            m.to_status,
            m.approval_required,
            m.binds_integration,
        )
        for m in CANONICAL_MUTATIONS
    ]
    theirs = [
        _shape(
            v.method,
            v.kind.value,
            v.from_statuses,
            v.to_status,
            v.approval_required,
            v.binds_integration,
        )
        for v in DOMAIN_LIFECYCLE_VERBS
    ]
    assert ours == theirs


def test_con_051_the_guards_left_to_the_daemon_are_the_ones_it_computes() -> None:
    assert frozenset(GUARD_COMPUTERS) == DAEMON_GUARDS


def test_con_051_every_guard_has_a_stated_condition() -> None:
    assert set(GUARD_CONDITIONS) == set(TransitionGuard)


def test_con_051_the_edge_is_stated_from_the_status_it_was_read_in() -> None:
    claim = MUTATIONS_BY_METHOD["domain.task.claim"]
    stated = consequence(claim, key="EAWF-0001", revision=3, status="PLANNED")
    assert stated.refusal is None
    assert stated.effects[:2] == (
        "task EAWF-0001 moves PLANNED → CLAIMED",
        "revision 3 becomes 4 · task.claimed is recorded",
    )


def test_con_051_a_status_the_verb_does_not_leave_is_refused_as_illegal() -> None:
    claim = MUTATIONS_BY_METHOD["domain.task.claim"]
    stated = consequence(claim, key="EAWF-0001", revision=3, status="DRAFT")
    assert stated.refusal is not None
    assert stated.refusal.code == "illegal_transition"
    assert stated.refusal.reason == "claim moves a task out of PLANNED, but EAWF-0001 is DRAFT"


@pytest.mark.parametrize(
    ("method", "status", "code"),
    [
        ("domain.task.ready", "RUNNING", "run_report_unbound"),
        ("domain.milestone.cancel", "ACTIVE", "transition_reason_missing"),
        ("domain.batch.activate", "PLANNED", "missing_transition_fields"),
        ("domain.milestone.accept", "ACCEPTANCE_REVIEW", "protected_approval_required"),
        ("domain.task.complete", "READY_TO_INTEGRATE", "task_integration_unproven"),
    ],
)
def test_con_051_a_guard_the_request_decides_refuses_with_its_code(
    method: str, status: str, code: str
) -> None:
    stated = consequence(MUTATIONS_BY_METHOD[method], key="K-1", revision=2, status=status)
    assert stated.refusal is not None
    assert stated.refusal.code == code
    assert stated.refusal.remediation


def test_con_051_a_guard_the_daemon_computes_is_a_condition_not_a_refusal() -> None:
    retire = MUTATIONS_BY_METHOD["domain.track.retire"]
    stated = consequence(retire, key="TRK-CORE", revision=2, status="ACTIVE")
    assert stated.refusal is None
    assert (
        "only if no milestone under the track is still open, which the daemon checks at commit"
        in stated.effects
    )


def test_con_051_a_clock_field_is_stamped_rather_than_refused() -> None:
    start = MUTATIONS_BY_METHOD["domain.run.start"]
    stated = consequence(start, key="RUN-1", revision=1, status="QUEUED")
    assert stated.refusal is None
    assert stated.clock_fields == ("started_at",)


def test_con_051_an_unread_status_states_every_edge_and_decides_nothing() -> None:
    cancel = MUTATIONS_BY_METHOD["domain.milestone.cancel"]
    stated = consequence(cancel, key="MLS-1", revision=5, status=None)
    assert stated.refusal is None
    assert stated.effects[0] == (
        "milestone MLS-1 moves PLANNED or ACTIVE or ACCEPTANCE_REVIEW → CANCELLED"
    )


@pytest.mark.parametrize("revision", [0, -1])
def test_con_051_a_revision_that_is_not_positive_is_refused(revision: int) -> None:
    with pytest.raises(ValueError, match="revision must be positive"):
        consequence(CANONICAL_MUTATIONS[0], key="K", revision=revision, status=None)


def test_con_051_a_status_outside_every_from_status_has_no_edge() -> None:
    mutation: CanonicalMutation = MUTATIONS_BY_METHOD["domain.run.finish"]
    assert mutation.row_from("QUEUED") is None
    assert mutation.row_from("RUNNING") is not None


@pytest.mark.parametrize("mutation", CANONICAL_MUTATIONS, ids=lambda m: m.method)
def test_con_053_every_verb_states_what_it_will_not_do(mutation: CanonicalMutation) -> None:
    stated = consequence(mutation, key="K", revision=1, status=None)
    assert stated.not_effects == mutation.not_effects
    assert len(stated.not_effects) >= 2


@pytest.mark.parametrize("revision", [1, 41208])
def test_con_054_the_reload_rule_names_the_bound_revision(revision: int) -> None:
    stated = consequence(CANONICAL_MUTATIONS[0], key="K", revision=revision, status=None)
    assert stated.if_stale.startswith(f"if revision {revision} moves before you confirm")
    assert "withdrawn, never re-targeted" in stated.if_stale
