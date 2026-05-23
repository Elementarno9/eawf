"""UI-scope mockup heuristic + real-test-paths grep helpers (C03).

Two heuristic-style helpers feed the C03 three-layer enforcement
stack:

1. :func:`is_ui_scope` — path-prefix check that returns True when any
   file scope lives under ``src/eawf/tui/`` or
   ``src/eawf/render/``. This is the D11 heuristic [§4 D11] frozen for
   v0.3; profile-driven overrides land in C08.
2. :func:`requires_mockup_reference` — given a WaveSpec, returns True
   when the wave's ``file_scopes`` are UI and the wave does NOT cite
   either a ``mockup`` block or a ``mockup_waiver_reason``. The
   ``WaveSpec._mockup_required`` model_validator (in :mod:`eawf.spec.wave`)
   consumes this predicate so the rule fires at schema load (success
   criterion 2 of P25-W05).
3. :func:`missing_test_paths` — given a list of :data:`TestRef` strings
   and a project root, returns the subset that do not exist on disk.
   Both the loader-side validator (:mod:`eawf.spec.validators`) and
   the pre-commit hook (:mod:`tools.pre_commit_spec_paths`) consume
   this helper.

The helpers are pure (no I/O at module import; ``missing_test_paths``
does its own ``Path.is_file`` lookup at call time) and self-contained
so they can be exercised in unit tests without setting up a full
WaveSpec.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from pathlib import Path

logger = logging.getLogger(__name__)

# Path-prefix prefixes that trigger the mockup-required heuristic
# (D11). Bare strings — callers compare with ``str.startswith`` against
# repo-relative file scopes.
UI_SCOPE_PREFIXES: tuple[str, ...] = (
    "src/eawf/tui/",
    "src/eawf/render/",
)


def is_ui_scope(file_scopes: Iterable[str]) -> bool:
    """Return True when any file scope lives under a UI prefix.

    Args:
        file_scopes: Iterable of repo-relative file-scope path strings.

    Returns:
        True when at least one path starts with a member of
        :data:`UI_SCOPE_PREFIXES`; False otherwise (including the empty
        iterable).
    """
    return any(
        any(scope.startswith(prefix) for prefix in UI_SCOPE_PREFIXES) for scope in file_scopes
    )


def requires_mockup_reference(
    *,
    file_scopes: Iterable[str],
    mockup_present: bool,
    mockup_waiver_reason: str | None,
) -> bool:
    """Return True when the wave needs a mockup citation but lacks one.

    Args:
        file_scopes: Iterable of repo-relative file-scope path strings.
        mockup_present: Whether the wave carries a non-None ``mockup``
            block (WaveMockup with ASCII + optional Mermaid).
        mockup_waiver_reason: When set + non-empty, the wave has opted
            out of the heuristic with a documented rationale per D11.

    Returns:
        True when (a) at least one file scope is UI per
        :func:`is_ui_scope`, AND (b) ``mockup_present`` is False, AND
        (c) ``mockup_waiver_reason`` is None or empty/whitespace.
    """
    if not is_ui_scope(file_scopes):
        return False
    if mockup_present:
        return False
    # Heuristic fires when neither mockup nor a non-empty waiver is set.
    # Whitespace-only waivers are treated as missing so empty strings
    # cannot accidentally satisfy the rule.
    return not (mockup_waiver_reason and mockup_waiver_reason.strip())


def missing_test_paths(test_refs: Iterable[str], project_root: Path) -> list[str]:
    """Return the subset of ``test_refs`` that do not exist under ``project_root``.

    Args:
        test_refs: Iterable of repo-relative test paths (e.g.
            ``tests/unit/test_x.py``).
        project_root: Absolute path to the repo root. Each test ref is
            resolved against this root before the ``is_file`` check.

    Returns:
        List of test refs (in input order) that do not resolve to an
        existing regular file. Empty list means every ref exists.
    """
    missing: list[str] = []
    for ref in test_refs:
        candidate = project_root / ref
        if not candidate.is_file():
            missing.append(ref)
    return missing


__all__ = [
    "UI_SCOPE_PREFIXES",
    "is_ui_scope",
    "missing_test_paths",
    "requires_mockup_reference",
]
