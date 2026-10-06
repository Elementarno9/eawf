"""Claude Code runtime adapter for Eä.

The package holds the hook router, the plugin install/update/doctor
commands, the SKILL/agent/hook template renderers, the statusline
modules and :mod:`~eawf.runtime.runtimes.claude.plugin_package`, which
emits a standalone CC-plugin-marketplace-installable tree.

The package init deliberately imports nothing: Claude Code spawns
``eawf cc statusline`` on every redraw, and an eager re-export here would
pull the plugin installer (and through it the daemon admission and
config stack) into that hot path. Import from the submodule you need.
"""

from __future__ import annotations
