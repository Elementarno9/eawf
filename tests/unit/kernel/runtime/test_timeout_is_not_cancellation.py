"""A timeout is its own typed outcome at every runtime layer, never a cancellation.

Cancellation is what a principal asked for; a timeout is what a clock
did. The two are kept apart by type at each place a runtime outcome is
recorded: the control vocabulary that ends a Run, the command result a
Run reports, the scoped-command output the gateway returns, the lapse of
a provider permission and the durable close attempt. A timeout folded
into ``cancelled`` at any one of them would read as an operator decision
nobody made.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Final, get_args

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.control import TERMINAL_EFFECT_STATUS
from eawf.kernel.runtime.events import CommandPayload
from eawf.kernel.runtime.permission import PermissionResolution
from eawf.kernel.runtime.provider import ControlKind
from eawf.kernel.runtime.semantic import RunScopedCommandOutput
from eawf.kernel.state.enums import CloseAttemptStatus, CloseFailureKind
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.runtime.daemon.methods import close as close_module

AT: Final = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)

#: The controls a principal issues to stop a Run on purpose.
PRINCIPAL_STOPS: Final = frozenset({ControlKind.CANCEL, ControlKind.INTERRUPT})


def test_del_005_only_a_principal_s_stop_control_cancels_a_run() -> None:
    cancelling = {
        control
        for control, status in TERMINAL_EFFECT_STATUS.items()
        if status is RunStatus.CANCELLED
    }

    assert cancelling == PRINCIPAL_STOPS


def test_del_005_no_control_is_named_for_a_clock() -> None:
    assert not [kind for kind in ControlKind if "time" in kind.value]


def test_del_005_a_command_result_types_a_timeout_apart_from_a_cancellation() -> None:
    outcomes = get_args(get_args(CommandPayload.model_fields["outcome"].annotation)[0])

    assert "timed_out" in outcomes
    assert "cancelled" in outcomes


def test_del_005_a_timed_out_scoped_command_is_neither_denied_nor_completed() -> None:
    outcomes = get_args(RunScopedCommandOutput.model_fields["outcome"].annotation)

    assert set(outcomes) == {"completed", "timed_out", "denied"}
    assert "cancelled" not in outcomes


def test_del_005_a_lapsed_permission_is_expired_by_the_provider_not_denied_by_a_principal() -> None:
    lapse = PermissionResolution(decision="expired", decided_by="provider", decided_at=AT)

    assert (lapse.decision, lapse.principal_ref) == ("expired", None)


def test_del_005_a_lapse_cannot_be_attributed_to_a_principal() -> None:
    with pytest.raises(ValidationError, match="names no principal"):
        PermissionResolution(
            decision="expired",
            decided_by="provider",
            principal_class="operator",
            principal_ref="OP-0001",
            decided_at=AT,
        )


def test_del_005_a_principal_s_denial_cannot_be_filed_as_a_lapse() -> None:
    with pytest.raises(ValidationError, match="decided by the provider exactly when it expired"):
        PermissionResolution(
            decision="denied",
            decided_by="provider",
            decided_at=AT + timedelta(seconds=1),
        )


@pytest.mark.parametrize(
    "fault",
    [TimeoutError("close stage exceeded its budget"), close_module.CloseTimedOutError("wall")],
)
def test_del_005_a_timed_out_close_is_filed_timed_out_not_operator_cancelled(
    fault: Exception,
) -> None:
    assert close_module._failure_status(fault) == (
        CloseAttemptStatus.FAILED,
        CloseFailureKind.TIMED_OUT,
    )
