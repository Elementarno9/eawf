"""Subagent prompt-rendering package (B025) + dispatch envelope (P10 W03).

Public API:

- :func:`build_subagent_spec` — project a validated
  :class:`~eawf.kernel.state.models.State` snapshot + wave id into a typed
  :class:`~eawf.workflow.agents.specs.models.SubagentSpec` by walking the
  wave → iter → phase → scope chain and collecting attached decisions,
  hypotheses, and recent audits.
- :func:`render_wave_prompt` — build the :class:`SubagentSpec` and
  render it to a self-contained Markdown prompt for one wave.
- :func:`render_dispatch_envelope` — wrap the wave prompt in a typed
  :class:`DispatchEnvelope` for either the ``claude-code`` or
  ``claude-agent-sdk`` runtime. The SDK branch projects
  :attr:`State.mcp_servers` and :attr:`State.mcp_grants` into
  ``mcp_servers`` and ``allowed_tools`` allow-lists.
- :class:`DispatchEnvelope` — frozen dataclass return type for the
  dispatch adapter.

Both renderers are pure functions — no I/O, no logging side-effects
beyond the module-level ``logger``. The CLI handlers in
:mod:`eawf.cli.commands.lifecycle` own all stdout / file writes.
"""

from __future__ import annotations

from eawf.workflow.dispatch.renderer import (
    DISPATCH_RUNTIMES,
    DispatchEnvelope,
    build_subagent_spec,
    render_dispatch_envelope,
    render_wave_prompt,
)

__all__ = [
    "DISPATCH_RUNTIMES",
    "DispatchEnvelope",
    "build_subagent_spec",
    "render_dispatch_envelope",
    "render_wave_prompt",
]
