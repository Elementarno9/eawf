"""The sweep that runs against a live checkout before a tag is pushed.

One function, and the reason it is a function rather than two call sites
is that a second composition would be free to disagree with the first.
``eawf release tag --push`` and ``eawf release preflight`` must gate on
*the same* sweep: a preflight that reported a different verdict from the
push it precedes would be worse than no preflight at all, because the
operator would have read a green they were never actually gated on.

Nothing here decides anything. The probes decide what is true about the
checkout and the readiness sweep decides what the checkpoint's profile
makes of it; this module binds them to one repository, one remote and one
package version so both operator surfaces run the identical composition.
"""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import ReleaseReadiness, compute_readiness

logger = logging.getLogger(__name__)


def sweep_for_tag(
    config: ReleaseConfig,
    *,
    version: str,
    repo_root: Path,
    remote: str,
    package_version: str,
    source: str | None,
    waiver_count: int,
    computed_at: datetime,
) -> ReleaseReadiness:
    """Return the readiness sweep for *version* over the checkout at *repo_root*.

    Args:
        config: The checkpoint configuration the sweep is judged against.
        version: Checkpoint version being swept. Kept separate from
            ``config.version`` on purpose: a request naming a different
            version than the configuration declares is precisely what
            the ``version_consistency`` row exists to catch, and
            deriving one from the other would hide it.
        repo_root: Working copy the probes read.
        remote: Remote whose branch the source must be reachable from.
        package_version: ``eawf.__version__`` of the tagging checkout.
        source: Revision the sweep is stamped with, or ``None``.
        waiver_count: Waivers recorded against the checkpoint.
        computed_at: Timezone-aware UTC instant the sweep runs at.

    Returns:
        The total sweep: one row per signal, one row per gate the
        checkpoint's profile admits.

    Raises:
        ValueError: When an input is blank, or *computed_at* is naive.
    """
    probes = build_tag_probes(
        TagPreflightInputs(
            repo_root=repo_root,
            version=version,
            tag=f"v{version}",
            package_version=package_version,
            remote=remote,
        )
    )
    readiness = compute_readiness(
        config,
        probes=probes,
        observed_revision=source,
        computed_at=computed_at,
        waiver_count=waiver_count,
    )
    logger.info(
        f"sweep_for_tag version={version!r} remote={remote!r} "
        f"ready={readiness.ready} waiver_count={readiness.waiver_count}"
    )
    return readiness


__all__ = ["sweep_for_tag"]
