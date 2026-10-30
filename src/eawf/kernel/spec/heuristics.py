"""UI-scope heuristic (C03).

:func:`is_ui_scope` is a path-prefix check that returns True when any file
scope lives under ``src/eawf/surfaces/tui/`` or ``src/eawf/surfaces/render/``.
This is the D11 heuristic [§4 D11] frozen for v0.3; profile-driven overrides
land in C08. It is pure and does no I/O.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

logger = logging.getLogger(__name__)

# Path-prefix prefixes that trigger the mockup-required heuristic
# (D11). Bare strings — callers compare with ``str.startswith`` against
# repo-relative file scopes.
UI_SCOPE_PREFIXES: tuple[str, ...] = (
    "src/eawf/surfaces/tui/",
    "src/eawf/surfaces/render/",
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


__all__ = [
    "UI_SCOPE_PREFIXES",
    "is_ui_scope",
]
