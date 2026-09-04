"""Signal producers this project ships, keyed by the signal they compute.

A readiness signal with no producer reports ``unavailable``, which is
honest but useless: it names a gap rather than a fact. This module holds
the producers that exist, so a signal graduates from "no producer yet"
to a real verdict the moment its check lands.

Only the ``platform`` signal is produced here today. It is deliberately
first: the platform claim is the one signal whose evidence can be
*forged by accident*, because a journey that runs against a stub or a
masked container looks exactly like a journey that ran, and the
difference only shows up in a user's install. Every other producer lands
with the wave that builds its check.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.release_config import ReleasePlatformClaim

logger = logging.getLogger(__name__)


def _unproven(claims: tuple[ReleasePlatformClaim, ...]) -> tuple[str, ...]:
    """Return the ids of the claims whose receipt did not come from a real host."""
    return tuple(claim.platform_id for claim in claims if not claim.real_host)


def platform_probe(context: ReleaseSignalContext) -> ReleaseSignalOutcome:
    """Return the ``platform`` verdict for the checkpoint in *context*.

    An empty claim set is ``unavailable``, not ``pass``. A checkpoint
    that advertises no platform has proven nothing about any platform,
    and reporting that as green would let the row satisfy a gate by
    being vacuous -- exactly the failure the twelve-row sweep exists to
    make visible.

    A non-empty claim set passes only when every claim carries a
    real-host receipt. At ``dev1`` the single claim is the Linux
    real-bwrap CI job, whose receipt is what the row resolves to.

    Args:
        context: The signal request, carrying the loaded configuration.

    Returns:
        ``unavailable`` with remediation when no platform is claimed,
        ``fail`` naming the unproven platforms when a claim has no
        real-host receipt, and ``pass`` carrying every receipt reference
        otherwise.
    """
    claims = context.config.platform_claims
    if not claims:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            remediation=(
                "the checkpoint advertises no platform, so no platform journey can be "
                "read back; declare a platform_claims entry with the receipt of a "
                "real-host run, or drop the platform gate from the profile"
            ),
        )
    unproven = _unproven(claims)
    if unproven:
        return ReleaseSignalOutcome(
            status=ReleaseSignalStatus.FAIL,
            remediation=(
                f"platform claim(s) {list(unproven)} carry a receipt that did not come "
                f"from a real host; rerun the journey on a real host of each platform "
                f"and repoint receipt_ref, or withdraw the claim"
            ),
            evidence_refs=tuple(claim.receipt_ref for claim in claims),
        )
    logger.info(
        f"platform_probe release_key={context.config.release_key!r} claims={len(claims)} unproven=0"
    )
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.PASS,
        evidence_refs=tuple(claim.receipt_ref for claim in claims),
    )


#: Producers this project ships, by signal. A caller's own probe for the
#: same signal wins: injection is how a test pins a verdict and how a
#: later checkpoint swaps in a stronger check without editing this map.
DEFAULT_RELEASE_PROBES: Final[Mapping[ReleaseSignalName, ReleaseSignalProbe]] = {
    ReleaseSignalName.PLATFORM: platform_probe,
}


__all__ = [
    "DEFAULT_RELEASE_PROBES",
    "platform_probe",
]
