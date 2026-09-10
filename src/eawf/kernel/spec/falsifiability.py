"""Gate-argv shapes whose exit status cannot depend on the tree.

A ``command_exit_zero`` gate is only evidence when the command it names
can exit non-zero. Some argv shapes cannot: they observe the repository
and report, or they preview an action whose checks are attached to a
flag the argv omits. Such a gate reads as protection on the roadmap, is
scored as a pass on every close, and proves nothing about any tree.

The two shapes declared here are not hypothetical -- each is taken from
a required blocking gate that shipped in this repository and passed on a
tree that a seeded defect had already broken:

* a history read (``git log --stat``) whose exit status is fixed at 0
  unless a named revision fails to resolve or ``--exit-code`` is passed;
* a preview (``eawf release tag <v> --dry-run``) whose readiness sweep
  hangs off ``--push``, so the preview form never runs the sweep it
  exists to rehearse.

Each rule carries the falsifiable repair alongside the reason, so a
refusal tells the author what to write instead rather than only what
not to write. The checks are pure functions over the argv vector: they
never execute anything, so they are safe at any validation boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from eawf.runtime.sandbox.argv_policy import (
    PYTHON_MODULE_WRAPPERS,
    WRAPPER_DIRECT_HEADS,
    WRAPPER_HEADS,
    WRAPPER_SCOPED_SUBCOMMANDS,
)

logger = logging.getLogger(__name__)

#: ``git`` sub-verbs that print history and exit 0 whatever the tree
#: holds. Each becomes falsifiable the moment the argv names a revision
#: that has to resolve, or asks for diff-style exit codes.
_HISTORY_READ_SUBVERBS: Final[frozenset[str]] = frozenset({"log", "show", "shortlog"})

#: The flag that turns a history read into a diff-style assertion.
_DIFF_EXIT_CODE_FLAG: Final[str] = "--exit-code"

#: The project-CLI verb whose ``--dry-run`` form skips its own checks.
_PREVIEW_COMMAND: Final[tuple[str, ...]] = ("release", "tag")

#: The flag that arms the sweep :data:`_PREVIEW_COMMAND` is gated on.
_PREVIEW_GATING_FLAG: Final[str] = "--push"

_DRY_RUN_FLAG: Final[str] = "--dry-run"


@dataclass(frozen=True, slots=True)
class UnfalsifiableArgv:
    """Why an argv cannot red, and the argv that can.

    Attributes:
        reason: One-line explanation naming the property that fixes the
            exit status, suitable for a refusal message.
        repair: The falsifiable argv the author should write instead.
            A placeholder token in angle brackets marks an operand only
            the author can supply.
    """

    reason: str
    repair: tuple[str, ...]

    def message(self) -> str:
        """Return the refusal text naming both the reason and the repair."""
        return f"{self.reason}; write {' '.join(self.repair)!r} instead"


def effective_argv(argv: Sequence[str]) -> tuple[str, ...]:
    """Return *argv* with its wrapper layers peeled off.

    ``uv run eawf release tag`` and ``eawf release tag`` name the same
    command, so a rule that reads the command has to see through the
    wrapper. Peeling stops at the first layer that does not expose an
    inner command -- an argv the L0 policy would already have rejected
    is returned as far as it could be resolved rather than raising a
    second time here.

    Args:
        argv: The gate's argv vector.

    Returns:
        The innermost command vector, empty when *argv* is empty.
    """
    current = list(argv)
    while current and current[0] in WRAPPER_HEADS:
        head = current[0]
        if head in WRAPPER_SCOPED_SUBCOMMANDS:
            if len(current) < 3 or current[1] not in WRAPPER_SCOPED_SUBCOMMANDS[head]:
                break
            current = current[2:]
        elif head in WRAPPER_DIRECT_HEADS:
            if len(current) < 2:
                break
            current = current[1:]
        elif head in PYTHON_MODULE_WRAPPERS:
            if len(current) < 3 or current[1] != "-m":
                break
            current = current[2:]
        else:
            if len(current) < 2:
                break
            current = current[1:]
    return tuple(current)


def _positional_operands(tokens: Sequence[str]) -> tuple[str, ...]:
    """Return the non-option tokens that precede a bare ``--`` separator.

    Everything after ``--`` is a pathspec, which git accepts even when
    nothing matches, so those tokens cannot make the command fail.

    Args:
        tokens: The command tokens after the sub-verb.

    Returns:
        The positional operands git would resolve as revisions.
    """
    operands: list[str] = []
    for token in tokens:
        if token == "--":
            break
        if not token.startswith("-"):
            operands.append(token)
    return tuple(operands)


def _history_read_without_an_operand(command: tuple[str, ...]) -> UnfalsifiableArgv | None:
    """Refuse a git history read that names nothing that has to resolve."""
    if len(command) < 2 or command[0] != "git" or command[1] not in _HISTORY_READ_SUBVERBS:
        return None
    rest = command[2:]
    if _DIFF_EXIT_CODE_FLAG in rest or _positional_operands(rest):
        return None
    return UnfalsifiableArgv(
        reason=(
            f"'git {command[1]}' exits 0 on every tree unless it names a revision that has "
            f"to resolve or asks for {_DIFF_EXIT_CODE_FLAG} diff exit codes"
        ),
        repair=("git", command[1], "--max-count=1", "<revision-that-must-exist>"),
    )


def _preview_that_skips_its_sweep(command: tuple[str, ...]) -> UnfalsifiableArgv | None:
    """Refuse a release-tag preview whose readiness sweep is not armed."""
    if len(command) < 3 or command[0] != "eawf":
        return None
    if tuple(command[1:3]) != _PREVIEW_COMMAND:
        return None
    rest = command[3:]
    if _DRY_RUN_FLAG not in rest or _PREVIEW_GATING_FLAG in rest:
        return None
    verb = " ".join(_PREVIEW_COMMAND)
    return UnfalsifiableArgv(
        reason=(
            f"'eawf {verb} {_DRY_RUN_FLAG}' runs its readiness sweep only under "
            f"{_PREVIEW_GATING_FLAG}, so without it the preview exits 0 however red the "
            f"checkpoint is"
        ),
        repair=(*command, _PREVIEW_GATING_FLAG),
    )


def unfalsifiable_argv(argv: Sequence[str]) -> UnfalsifiableArgv | None:
    """Return why *argv* cannot exit non-zero, or ``None`` when it can.

    The check is conservative: an argv shape that is not declared here
    is treated as falsifiable, so the rule only ever refuses a command
    whose fixed exit status has been established.

    Args:
        argv: The gate's argv vector, wrapper layers included.

    Returns:
        The reason plus its falsifiable repair, or ``None`` when the
        argv's exit status can depend on the tree.
    """
    command = effective_argv(argv)
    if not command:
        return None
    for rule in (_history_read_without_an_operand, _preview_that_skips_its_sweep):
        verdict = rule(command)
        if verdict is not None:
            logger.warning(
                f"unfalsifiable_argv reject command={' '.join(command)!r} reason={verdict.reason!r}"
            )
            return verdict
    return None


__all__ = [
    "UnfalsifiableArgv",
    "effective_argv",
    "unfalsifiable_argv",
]
