"""Release-checkpoint vocabulary: signals, gate bindings and waivers.

The typed *records* of a release live in :mod:`eawf.kernel.spec.release`
and its authored configuration in
:mod:`eawf.kernel.spec.release_config`. This package holds the closed
vocabularies a preflight sweep is written against -- the twelve signal
names, the gate-to-evidence binding, and the waiver rows -- so the sweep
(:mod:`eawf.workflow.verify.release_readiness`) and the signal producers
(:mod:`eawf.workflow.release.producers`) can each depend on one
declaration instead of on each other.
"""

from __future__ import annotations

__all__: list[str] = []
