"""Runtime layer: daemon, harness adapters, and execution substrate.

The runtime super-package groups the packages that execute waves and own
the live process substrate on top of the kernel and workflow layers:
:mod:`~eawf.runtime.daemon` (the canonical state mutator + JSON-RPC
service), :mod:`~eawf.runtime.runtimes` (per-harness adapters such as
Claude, Codex, and OpenCode), :mod:`~eawf.runtime.mcp` (MCP server +
installer), :mod:`~eawf.runtime.sandbox` (deny-list policy enforcement),
:mod:`~eawf.runtime.session` (session bookkeeping),
:mod:`~eawf.runtime.lock` (portalocker file locks),
:mod:`~eawf.runtime.budget` (token / EU budgeting),
:mod:`~eawf.runtime.ci_loop` (the verify gate loop),
:mod:`~eawf.runtime.worktree` (git worktree lifecycle),
:mod:`~eawf.runtime.hooks` (lifecycle hook dispatch), and
:mod:`~eawf.runtime.vcs` (git / gh wrappers).
"""

from __future__ import annotations
