"""Name why a criterion's proof can only come from remote CI, a PR, a push or a tag.

A Milestone closes before its branch is pushed, so CI runs, pull requests,
pushes and observed tags or publications are the pull-request stop's to
check, never a Task's or a Milestone's. A criterion or acceptance step
whose proof reads one of them can never be evidenced inside the work it
belongs to, and planning one only defers the failure to close time.

The shapes below are deliberately narrow: each names a command or a phrase
whose proof needs a remote run, a pull request or tag state. A static check
of a workflow file ("ci.yaml declares 3 CI jobs", "the CI build matrix
covers 3.14"), ordinary prose such as "pushes the cursor", a ``pre-push``
hook, a ``git push --dry-run`` or a local ``git diff origin/main``
comparison is not one of them.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Final

from eawf.kernel.spec.common import CriterionSpec

#: Each remote-state shape beside the reason it names. Order decides which
#: reason a text matching several shapes reports.
_REMOTE_PROOF_SHAPES: Final[tuple[tuple[re.Pattern[str], str], ...]] = tuple(
    (re.compile(pattern, re.IGNORECASE), reason)
    for pattern, reason in (
        (
            r"\bgh\s+(?:run|pr|workflow|release)\b",
            "reads GitHub run, pull-request, workflow or release state",
        ),
        (r"\bgit\s+push\b(?![^`\n;]*?(?:--dry-run|\s-n\b))", "needs a git push"),
        (
            r"\bCI\s+(?:is|are|stays|remains|to\s+stay|must\s+(?:be|stay))\s+green\b"
            r"|\bCI\s+(?:passe[sd]|passing|succeed(?:s|ed)?)\b"
            r"|\bgreen\s+CI\b",
            "reads a remote CI result",
        ),
        (
            r"\blast\s+\d+\s+[^.;]{0,40}?\b(?:CI|PRs?|pull[ _-]?requests?|push|workflow)`?"
            r"\s+runs\b"
            r"|\b(?:dispatched|triggered)\s+(?:CI|workflow)\s+runs?\b",
            "reads remote CI runs",
        ),
        (
            r"\bgithub\s+actions\b[^.;]{0,40}\b(?:runs?|jobs?|conclusions?)\b"
            r"|\bruns\b[^.;]{0,80}\bconcluded\b",
            "reads GitHub Actions run conclusions",
        ),
        (
            r"\bruns?\s+of\s+`?(?:\.github/workflows/)?[\w.-]+\.ya?ml\b",
            "reads workflow run results",
        ),
        (
            r"--is-ancestor\s+(?:HEAD|@)\S*\s+`?origin/"
            r"|\bHEAD\b[^.;]{0,40}\b(?:ancestor\s+of|reachable\s+from)\s+`?origin/"
            r"|\bpushed\s+to\s+`?(?:origin|the\s+remote|github)\b",
            "needs the work pushed to origin",
        ),
        (
            r"\b(?:tagged|published|publication\s+targets?)\b[^.;]{0,60}\bobserved\b"
            r"|\bpublished\s+to\s+(?:PyPI|npm)\b"
            r"|\bpublish(?:es|ed)?\s+v?\d+\.\d+\.\d+"
            r"|\bv?\d+\.\d+\.\d+\S*\s+is\s+(?:published|tagged|released)\b",
            "needs an observed tag or publication",
        ),
    )
)


def remote_proof_reason(*texts: str) -> str | None:
    """Return why *texts* can only be proved from remote state, or ``None``.

    Args:
        texts: The prose and signals of one criterion or acceptance step.

    Returns:
        The first matching reason with the text that matched it, or
        ``None`` when every text is provable locally.
    """
    for pattern, reason in _REMOTE_PROOF_SHAPES:
        for text in texts:
            match = pattern.search(text)
            if match is not None:
                return f"{reason} ({match.group(0)!r})"
    return None


def criteria_needing_remote_ci(criteria: Iterable[CriterionSpec]) -> tuple[str, ...]:
    """Return one ``<criterion id>: <reason>`` entry per remotely-proved criterion.

    Args:
        criteria: The criteria a write is about to commit.

    Returns:
        The offending criteria in input order; empty when none is.
    """
    named: list[str] = []
    for criterion in criteria:
        reason = remote_proof_reason(criterion.text, criterion.measurable_signal)
        if reason is not None:
            named.append(f"{criterion.id}: {reason}")
    return tuple(named)


__all__ = ["criteria_needing_remote_ci", "remote_proof_reason"]
