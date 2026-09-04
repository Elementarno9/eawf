"""Derive the npm dist-tag and npm version a released version publishes under.

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

:func:`npm_version_for` is the second half of the same question. npm
speaks SemVer and rejects ``0.7.0.dev1`` outright, so the publish step
needs the ``0.7.0-dev.1`` spelling of the checkpoint it is tagging. It
reads the same grammar, then refuses the two spellings SemVer would
order backwards -- one shared parse, so the pair can never disagree
about what the string even is.

:func:`~eawf.kernel.spec.release.semver_equivalent` answers the same
mapping one layer down and stays: it is total over the *train* grammar
only (``X.Y.Z`` / ``X.Y.ZrcN`` / ``X.Y.Z.devN``) because the observation
adapters that call it read back checkpoints, and a read-back of a
version no checkpoint declares is a bug rather than a spelling. The
publish pipeline sees whatever ``eawf.__version__`` says, which is the
wider grammar above, so it asks here.
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


def npm_version_for(version: str) -> str:
    """Return the SemVer spelling npm publishes *version* under.

    npm's version grammar is SemVer, which has no PEP-440 ``.devN`` or
    ``rcN`` segment: ``npm publish`` refuses ``0.7.0.dev1`` as invalid
    before it ever reaches the registry. The prerelease segment moves
    behind a hyphen and its counter behind a dot, so ``0.7.0.dev1``
    publishes as ``0.7.0-dev.1`` and ``0.7.0rc1`` as ``0.7.0-rc.1``.

    Two spellings are refused rather than mapped, because every
    plausible SemVer name for them sorts the wrong way round and a
    silently misordered publish is worse than a red job. ``0.7.0.post1``
    would have to become ``0.7.0-post.1``, which SemVer resolves *below*
    the ``0.7.0`` it was cut to supersede; ``0.7.0rc1.dev4`` is a dev
    build *of* rc1 under PEP 440, but ``0.7.0-rc.1.dev.4`` outranks
    ``0.7.0-rc.1`` under SemVer. A post-release ships as a new patch; a
    dev build of a release candidate ships as its own ``.devN``.

    Args:
        version: A release version string such as ``0.7.0``,
            ``0.7.0rc1`` or ``0.7.0.dev3``. No ``v`` prefix and no
            surrounding whitespace: the caller passes
            ``eawf.__version__``, not a git tag name.

    Returns:
        The SemVer-equivalent string: the release core alone for a final
        release, or the core plus a ``-<phase>.<n>`` prerelease segment.

    Raises:
        TypeError: When *version* is not a :class:`str`.
        ValueError: When *version* does not match the release grammar,
            or carries a segment combination SemVer cannot order
            faithfully (``.postN``, or a pre-release with ``.devN``).
    """
    if not isinstance(version, str):
        raise TypeError(f"version must be a str, got {type(version).__name__}")
    match = _VERSION_RE.match(version)
    if match is None:
        raise ValueError(f"unsupported version string: {version!r}")
    release = match["release"]
    pre_phase, dev_n = match["pre_phase"], match["dev_n"]
    if match["post_n"] is not None:
        raise ValueError(
            f"no npm version for post-release {version!r}: SemVer sorts every "
            f"prerelease below {release}, so a post-release must ship as a new patch"
        )
    if pre_phase is not None and dev_n is not None:
        raise ValueError(
            f"no npm version for {version!r}: SemVer sorts a dev build of "
            f"{release}{pre_phase}{match['pre_n']} above it, not below; cut it as a "
            f"plain '{release}.dev{int(dev_n)}' instead"
        )
    if pre_phase is not None:
        return f"{release}-{pre_phase}.{int(match['pre_n'])}"
    if dev_n is not None:
        return f"{release}-dev.{int(dev_n)}"
    return release
