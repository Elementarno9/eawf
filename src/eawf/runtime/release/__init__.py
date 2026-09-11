"""Release-time execution substrate: the tag chokepoint.

The release *policy* lives in the workflow layer --
:mod:`eawf.workflow.verify.release_probes` knows how to read one fact out
of a working copy and :mod:`eawf.workflow.verify.release_readiness` knows
what the twelve facts together mean. What lives here is the part that
runs: the composition that binds a live checkout, the running package's
own version and a configured remote into one sweep, immediately before
the irreversible act of pushing a tag.

That split is the same one :mod:`eawf.runtime.sandbox` draws. Deciding
what is allowed is policy; enforcing it against a live process is
substrate. A tag push starts a publication that cannot be recalled, so
the last thing to run before it is an execution-time recomputation over
the tree as it actually stands -- never a verdict carried over from an
approval computed at some earlier revision.

Public API:

- :func:`~eawf.runtime.release.chokepoint.sweep_for_tag` -- the sweep
  ``eawf release tag --push`` is gated on, and the same one
  ``eawf release preflight`` prints.
"""

from __future__ import annotations

from eawf.runtime.release.chokepoint import sweep_for_tag

__all__ = ["sweep_for_tag"]
