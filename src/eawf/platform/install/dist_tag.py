"""Derive the npm dist-tag a released version publishes under.

``npm publish`` with no ``--tag`` writes the ``latest`` dist-tag, so a
prerelease upload silently becomes the default install for every new
user — ``npm install @elementarno/eawf`` would hand out an ``rc`` or a
``dev`` build. Routing prereleases to a separate channel keeps ``latest``
pinned to the newest stable release; a prerelease is then reachable only
by an explicit ``@next`` request.

The grammar accepted here is the project's own release grammar (the
semver core plus the optional PEP-440 segments ``tools/version_bump.py``
can emit), widened with ``.postN`` / ``.devN``. Anything outside it
raises rather than defaulting, because a version string the release
pipeline cannot classify must red the publish job instead of guessing
``latest`` and shipping a prerelease to every user.
"""

from __future__ import annotations

import re
from typing import Final

#: Dist-tag for a final release: the default ``npm install`` resolves here.
DIST_TAG_LATEST: Final[str] = "latest"

#: Dist-tag for any prerelease (``a`` / ``b`` / ``rc`` / ``.dev``): opt-in only.
DIST_TAG_NEXT: Final[str] = "next"

#: Release core plus the optional PEP-440 pre / post / dev segments.
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<release>\d+(?:\.\d+)*)"
    r"(?:(?P<pre_phase>a|b|rc)(?P<pre_n>\d+))?"
    r"(?:\.post(?P<post_n>\d+))?"
    r"(?:\.dev(?P<dev_n>\d+))?$"
)


def dist_tag_for_version(version: str) -> str:
    """Return the npm dist-tag *version* must be published under.

    Args:
        version: A release version string such as ``0.7.0``, ``0.7.0rc1``
            or ``0.7.0.dev3``. No ``v`` prefix and no surrounding
            whitespace: the caller passes ``eawf.__version__``, not a git
            tag name.

    Returns:
        :data:`DIST_TAG_NEXT` when *version* carries a pre-release
        (``a`` / ``b`` / ``rc``) or ``.dev`` segment, otherwise
        :data:`DIST_TAG_LATEST`.

    Raises:
        TypeError: When *version* is not a :class:`str`.
        ValueError: When *version* does not match the release grammar.
    """
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"unsupported version string: {version!r}")
    if match["pre_phase"] is not None or match["dev_n"] is not None:
        return DIST_TAG_NEXT
    return DIST_TAG_LATEST
