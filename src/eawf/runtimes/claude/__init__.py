"""Claude Code runtime adapter for Eä.

Phase 4 W04 establishes this package skeleton with the hook router
(:mod:`~eawf.runtimes.claude.hooks_router`). W05 extends it with the
plugin install/update/doctor commands and the SKILL/agent/hook
template renderers; W06 adds the statusline modules.

The package is intentionally empty of business logic so importing
``eawf.runtimes.claude`` is cheap and side-effect-free.
"""

from __future__ import annotations
