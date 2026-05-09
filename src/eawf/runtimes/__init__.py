"""Runtime adapters for Eä.

Phase 4 W04 creates this package as the home for per-runtime adapters
that translate platform-specific hook payloads into Eä canonical
:class:`~eawf.hooks.event.HookEvent` instances. W05 layers
``runtimes/claude/plugin_install.py`` on top; W06 adds the statusline
modules.

The package itself is intentionally empty of business logic so importing
``eawf.runtimes`` is cheap and side-effect-free.
"""

from __future__ import annotations
