"""Release-checkpoint vocabulary and the manifest leaves it pins.

The typed *records* of a release live in :mod:`eawf.kernel.spec.release`
and its authored configuration in
:mod:`eawf.kernel.spec.release_config`. This package holds two things
those two cannot: the closed vocabularies a preflight sweep is written
against -- the twelve signal names, the gate-to-evidence binding, and
the waiver rows -- so the sweep
(:mod:`eawf.workflow.verify.release_readiness`) and the signal probes
(:mod:`eawf.workflow.release.signal_probes`) can each depend on one
declaration instead of on each other; and the strict manifest leaves
(:mod:`eawf.kernel.release.models`) a checkpoint's artifact inventory,
platform claims, notes and approvals are assembled from.
"""

from __future__ import annotations

from eawf.kernel.release.models import (
    ArtifactIdStr,
    PlatformClaimSet,
    ReleaseApprovalReceipt,
    ReleaseArtifactEntry,
    ReleaseArtifactInventory,
    ReleaseManifest,
    ReleaseNotes,
)

__all__ = [
    "ArtifactIdStr",
    "PlatformClaimSet",
    "ReleaseApprovalReceipt",
    "ReleaseArtifactEntry",
    "ReleaseArtifactInventory",
    "ReleaseManifest",
    "ReleaseNotes",
]
