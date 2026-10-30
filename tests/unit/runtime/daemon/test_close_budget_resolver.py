"""Tests: the single close-budget resolver the daemon, CLI and TUI all read (REL-004).

Boundary + error-path coverage for the public surface introduced to replace the
per-consumer budget literals:
:func:`~eawf.kernel.state.models.resolve_close_budget` and
:class:`~eawf.kernel.state.models.CloseBudget`.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from eawf.kernel.state.models import (
    CLOSE_BUDGET_AXES,
    CLOSE_BUDGET_RECEIPT_PREFIX,
    DEFAULT_CLOSE_INFRASTRUCTURE_RETRY_BUDGET,
    DEFAULT_CLOSE_REPAIR_BUDGET,
    CloseBudget,
    resolve_close_budget,
)

# --------------------------------------------------------------------------
# resolve_close_budget
# --------------------------------------------------------------------------


def test_resolve_close_budget_without_attempt_returns_the_seed_budget() -> None:
    """No attempt yields the seed budget the daemon stamps on a fresh attempt."""
    budget = resolve_close_budget()
    assert budget.repair == DEFAULT_CLOSE_REPAIR_BUDGET
    assert budget.infrastructure_retry == DEFAULT_CLOSE_INFRASTRUCTURE_RETRY_BUDGET


def test_resolve_close_budget_takes_no_attempt() -> None:
    """The resolver reads no live attempt, so passing one fails fast."""
    with pytest.raises(TypeError, match="attempt"):
        resolve_close_budget(attempt="CA-01")  # type: ignore[call-arg]


# --------------------------------------------------------------------------
# CloseBudget
# --------------------------------------------------------------------------


def test_close_budget_rejects_a_negative_axis() -> None:
    """A negative budget is not representable (the counters are ``ge=0``)."""
    with pytest.raises(ValidationError):
        CloseBudget(repair=-1, infrastructure_retry=1)


def test_close_budget_forbids_unknown_keys() -> None:
    """The budget model forbids extras, so a typo can never silently pass."""
    with pytest.raises(ValidationError):
        CloseBudget.model_validate({"repair": 1, "infrastructure_retry": 1, "repare": 1})


def test_close_budget_total_repair_attempts_counts_the_first_dispatch() -> None:
    """The display denominator is the first dispatch plus each funded repair."""
    assert CloseBudget(repair=0, infrastructure_retry=0).total_repair_attempts == 1
    assert CloseBudget(repair=1, infrastructure_retry=0).total_repair_attempts == 2
    assert CloseBudget(repair=7, infrastructure_retry=0).total_repair_attempts == 8


def test_close_budget_remaining_reads_each_axis() -> None:
    """Each axis reads back the counter it bounds."""
    budget = CloseBudget(repair=2, infrastructure_retry=3)
    assert budget.remaining("repair") == 2
    assert budget.remaining("infrastructure_retry") == 3


def test_close_budget_remaining_rejects_an_unknown_axis() -> None:
    """An unknown axis raises rather than defaulting to a funded number."""
    with pytest.raises(KeyError, match="unknown close budget axis"):
        CloseBudget(repair=1, infrastructure_retry=1).remaining("repare")  # type: ignore[arg-type]


def test_close_budget_exhausted_axes_orders_by_the_axis_contract() -> None:
    """Exhausted axes come back in :data:`CLOSE_BUDGET_AXES` order."""
    assert CloseBudget(repair=1, infrastructure_retry=1).exhausted_axes() == ()
    assert CloseBudget(repair=0, infrastructure_retry=1).exhausted_axes() == ("repair",)
    assert CloseBudget(repair=1, infrastructure_retry=0).exhausted_axes() == (
        "infrastructure_retry",
    )
    assert CloseBudget(repair=0, infrastructure_retry=0).exhausted_axes() == CLOSE_BUDGET_AXES


def test_close_budget_receipt_ref_names_the_axis_and_the_funded_value() -> None:
    """The receipt ref carries the attempt, the exhausted axis and the funded value."""
    ref = CloseBudget(repair=2, infrastructure_retry=1).receipt_ref(
        attempt_id="CA-01",
        axis="repair",
    )
    assert ref == f"{CLOSE_BUDGET_RECEIPT_PREFIX}:CA-01:repair:2"


def test_close_budget_receipt_ref_rejects_an_empty_attempt_id() -> None:
    """An anonymous receipt would be unattributable, so it is refused."""
    with pytest.raises(ValueError, match="non-empty close attempt id"):
        CloseBudget(repair=1, infrastructure_retry=1).receipt_ref(attempt_id="", axis="repair")


def test_close_budget_receipt_ref_rejects_an_unknown_axis() -> None:
    """An unknown axis cannot be named in a receipt."""
    with pytest.raises(KeyError, match="unknown close budget axis"):
        CloseBudget(repair=1, infrastructure_retry=1).receipt_ref(
            attempt_id="CA-01",
            axis="repare",  # type: ignore[arg-type]
        )
