"""Build-flag gate for the W10 planted TUI defects (PoC fixture).

This module exists only to support the jury proof-of-concept: it injects
three *real* UI misbehaviours behind a single environment build flag so
the W11 jury can be shown catching them. The flag (:data:`POC_DEFECTS_ENV`)
defaults OFF, and every defect site reads it through the one helper
:func:`poc_defects_enabled` -- no module scatters its own ``os.environ``
read -- so the off-path (the only path CI and operators ever take) is
byte-identical to the un-instrumented surface.

The three flag-gated defects, each small and trivially reversible:

* **dead-click** -- a ``@click``-wired App action
  (:meth:`~eawf.surfaces.tui.app.EaApp.action_poc_dead_click`) that
  RESOLVES but changes nothing observable (the resolved-but-inert
  dead-click the behaviour probe classifies ``no_op``). With the flag
  unset the action raises ``SkipAction`` so it never resolves -- the
  honest "no live handler" shape.
* **stale-feed** -- the Home attention band
  (:class:`~eawf.surfaces.tui.widgets.attention_feed.AttentionFeed`)
  suppresses its rebuild on a fresh ``on_state`` delivery, so the feed
  never updates. With the flag unset the band refreshes as designed.
* **hard near-miss** -- the breadcrumb ``code`` segment renders as PLAIN
  (de-linked) text yet its ``app.switch_mode('home')`` action STILL
  resolves (looks de-linked, behaves live -- the subtle de-link
  regression). With the flag unset the segment is a genuine ``[@click]``
  link.

This is a fixture, not a permanent feature: when the jury PoC is retired
the flag, this module, and the three guarded branches come out together.
"""

from __future__ import annotations

import os

#: Environment build flag that arms the three planted PoC defects. Unset
#: (the default) leaves the TUI surface un-instrumented; set to a truthy
#: value (any non-empty string) it arms all three defects at once. Read
#: only via :func:`poc_defects_enabled`.
POC_DEFECTS_ENV: str = "EAWF_POC_DEFECTS"


def poc_defects_enabled() -> bool:
    """Return whether the planted PoC defects are armed.

    The single read of :data:`POC_DEFECTS_ENV`: every defect site calls
    this so the flag is sampled in exactly one place (no scattered
    ``os.environ`` reads). A missing or empty value reads as OFF -- the
    default, un-instrumented surface.

    Returns:
        ``True`` when :data:`POC_DEFECTS_ENV` is set to a non-empty value,
        ``False`` otherwise.
    """
    return bool(os.environ.get(POC_DEFECTS_ENV))


__all__ = ["POC_DEFECTS_ENV", "poc_defects_enabled"]
