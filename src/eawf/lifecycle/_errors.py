"""Shared exception type for lifecycle transitions.

Lives in its own module so the per-entity transition modules
(:mod:`eawf.lifecycle.phase`, :mod:`eawf.lifecycle.iter_`,
:mod:`eawf.lifecycle.wave`, :mod:`eawf.lifecycle.project`) can share the
single :class:`LifecycleError` type without importing one another.
"""

from __future__ import annotations


class LifecycleError(Exception):
    """Raised by lifecycle transitions when a guard rejects the change.

    The CLI layer catches this and remaps to the appropriate exit code.
    """
