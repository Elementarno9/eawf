"""Stateful + coverage tests for the lifecycle FSM tables (FS08).

The lifecycle FSM tables in :mod:`eawf.workflow.lifecycle.spec`
(``WAVE_TRANSITIONS`` / ``PHASE_TRANSITIONS`` / ``ITER_TRANSITIONS`` /
``SPEC_TRANSITIONS``) declare the legal status moves. This module pins two
things about them:

* CR-2 (holds-for-all) -- a Hypothesis
  :class:`~hypothesis.stateful.RuleBasedStateMachine` explores the wave FSM
  and an ``@invariant`` asserts every applied transition is table-legal via
  :func:`~eawf.workflow.lifecycle.spec.validate_transition`, so the machine
  never reaches an illegal status (:class:`WaveMachine` below).
* CR-1 / CR-3 (returns) -- the ``transition_coverage`` audit-DSL kind passes
  iff the machine-exercised edge set equals the table's declared edge set,
  and fails naming the uncovered edge when a covered-set omits one.

The ``@precondition`` on :meth:`WaveMachine.advance` honours the guard shape
of the table edges: the wave guards are held satisfied through the
all-open :class:`~eawf.workflow.lifecycle.spec.GuardContext` so the machine
drives the *structural* status moves rather than the orthogonal
guard-predicate plumbing.
"""

from __future__ import annotations

from pathlib import Path

from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from eawf.kernel.state.enums import WaveStatus
from eawf.workflow.audit_dsl import CHECK_REGISTRY, CheckSpec
from eawf.workflow.audit_dsl.kinds.transition_coverage import (
    built_states,
    check_transition_coverage,
    collect_covered_edges,
    table_edges,
)
from eawf.workflow.lifecycle.spec import (
    WAVE_TRANSITIONS,
    GuardContext,
    GuardName,
    validate_transition,
)

# An all-satisfied guard context: the machine drives the structural edge set
# of the wave FSM, so every named guard predicate is held satisfied.
_OPEN_CTX = GuardContext(
    deps_closed=True,
    sibling_ordered=True,
    out_of_order=True,
    not_paused=True,
)


# ---- CR-2: stateful exploration never reaches an illegal status -------------


class WaveMachine(RuleBasedStateMachine):
    """Explore the wave FSM, asserting every transition it takes is legal.

    The machine begins at :attr:`WaveStatus.PENDING` and, on each step,
    Hypothesis draws one out-edge of the current status; the ``advance`` rule
    re-checks the move is table-legal via :func:`validate_transition` before
    applying it. A terminal status (no out-edges) resets the machine to
    ``PENDING`` so the exploration keeps moving. The ``@invariant`` pins that
    the machine never sits in a status absent from the table -- i.e. it never
    reaches an illegal status.
    """

    def __init__(self) -> None:
        super().__init__()
        self.current: WaveStatus = WaveStatus.PENDING

    def _out_edges(self) -> list[tuple[WaveStatus, GuardName]]:
        return sorted(
            WAVE_TRANSITIONS.get(self.current, frozenset()),
            key=lambda pair: (pair[0].value, pair[1].value),
        )

    @precondition(lambda self: bool(WAVE_TRANSITIONS.get(self.current)))
    @rule(data=st.data())
    def advance(self, data: st.DataObject) -> None:
        """Draw + apply one legal out-edge of the current status.

        The ``@precondition`` fires this rule only when the current status has
        at least one out-edge; the ``reset`` rule handles terminal statuses.
        """
        edges = self._out_edges()
        idx = data.draw(st.integers(min_value=0, max_value=len(edges) - 1))
        to, _guard = edges[idx]
        validate_transition(
            WAVE_TRANSITIONS,
            self.current,
            to,
            _OPEN_CTX,
            illegal_message=f"machine took illegal edge {self.current.value!r} -> {to.value!r}",
        )
        self.current = to

    @precondition(lambda self: not WAVE_TRANSITIONS.get(self.current))
    @rule()
    def reset(self) -> None:
        """Reset to the entry node from a terminal status."""
        self.current = WaveStatus.PENDING

    @invariant()
    def current_status_is_table_legal(self) -> None:
        """Assert the machine never sits in a status absent from the table."""
        assert self.current in WAVE_TRANSITIONS, (
            f"machine reached off-table status {self.current!r}"
        )


# Bind modest budgets so the stateful run finishes in seconds.
WaveMachine.TestCase.settings = settings(
    max_examples=50,
    stateful_step_count=20,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestWaveMachine = WaveMachine.TestCase


# ---- CR-1: full-coverage run -> check_transition_coverage passes ------------


def test_check_transition_coverage_full_run_passes(tmp_path: Path) -> None:
    spec = CheckSpec(kind="transition_coverage", name="tc-wave", args={"table": "wave"})
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "pass"
    assert result.passed is True
    assert result.details is not None
    assert "all" in result.details


def test_collect_covered_edges_wave_equals_table() -> None:
    covered = collect_covered_edges("wave")
    assert covered == table_edges("wave")


def test_check_transition_coverage_all_tables_pass(tmp_path: Path) -> None:
    for name in ("wave", "phase", "iter", "spec"):
        spec = CheckSpec(kind="transition_coverage", name=f"tc-{name}", args={"table": name})
        result = check_transition_coverage(spec, tmp_path)
        assert result.status == "pass", f"table={name} did not pass: {result.details}"


def test_transition_coverage_state_completeness_passes(tmp_path: Path) -> None:
    """Live lifecycle FSMs expose every declared state in their built table."""
    for name in ("wave", "phase", "iter", "spec"):
        spec = CheckSpec(
            kind="transition_coverage",
            name=f"tc-states-{name}",
            args={
                "table": name,
                "built_states": sorted(built_states(name)),
                "covered_edges": [list(edge) for edge in table_edges(name)],
            },
        )
        result = check_transition_coverage(spec, tmp_path)
        assert result.status == "pass", f"table={name} did not pass: {result.details}"


# ---- CR-3: a table edge with no covering rule -> fail naming the edge --------


def test_check_transition_coverage_missing_edge_fails(tmp_path: Path) -> None:
    declared = table_edges("wave")
    # Drop one known edge from the covered-set to simulate a rule that does
    # not exercise it. ``pending -> failed[none]`` is a stable wave edge.
    missing = (WaveStatus.PENDING.value, WaveStatus.FAILED.value, GuardName.NONE.value)
    assert missing in declared
    covered = [list(edge) for edge in declared if edge != missing]
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-missing",
        args={"table": "wave", "covered_edges": covered},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "uncovered" in result.details
    assert f"{WaveStatus.PENDING.value}->{WaveStatus.FAILED.value}[{GuardName.NONE.value}]" in (
        result.details
    )


def test_check_transition_coverage_explicit_full_covered_passes(tmp_path: Path) -> None:
    declared = table_edges("wave")
    covered = [list(edge) for edge in declared]
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-explicit-full",
        args={"table": "wave", "covered_edges": covered},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "pass"


# ---- error-path: malformed args degrade to fail, never raise ----------------


def test_check_transition_coverage_missing_table_arg_fails(tmp_path: Path) -> None:
    spec = CheckSpec(kind="transition_coverage", name="tc-no-table", args={})
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"
    assert result.passed is False
    assert result.details is not None
    assert "table" in result.details


def test_check_transition_coverage_unknown_table_arg_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-bad-table",
        args={"table": "nope"},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "table" in result.details


def test_check_transition_coverage_non_str_table_arg_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-int-table",
        args={"table": 3},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"


def test_check_transition_coverage_bad_covered_shape_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-bad-covered",
        args={"table": "wave", "covered_edges": [["pending", "claimed"]]},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "triple" in result.details


def test_check_transition_coverage_non_list_covered_fails(tmp_path: Path) -> None:
    spec = CheckSpec(
        kind="transition_coverage",
        name="tc-covered-not-list",
        args={"table": "wave", "covered_edges": "pending,claimed"},
    )
    result = check_transition_coverage(spec, tmp_path)
    assert result.status == "fail"
    assert result.details is not None
    assert "list" in result.details


# ---- registry dispatch (mirror schema_validate / affordance_parity) ---------


def test_check_registry_transition_coverage_dispatches_pass(tmp_path: Path) -> None:
    spec = CheckSpec(kind="transition_coverage", name="tc-registry", args={"table": "spec"})
    fn = CHECK_REGISTRY["transition_coverage"]
    result = fn(spec, tmp_path)
    assert result.status == "pass"
