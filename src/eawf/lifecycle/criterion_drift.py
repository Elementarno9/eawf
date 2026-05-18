"""Wave success_criteria drift detector (P23-I02-W01).

A *drift* is a path-shaped token inside a wave's ``success_criteria`` that
does not resolve to any file on disk. Two failure modes observed during the
P23 audit (see ``.ea/artifacts/A29-P23-ship-gate.md`` followups F2):

- A test path wildcard like ``tests/unit/test_state_urn*.py`` that matches no
  file (the real module was ``tests/unit/test_urn.py``).
- A non-wildcard path like ``src/eawf/lifecycle/foo.py`` that was never
  created in the wave.

This module exposes the pure-functional detector. The CLI ``wave close``
handler surfaces unresolved globs as an *advisory* stderr warning — close
itself still proceeds because closed waves are immutable per AGENTS rule 20.

Library-private. No CLI surface.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from eawf.state.models import Wave

logger = logging.getLogger(__name__)

_PATH_PREFIXES = ("src", "tests", "build", "scripts", "docs")
_PATH_GLOB_RE = re.compile(
    r"(?:" + r"|".join(_PATH_PREFIXES) + r")/[^\s,()`'\"]+",
)


def extract_path_globs(criteria: list[str]) -> list[str]:
    """Return all path-shaped tokens from a list of success_criteria strings.

    A path-shaped token is a substring that starts with one of the canonical
    top-level prefixes (``src/``, ``tests/``, ``build/``, ``scripts/``,
    ``docs/``) and runs until the next whitespace, comma, parenthesis, or
    quoting character. Returned list preserves source order and deduplicates
    while keeping the first occurrence.
    """
    seen: set[str] = set()
    out: list[str] = []
    for line in criteria:
        for match in _PATH_GLOB_RE.findall(line):
            if match not in seen:
                seen.add(match)
                out.append(match)
    return out


def unresolved_globs(repo_root: Path, globs: list[str]) -> list[str]:
    """Return globs that resolve to zero files relative to *repo_root*.

    Wildcard globs (``*``, ``?``, ``[...]``) are expanded via
    :meth:`pathlib.Path.glob`. Plain non-wildcard paths are checked with
    :meth:`pathlib.Path.exists`. Returned list preserves the input order.
    """
    out: list[str] = []
    for glob in globs:
        has_wildcard = any(ch in glob for ch in "*?[]")
        if has_wildcard:
            matches = list(repo_root.glob(glob))
        else:
            target = repo_root / glob
            matches = [target] if target.exists() else []
        if not matches:
            out.append(glob)
    return out


def check_wave_criteria_drift(wave: Wave, repo_root: Path) -> list[str]:
    """Return unresolved path globs from ``wave.success_criteria``.

    Returns the empty list when:

    - the wave has no success_criteria, OR
    - none of the criteria contain a path-shaped token, OR
    - every extracted token resolves to ≥1 file on disk.
    """
    if not wave.success_criteria:
        return []
    globs = extract_path_globs(list(wave.success_criteria))
    if not globs:
        return []
    unresolved = unresolved_globs(repo_root, globs)
    logger.debug(
        f"check_wave_criteria_drift wave={wave.id!r} "
        f"globs={len(globs)} unresolved={len(unresolved)}"
    )
    return unresolved
