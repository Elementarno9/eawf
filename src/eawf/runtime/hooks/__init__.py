"""Eä hook router + runner package.

The package holds:

- :class:`~eawf.runtime.hooks.event.HookEvent` — typed Pydantic model with
  ``extra="forbid"`` carrying the canonical fields (``event_type``,
  ``scope_id``, ``command``, ``args``, ``runtime``, ``occurred_at``,
  ``payloads``) shared across every Eä runtime adapter.
- :class:`~eawf.runtime.hooks.event.HookEventType` — frozen v1 enum of every
  event the runner dispatches on. Initial set per
  the hook design spec §3.3.
- :class:`~eawf.runtime.hooks.runner.HookRunner` — registers callables under one
  or more event types and runs them on demand, returning a list of
  :class:`~eawf.runtime.hooks.runner.HookResult` records and never propagating
  hook-side exceptions.

This package contains library logic only; the CLI entry-point lives in
:mod:`eawf.surfaces.cli.commands.hook`. A Claude-runtime
adapter layers on top via :mod:`eawf.runtime.runtimes.claude.hooks_router`.
"""

from __future__ import annotations

from eawf.runtime.hooks.event import HookEvent, HookEventType
from eawf.runtime.hooks.runner import HookResult, HookRunner

__all__ = [
    "HookEvent",
    "HookEventType",
    "HookResult",
    "HookRunner",
]
