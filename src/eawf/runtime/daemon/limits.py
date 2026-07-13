"""Time limits shared by the daemon watchdog and the CLI's mutation client.

A gated wave close spawns a real fresh-context auditor INSIDE the watched
mutation, so three deadlines have to agree about how long that close may take:

* the auditor's own ceiling (``VerifyBlock.juror_wall_clock_seconds``),
* the daemon watchdog's hard-abort limit, which must sit above it or it kills
  every legitimate long audit,
* the CLI's wire timeout, which must sit above BOTH or the operator is told the
  mutation is indeterminate while the daemon is still working on it -- which is
  exactly what happened for the whole of P30-I25: the wire timeout was a 30s
  constant, so every gated close reported ``DaemonMutationIndeterminate``
  (exit 4) while the daemon went on to apply it successfully.

They live here, in one module both sides import, so a repo that RAISES the juror
ceiling raises all three together instead of one of them silently staying behind.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


#: Hard abort limit (seconds) for one in-flight mutation when no juror ceiling is
#: configured. Past it the daemon watchdog cancels the mutation task so the daemon
#: recovers without a manual pkill. 900 = the 600s default juror bound + the
#: commit/routing margin below.
MUTATION_HARD_LIMIT_SECONDS: float = 900.0

#: Headroom the watchdog leaves above the juror bound for the commit + routing
#: work that follows the auditor spawn inside the same mutation.
COMMIT_MARGIN_SECONDS: float = 300.0

#: Headroom the CLI leaves above the daemon's own hard limit, so a mutation the
#: watchdog aborts reports the watchdog's structured outcome rather than tripping
#: the client's timeout first (which would surface as "indeterminate" -- the one
#: answer the operator cannot act on).
CLI_WIRE_MARGIN_SECONDS: float = 30.0


def mutation_hard_limit_for(juror_wall_clock_seconds: float | None) -> float:
    """Return the watchdog hard limit that accommodates *juror_wall_clock_seconds*.

    The limit tracks the juror ceiling rather than sitting at a constant: an
    auditor allowed 1800s inside a mutation watched at 900s is killed by the
    watchdog instead of finishing, which is strictly worse than the timeout it
    was raised to escape.

    Args:
        juror_wall_clock_seconds: The repo's configured juror ceiling, or ``None``
            when it has none.

    Returns:
        The hard-abort limit in seconds.
    """
    if juror_wall_clock_seconds is None or juror_wall_clock_seconds <= 0:
        return MUTATION_HARD_LIMIT_SECONDS
    return max(MUTATION_HARD_LIMIT_SECONDS, juror_wall_clock_seconds + COMMIT_MARGIN_SECONDS)


def cli_mutation_timeout_for(juror_wall_clock_seconds: float | None) -> float:
    """Return the CLI wire timeout that outlives the daemon's own hard limit.

    The client must not give up before the daemon does. When it does, the CLI
    raises ``DaemonMutationIndeterminate`` -- "the write may or may not have
    applied" -- for a mutation that is neither indeterminate nor finished, and the
    operator is left re-checking a close the daemon is still running.

    Args:
        juror_wall_clock_seconds: The repo's configured juror ceiling, or ``None``.

    Returns:
        The wire timeout in seconds: the watchdog's hard limit plus the margin
        needed to hear the watchdog's own answer.
    """
    return mutation_hard_limit_for(juror_wall_clock_seconds) + CLI_WIRE_MARGIN_SECONDS


def configured_juror_wall_clock(repo_root: Path) -> float | None:
    """Return the repo's configured juror wall clock, when it has one.

    Read best-effort: an unreadable config leaves the caller on its default limit
    rather than failing the daemon boot or the CLI call.

    Args:
        repo_root: Repository root whose layered config is consulted.

    Returns:
        The configured ceiling in seconds, or ``None`` when it is absent,
        unreadable, or not a positive number.
    """
    try:
        from eawf.kernel.config.layered import get_dotted, merge_config

        merged, _sources = merge_config(repo=repo_root)
        raw = get_dotted(merged, "verify.juror_wall_clock_seconds")
    except Exception as exc:
        logger.debug(f"configured_juror_wall_clock status='default' err={exc!r}")
        return None
    if isinstance(raw, bool) or not isinstance(raw, int | float) or raw <= 0:
        return None
    return float(raw)


__all__ = [
    "CLI_WIRE_MARGIN_SECONDS",
    "COMMIT_MARGIN_SECONDS",
    "MUTATION_HARD_LIMIT_SECONDS",
    "cli_mutation_timeout_for",
    "configured_juror_wall_clock",
    "mutation_hard_limit_for",
]
