"""CR-02: lens findings and the content digest are invariant to input key order.

:func:`~eawf.kernel.state.epoch2.plan_revision.plan_content_digest` and
:func:`~eawf.workflow.planning.lenses.run_plan_lenses` both operate on the
*validated* :class:`~eawf.kernel.state.epoch2.plan_revision.PlanBody`,
never on the raw mapping a proposal arrived as. That is what should make
"same plan and policy" order-independent: two JSON objects naming the
same fields in a different key order parse to the same model, and the
same model always digests and lenses to the same bytes. This property
pins that fact against the concrete regression it guards -- a future
change that digests or lenses the raw un-parsed payload instead of the
typed model would reintroduce order sensitivity, and this test would
catch it.

List order is left alone for the key-order property below: a Task's
position in ``tasks`` is part of what :func:`plan_content_digest` hashes
(the digest sorts mapping keys, never list order), so two Tasks in a
different order are not guaranteed to digest the same, and this property
only claims mapping key order is insignificant.

A second property further down shuffles task order and each Task's own
criteria order instead, and checks only that :func:`run_plan_lenses`'s
findings do not move -- not the digest, which is expected to differ.
Both axes are referential (``depends_on`` names a Task by URN, and the
duplicate-mechanism lens scopes a criterion id to the Task that declares
it, not to either list's position), so a lens run is entitled to agree
on findings across either shuffle even where the digest does not.
"""

from __future__ import annotations

import random
from datetime import UTC, datetime
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from eawf.kernel.state.epoch2.plan_revision import PlanBody, plan_content_digest
from eawf.workflow.planning.lenses import run_plan_lenses

SLOT = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
TRACK = f"{SLOT}/track/TRK-RUNTIME"
MILESTONE = f"{SLOT}/milestone/MLS-0050"
BATCH = f"{SLOT}/batch/BAT-0001"
TASK = f"{SLOT}/task/EAWF-0001"
SECOND_TASK = f"{SLOT}/task/EAWF-0002"
REPOSITORY = f"{SLOT}/repository/REP-EAWF"

AT = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _criterion(id_: str) -> dict[str, Any]:
    """Return one loader-valid criterion payload keyed *id_*, shared by two Tasks.

    Sharing one id on purpose seeds a real DUPLICATE_MECHANISM finding, so
    the property below proves more than "an empty tuple equals an empty
    tuple" -- the shuffled and canonical runs must agree on actual
    content, not just on absence of it.
    """
    return {
        "id": id_,
        "text": "the described behaviour holds",
        "kind": "functional_suitability",
        "acceptance_style": "binary",
        "evidence_kind": "deterministic",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "uv run pytest tests/unit/kernel/state exits zero",
    }


def _plan_payload() -> dict[str, Any]:
    """Return one loader-valid plan body payload carrying two live findings.

    ``CR-SHARED`` is declared once on each of two Tasks -- reusing an id
    across Tasks is not a defect (F7), so this must NOT fire
    DUPLICATE_MECHANISM. ``CR-DUP`` is declared twice on the first Task's
    own ``criteria`` list, which IS the defect the lens catches, so the
    property below proves more than "an empty tuple equals an empty
    tuple": a real DUPLICATE_MECHANISM finding, scoped to the one Task
    that actually repeats an id, must survive both shuffles. One
    producer-less surface (IDLE_CONTRACT_PRODUCER_MISSING) fires too, over
    a two-Task, two-Batch shape wide enough to exercise nested lists of
    dicts at several levels.
    """
    return {
        "milestone_urn": MILESTONE,
        "milestone": {
            "key": "MLS-0050",
            "primary_track_ref": TRACK,
            "title": "Ship the described behaviour",
            "outcome": "An operator observes the described behaviour end to end.",
            "appetite": "M",
            "exclusions": ["none"],
            "acceptance_journey": [
                {
                    "step_id": "AS-01",
                    "actor": "operator",
                    "action": "observe the described behaviour",
                    "expected_observation": "the behaviour is present",
                    "evidence_kinds": ["artifact"],
                }
            ],
        },
        "batches": [{"urn": BATCH, "repository_ref": REPOSITORY}],
        "tasks": [
            {
                "urn": TASK,
                "batch_ref": BATCH,
                "priority": "P1",
                "intent": "do the described work",
                "criteria": [
                    _criterion("CR-SHARED"),
                    _criterion("CR-DUP"),
                    _criterion("CR-DUP"),
                ],
            },
            {
                "urn": SECOND_TASK,
                "batch_ref": BATCH,
                "priority": "P2",
                "intent": "do the other described work",
                "criteria": [_criterion("CR-SHARED")],
            },
        ],
        "citations": [],
        "new_surfaces": [{"surface_id": "SUR-01", "kind": "rpc"}],
    }


def _shuffle_mapping_keys(value: Any, rng: random.Random) -> Any:
    """Return *value* with every nested mapping's key order randomized.

    List order is left exactly as given -- only ``dict`` key order, which
    JSON treats as insignificant, is shuffled.

    Args:
        value: A JSON-shaped value (nested dicts, lists, and scalars).
        rng: The random source driving the shuffle.

    Returns:
        A structurally equal value with every mapping's keys reordered.
    """
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _shuffle_mapping_keys(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_shuffle_mapping_keys(item, rng) for item in value]
    return value


pytestmark = pytest.mark.property


@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=25, deadline=None)
def test_lens_output_and_digest_are_invariant_to_key_order(seed: int) -> None:
    """Shuffling every mapping's key order yields byte-identical findings and digest."""
    canonical = PlanBody.model_validate(_plan_payload())
    shuffled_payload = _shuffle_mapping_keys(_plan_payload(), random.Random(seed))
    shuffled = PlanBody.model_validate(shuffled_payload)

    assert shuffled == canonical
    assert plan_content_digest(shuffled) == plan_content_digest(canonical)
    assert run_plan_lenses(shuffled) == run_plan_lenses(canonical)


def test_lens_output_carries_the_seeded_findings() -> None:
    """The fixture actually exercises DUPLICATE_MECHANISM and IDLE_CONTRACT.

    Guards the property above against vacuously proving determinism over
    an empty findings tuple.
    """
    body = PlanBody.model_validate(_plan_payload())

    codes = {finding.code.value for finding in run_plan_lenses(body)}

    assert "plan_duplicate_criterion_id" in codes
    assert "plan_idle_contract_producer_missing" in codes


def test_duplicate_mechanism_lens_scopes_ids_per_task() -> None:
    """F7: a criterion id shared by two different Tasks submits.

    The fixture declares ``CR-SHARED`` once on each Task (not a defect)
    and ``CR-DUP`` twice on one Task (a real defect). Exactly one
    DUPLICATE_MECHANISM finding must fire, naming ``CR-DUP`` and the one
    Task that repeats it -- not ``CR-SHARED``, and not both Tasks.
    """
    body = PlanBody.model_validate(_plan_payload())

    findings = [
        finding
        for finding in run_plan_lenses(body)
        if finding.code.value == "plan_duplicate_criterion_id"
    ]

    assert len(findings) == 1
    assert findings[0].entity_refs[0] == "CR-DUP"


def _shuffle_task_and_criterion_order(
    payload: dict[str, Any], rng: random.Random
) -> dict[str, Any]:
    """Return *payload* with task order and each Task's criteria order shuffled.

    Both axes address their target by reference rather than by position
    -- ``depends_on`` names a Task by URN, and the duplicate-mechanism
    lens scopes a criterion id to the Task that declares it, not to
    either list's slot -- so reordering either list changes no plan
    meaning a lens is entitled to read.

    Args:
        payload: A loader-valid plan body payload.
        rng: The random source driving the shuffle.

    Returns:
        A structurally equal payload with ``tasks`` and each Task's own
        ``criteria`` reordered.
    """
    tasks = [dict(task, criteria=list(task["criteria"])) for task in payload["tasks"]]
    for task in tasks:
        rng.shuffle(task["criteria"])
    rng.shuffle(tasks)
    return {**payload, "tasks": tasks}


@given(seed=st.integers(min_value=0, max_value=2**32 - 1))
@settings(max_examples=25, deadline=None)
def test_lens_output_is_invariant_to_task_and_criterion_order(seed: int) -> None:
    """M3: reordering Tasks or one Task's own criteria list moves no finding.

    Unlike the key-order property above, this shuffle is not expected to
    preserve ``plan_content_digest`` or model equality -- a Task's
    position is part of what the plan's bytes digest, so two orderings
    are different plans by that measure. What must not move is which
    findings a lens run reports: the duplicate-mechanism lens (like the
    approval gate) scopes a criterion id to its own Task, so its findings
    -- and the fixed lens order they slot into -- do not depend on
    either list's order.
    """
    canonical = PlanBody.model_validate(_plan_payload())
    reordered = PlanBody.model_validate(
        _shuffle_task_and_criterion_order(_plan_payload(), random.Random(seed))
    )

    assert run_plan_lenses(reordered) == run_plan_lenses(canonical)
