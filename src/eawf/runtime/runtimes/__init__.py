"""Runtime adapters for Eä.

This package is the home for per-runtime adapters
that translate platform-specific hook payloads into Eä canonical
:class:`~eawf.runtime.hooks.event.HookEvent` instances. W05 layers
``runtimes/claude/plugin_install.py`` on top; W06 adds the statusline
modules.

The package itself is intentionally empty of business logic so importing
``eawf.runtime.runtimes`` is cheap and side-effect-free.
"""

from __future__ import annotations
