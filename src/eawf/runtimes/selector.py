"""Adapter + capability selectors — single source-of-truth indirection.

The dispatch router and ``eawf doctor`` consume runtime capabilities via
this module rather than reading hard-coded class attributes on each
:class:`~eawf.runtimes.adapter.RuntimeAdapter` implementation. The
capability state lives in ``capabilities.yaml`` (loaded via
:func:`eawf.runtimes.capabilities.get_matrix`); adapter classes derive
their :attr:`accepts_continue` / :attr:`supports_cache_control` class
attributes from this module so the YAML stays the single source of
truth.

Public surface
--------------

* :func:`runtime_supports(runtime_id, capability)` — boolean view over
  the declared cell (``supported`` / ``partial`` map to ``True``;
  ``unsupported`` / ``unknown`` map to ``False``). Used by adapter
  class-attribute derivation.
* :func:`select_adapter(runtime_id)` — return the adapter class for a
  canonical runtime id. Lazy import keeps the selector cheap to import
  from low-level modules (adapter classes import this for their
  capability flags).
* :data:`SUPPORTED_CELLS` — closed set of cell values that map to
  boolean ``True`` for adapter-side capability flags.

Boolean mapping rule
--------------------

The matrix carries four cell states (``supported`` / ``unsupported`` /
``partial`` / ``unknown``); the legacy adapter Protocol declares two
booleans. The mapping is:

* ``supported`` → ``True``
* ``partial``   → ``True``   (operator surface treats partial as available
                              with caveats; the daemon's fallback layer
                              handles the caveat)
* ``unsupported`` → ``False``
* ``unknown``   → ``False``  (conservative; daemon refuses to lean on a
                              capability it cannot verify)
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Final

from eawf.runtimes.capabilities import (
    CAPABILITY_CELLS,
    CapabilityCell,
    get_runtime_capabilities,
)

if TYPE_CHECKING:
    from eawf.runtimes.adapter import RuntimeAdapter

logger = logging.getLogger(__name__)

SUPPORTED_CELLS: Final[frozenset[CapabilityCell]] = frozenset({"supported", "partial"})
"""Cells that map to boolean ``True`` for the adapter-side flags.

The closed set is mirrored against :data:`CAPABILITY_CELLS` (loader-side
validation), so any future cell addition forces an explicit edit here
rather than a silent default."""


# Sanity: every member of SUPPORTED_CELLS is a valid cell value. Kept as
# a module-level assertion so import-time drift is caught immediately.
assert SUPPORTED_CELLS.issubset(set(CAPABILITY_CELLS))


def runtime_supports(runtime_id: str, capability: str) -> bool:
    """Return whether ``runtime_id`` exposes ``capability``.

    Reads from the YAML-backed capability matrix (no duplication of
    adapter-side hard-coded tables).

    Args:
        runtime_id: Canonical runtime id (``claude-code`` / ``codex``
            / ``opencode``).
        capability: Capability row name (e.g. ``"session_resume"``).

    Returns:
        ``True`` when the declared cell is one of
        :data:`SUPPORTED_CELLS` (``supported`` or ``partial``), else
        ``False``.

    Raises:
        ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
            ids.
        KeyError: ``capability`` is not a row in the matrix.
    """
    caps = get_runtime_capabilities(runtime_id)
    if capability not in caps:
        raise KeyError(f"unknown capability: {capability!r}")
    cell = caps[capability]
    return cell in SUPPORTED_CELLS


def select_adapter(runtime_id: str) -> RuntimeAdapter:
    """Return a freshly-constructed adapter instance for ``runtime_id``.

    The selector lives in this module (not in
    :mod:`eawf.runtimes.adapter`) so the adapter modules can import
    :func:`runtime_supports` without forming an import cycle. Lazy
    imports inside the function body keep the selector itself cheap to
    import from adapter modules.

    Args:
        runtime_id: Canonical runtime id.

    Returns:
        Adapter instance implementing
        :class:`~eawf.runtimes.adapter.RuntimeAdapter`.

    Raises:
        ValueError: ``runtime_id`` is not one of the three v0.3-v0.5
            ids.
    """
    if runtime_id == "claude-code":
        from eawf.runtimes.claude.adapter import ClaudeAdapter

        return ClaudeAdapter()
    if runtime_id == "codex":
        from eawf.runtimes.codex.adapter import CodexAdapter

        return CodexAdapter()
    if runtime_id == "opencode":
        from eawf.runtimes.opencode.adapter import OpenCodeAdapter

        return OpenCodeAdapter()
    raise ValueError(f"unknown runtime: {runtime_id!r}")


__all__ = [
    "SUPPORTED_CELLS",
    "runtime_supports",
    "select_adapter",
]
