"""Unit tests for :mod:`eawf.budget.policy`.

Covers the boundary conditions of the warn/block threshold classifier:
no-budget, well-under, exactly-at-warn, exactly-at-block, over-block.
"""

from __future__ import annotations

from eawf.budget.policy import (
    BLOCK_TAG,
    WARN_TAG,
    classify,
)


def test_classify_none_budget_returns_none() -> None:
    assert classify(consumed=0, budget=None) is None
    assert classify(consumed=10_000, budget=None) is None


def test_classify_under_warn() -> None:
    # 74.9% — still under the warn threshold.
    assert classify(consumed=749, budget=1000) is None
    assert classify(consumed=0, budget=1000) is None


def test_classify_at_75_percent_returns_warn() -> None:
    assert classify(consumed=750, budget=1000) == WARN_TAG
    # mid-range warn band.
    assert classify(consumed=900, budget=1000) == WARN_TAG


def test_classify_at_100_percent_returns_block() -> None:
    assert classify(consumed=1000, budget=1000) == BLOCK_TAG


def test_classify_over_100_percent_returns_block() -> None:
    assert classify(consumed=1500, budget=1000) == BLOCK_TAG
    assert classify(consumed=10**9, budget=1000) == BLOCK_TAG


def test_classify_zero_budget_blocks_immediately() -> None:
    # A zero budget cannot be under-spent — anything is "over budget".
    assert classify(consumed=0, budget=0) == BLOCK_TAG
    assert classify(consumed=1, budget=0) == BLOCK_TAG
