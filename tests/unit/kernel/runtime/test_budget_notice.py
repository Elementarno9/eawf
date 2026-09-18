"""The budget notice blocks nothing, and no caller can make it.

``blocking`` is ``Literal[False]`` rather than a ``bool`` defaulted to
false, and the difference is the whole point of the field: a default is a
value a caller may override, a literal is a value no caller can supply.
The notice is written on the path that reaps a live process group, so a
constructible-blocking notice would be a notice that could stall the reap
it exists to explain.

Which control a budget termination opens is also settled here rather than
per caller. ``cancel`` is reused, and :func:`compile_budget_termination`
proves at import that a confirmed effect of it ends the Run -- so a future
edit that took ``cancel`` out of the terminal map fails at startup instead
of shipping a cap that terminates nothing.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.budget_notice import (
    BUDGET_TERMINATION_CONTROL,
    BUDGET_TERMINATION_STATUS,
    BudgetNotice,
    compile_budget_termination,
)
from eawf.kernel.runtime.control import TERMINAL_EFFECT_STATUS
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.state.epoch2.run import RunStatus

RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
REQUEST_REF = "CTL-0000000a"
AT = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def notice(**overrides: object) -> BudgetNotice:
    """Build one notice, with *overrides* applied to the default row."""
    fields: dict[str, object] = {
        "run_ref": RUN_URN,
        "control_request_ref": REQUEST_REF,
        "cap_tokens": 1_000,
        "observed_tokens": 1_000,
        "noticed_at": AT,
    }
    fields.update(overrides)
    return BudgetNotice.model_validate(fields)


# ---------------------------------------------------------------------------
# NTFY-003: blocking is a literal false
# ---------------------------------------------------------------------------


def test_a_budget_notice_is_not_blocking() -> None:
    assert notice().blocking is False


def test_no_constructor_can_set_the_notice_blocking() -> None:
    """The literal refuses the value; a bool default would have taken it."""
    with pytest.raises(ValidationError, match="blocking"):
        notice(blocking=True)


def test_restating_the_literal_false_is_still_accepted() -> None:
    """A caller that spells the value out gets the same non-blocking row."""
    assert notice(blocking=False).blocking is False


def test_a_notice_cannot_be_made_blocking_after_construction() -> None:
    """The record is frozen, so the field has no second chance to move."""
    row = notice()

    with pytest.raises(ValidationError):
        row.blocking = True  # type: ignore[misc]


def test_a_notice_refuses_an_unknown_field() -> None:
    """An absorbed misspelling is how a blocking flag would sneak back in."""
    with pytest.raises(ValidationError, match="extra_forbidden"):
        notice(is_blocking=True)


def test_the_notice_payload_kind_discriminates_it_from_a_control_fact() -> None:
    assert notice().model_dump(mode="json")["payload_kind"] == "budget_notice"
    assert notice().model_dump(mode="json")["blocking"] is False


# ---------------------------------------------------------------------------
# Which control a budget termination opens
# ---------------------------------------------------------------------------


def test_a_budget_termination_reuses_cancel() -> None:
    """No driver honours "budget"; the cap is the daemon's own arithmetic."""
    assert BUDGET_TERMINATION_CONTROL is ControlKind.CANCEL


def test_the_reused_control_really_terminalizes_the_run() -> None:
    assert TERMINAL_EFFECT_STATUS[BUDGET_TERMINATION_CONTROL] is RunStatus.CANCELLED
    assert BUDGET_TERMINATION_STATUS is RunStatus.CANCELLED


@pytest.mark.parametrize(
    "control",
    [kind for kind, status in TERMINAL_EFFECT_STATUS.items() if status is None],
    ids=lambda kind: kind.value,
)
def test_compiling_a_control_that_ends_no_run_is_refused(control: ControlKind) -> None:
    """Routing through one would append three facts and terminate nothing."""
    with pytest.raises(ValueError, match="ends no Run"):
        compile_budget_termination(control)


@pytest.mark.parametrize(
    "control",
    [kind for kind, status in TERMINAL_EFFECT_STATUS.items() if status is not None],
    ids=lambda kind: kind.value,
)
def test_compiling_a_terminalizing_control_returns_it_unchanged(control: ControlKind) -> None:
    assert compile_budget_termination(control) is control


# ---------------------------------------------------------------------------
# Boundaries and error paths of the recorded reading
# ---------------------------------------------------------------------------


def test_a_zero_cap_is_recorded_rather_than_refused() -> None:
    """A cap that floors to zero is a real configuration, and every reading crosses it."""
    assert notice(cap_tokens=0, observed_tokens=0).cap_tokens == 0


def test_a_reading_exactly_at_the_cap_is_a_recordable_notice() -> None:
    row = notice(cap_tokens=1_000, observed_tokens=1_000)

    assert row.observed_tokens == row.cap_tokens


def test_a_negative_observation_is_refused() -> None:
    with pytest.raises(ValidationError, match="observed_tokens"):
        notice(observed_tokens=-1)


def test_a_negative_cap_is_refused() -> None:
    with pytest.raises(ValidationError, match="cap_tokens"):
        notice(cap_tokens=-1)


def test_a_notice_naming_no_control_request_is_refused() -> None:
    """The notice always names what was attempted, even when it lost the lease."""
    with pytest.raises(ValidationError, match="control_request_ref"):
        notice(control_request_ref="not-a-request-id")


def test_a_notice_naming_a_non_run_urn_is_refused() -> None:
    with pytest.raises(ValidationError, match="run_ref"):
        notice(run_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042")


def test_a_notice_with_a_naive_timestamp_is_refused() -> None:
    with pytest.raises(ValidationError, match="noticed_at"):
        notice(noticed_at=datetime(2026, 9, 18, 12, 0))
