"""Claude Code runtime adapter for Eä.

Phase 4 W04 establishes this package skeleton with the hook router
(:mod:`~eawf.runtimes.claude.hooks_router`). W05 extends it with the
plugin install/update/doctor commands and the SKILL/agent/hook
template renderers; W06 adds the statusline modules. Phase 6 W05 adds
:func:`~eawf.runtimes.claude.plugin_package.package_plugin`, which
emits a standalone CC-plugin-marketplace-installable tree (separate
from the per-repo ``.claude/`` install path).

The package is intentionally empty of business logic so importing
``eawf.runtimes.claude`` is cheap and side-effect-free; the convenience
re-exports below pull lazily from their owning submodules.
"""

from __future__ import annotations

from eawf.runtimes.claude.plugin_package import PackageResult, package_plugin

__all__ = [
    "PackageResult",
    "package_plugin",
]
