"""Subagent prompt-rendering package (B025).

Public API:

- :func:`render_wave_prompt` — build a self-contained Markdown prompt
  for one wave by walking the wave → iter → phase → scope chain and
  collecting attached decisions, hypotheses, and recent audits.

The renderer is a pure function — no I/O, no logging side-effects beyond
the module-level ``logger``. The CLI handlers in
:mod:`eawf.cli.commands.lifecycle` own all stdout / file writes.
"""

from __future__ import annotations

from eawf.dispatch.renderer import render_wave_prompt

__all__ = ["render_wave_prompt"]
