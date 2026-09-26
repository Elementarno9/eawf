"""The budget notice: a Run crossed its cap, and nothing waits on saying so.

A notice is a reading, not a question. It records the consumption that
crossed the cap and the control that was opened about it, and it asks the
operator nothing -- which is why :attr:`BudgetNotice.blocking` is
``Literal[False]`` rather than a ``bool`` defaulted to false. A default is
a value a caller may override; a literal is a value no caller can supply.
The distinction matters because the notice is written on the path that
reaps a live process group: a notice that could be constructed blocking
would be a notice that could stall the reap it exists to explain.

On the Run's ledger this record is the budget control's receipt; the one
notice a reader lists is the ``BudgetThresholdNotice`` row it folds into in
the budget notice ledger (:mod:`eawf.runtime.budget.notices`).

Which control a budget termination opens is decided here rather than left
to each caller. ``cancel`` is reused rather than a ninth control kind
added:
:class:`~eawf.kernel.runtime.provider.ControlKind` enumerates what a
*driver capability* may be required to honour, and no driver honours
"budget" -- the cap is the daemon's own arithmetic over the driver's
reported usage. A stopped episode that produced no report is cancelled,
which is exactly what
:data:`~eawf.kernel.runtime.control.TERMINAL_EFFECT_STATUS` already says
about ``cancel``. :func:`compile_budget_termination` checks that at
import, so retiring ``cancel`` from the terminal map becomes a startup
failure rather than a budget breach that silently stops terminating
anything.
"""

from __future__ import annotations

import logging
from typing import Final, Literal

from eawf.kernel.runtime.control import (
    TERMINAL_EFFECT_STATUS,
    ControlRequestId,
)
from eawf.kernel.runtime.provider import ControlKind, RuntimeRecord
from eawf.kernel.state.epoch2.base import StrictNonNegativeInt
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.urns import RunUrn
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


def compile_budget_termination(control: ControlKind) -> ControlKind:
    """Return *control* once it is proven to end a Run in a terminal status.

    Args:
        control: The control a budget termination opens on the over-cap Run.

    Returns:
        *control* unchanged.

    Raises:
        ValueError: A confirmed effect of *control* terminalizes no Run, so
            a budget termination routed through it would append three
            control facts and leave the Run running.
    """
    terminal = TERMINAL_EFFECT_STATUS[control]
    if terminal is None:
        raise ValueError(
            f"control {control.value!r} ends no Run, so a budget termination "
            "routed through it would terminate nothing"
        )
    return control


#: The control a budget termination opens. Checked at import against the
#: terminal-effect map so the reuse can never drift into a no-op.
BUDGET_TERMINATION_CONTROL: Final[ControlKind] = compile_budget_termination(ControlKind.CANCEL)

#: The Run status a confirmed budget termination leaves behind.
BUDGET_TERMINATION_STATUS: Final[RunStatus] = RunStatus.CANCELLED


class BudgetNotice(RuntimeRecord):
    """One reading that crossed a Run's cap, as the run ledger records it.

    This line is the budget control's receipt on the Run's ledger: it anchors
    the control facts and answers a retried termination. It is not a second
    notice. The one notice a crossing produces is the non-blocking
    ``BudgetThresholdNotice`` row of the budget notice ledger, which this
    receipt is folded into when it is written, so a reader that lists
    notices reads that ledger and never counts the receipt beside it.

    Attributes:
        payload_kind: The discriminator separating a notice line from a
            control fact, a contract binding and a compacted Run record.
        run_ref: The Run whose cap the reading crossed.
        control_request_ref: The control opened about the crossing. It is
            recorded even when that control loses the Run's one lease, so
            the notice always names what was attempted.
        cap_tokens: The effective cap the reading was tested against.
        observed_tokens: The metered consumption at the crossing.
        blocking: Always false, and unsettable. The notice reports a
            reading; it waits on no operator answer.
        noticed_at: When the daemon read the crossing.
    """

    payload_kind: Literal["budget_notice"] = "budget_notice"
    run_ref: RunUrn
    control_request_ref: ControlRequestId
    cap_tokens: StrictNonNegativeInt
    observed_tokens: StrictNonNegativeInt
    blocking: Literal[False] = False
    noticed_at: UtcDatetime


__all__ = [
    "BUDGET_TERMINATION_CONTROL",
    "BUDGET_TERMINATION_STATUS",
    "BudgetNotice",
    "compile_budget_termination",
]
