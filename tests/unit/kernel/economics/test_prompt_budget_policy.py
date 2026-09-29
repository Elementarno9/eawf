"""The prompt budget: one ceiling over the window, judged on measured sizes."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.economics.prompt_budget import (
    DEFAULT_PROMPT_BUDGET,
    UNDROPPABLE_CLASSES,
    BudgetClassId,
    ClassStatus,
    PromptBudgetOutcome,
    PromptBudgetPolicy,
    RenderedSize,
    evaluate_prompt_budget,
)
from eawf.kernel.runtime.capsule import AuthorityCapsule
from tests.unit.kernel.runtime.test_authority_capsule import capsule_fields

#: The usable window of the shipped policy: 200,000 less 32,000 reserved.
USABLE = 168_000


def policy(**overrides: Any) -> PromptBudgetPolicy:
    """Return the shipped policy with *overrides* applied, validated afresh."""
    return PromptBudgetPolicy.model_validate(
        {**DEFAULT_PROMPT_BUDGET.model_dump(mode="json"), **overrides}
    )


def allocations(**tokens: int) -> list[dict[str, Any]]:
    """Return the shipped allocations with the named classes' token ceilings changed."""
    rows = DEFAULT_PROMPT_BUDGET.model_dump(mode="json")["allocations"]
    return [{**row, "max_tokens": tokens.get(row["class_id"], row["max_tokens"])} for row in rows]


def measured(class_id: BudgetClassId, tokens: int, size: int | None = None) -> RenderedSize:
    """Return a measured size of *class_id*."""
    return RenderedSize(class_id=class_id, tokens=tokens, bytes=size)


def every_class_at(tokens: int) -> list[RenderedSize]:
    """Return a measured size of *tokens* for every class, bytes included."""
    return [measured(item, tokens, tokens) for item in BudgetClassId]


# ---- ECON-001: allocations sum to at most the usable window ------------------


def test_econ_001_the_shipped_policy_fits_its_window() -> None:
    total = sum(row.max_tokens for row in DEFAULT_PROMPT_BUDGET.allocations)
    assert total <= DEFAULT_PROMPT_BUDGET.usable_window_tokens == USABLE


def test_econ_001_allocations_summing_exactly_to_the_usable_window_validate() -> None:
    spare = USABLE - sum(row.max_tokens for row in DEFAULT_PROMPT_BUDGET.allocations)
    exact = policy(allocations=allocations(memory_injection=8_000 + spare))
    assert sum(row.max_tokens for row in exact.allocations) == USABLE


def test_econ_001_one_token_over_the_usable_window_fails_validation() -> None:
    spare = USABLE - sum(row.max_tokens for row in DEFAULT_PROMPT_BUDGET.allocations)
    with pytest.raises(ValidationError, match="over the usable window"):
        policy(allocations=allocations(memory_injection=8_000 + spare + 1))


def test_econ_001_a_reservation_that_leaves_no_window_fails_validation() -> None:
    with pytest.raises(ValidationError, match="leaves no usable window"):
        policy(input_window_tokens=32_000)


def test_econ_001_a_class_with_no_row_fails_validation() -> None:
    rows = [row for row in allocations() if row["class_id"] != "tool_catalog"]
    with pytest.raises(ValidationError, match="no allocation: tool_catalog"):
        policy(allocations=rows)


def test_econ_001_a_class_allocated_twice_fails_validation() -> None:
    rows = allocations()
    with pytest.raises(ValidationError, match="allocated twice: memory_injection"):
        policy(allocations=[*rows, rows[-1]])


def test_econ_001_an_empty_allocation_set_fails_validation() -> None:
    with pytest.raises(ValidationError):
        policy(allocations=[])


def test_econ_001_a_non_integer_ceiling_fails_validation() -> None:
    rows = allocations()
    rows[0] = {**rows[0], "max_tokens": "4000"}
    with pytest.raises(ValidationError):
        policy(allocations=rows)


def test_econ_001_an_undeclared_key_fails_validation() -> None:
    with pytest.raises(ValidationError):
        policy(silent_truncation=True)


# ---- ECON-002 / RUN-013: the anchors are never droppable -------------------


def test_econ_002_the_capsule_and_the_packet_are_the_undroppable_classes() -> None:
    assert (
        frozenset({BudgetClassId.AUTHORITY_CAPSULE, BudgetClassId.TASK_PACKET})
        == UNDROPPABLE_CLASSES
    )


def test_econ_002_the_capsule_below_the_highest_priority_fails_validation() -> None:
    rows = [
        {**row, "priority": 0} if row["class_id"] == "authority_capsule" else row
        for row in allocations()
    ]
    with pytest.raises(ValidationError, match="authority_capsule must carry the highest"):
        policy(allocations=rows)


@pytest.mark.parametrize("mode", ["deny_dispatch", "degrade_by_priority"])
def test_econ_002_an_exhausted_anchor_is_never_dropped_under_any_mode(mode: str) -> None:
    lowest = [
        {**row, "priority": 0}
        if row["class_id"] == "task_packet"
        else {**row, "priority": 100}
        if row["class_id"] == "authority_capsule"
        else row
        for row in allocations()
    ]
    judged = evaluate_prompt_budget(
        policy(on_exhaustion=mode, allocations=lowest),
        [
            measured(BudgetClassId.AUTHORITY_CAPSULE, 4_001),
            measured(BudgetClassId.TASK_PACKET, 24_001),
        ],
    )
    assert not judged.admitted
    assert judged.dropped == ()
    assert set(judged.exhausted) == UNDROPPABLE_CLASSES


def test_econ_002_an_outcome_that_drops_an_anchor_fails_validation() -> None:
    judged = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, [])
    with pytest.raises(ValidationError, match="never droppable"):
        PromptBudgetOutcome.model_validate(
            {**judged.model_dump(mode="json"), "dropped": ["task_packet"]}
        )


def test_run_013_compaction_by_priority_keeps_authority_criteria_base_and_receipts() -> None:
    """Degrading drops whole low-priority classes and leaves the anchors whole.

    The capsule carries the authority and the criteria digest; the task
    packet carries the criteria, the decision receipts and the exact base.
    Every other class over its ceiling goes, and neither anchor is touched.
    """
    over = [
        measured(item, row.max_tokens + 1)
        for item in BudgetClassId
        if item not in UNDROPPABLE_CLASSES
        for row in (DEFAULT_PROMPT_BUDGET.allocation(item),)
    ]
    anchors = [
        measured(BudgetClassId.AUTHORITY_CAPSULE, 10),
        measured(BudgetClassId.TASK_PACKET, 10),
    ]
    judged = evaluate_prompt_budget(policy(on_exhaustion="degrade_by_priority"), over + anchors)
    assert judged.admitted
    kept = {row.class_id: row.status for row in judged.verdicts}
    assert kept[BudgetClassId.AUTHORITY_CAPSULE] is ClassStatus.WITHIN
    assert kept[BudgetClassId.TASK_PACKET] is ClassStatus.WITHIN
    assert judged.dropped == (
        BudgetClassId.MEMORY_INJECTION,
        BudgetClassId.STEERING_ZONE2,
        BudgetClassId.TOOL_CATALOG,
        BudgetClassId.STEERING_ZONE1,
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("criteria_digest", f"sha256:{'e' * 64}"),
        ("authority", {"git": "none"}),
    ],
)
def test_run_013_a_changed_anchor_moves_the_digest_a_reassertion_must_echo(
    field: str, value: Any
) -> None:
    """A context rebuilt with other criteria or authority echoes another digest.

    The worker reasserts the capsule digest after a compaction, and the
    handshake refuses any digest other than the bound one, so a compaction
    that altered either anchor is refused rather than continued.
    """
    base = AuthorityCapsule.seal(capsule_fields())
    changed = capsule_fields()
    changed[field] = {**changed[field], **value} if isinstance(value, dict) else value
    assert AuthorityCapsule.seal(changed).contract_digest != base.contract_digest


# ---- ECON-003: measured sizes only, and unmeasured is unavailable -----------


def test_econ_003_a_class_nobody_measured_is_unavailable_not_within() -> None:
    judged = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, [])
    assert {row.status for row in judged.verdicts} == {ClassStatus.UNAVAILABLE}
    assert judged.admitted


def test_econ_003_a_class_declared_unmeasured_is_never_enforced() -> None:
    rows = [
        {**row, "measured": False} if row["class_id"] == "tool_catalog" else row
        for row in allocations()
    ]
    judged = evaluate_prompt_budget(
        policy(allocations=rows), [measured(BudgetClassId.TOOL_CATALOG, 10**6)]
    )
    verdict = {row.class_id: row for row in judged.verdicts}[BudgetClassId.TOOL_CATALOG]
    assert verdict.status is ClassStatus.UNAVAILABLE
    assert judged.admitted


def test_econ_003_bytes_without_tokens_leave_the_token_ceiling_unavailable() -> None:
    judged = evaluate_prompt_budget(
        DEFAULT_PROMPT_BUDGET, [RenderedSize(class_id=BudgetClassId.TASK_PACKET, bytes=10)]
    )
    verdict = {row.class_id: row for row in judged.verdicts}[BudgetClassId.TASK_PACKET]
    assert verdict.status is ClassStatus.UNAVAILABLE
    assert verdict.measured_bytes == 10


def test_econ_003_a_byte_ceiling_binds_where_bytes_were_measured() -> None:
    ceiling = DEFAULT_PROMPT_BUDGET.allocation(BudgetClassId.STEERING_ZONE1).max_bytes
    assert ceiling is not None
    judged = evaluate_prompt_budget(
        DEFAULT_PROMPT_BUDGET,
        [RenderedSize(class_id=BudgetClassId.STEERING_ZONE1, bytes=ceiling + 1)],
    )
    assert judged.exhausted == (BudgetClassId.STEERING_ZONE1,)


def test_econ_003_every_class_measured_at_its_ceiling_is_within() -> None:
    sizes = [
        measured(row.class_id, row.max_tokens, row.max_bytes)
        for row in DEFAULT_PROMPT_BUDGET.allocations
    ]
    judged = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, sizes)
    assert {row.status for row in judged.verdicts} == {ClassStatus.WITHIN}


def test_econ_003_two_sizes_for_one_class_are_refused() -> None:
    with pytest.raises(ValueError, match="two sizes reported for task_packet"):
        evaluate_prompt_budget(
            DEFAULT_PROMPT_BUDGET,
            [measured(BudgetClassId.TASK_PACKET, 1), measured(BudgetClassId.TASK_PACKET, 2)],
        )


def test_econ_003_a_negative_measurement_is_refused() -> None:
    with pytest.raises(ValidationError):
        RenderedSize(class_id=BudgetClassId.TASK_PACKET, tokens=-1)


# ---- ECON-004: exhaustion is a named outcome, never a truncation ------------


def test_econ_004_deny_names_the_exhausted_class() -> None:
    judged = evaluate_prompt_budget(
        DEFAULT_PROMPT_BUDGET, [measured(BudgetClassId.MEMORY_INJECTION, 8_001)]
    )
    assert not judged.admitted
    assert judged.exhausted == (BudgetClassId.MEMORY_INJECTION,)
    verdict = {row.class_id: row for row in judged.verdicts}[BudgetClassId.MEMORY_INJECTION]
    assert (verdict.measured_tokens, verdict.ceiling_tokens) == (8_001, 8_000)


def test_econ_004_degrade_names_the_dropped_class_whole() -> None:
    judged = evaluate_prompt_budget(
        policy(on_exhaustion="degrade_by_priority"),
        [measured(BudgetClassId.MEMORY_INJECTION, 8_001)],
    )
    assert judged.admitted
    assert judged.dropped == (BudgetClassId.MEMORY_INJECTION,)
    assert judged.exhausted == ()


def test_econ_004_silent_truncation_is_not_an_exhaustion_mode() -> None:
    with pytest.raises(ValidationError):
        policy(on_exhaustion="truncate")


def test_econ_004_an_outcome_hiding_its_exhaustion_fails_validation() -> None:
    judged = evaluate_prompt_budget(
        DEFAULT_PROMPT_BUDGET, [measured(BudgetClassId.MEMORY_INJECTION, 8_001)]
    )
    with pytest.raises(ValidationError, match="exhausted must name"):
        PromptBudgetOutcome.model_validate(
            {**judged.model_dump(mode="json"), "exhausted": [], "admitted": True}
        )


def test_econ_004_every_class_one_over_is_exhausted_by_name() -> None:
    sizes = [
        measured(row.class_id, row.max_tokens + 1) for row in DEFAULT_PROMPT_BUDGET.allocations
    ]
    judged = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, sizes)
    assert set(judged.exhausted) == set(BudgetClassId)


# ---- ECON-005: the policy digest moves with every revision -----------------


def test_econ_005_a_revision_moves_the_policy_digest() -> None:
    assert policy(revision=2).policy_digest != DEFAULT_PROMPT_BUDGET.policy_digest
    assert policy().policy_digest == DEFAULT_PROMPT_BUDGET.policy_digest


def test_econ_005_the_outcome_carries_the_digest_it_was_decided_under() -> None:
    judged = evaluate_prompt_budget(DEFAULT_PROMPT_BUDGET, every_class_at(0))
    assert judged.policy_digest == DEFAULT_PROMPT_BUDGET.policy_digest
    assert judged.policy_revision == 1
