"""Unit tests for :mod:`eawf.runtime.budget.policy`.

Covers the boundary conditions of the over-budget classifier: no-budget,
well-under, just-under, exactly-at-block, over-block.
"""

from __future__ import annotations

from eawf.runtime.budget.policy import (
    BLOCK_TAG,
    classify,
)


def test_classify_none_budget_returns_none() -> None:
    assert classify(consumed=0, budget=None) is None
    assert classify(consumed=10_000, budget=None) is None


def test_classify_under_budget_returns_none() -> None:
    assert classify(consumed=0, budget=1000) is None
    assert classify(consumed=750, budget=1000) is None
    assert classify(consumed=999, budget=1000) is None


def test_classify_at_100_percent_returns_block() -> None:
    assert classify(consumed=1000, budget=1000) == BLOCK_TAG


def test_classify_over_100_percent_returns_block() -> None:
    assert classify(consumed=1500, budget=1000) == BLOCK_TAG
    assert classify(consumed=10**9, budget=1000) == BLOCK_TAG


def test_classify_zero_budget_blocks_immediately() -> None:
    # A zero budget cannot be under-spent — anything is "over budget".
    assert classify(consumed=0, budget=0) == BLOCK_TAG
    assert classify(consumed=1, budget=0) == BLOCK_TAG
