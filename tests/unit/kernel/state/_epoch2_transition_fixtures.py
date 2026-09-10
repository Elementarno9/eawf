"""Fixture adapter for the epoch-2 transition registry tests.

Three jobs live here so the guard sweep, the illegal-edge property and
the recovery walk all drive the reducers from the same data. It loads the
seed records under ``tests/fixtures/epoch2/transitions``, it supplies the
field values a target status makes facts, and it stands in for the
observed-fact reader: at this checkpoint nothing reads a host back, so
:func:`observations_for` presents exactly the facts an edge's guards ask
for and :func:`observations_for` with ``withheld`` presents none of them.

The mermaid parser is deliberately independent of the renderer. Comparing
a renderer's output with itself proves nothing, so the parity check reads
the checked-in diagram with this parser and compares the parsed edges
against the registry rows in both directions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import yaml

from eawf.kernel.state.epoch2.batch import DeliveryBatch
from eawf.kernel.state.epoch2.milestone import Milestone
from eawf.kernel.state.epoch2.run import Run
from eawf.kernel.state.epoch2.task import Task
from eawf.kernel.state.epoch2.track import Track
from eawf.kernel.state.epoch2.transitions import (
    OBSERVED_GUARD_FACTS,
    LifecycleEntity,
    LifecycleStatus,
    ObservedFact,
    TransitionRow,
)
from eawf.workflow.lifecycle.epoch2 import LifecycleRecord

#: ``tests/fixtures/epoch2/transitions`` - four levels up lands on ``tests/``.
FIXTURES: Final = Path(__file__).resolve().parents[3] / "fixtures" / "epoch2" / "transitions"

#: The moment every fixture transition is stamped with. The reducers take
#: the time from the caller, so a fixed stamp keeps a run reproducible.
AT: Final = datetime(2026, 9, 8, 3, 0, tzinfo=UTC)

#: Which model each entity's seed records validate through.
RECORD_MODEL: Final[dict[LifecycleEntity, type[LifecycleRecord]]] = {
    LifecycleEntity.TRACK: Track,
    LifecycleEntity.MILESTONE: Milestone,
    LifecycleEntity.DELIVERY_BATCH: DeliveryBatch,
    LifecycleEntity.TASK: Task,
    LifecycleEntity.RUN: Run,
}

_BINDING: Final[dict[str, Any]] = {
    "head_sha": "a" * 40,
    "tree_sha": "b" * 40,
    "contract_digest": "sha256:" + "c" * 64,
    "policy_revision": 1,
    "evidence_digest": "sha256:" + "d" * 64,
}

_REASON: Final[dict[str, Any]] = {
    "code": "operator-stopped-the-work",
    "message": "The operator stopped the work before it integrated.",
    "evidence_refs": [],
}

_CRITERION: Final[dict[str, Any]] = {
    "id": "CR-01",
    "text": "the published wheel installs into a clean environment",
    "kind": "functional_suitability",
    "acceptance_style": "binary",
    "evidence_kind": "deterministic",
    "gate_ids": ["G-01"],
    "quality_dimension": "functional_suitability",
    "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
}

#: The value each required field takes when a fixture edge supplies it.
#: ``suspension_reason`` is absent because it is the one field whose value
#: depends on the target status rather than only on the field name.
_UPDATE_VALUES: Final[dict[str, Any]] = {
    "accepted_binding": _BINDING,
    "acceptance_bundle_revision": 1,
    "active_run_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010",
    "batch_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/batch/BAT-0007",
    "criteria": [_CRITERION],
    "current_head_binding": _BINDING,
    "due_scope": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030",
    "ended_at": "2026-09-08T02:00:00Z",
    "failure": _REASON,
    "integrated_binding": _BINDING,
    "started_at": "2026-09-08T01:00:00Z",
    "target_branch": "feature/eawf-v0.7",
}


def _seed_document() -> dict[str, Any]:
    """Return the parsed seed-record document."""
    parsed = yaml.safe_load((FIXTURES / "seed_records.yaml").read_text(encoding="utf-8"))
    assert isinstance(parsed, dict)
    return parsed


def seed_record(entity: LifecycleEntity, status: LifecycleStatus) -> LifecycleRecord:
    """Return the seeded record of *entity* sitting in *status*.

    Args:
        entity: The entity to build.
        status: The status the record should be in.

    Returns:
        A validated record.

    Raises:
        KeyError: No seed is declared for that entity or status.
    """
    document = _seed_document()["records"][entity.value][str(status)]
    return RECORD_MODEL[entity].model_validate(document)


def legacy_task() -> Task:
    """Return a Task projected from epoch 1, otherwise ready to move."""
    return Task.model_validate(_seed_document()["legacy_origin"]["task"])


def updates_for(row: TransitionRow) -> dict[str, Any]:
    """Return the field values *row* requires of its caller.

    Args:
        row: The registry row about to be applied.

    Returns:
        A mapping of every field in ``row.required_updates`` to a value
        the target status accepts. A suspension reason is set only when
        the move lands on ``SUSPENDED``, and cleared otherwise, because
        the model binds it to that one status.
    """
    updates: dict[str, Any] = {}
    for name in row.required_updates:
        if name == "suspension_reason":
            updates[name] = "AWAITING_LEASE" if str(row.to) == "SUSPENDED" else None
            continue
        updates[name] = _UPDATE_VALUES[name]
    return updates


def observations_for(row: TransitionRow) -> frozenset[ObservedFact]:
    """Return the observed facts *row* needs, as a reader would present them.

    Args:
        row: The registry row about to be applied.

    Returns:
        Exactly the facts the row's observation-backed guards ask for;
        empty when the row asks for none.
    """
    return frozenset(
        OBSERVED_GUARD_FACTS[guard] for guard in row.guards if guard in OBSERVED_GUARD_FACTS
    )


def diagram_path(entity: LifecycleEntity) -> Path:
    """Return the checked-in diagram of *entity*."""
    return FIXTURES / f"{entity.value}.mmd"


class DiagramParseError(ValueError):
    """A checked-in diagram is not the shape the parity check reads."""


def _parse_edge(body: str, line: str) -> tuple[str, str, str, tuple[str, ...]]:
    """Return the edge a ``-->`` line declares.

    Args:
        body: The line with its indentation stripped.
        line: The original line, quoted in any rejection.

    Returns:
        The source status, target status, verb and guard names.

    Raises:
        DiagramParseError: The line is not
            ``SRC --> DST: verb [guard+guard]``.
    """
    head, _, tail = body.partition("-->")
    target, separator, label = tail.partition(":")
    if not separator:
        raise DiagramParseError(f"edge line has no verb label: {line!r}")
    label = label.strip()
    if not label.endswith("]") or "[" not in label:
        raise DiagramParseError(f"edge line has no guard bracket: {line!r}")
    verb, _, guard_text = label[:-1].partition("[")
    guards = guard_text.strip()
    parsed_guards = () if guards == "none" else tuple(guards.split("+"))
    return head.strip(), target.strip(), verb.strip(), parsed_guards


def parse_state_diagram(
    text: str,
) -> tuple[tuple[str, ...], tuple[tuple[str, str, str, tuple[str, ...]], ...]]:
    """Return the states and edges a mermaid state diagram declares.

    Args:
        text: The diagram source.

    Returns:
        The declared state names in declaration order, and the edges as
        ``(from, to, verb, guards)`` tuples.

    Raises:
        DiagramParseError: The header is missing, a line is malformed, an
            edge names a state the diagram never declared, or one edge is
            drawn twice.
    """
    states: list[str] = []
    edges: list[tuple[str, str, str, tuple[str, ...]]] = []
    header_seen = False
    for line in text.splitlines():
        body = line.strip()
        if not body or body.startswith("%%"):
            continue
        if body == "stateDiagram-v2":
            header_seen = True
            continue
        if not header_seen:
            raise DiagramParseError(f"content before the stateDiagram-v2 header: {line!r}")
        if "-->" not in body:
            states.append(body)
            continue
        edges.append(_parse_edge(body, line))
    if not header_seen:
        raise DiagramParseError("diagram declares no stateDiagram-v2 header")
    declared = set(states)
    for frm, to, _verb, _guards in edges:
        undeclared = {frm, to} - declared
        if undeclared:
            raise DiagramParseError(f"edge names undeclared states {sorted(undeclared)}")
    drawn = [(frm, to) for frm, to, _verb, _guards in edges]
    if len(set(drawn)) != len(drawn):
        raise DiagramParseError("diagram draws one edge twice")
    return tuple(states), tuple(edges)


__all__ = [
    "AT",
    "FIXTURES",
    "RECORD_MODEL",
    "DiagramParseError",
    "diagram_path",
    "legacy_task",
    "observations_for",
    "parse_state_diagram",
    "seed_record",
    "updates_for",
]
