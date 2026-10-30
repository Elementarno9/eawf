"""Service layer for token budgets.

* **Process termination ladder** — :func:`terminate_with_grace`
  implements the SIGTERM -> grace-window -> SIGKILL escalation the
  ``hard`` budget-enforce mode uses to stop a runaway subprocess. SIGTERM
  is sent first; SIGKILL only fires after the grace window elapses *and*
  the process is still alive. The clock and sleep are injectable so the
  ladder is deterministically testable against a fake process.
* :func:`emit_budget_notice` — upsert a running Run's non-blocking notice
  once its consumption reached its hard ceiling. Nothing opens below the
  ceiling or under a soft one: the observed fraction stays readable in the
  statusline, and a notice reports an enforced limit that was reached
  rather than an estimate or a prediction. A storage failure is logged and
  swallowed: a notice is observability, so losing one must never stop the
  work it describes.
* :func:`load_budget_config` — read and validate the repo's ``flow.budget``
  table, the one source every ceiling is derived from.
* :func:`emit_termination_notice` — fold a Run's budget-termination receipt
  into the notice ledger, so a Run's crossing is the same one notice row a
  wave's crossing is rather than a second kind of notice.
"""

from __future__ import annotations

import logging
import signal
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from eawf.kernel.config.layered import merge_config
from eawf.kernel.economics.notice_policy import DEFAULT_NOTICE_POLICY
from eawf.kernel.runtime.budget_notice import BudgetNotice
from eawf.runtime.budget.notices import (
    LOCAL_OPERATOR,
    BudgetCrossing,
    NoticeUpsert,
    upsert_notice,
)
from eawf.runtime.budget.policy import (
    BudgetConfig,
    PromptBudgetCeiling,
    budget_config_from,
)
from eawf.runtime.lock.portalock import LockTimeout

logger = logging.getLogger(__name__)

#: Default grace window (seconds) between SIGTERM and SIGKILL. Mirrors the
#: daemon wave-wall-clock cap ladder ("SIGTERM at cap, SIGKILL +15 s").
DEFAULT_GRACE_SECONDS: float = 15.0

#: How often (seconds) the ladder polls the process for liveness while
#: waiting out the grace window. Kept small so a process that exits early
#: is reaped promptly rather than blocking for the whole window.
_POLL_INTERVAL_SECONDS: float = 0.1


def load_budget_config(repo: Path) -> BudgetConfig:
    """Return the validated ``flow.budget`` table of *repo*'s layered config.

    Args:
        repo: The repo root whose ``.ea/`` config layers are merged.

    Returns:
        The validated table, defaulted when no layer declares one.

    Raises:
        eawf.runtime.budget.policy.DuplicateCeilingError: A layer declares a
            second ceiling.
        ValueError: The table is not a mapping.
        pydantic.ValidationError: A declared value is out of range.
    """
    merged, _sources = merge_config(workspace=repo, repo=repo)
    return budget_config_from(merged)


def emit_budget_notice(
    notices_file: Path,
    *,
    scope_id: str,
    running: bool,
    consumed: int,
    ceiling: PromptBudgetCeiling | None,
    observed_at: datetime,
    contract_digest: str | None = None,
    audience: tuple[str, ...] = (LOCAL_OPERATOR,),
) -> NoticeUpsert | None:
    """Upsert *scope_id*'s token notice once a running Run reached its hard ceiling.

    Whether anything is recorded is the notice policy's one rule: a Run
    that is not running, a soft ceiling, no ceiling, or a consumption
    short of it records nothing. The notice never pauses, asks or holds
    anything -- it is recorded and left for a reader.

    Args:
        notices_file: The notice ledger file.
        scope_id: The Run whose consumption was metered.
        running: Whether the Run is in ``RUNNING``.
        consumed: Cumulative consumption after the latest increment.
        ceiling: The Run's one ceiling, or ``None`` when none is set.
        observed_at: When the consumption was read.
        contract_digest: The compiled contract the Run's ceiling came
            from, part of the notice's identity.
        audience: The principals the notice is for.

    Returns:
        The upsert result, or ``None`` when the rule opened nothing or the
        ledger could not be written.

    Raises:
        ValueError: ``consumed`` is negative.
    """
    if consumed < 0:
        raise ValueError(f"consumed must be non-negative; got {consumed}")
    reached = ceiling is not None and ceiling.enforce == "hard" and ceiling.reached(consumed)
    opens = DEFAULT_NOTICE_POLICY.estimated_time.opens(running=running, ceiling_reached=reached)
    if ceiling is None or not opens:
        return None
    crossing = BudgetCrossing(
        scope_id=scope_id,
        basis="hard_limit",
        band="limit_reached",
        observed_value=consumed,
        budget_value=ceiling.tokens,
        observed_at=observed_at,
        contract_digest=contract_digest,
        audience=audience,
    )
    try:
        return upsert_notice(notices_file, crossing)
    except (OSError, LockTimeout, ValidationError) as exc:
        logger.warning(f"emit_budget_notice scope={scope_id} failed={exc!r}")
        return None


def emit_termination_notice(
    notices_file: Path,
    receipt: BudgetNotice,
    *,
    contract_digest: str | None,
    audience: tuple[str, ...],
) -> NoticeUpsert | None:
    """Upsert the one notice a Run's budget termination reports.

    The run-ledger *receipt* anchors the budget control and answers its
    retries; the notice readers list is the ledger row this writes. A
    termination only happens at a ``hard`` ceiling, so the row's basis is
    always ``hard_limit``, and a retried termination folds into the same row.
    The row is keyed by the Run, its compiled contract and the axis, inside
    the root's own notice ledger.

    Args:
        notices_file: The notice ledger file.
        receipt: The termination receipt the run ledger holds.
        contract_digest: The contract the Run was bound to, or ``None``
            for a Run metered without a dispatch binding.
        audience: The principals the notice is for.

    Returns:
        The upsert result, or ``None`` when the ledger could not be written.
    """
    return emit_budget_notice(
        notices_file,
        scope_id=receipt.run_ref.entity_key,
        # the receipt is written as the ladder stops the Run, so it was running at the reading
        running=True,
        consumed=receipt.observed_tokens,
        ceiling=PromptBudgetCeiling(tokens=receipt.cap_tokens, enforce="hard"),
        observed_at=receipt.noticed_at,
        contract_digest=contract_digest,
        audience=audience,
    )


class TerminableProcess(Protocol):
    """Minimal process surface the termination ladder drives.

    Mirrors the subset of :class:`subprocess.Popen` the ladder needs, so
    a fake can be substituted in tests without spawning a real process.
    """

    def poll(self) -> int | None:
        """Return the exit code, or ``None`` while the process is alive."""
        ...

    def send_signal(self, sig: int) -> None:
        """Deliver signal *sig* to the process."""
        ...


@dataclass(frozen=True, slots=True)
class TerminationResult:
    """Outcome of a :func:`terminate_with_grace` call.

    Attributes:
        sigterm_sent: Always ``True`` when a live process was signalled
            (SIGTERM is the first rung); ``False`` only when the process
            was already dead before the ladder started.
        sigkill_sent: ``True`` when the grace window elapsed with the
            process still alive and SIGKILL was delivered.
        exited_on_term: ``True`` when the process exited during the grace
            window (so SIGKILL was unnecessary).
        waited_seconds: Total time spent polling the grace window before
            the ladder resolved.
    """

    sigterm_sent: bool
    sigkill_sent: bool
    exited_on_term: bool
    waited_seconds: float


def terminate_with_grace(
    proc: TerminableProcess,
    *,
    grace_seconds: float = DEFAULT_GRACE_SECONDS,
    poll_interval: float = _POLL_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TerminationResult:
    """Run the SIGTERM -> grace -> SIGKILL termination ladder on *proc*.

    The ladder sends SIGTERM, then polls *proc* until either it exits or
    the grace window elapses. SIGKILL is delivered **only** when the
    window is fully exhausted and the process is still alive — never
    before. A process that is already dead on entry is a no-op (no signal
    is sent).

    The ``monotonic`` clock and ``sleep`` callables are injectable so the
    ladder is deterministic under test: a fake clock advances the window
    in controlled steps and a fake sleep records the requested durations
    without real wall-clock delay.

    Args:
        proc: The process to terminate (see :class:`TerminableProcess`).
        grace_seconds: Seconds to wait after SIGTERM before escalating to
            SIGKILL. Must be non-negative.
        poll_interval: Seconds between liveness polls within the window.
            Must be strictly positive.
        monotonic: Monotonic clock source (defaults to
            :func:`time.monotonic`).
        sleep: Sleep function (defaults to :func:`time.sleep`).

    Returns:
        A :class:`TerminationResult` recording which signals fired and how
        long the ladder waited.

    Raises:
        ValueError: ``grace_seconds`` is negative or ``poll_interval`` is
            not strictly positive.
    """
    if grace_seconds < 0:
        raise ValueError(f"grace_seconds must be non-negative; got {grace_seconds}")
    if poll_interval <= 0:
        raise ValueError(f"poll_interval must be positive; got {poll_interval}")

    if proc.poll() is not None:
        # Process already dead — nothing to signal.
        logger.info("terminate_with_grace already_dead=true sigterm=false sigkill=false")
        return TerminationResult(
            sigterm_sent=False,
            sigkill_sent=False,
            exited_on_term=True,
            waited_seconds=0.0,
        )

    proc.send_signal(signal.SIGTERM)
    logger.info(f"terminate_with_grace sigterm=true grace={grace_seconds}")

    start = monotonic()
    while True:
        if proc.poll() is not None:
            waited = monotonic() - start
            logger.info(
                f"terminate_with_grace exited_on_term=true sigkill=false waited={waited:.3f}"
            )
            return TerminationResult(
                sigterm_sent=True,
                sigkill_sent=False,
                exited_on_term=True,
                waited_seconds=waited,
            )
        if monotonic() - start >= grace_seconds:
            break
        sleep(poll_interval)

    # SIGKILL is POSIX-only; win32 handles read the POSIX number (9) as the
    # hard rung (see eawf.runtime.runtimes.cancel).
    proc.send_signal(int(getattr(signal, "SIGKILL", 9)))
    waited = monotonic() - start
    logger.info(f"terminate_with_grace sigkill=true waited={waited:.3f}")
    return TerminationResult(
        sigterm_sent=True,
        sigkill_sent=True,
        exited_on_term=False,
        waited_seconds=waited,
    )
