"""What the source-host leg attaches to a checkpoint's tag release.

The ``github`` target of the ``dev1`` checkpoint declares three artifact
kinds -- ``release_notes``, ``checksums`` and ``plugin_bundle`` -- and
:func:`~eawf.workflow.release.adapters.observe_source_host_release`
reads the published release object back asset-by-asset, comparing each
name against the frozen manifest. So the publishing workflow and the
observing adapter have to agree on three filenames, and the only way two
places agree on a string is for there to be one place.

That is this module. The publish job writes :func:`source_host_assets`
into the release; the workflow lint asserts it does; the manifest pins
the digest of each. A leg that uploads ``notes.md`` instead publishes
fine and then observes as ``digest_mismatch``, which reads like a
tampered artifact rather than a typo -- the failure this module exists
to make impossible.

The checksums file is deliberately one file over *every* published
artifact rather than one per leg: a reader who has the wheel, the sdist
and the plugin bundle in hand wants a single list to verify against, and
a per-leg split invites a partial one that looks complete.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Final

from eawf.kernel.spec.release_config import ReleaseArtifactKind

logger = logging.getLogger(__name__)

#: Name of the release-notes asset. Uppercase and extension-bearing so
#: it reads as a document in the source host's asset list rather than as
#: the release body, which is a different surface.
RELEASE_NOTES_FILENAME: Final[str] = "RELEASE_NOTES.md"

#: Name of the checksums asset. The bare uppercase stem is the
#: convention ``sha256sum -c`` users already reach for.
CHECKSUMS_FILENAME: Final[str] = "SHA256SUMS"

#: Format of the plugin-bundle asset. A tarball rather than a zip
#: because the rendered plugin trees are dotdirs with executable hooks,
#: and tar preserves both without the mode-stripping a zip imposes.
PLUGIN_BUNDLE_TEMPLATE: Final[str] = "eawf-plugin-{version}.tar.gz"


def source_host_assets(version: str) -> Mapping[ReleaseArtifactKind, str]:
    """Return the filename each source-host artifact kind is attached as.

    Args:
        version: The checkpoint version being published, in its PEP 440
            spelling -- the source host carries the same version the tag
            does, unlike the npm leg.

    Returns:
        One entry per artifact kind the ``github`` target declares,
        keyed by kind so a caller cannot mix up positional names.

    Raises:
        ValueError: When *version* is empty. An empty version would
            name the bundle ``eawf-plugin-.tar.gz``, which uploads
            without complaint and observes as a missing artifact.
    """
    if not version:
        raise ValueError("version must be non-empty to name the plugin bundle")
    return {
        ReleaseArtifactKind.RELEASE_NOTES: RELEASE_NOTES_FILENAME,
        ReleaseArtifactKind.CHECKSUMS: CHECKSUMS_FILENAME,
        ReleaseArtifactKind.PLUGIN_BUNDLE: PLUGIN_BUNDLE_TEMPLATE.format(version=version),
    }


__all__ = [
    "CHECKSUMS_FILENAME",
    "PLUGIN_BUNDLE_TEMPLATE",
    "RELEASE_NOTES_FILENAME",
    "source_host_assets",
]
