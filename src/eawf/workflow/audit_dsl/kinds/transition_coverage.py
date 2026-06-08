"""``transition_coverage`` audit-DSL kind (Fidelity Spine FS08).

Closes the transition-coverage half of the FSM-per-element spine: the
lifecycle FSM tables in :mod:`eawf.workflow.lifecycle.spec`
(:data:`~eawf.workflow.lifecycle.spec.WAVE_TRANSITIONS`,
:data:`~eawf.workflow.lifecycle.spec.PHASE_TRANSITIONS`,
:data:`~eawf.workflow.lifecycle.spec.ITER_TRANSITIONS`, and the unguarded
:data:`~eawf.workflow.lifecycle.spec.SPEC_TRANSITIONS`) declare the legal
status moves, but a unit test that walks a hand-picked path proves only
the path it walks -- a table edge with no covering rule slips through.

This kind compares the FULL edge set declared by a table against the
edge set a Hypothesis :class:`~hypothesis.stateful.RuleBasedStateMachine`
exploration actually exercised. Hypothesis gives random state-space
exploration, NOT built-in transition coverage, so the machine
self-instruments: every ``@rule`` that applies a transition appends the
``(frm, to, guard)`` edge it took to a covered-set the run accumulates.
The check passes iff ``covered_edges == table_edges``; otherwise it fails
naming each uncovered edge so the gap is actionable.

``hypothesis`` is a dev-only dependency, so the machine is built lazily
inside :func:`_make_machine` rather than at module scope -- importing this
kind (the audit-DSL registry binds every kind eagerly at CLI startup) must
not require it. Only the machine-driven coverage path (``covered_edges``
omitted) pulls ``hypothesis`` in; the deterministic ``covered_edges``-supplied
path and the table-edge readers stay import-free. This honours the lazy-load
contract the :mod:`eawf.workflow.audit_dsl.kinds` package docstring states.

Edge normal form
----------------

Every edge -- guarded (wave) or unguarded (phase / iter / spec) -- is
normalised to a comparable ``(frm_value, to_value, guard_value)`` string
triple. The guarded tables carry their :class:`GuardName` per edge; the
unguarded :data:`SPEC_TRANSITIONS` has no guard column, so its edges
normalise with the :attr:`GuardName.NONE` sentinel value -- the same
shape the unguarded wave / phase / iter edges already use.

Args (read from ``spec.args``)
------------------------------

* ``table`` -- the table name to cover: one of ``"wave"`` / ``"phase"``
  / ``"iter"`` / ``"spec"``. An unknown name is a ``fail``.
* ``covered_edges`` -- optional explicit covered-set, a list of
  ``[frm, to, guard]`` string triples. When omitted the kind runs the
  in-process machine to collect coverage; when supplied (e.g. a set
  missing one known edge) it drives the deterministic error path. A
  malformed entry is a ``fail``.

A malformed ``args`` (non-str ``table``, unknown table, bad
``covered_edges`` shape) yields ``status="fail"`` with a ``details`` note
rather than propagating an exception, so one bad criterion cannot abort
the audit run -- the same degrade-not-raise contract
:func:`~eawf.workflow.audit_dsl.kinds.affordance_parity.check_affordance_parity`
follows.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from eawf.kernel.state.enums import (
    IterStatus,
    PhaseStatus,
    SpecStatus,
    WaveStatus,
)
from eawf.workflow.audit_dsl.models import CheckResult, CheckSpec
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

logger = logging.getLogger(__name__)


#: A normalised edge: ``(frm_value, to_value, guard_value)`` strings.
Edge = tuple[str, str, str]

#: The four lifecycle table names this kind can cover. The guarded tables
#: feed the in-process machine directly; ``spec`` is the unguarded
#: forward-only chain whose edges carry the :attr:`GuardName.NONE` guard
#: sentinel in normal form.
_TABLE_NAMES: frozenset[str] = frozenset({"wave", "phase", "iter", "spec"})


def _guarded_table_edges(
    table: dict[Any, frozenset[tuple[Any, GuardName]]],
) -> frozenset[Edge]:
    """Return the normalised edge set of a guarded transition *table*.

    Each ``(target, GuardName)`` pair in the table becomes a
    ``(frm_value, to_value, guard_value)`` string triple.
    """
    edges: set[Edge] = set()
    for frm, targets in table.items():
        for to, guard in targets:
            edges.add((frm.value, to.value, guard.value))
    return frozenset(edges)


def _spec_table_edges() -> frozenset[Edge]:
    """Return the normalised edge set of the unguarded SPEC table.

    The :data:`SPEC_TRANSITIONS` table has no guard column, so each edge
    normalises with the :attr:`GuardName.NONE` sentinel value -- the same
    shape the unguarded guarded-table edges already use.
    """
    edges: set[Edge] = set()
    for frm, targets in SPEC_TRANSITIONS.items():
        for to in targets:
            edges.add((frm.value, to.value, GuardName.NONE.value))
    return frozenset(edges)


def table_edges(name: str) -> frozenset[Edge]:
    """Return the full normalised edge set declared by lifecycle table *name*.

    Args:
        name: One of ``"wave"`` / ``"phase"`` / ``"iter"`` / ``"spec"``.

    Returns:
        The frozen set of ``(frm, to, guard)`` string triples the table
        declares.

    Raises:
        ValueError: when *name* is not a known table name.
    """
    if name == "wave":
        return _guarded_table_edges(WAVE_TRANSITIONS)
    if name == "phase":
        return _guarded_table_edges(PHASE_TRANSITIONS)
    if name == "iter":
        return _guarded_table_edges(ITER_TRANSITIONS)
    if name == "spec":
        return _spec_table_edges()
    raise ValueError(f"unknown transition table: {name!r}")


# The four lifecycle entry statuses the machines start from. A fresh
# entity always begins at its planned/draft entry node.
_WAVE_START = WaveStatus.PENDING
_PHASE_START = PhaseStatus.PLANNED
_ITER_START = IterStatus.PLANNED
_SPEC_START = SpecStatus.DRAFT


# An all-satisfied guard context: the machine drives the structural edge
# set, so every named guard's predicate is held satisfied. This keeps the
# @invariant check on validate_transition focused on table-legality of the
# status move itself, not on the orthogonal guard-predicate plumbing.
_OPEN_CTX = GuardContext(
    deps_closed=True,
    sibling_ordered=True,
    out_of_order=True,
    not_paused=True,
)


def _resolve_machine_table(
    name: str,
) -> tuple[dict[Any, frozenset[tuple[Any, GuardName]]], Any, bool]:
    """Resolve the ``(table, start_status, is_guarded)`` triple for table *name*.

    The unguarded SPEC table is normalised into the guarded shape (each plain
    target paired with the :attr:`GuardName.NONE` sentinel) so one machine body
    covers all four lifecycle FSMs.

    Args:
        name: One of ``"wave"`` / ``"phase"`` / ``"iter"`` / ``"spec"``.

    Returns:
        The bound transition table, its entry-node status, and whether the
        table carries meaningful per-edge guards.

    Raises:
        ValueError: when *name* is not a known table name.
    """
    if name == "wave":
        return WAVE_TRANSITIONS, _WAVE_START, True
    if name == "phase":
        return PHASE_TRANSITIONS, _PHASE_START, False
    if name == "iter":
        return ITER_TRANSITIONS, _ITER_START, False
    if name == "spec":
        spec_table = {
            frm: frozenset((to, GuardName.NONE) for to in targets)
            for frm, targets in SPEC_TRANSITIONS.items()
        }
        return spec_table, _SPEC_START, False
    raise ValueError(f"unknown transition table: {name!r}")


def _make_machine(name: str) -> tuple[type[Any], Any]:
    """Build the bound lifecycle machine subclass + run settings for *name*.

    ``hypothesis`` is imported here, not at module scope, so importing this
    kind (the audit-DSL registry binds every kind eagerly at CLI startup)
    never requires the dev-only dependency -- only the machine-driven
    coverage path pulls it in. The machine class is defined inside this
    function for the same reason: its :class:`RuleBasedStateMachine` base and
    the ``@rule`` / ``@invariant`` decorators evaluate at class-definition
    time, which at module scope would force the import on load.

    Args:
        name: One of ``"wave"`` / ``"phase"`` / ``"iter"`` / ``"spec"``.

    Returns:
        A ``(machine_cls, run_settings)`` pair: a fresh
        :class:`~hypothesis.stateful.RuleBasedStateMachine` subclass with a
        fresh :attr:`covered` set bound, and the Hypothesis ``settings``
        profile the coverage run drives the machine under.

    Raises:
        ValueError: when *name* is not a known table name.
    """
    from hypothesis import HealthCheck, settings
    from hypothesis import strategies as st
    from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

    class _LifecycleMachine(RuleBasedStateMachine):
        """Hypothesis machine that explores a lifecycle FSM + records covered edges.

        The machine holds a current status and, on each step, records every
        out-edge of that status into :attr:`covered` (each re-checked table-legal
        via :func:`validate_transition`), then Hypothesis draws which out-edge to
        traverse so the exploration keeps reaching new statuses. A terminal status
        (no out-edges) resets the machine to the entry node so a single long run
        re-explores every branch. Coverage is therefore a function of which
        statuses are reached, so a run that reaches every status touches every
        declared edge across a multi-example run.

        Subclasses bind :attr:`_table`, :attr:`_start`, and :attr:`_is_guarded`
        so one machine body covers all four lifecycle FSMs.
        """

        #: Bound per subclass: the guarded transition table to explore.
        _table: dict[Any, frozenset[tuple[Any, GuardName]]]
        #: Bound per subclass: the entry-node status the machine starts (+ resets)
        #: at.
        _start: Any
        #: Bound per subclass: True when edges carry meaningful guards (wave),
        #: False for the unguarded tables (phase / iter / spec) where every
        #: edge normalises to the GuardName.NONE sentinel.
        _is_guarded: bool

        #: Accumulates every ``(frm, to, guard)`` edge the run applied. Shared
        #: across all instances of a subclass so a multi-example run aggregates
        #: coverage; reset by the kind / test before a fresh collection.
        covered: set[Edge]

        def __init__(self) -> None:
            super().__init__()
            self.current: Any = self._start

        def _out_edges(self) -> list[tuple[Any, GuardName]]:
            """Return the sorted out-edges of the current status."""
            return sorted(
                self._table.get(self.current, frozenset()),
                key=lambda pair: (pair[0].value, pair[1].value),
            )

        def _normalise_guard(self, guard: GuardName) -> str:
            """Return the normal-form guard value for the bound table shape."""
            return guard.value if self._is_guarded else GuardName.NONE.value

        @rule(data=st.data())
        def take_edge(self, data: st.DataObject) -> None:
            """Record every out-edge of the current status, then traverse one.

            Coverage of an edge ``(frm, to, guard)`` means the machine reached
            ``frm`` and the edge is table-legal from there -- so on visiting a
            status every one of its out-edges is recorded into :attr:`covered`
            (each first re-checked table-legal via :func:`validate_transition`).
            Hypothesis then draws which out-edge to traverse so the exploration
            keeps reaching new statuses; on a terminal status (no out-edges) the
            machine resets to the entry node. This makes coverage a function of
            *which statuses are reached* rather than which random path is walked,
            so a run that reaches every status deterministically covers every
            declared edge.
            """
            edges = self._out_edges()
            if not edges:
                self.current = self._start
                return
            frm = self.current
            for to, guard in edges:
                validate_transition(
                    self._table,
                    frm,
                    to,
                    _OPEN_CTX,
                    illegal_message=f"machine took illegal edge {frm.value!r} -> {to.value!r}",
                )
                type(self).covered.add((frm.value, to.value, self._normalise_guard(guard)))
            idx = data.draw(st.integers(min_value=0, max_value=len(edges) - 1))
            to, _ = edges[idx]
            self.current = to

        @invariant()
        def current_status_is_legal(self) -> None:
            """Assert the machine never sits in a status absent from the table.

            Every status the machine moves into is either the start node or a
            target of an edge :meth:`take_edge` already validated via
            :func:`validate_transition`, so the status must be a key of the table.
            A status with no table entry would mean the machine drifted off the
            FSM -- this invariant pins that the exploration stays inside the
            declared status set.
            """
            assert self.current in self._table, f"machine reached off-table status {self.current!r}"

    table, start, is_guarded = _resolve_machine_table(name)

    machine_cls = type(
        f"_LifecycleMachine_{name}",
        (_LifecycleMachine,),
        {
            "_table": table,
            "_start": start,
            "_is_guarded": is_guarded,
            "covered": set(),
        },
    )

    # Modest budgets keep the stateful exploration finishing in seconds while
    # still walking every branch of the small lifecycle FSMs.
    run_settings = settings(
        max_examples=50,
        stateful_step_count=20,
        deadline=None,
        suppress_health_check=[HealthCheck.too_slow],
    )
    return machine_cls, run_settings


def collect_covered_edges(name: str) -> frozenset[Edge]:
    """Run the in-process machine for table *name* + return the edges it covered.

    Constructs the bound machine subclass + run settings via
    :func:`_make_machine`, runs the machine, and returns the accumulated
    covered-edge set.

    Args:
        name: One of ``"wave"`` / ``"phase"`` / ``"iter"`` / ``"spec"``.

    Returns:
        The frozen set of ``(frm, to, guard)`` edges the run exercised.

    Raises:
        ValueError: when *name* is not a known table name.
        ImportError: when the dev-only ``hypothesis`` dependency is absent
            (the machine path requires it; the deterministic
            ``covered_edges``-supplied path does not).
    """
    machine_cls, run_settings = _make_machine(name)
    machine_cls.TestCase.settings = run_settings
    case = machine_cls.TestCase()
    case.runTest()
    return frozenset(machine_cls.covered)


def _parse_covered_arg(raw: Any) -> frozenset[Edge]:
    """Coerce an explicit ``covered_edges`` arg into a normalised edge set.

    Args:
        raw: A list of ``[frm, to, guard]`` triples (lists or tuples of three
            strings).

    Returns:
        The frozen set of normalised edges.

    Raises:
        ValueError: when *raw* is not a list of three-string triples.
    """
    if not isinstance(raw, list):
        raise ValueError(f"covered_edges must be a list: {raw!r}")
    out: set[Edge] = set()
    for entry in raw:
        if not isinstance(entry, (list, tuple)) or len(entry) != 3:
            raise ValueError(f"covered_edges entry must be a [frm, to, guard] triple: {entry!r}")
        frm, to, guard = entry
        if not (isinstance(frm, str) and isinstance(to, str) and isinstance(guard, str)):
            raise ValueError(f"covered_edges entry values must be strings: {entry!r}")
        out.add((frm, to, guard))
    return frozenset(out)


def _format_edges(edges: frozenset[Edge]) -> str:
    """Render an edge set as a stable comma-joined ``frm->to[guard]`` string."""
    return ", ".join(f"{frm}->{to}[{guard}]" for frm, to, guard in sorted(edges))


def check_transition_coverage(spec: CheckSpec, cwd: Path) -> CheckResult:
    """Compare a lifecycle FSM table's full edge set against exercised coverage.

    Args (read from ``spec.args``):
        table: The table name to cover -- ``"wave"`` / ``"phase"`` /
            ``"iter"`` / ``"spec"``.
        covered_edges: Optional explicit covered-set (a list of
            ``[frm, to, guard]`` triples). When omitted the kind runs the
            in-process machine to collect coverage.

    Returns:
        :class:`CheckResult` with ``status="pass"`` iff ``covered_edges ==
        table_edges`` (every declared edge was exercised); ``status="fail"``
        (naming the uncovered edges) when one or more table edges have no
        covering rule, or when the args are malformed. Never raises -- a bad
        criterion degrades to a failed check, not an aborted run.
    """
    name = spec.args.get("table")
    if not isinstance(name, str) or name not in _TABLE_NAMES:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"missing or unknown arg 'table' (expected one of {sorted(_TABLE_NAMES)})",
        )

    declared = table_edges(name)

    raw_covered = spec.args.get("covered_edges")
    try:
        if raw_covered is None:
            covered = collect_covered_edges(name)
        else:
            covered = _parse_covered_arg(raw_covered)
    except ValueError as exc:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=str(exc),
        )
    except ImportError as exc:
        # The machine path imports the dev-only ``hypothesis`` dependency,
        # absent from a runtime ``uv tool install``. Degrade to a failed check
        # naming the missing dep rather than aborting the whole audit run.
        logger.debug(f"check_transition_coverage missing-dep table={name!r} reason={exc!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=(
                f"table={name} machine-coverage path needs the optional "
                f"'hypothesis' dependency (dev extra), which is not installed"
            ),
        )
    except LifecycleError as exc:
        # The machine asserts every applied edge is table-legal; an illegal
        # edge would surface here. Degrade rather than abort the audit run.
        logger.debug(f"check_transition_coverage machine-fail table={name!r} reason={exc!r}")
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"machine took an illegal edge for table={name}: {exc}",
        )

    uncovered = declared - covered
    if uncovered:
        return CheckResult(
            name=spec.name,
            kind=spec.kind,
            passed=False,
            status="fail",
            details=f"table={name} uncovered edges: {_format_edges(uncovered)}",
        )

    logger.debug(
        f"check_transition_coverage ok table={name!r} edges={len(declared)} covered={len(covered)}"
    )
    return CheckResult(
        name=spec.name,
        kind=spec.kind,
        passed=True,
        status="pass",
        details=f"table={name} all {len(declared)} edges covered",
    )


__all__ = [
    "Edge",
    "check_transition_coverage",
    "collect_covered_edges",
    "table_edges",
]
