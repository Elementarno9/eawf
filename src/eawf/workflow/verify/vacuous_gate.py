"""Gate runs whose exit status says nothing about the tree.

:mod:`eawf.kernel.spec.falsifiability` refuses argv shapes whose exit
status is fixed at zero before anything runs. This module covers the
mirror-image failure, which is only visible AFTER the run: an argv that
reached its runner, exited non-zero, and still observed nothing. The two
shapes that matter both come from ``pytest``:

* a selector (``-k``) that matches no test -- pytest deselects everything
  and exits 5. Nothing was asserted, so the non-zero status reports the
  selector, not the tree;
* a named path that does not resolve -- pytest never builds a session and
  exits 4. Again nothing was asserted.

Both were shipped as required blocking gates in this repository. Left
unclassified they are indistinguishable from a genuine red, which files
the refusal against the wave's own work: the operator is told the
criterion failed when the truth is that the gate named the wrong target
and no observation exists either way. Classifying them turns a false
accusation into an accurate one -- the gate argv is unusable and must be
repaired before the criterion can be judged at all.

The check is a pure function of the argv plus the exit status. It runs
nothing, so it is safe at any scoring boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from eawf.kernel.spec.falsifiability import effective_argv

logger = logging.getLogger(__name__)

#: pytest's ``EXIT_NOTESTSCOLLECTED``. Every test the session could have
#: run was deselected or never collected, so no assertion executed.
PYTEST_EXIT_NO_TESTS_COLLECTED: Final[int] = 5

#: pytest's ``EXIT_USAGEERROR``. The command line itself did not resolve
#: -- most often a named file or directory that is not in the tree.
PYTEST_EXIT_USAGE_ERROR: Final[int] = 4

#: Command heads that run a pytest session. ``pytest`` is the direct
#: form; the module form arrives as ``python -m pytest``, which
#: :func:`~eawf.kernel.spec.falsifiability.effective_argv` has already
#: peeled down to ``pytest`` by the time this module sees it.
_PYTEST_HEADS: Final[frozenset[str]] = frozenset({"pytest", "py.test"})


@dataclass(frozen=True, slots=True)
class VacuousGateRun:
    """Why a completed gate run observed nothing.

    Attributes:
        reason: One-line explanation of what the exit status actually
            reported, suitable for a refusal message.
        repair: What the author must do for the gate to observe the tree.
    """

    reason: str
    repair: str

    def message(self) -> str:
        """Return the refusal text naming both the reason and the repair."""
        return f"{self.reason}; {self.repair}"


def vacuous_gate_run(argv: Sequence[str], *, exit_status: int | None) -> VacuousGateRun | None:
    """Return why the run observed nothing, or ``None`` when it observed.

    Conservative by construction: an argv head this module does not know,
    a missing exit status, and every exit status other than the two pytest
    no-observation codes all return ``None``, so the rule only ever
    refuses a run whose vacuousness is established.

    Args:
        argv: The argv the runner executed, wrapper layers included.
        exit_status: The process exit status, or ``None`` when the runner
            never reached a process.

    Returns:
        The classification, or ``None`` when the exit status does reflect
        an observation of the tree.
    """
    if exit_status is None:
        return None
    command = effective_argv(argv)
    if not command or command[0] not in _PYTEST_HEADS:
        return None
    verdict: VacuousGateRun | None = None
    if exit_status == PYTEST_EXIT_NO_TESTS_COLLECTED:
        verdict = VacuousGateRun(
            reason=(
                f"pytest exited {PYTEST_EXIT_NO_TESTS_COLLECTED} (no tests collected): the "
                "selector matched no test, so nothing was asserted about the tree"
            ),
            repair=(
                "name the test node ids the criterion is falsified by, or fix the -k "
                "expression so it selects at least one test"
            ),
        )
    elif exit_status == PYTEST_EXIT_USAGE_ERROR:
        verdict = VacuousGateRun(
            reason=(
                f"pytest exited {PYTEST_EXIT_USAGE_ERROR} (usage error): a named path or "
                "option did not resolve, so no test session ran"
            ),
            repair="point the gate at a path that exists in the tree under test",
        )
    if verdict is not None:
        logger.warning(
            f"vacuous_gate_run reject command={' '.join(command)!r} "
            f"exit_status={exit_status} reason={verdict.reason!r}"
        )
    return verdict


__all__ = [
    "PYTEST_EXIT_NO_TESTS_COLLECTED",
    "PYTEST_EXIT_USAGE_ERROR",
    "VacuousGateRun",
    "vacuous_gate_run",
]
