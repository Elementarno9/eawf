"""Claude Code runtime adapter for Eä.

Phase 4 W04 establishes this package skeleton with the hook router
(:mod:`~eawf.runtimes.claude.hooks_router`). W05 extends it with the
plugin install/update/doctor commands and the SKILL/agent/hook
template renderers; W06 adds the statusline modules. Phase 6 W05 adds
:func:`~eawf.runtimes.claude.plugin_package.package_plugin`, which
emits a standalone CC-plugin-marketplace-installable tree (separate
from the per-repo ``.claude/`` install path).

The convenience re-exports below pull from
:mod:`~eawf.runtimes.claude.plugin_package`, so importing
``eawf.runtimes.claude`` eagerly loads the renderer dependencies
(jinja2, tomllib, the skill / agent registries). Callers that need a
lighter import path should reach into the relevant submodule directly.
"""

from __future__ import annotations

from eawf.runtimes.claude.plugin_package import PackageResult, package_plugin

__all__ = [
    "PackageResult",
    "package_plugin",
]
