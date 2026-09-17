"""The sweep that runs against a live checkout before release effect.

One composition, and the reason it is a function rather than several
call sites is that a second composition would be free to disagree with
the first. ``eawf release tag --push``, ``eawf release preflight`` and the
daemon's ``release.publish`` and ``release.compute_readiness`` must gate
on *the same* sweep: a preflight that reported a different verdict from
the push or publication it precedes would be worse than no preflight at
all, because the operator would have read a green they were never
actually gated on.

Nothing here decides anything. The probes decide what is true about the
checkout and the readiness sweep decides what the checkpoint's profile
makes of it; this module binds them to one repository, one remote and one
source so every operator surface runs the identical composition.
:func:`sweep_pinned_source` is that same sweep as the daemon reaches it,
bound to the checkout holding its state root.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Final

from eawf.kernel.release.waiver import ReleaseWaiver
from eawf.kernel.spec.release_config import ReleaseConfig
from eawf.workflow.release.signal_probes import build_receipt_probes
from eawf.workflow.verify.release_probes import TagPreflightInputs, build_tag_probes
from eawf.workflow.verify.release_readiness import (
    DEFAULT_SIGNAL_TTL_SECONDS,
    ReleaseReadiness,
    WaiverAcknowledgement,
    compute_readiness,
)

logger = logging.getLogger(__name__)

#: Remote a daemon-side sweep proves ancestry against. The daemon has no
#: operator flag to read one from, and the publish workflows push to the
#: same remote the tag verb defaults to.
PUBLISHING_REMOTE: Final[str] = "origin"


def sweep_for_tag(
    config: ReleaseConfig,
    *,
    version: str,
    repo_root: Path,
    remote: str,
    source: str | None,
    waiver_count: int,
    computed_at: datetime,
    package_version: str | None = None,
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
    waivers: Sequence[ReleaseWaiver] = (),
    acknowledgements: Sequence[WaiverAcknowledgement] = (),
) -> ReleaseReadiness:
    """Return the readiness sweep for *version* over the checkout at *repo_root*.

    The tag probes and the receipt probes are both bound to *repo_root*,
    so no row is read from the process's working directory.

    Args:
        config: The checkpoint configuration the sweep is judged against.
        version: Checkpoint version being swept. Kept separate from
            ``config.version`` on purpose: a request naming a different
            version than the configuration declares is precisely what
            the ``version_consistency`` row exists to catch, and
            deriving one from the other would hide it.
        repo_root: Checkout the probes read.
        remote: Remote whose branch the source must be reachable from.
        source: Revision the sweep is stamped with, or ``None``. Without
            a *package_version* it is also the commit the version module
            and the changelog are read out of and whose ancestry is
            proven, so a later commit on HEAD cannot change the verdict;
            ``None`` there reads the commit HEAD names.
        waiver_count: Waivers recorded against the checkpoint.
        computed_at: Timezone-aware UTC instant the sweep runs at.
        package_version: ``eawf.__version__`` of a tagging checkout.
            When given, the probes read the working copy and HEAD, which
            is the tree a tag push is about to name.
        ttl_seconds: Freshness window stamped on each row.
        waivers: The counted waiver rows explaining *waiver_count*.
        acknowledgements: Operator acceptances of those waivers.

    Returns:
        The total sweep: one row per signal, one row per gate the
        checkpoint's profile admits.

    Raises:
        ValueError: When an input is blank, *computed_at* is naive, or
            the waiver block is rejected by the sweep.
    """
    inputs = TagPreflightInputs(
        repo_root=repo_root,
        version=version,
        tag=f"v{version}",
        remote=remote,
        package_version=package_version,
        source_sha=None if package_version is not None else source,
    )
    probes = build_tag_probes(inputs) | build_receipt_probes(repo_root)
    readiness = compute_readiness(
        config,
        probes=probes,
        observed_revision=source,
        computed_at=computed_at,
        ttl_seconds=ttl_seconds,
        waiver_count=waiver_count,
        waivers=waivers,
        acknowledgements=acknowledgements,
    )
    logger.info(
        f"sweep_for_tag version={version!r} remote={remote!r} "
        f"revision={inputs.revision!r} ready={readiness.ready} "
        f"waiver_count={readiness.waiver_count}"
    )
    return readiness


def sweep_pinned_source(
    config: ReleaseConfig,
    *,
    version: str,
    state_path: Path,
    pinned: str | None,
    observed_revision: str | None,
    waiver_count: int,
    computed_at: datetime,
    ttl_seconds: int = DEFAULT_SIGNAL_TTL_SECONDS,
    waivers: Sequence[ReleaseWaiver] = (),
    acknowledgements: Sequence[WaiverAcknowledgement] = (),
) -> ReleaseReadiness:
    """Return the sweep a daemon verb runs over the checkout holding *state_path*.

    A daemon's working directory is whichever directory it was started
    in, and the checkout's HEAD may have moved on since the approval, so
    neither enters this sweep: the checkout is the one the state root
    lives in and the commit is the one the record pins.

    Args:
        config: The checkpoint configuration the sweep is judged against.
        version: Checkpoint version the verb names.
        state_path: Path to ``<repo>/.ea/state.json``; its grandparent is
            the checkout swept.
        pinned: The ``source_sha`` the verb's record carries, if any.
        observed_revision: The revision the caller asked about, if any.
            Beside a pin it may only repeat it. Without one it is the
            commit swept, and ``None`` sweeps the commit HEAD names.
        waiver_count: Waivers recorded against the checkpoint.
        computed_at: Timezone-aware UTC instant the sweep runs at.
        ttl_seconds: Freshness window stamped on each row.
        waivers: The counted waiver rows explaining *waiver_count*.
        acknowledgements: Operator acceptances of those waivers.

    Returns:
        The total sweep, stamped with the commit it read.

    Raises:
        ValueError: When *observed_revision* names a commit other than
            *pinned* -- the approval binds the pin, so a sweep of any
            other commit cannot clear it -- or when the sweep rejects
            its inputs.
    """
    if pinned is not None and observed_revision not in (None, pinned):
        raise ValueError(
            f"observed_revision {observed_revision!r} is not the pinned source {pinned!r}; "
            f"the sweep runs at the commit the record pins"
        )
    return sweep_for_tag(
        config,
        version=version,
        repo_root=state_path.parent.parent,
        remote=PUBLISHING_REMOTE,
        source=pinned or observed_revision,
        waiver_count=waiver_count,
        computed_at=computed_at,
        ttl_seconds=ttl_seconds,
        waivers=waivers,
        acknowledgements=acknowledgements,
    )


__all__ = ["PUBLISHING_REMOTE", "sweep_for_tag", "sweep_pinned_source"]
