"""Live readers for the publication observation adapters.

An adapter *judges* one registry answer; a reader *fetches* it. The
readers here query the public registries with the standard library's
:mod:`urllib` -- no HTTP client is a runtime dependency of this
distribution -- and hand back the
:class:`~eawf.workflow.release.observation.RecordedResponse` the adapter
judges. Every reader takes its :class:`HttpOpener` as a field, so the
whole fetch path is exercised against recorded bodies without a socket.

A recorded response is the registry's answer *plus* the facts only the
reader can supply, because two registries do not publish what the
frozen manifest pins:

* **npm** advertises a tarball's sha1 (``dist.shasum``) and sha512
  (``dist.integrity``) but never its sha256. The reader downloads
  ``dist.tarball``, hashes it, and records the digest under
  :data:`NPM_TARBALL_DIGESTS_FIELD` keyed by the frozen filename -- the
  registry's own tarball name drops the package scope, so the frozen
  name is the only one the manifest comparison can use.
* **GitHub** release objects do not name their repository. The reader
  asked about exactly one ``owner/repo`` -- the URL is built from it --
  and records that identity under :data:`SOURCE_HOST_REPOSITORY_FIELD`.

A request that got no answer at all (DNS, TLS, a refused or dropped
connection, a timeout) reads as status ``0``, which the adapters report
as ``registry_unreachable``: nothing was learned, so nothing is settled.

One leg is not HTTP at all. The Codex plugin tree is published to a git
branch, so :class:`GitRefReader` reads it with git plumbing through
:class:`SubprocessGitRunner` rather than :mod:`urllib`, and maps the same
statuses onto the same three meanings. Its answer carries the ref, the
tip it resolved to and the published tree's digest, because none of the
three is legible from a ref listing alone.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Final, Protocol

from eawf._version import __version__
from eawf.kernel.spec.release import semver_equivalent
from eawf.workflow.release.observation import ObservationRequest, RecordedResponse

logger = logging.getLogger(__name__)

#: Base URL of the Python package index's JSON API.
PACKAGE_INDEX_URL: Final[str] = "https://pypi.org"

#: Base URL of the npm registry. Tarballs are only fetched from here.
NPM_REGISTRY_URL: Final[str] = "https://registry.npmjs.org"

#: Base URL of the source host's REST API.
SOURCE_HOST_API_URL: Final[str] = "https://api.github.com"

#: Seconds one request may take before it counts as unanswered.
READ_TIMEOUT_SECONDS: Final[float] = 30.0

#: Field the npm reader adds to ``versions.<semver>.dist``: the sha256 of
#: the downloaded tarball, keyed by the frozen filename.
NPM_TARBALL_DIGESTS_FIELD: Final[str] = "tarball_digests"

#: Field the source-host reader adds to the release object: the
#: ``owner/repo`` the release was requested for.
SOURCE_HOST_REPOSITORY_FIELD: Final[str] = "repository"

#: Branch the Codex plugin tree is published to. The Codex ``git-subdir``
#: source pins this ref by name, so it is a property of the distribution
#: rather than a per-checkpoint choice, and a tree read off any other ref
#: says nothing about what a Codex install resolves.
PLUGINS_DIST_REF: Final[str] = "refs/heads/plugins-dist"

#: Field the git reader records: the ref it listed the tree off.
GIT_REF_FIELD: Final[str] = "ref"

#: Field the git reader records: the commit the ref resolved to.
GIT_TIP_FIELD: Final[str] = "tip"

#: Field the git reader records: the published tree's digest, keyed by
#: the repository path it was read at.
GIT_TREE_DIGESTS_FIELD: Final[str] = "tree_digests"

#: Seconds one git invocation may take before it counts as unanswered.
GIT_TIMEOUT_SECONDS: Final[float] = 120.0

#: Identifies the observer to the registries; the source host refuses
#: requests without one.
USER_AGENT: Final[str] = f"eawf/{__version__} (release observe)"

_JSON_HEADERS: Final[Mapping[str, str]] = {"Accept": "application/json"}
_TARBALL_HEADERS: Final[Mapping[str, str]] = {"Accept": "application/octet-stream"}
_SOURCE_HOST_HEADERS: Final[Mapping[str, str]] = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

#: An ``owner/repo`` pair as the source host spells it. The repository
#: segment may not be ``.`` or ``..``, which would walk the API path.
_REPOSITORY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9-]*/(?!\.{1,2}$)[A-Za-z0-9._-]+"
)

#: A git object name as ``ls-remote`` prints it in its first column.
_OBJECT_NAME_PATTERN: Final[re.Pattern[str]] = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True, slots=True)
class HttpReply:
    """One HTTP answer as a reader sees it.

    Attributes:
        status: Transport status, or ``0`` when no answer arrived.
        body: Response body; empty unless the status is ``200``.
    """

    status: int
    body: bytes = b""


class HttpOpener(Protocol):
    """Performs one unauthenticated GET and returns its answer."""

    def __call__(self, url: str, *, headers: Mapping[str, str]) -> HttpReply:
        """Return the answer to a GET of *url* sent with *headers*."""
        ...


class UrlOpen(Protocol):
    """The slice of :func:`urllib.request.urlopen` the opener calls."""

    def __call__(self, url: urllib.request.Request, *, timeout: float) -> Any:
        """Return the open response for *url*, or raise a transport error."""
        ...


@dataclass(frozen=True, slots=True)
class UrllibOpener:
    """The production :class:`HttpOpener`, built on :mod:`urllib`.

    An HTTP error status is passed through because ``404`` is itself an
    answer. Every failure to get an answer maps to status ``0`` rather
    than raising, so a flaky network reads as ``registry_unreachable``
    instead of aborting the verb.

    Attributes:
        urlopen: The urllib entry point; a field so the error mapping is
            testable without a socket.
        timeout_seconds: Per-request timeout.
    """

    urlopen: UrlOpen = urllib.request.urlopen
    timeout_seconds: float = READ_TIMEOUT_SECONDS

    def __call__(self, url: str, *, headers: Mapping[str, str]) -> HttpReply:
        """Return the answer to a GET of *url*.

        Args:
            url: Absolute ``https`` URL to fetch.
            headers: Request headers; the user agent is added here.

        Returns:
            The status and body, or status ``0`` when nothing answered.
        """
        request = urllib.request.Request(
            url, headers={"User-Agent": USER_AGENT, **headers}, method="GET"
        )
        try:
            with self.urlopen(request, timeout=self.timeout_seconds) as answer:
                reply = HttpReply(status=int(answer.status), body=answer.read())
        except urllib.error.HTTPError as exc:
            exc.close()
            reply = HttpReply(status=exc.code)
        except (http.client.HTTPException, OSError) as exc:
            # URLError and TimeoutError are OSErrors; HTTPException covers
            # a connection that dropped mid-body.
            logger.warning(f"registry_read_failed url={url!r} error={exc!r}")
            return HttpReply(status=0)
        logger.info(f"registry_read url={url!r} status={reply.status} bytes={len(reply.body)}")
        return reply


def _segment(value: str) -> str:
    """Return *value* quoted as one URL path segment."""
    return urllib.parse.quote(value, safe="")


def _decoded(reply: HttpReply) -> RecordedResponse:
    """Return the recorded response a JSON *reply* supports.

    Args:
        reply: The registry's answer.

    Returns:
        The status alone for a non-``200`` answer; otherwise the status
        with the decoded object, or with no payload when the body is not
        a JSON object -- which the adapters report as unreadable.
    """
    if reply.status != HTTPStatus.OK:
        return RecordedResponse(status=reply.status)
    try:
        decoded = json.loads(reply.body)
    except ValueError:
        return RecordedResponse(status=reply.status)
    return RecordedResponse(
        status=reply.status, payload=decoded if isinstance(decoded, dict) else None
    )


def _mapping(value: Any) -> Mapping[str, Any]:
    """Return *value* when it is a mapping, else an empty one."""
    return value if isinstance(value, Mapping) else {}


def package_index_url(request: ObservationRequest) -> str:
    """Return the index's per-version JSON URL for *request*.

    Args:
        request: The read-back request for the package index leg.

    Returns:
        ``<index>/pypi/<name>/<version>/json``.
    """
    return f"{PACKAGE_INDEX_URL}/pypi/{_segment(request.identity)}/{_segment(request.version)}/json"


def npm_packument_url(request: ObservationRequest) -> str:
    """Return the registry's packument URL for *request*.

    Args:
        request: The read-back request for the npm leg.

    Returns:
        ``<registry>/<package>``, with a scope's slash percent-encoded.
    """
    return f"{NPM_REGISTRY_URL}/{urllib.parse.quote(request.identity, safe='@')}"


def source_host_release_url(request: ObservationRequest) -> str:
    """Return the source host's release-by-tag URL for *request*.

    Args:
        request: The read-back request for the source-host leg.

    Returns:
        ``<api>/repos/<owner>/<repo>/releases/tags/v<version>``.

    Raises:
        ValueError: When the identity is not an ``owner/repo`` pair.
    """
    if _REPOSITORY_PATTERN.fullmatch(request.identity) is None:
        raise ValueError(f"source-host identity {request.identity!r} is not an 'owner/repo' pair")
    return (
        f"{SOURCE_HOST_API_URL}/repos/{request.identity}/releases/tags/"
        f"{_segment(f'v{request.version}')}"
    )


def _served_by_registry(url: str) -> bool:
    """Return whether *url* is an ``https`` URL on the npm registry host."""
    parts = urllib.parse.urlsplit(url)
    return (
        parts.scheme == "https" and parts.netloc == urllib.parse.urlsplit(NPM_REGISTRY_URL).netloc
    )


@dataclass(frozen=True, slots=True)
class PackageIndexReader:
    """Reads the package index's JSON document for one version.

    Attributes:
        opener: Performs the GET.
    """

    opener: HttpOpener

    def __call__(self, request: ObservationRequest) -> RecordedResponse:
        """Return the index's answer for *request*.

        Args:
            request: The read-back request for the package index leg.

        Returns:
            The decoded per-version document, or its bare status.
        """
        return _decoded(self.opener(package_index_url(request), headers=_JSON_HEADERS))


@dataclass(frozen=True, slots=True)
class NpmRegistryReader:
    """Reads the npm packument and hashes the version's tarball.

    The packument is fetched first; the tarball only when the packument
    lists the version with a ``dist.tarball`` on the registry host. A
    version the packument does not list is returned as-is, which the
    adapter reports as ``version_absent`` -- the shape a stale CDN copy
    of the packument takes. A tarball URL off the registry host is not
    followed, because the packument is not trusted to pick what this
    process downloads.

    Attributes:
        opener: Performs both GETs.
    """

    opener: HttpOpener

    def __call__(self, request: ObservationRequest) -> RecordedResponse:
        """Return the packument with the tarball digest recorded in it.

        Args:
            request: The read-back request for the npm leg.

        Returns:
            The packument, with ``versions.<semver>.dist`` carrying
            :data:`NPM_TARBALL_DIGESTS_FIELD` once the tarball was read.
            A tarball answering other than ``200`` returns that status
            alone; an off-registry tarball returns status ``0``.

        Raises:
            ValueError: When the frozen set does not pin exactly one
                artifact. An npm version publishes one tarball, so any
                other count cannot be compared.
        """
        if len(request.artifacts) != 1:
            raise ValueError(
                f"an npm version publishes one tarball, but {request.identity} freezes "
                f"{len(request.artifacts)} artifacts: "
                f"{[artifact.filename for artifact in request.artifacts]}"
            )
        packument = _decoded(self.opener(npm_packument_url(request), headers=_JSON_HEADERS))
        if packument.payload is None:
            return packument
        semver = semver_equivalent(request.version)
        versions = _mapping(packument.payload.get("versions"))
        entry = _mapping(versions.get(semver))
        dist = _mapping(entry.get("dist"))
        tarball = dist.get("tarball")
        if not isinstance(tarball, str):
            return packument
        if not _served_by_registry(tarball):
            logger.warning(
                f"npm_tarball_refused identity={request.identity!r} version={semver!r} "
                f"tarball={tarball!r}; only {NPM_REGISTRY_URL} is read"
            )
            return RecordedResponse(status=0)
        reply = self.opener(tarball, headers=_TARBALL_HEADERS)
        if reply.status != HTTPStatus.OK:
            return RecordedResponse(status=reply.status)
        digest = f"sha256:{hashlib.sha256(reply.body).hexdigest()}"
        recorded_dist = {**dist, NPM_TARBALL_DIGESTS_FIELD: {request.artifacts[0].filename: digest}}
        payload = {
            **packument.payload,
            "versions": {**versions, semver: {**entry, "dist": recorded_dist}},
        }
        return RecordedResponse(status=packument.status, payload=payload)


@dataclass(frozen=True, slots=True)
class SourceHostReleaseReader:
    """Reads the source host's release object for one version tag.

    Attributes:
        opener: Performs the GET.
    """

    opener: HttpOpener

    def __call__(self, request: ObservationRequest) -> RecordedResponse:
        """Return the release object with its repository recorded.

        Args:
            request: The read-back request for the source-host leg.

        Returns:
            The release object carrying
            :data:`SOURCE_HOST_REPOSITORY_FIELD`, or its bare status.

        Raises:
            ValueError: When the identity is not an ``owner/repo`` pair.
        """
        answer = _decoded(
            self.opener(source_host_release_url(request), headers=_SOURCE_HOST_HEADERS)
        )
        if answer.payload is None:
            return answer
        return RecordedResponse(
            status=answer.status,
            payload={**answer.payload, SOURCE_HOST_REPOSITORY_FIELD: request.identity},
        )


def plugins_dist_path(version: str) -> str:
    """Return the retained tree path one version is published at.

    Args:
        version: Normalized checkpoint version.

    Returns:
        The repository-relative path of that version's Codex plugin
        tree on the publication branch.
    """
    return f"versions/{version}/plugins/eawf"


def source_host_clone_url(identity: str) -> str:
    """Return the ``https`` clone URL of the ``owner/repo`` *identity*.

    Args:
        identity: The ``owner/repo`` pair the branch is published on.

    Returns:
        The clone URL ``git ls-remote`` is pointed at.

    Raises:
        ValueError: When *identity* is not an ``owner/repo`` pair. The
            value reaches a subprocess argument, so an identity that
            does not match the pattern is refused rather than passed on.
    """
    if _REPOSITORY_PATTERN.fullmatch(identity) is None:
        raise ValueError(f"git-ref identity {identity!r} is not an 'owner/repo' pair")
    return f"https://github.com/{identity}.git"


def tree_listing_digest(listing: bytes) -> str:
    """Return the ``sha256:`` digest of one ``git ls-tree`` listing.

    The listing is hashed as git emitted it, with no normalization. Both
    sides of the comparison run the same plumbing command over the same
    tree, so the raw bytes already agree; re-sorting or re-spacing them
    here would only give the publisher and the observer two ways to
    disagree about what they hashed.

    Args:
        listing: Raw stdout of ``git ls-tree -r --full-tree``.

    Returns:
        The ``sha256:``-prefixed digest of those bytes.
    """
    return f"sha256:{hashlib.sha256(listing).hexdigest()}"


@dataclass(frozen=True, slots=True)
class GitReply:
    """One git invocation's answer as a reader sees it.

    Attributes:
        exit_code: Process exit status; non-zero means the command did
            not answer.
        stdout: Raw standard output, kept as bytes because a tree
            listing is hashed rather than read.
    """

    exit_code: int
    stdout: bytes = b""


class GitRunner(Protocol):
    """Runs one read-only git command and returns its answer."""

    def __call__(self, argv: Sequence[str]) -> GitReply:
        """Return the answer to running ``git`` with *argv*."""
        ...


@dataclass(frozen=True, slots=True)
class SubprocessGitRunner:
    """The production :class:`GitRunner`, built on :mod:`subprocess`.

    Every failure to get an answer maps to a non-zero exit code rather
    than raising, so an unreachable remote reads as
    ``registry_unreachable`` instead of aborting the verb -- the same
    contract :class:`UrllibOpener` keeps for the HTTP legs.

    Attributes:
        run: The subprocess entry point; a field so the error mapping is
            testable without spawning git.
        timeout_seconds: Per-invocation timeout.
    """

    run: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
    timeout_seconds: float = GIT_TIMEOUT_SECONDS

    def __call__(self, argv: Sequence[str]) -> GitReply:
        """Return the answer to running ``git`` with *argv*.

        Args:
            argv: Arguments after the ``git`` executable itself.

        Returns:
            The exit code and raw stdout, or a non-zero code when git
            could not be run at all.
        """
        command = ["git", *argv]
        try:
            completed = self.run(
                command,
                capture_output=True,
                check=False,
                timeout=self.timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning(f"git_read_failed argv={list(argv)!r} error={exc!r}")
            return GitReply(exit_code=1)
        logger.info(
            f"git_read argv={list(argv)!r} exit_code={completed.returncode} "
            f"bytes={len(completed.stdout)}"
        )
        return GitReply(exit_code=int(completed.returncode), stdout=completed.stdout)


@dataclass(frozen=True, slots=True)
class GitRefReader:
    """Reads the publication branch and the version's published tree.

    Three plumbing calls, in the order that lets each answer the next
    one's precondition: ``ls-remote`` settles whether the branch exists
    at all, a shallow ``fetch`` brings its tip into the local object
    database, and ``ls-tree`` lists the version's retained path out of
    that tip. The fetch is what makes the tree readable without a full
    clone; nothing else about the local repository is touched, and the
    ref is never checked out.

    A branch the remote does not carry is status ``404`` -- the
    publication never landed. A version path the branch does not carry
    answers ``200`` with no tree digest, which the adapter reads the
    same way for the same reason: the branch is there and this version
    is not on it. Every other failure is status ``0``, meaning the
    read-back learned nothing.

    Attributes:
        runner: Runs each git command.
    """

    runner: GitRunner

    def __call__(self, request: ObservationRequest) -> RecordedResponse:
        """Return the branch's answer for *request*.

        Args:
            request: The read-back request for the git-ref leg.

        Returns:
            The recorded answer, carrying the ref, its tip and the
            published tree's digest keyed by the path it was read at.

        Raises:
            ValueError: When the identity is not an ``owner/repo`` pair.
        """
        url = source_host_clone_url(request.identity)
        listing = self.runner(["ls-remote", url, PLUGINS_DIST_REF])
        if listing.exit_code != 0:
            return RecordedResponse(status=0)
        tip = _first_ref_sha(listing.stdout)
        if tip is None:
            return RecordedResponse(status=int(HTTPStatus.NOT_FOUND))
        fetched = self.runner(["fetch", "--quiet", "--no-tags", "--depth", "1", url, tip])
        if fetched.exit_code != 0:
            return RecordedResponse(status=0)
        path = plugins_dist_path(request.version)
        tree = self.runner(["ls-tree", "-r", "--full-tree", tip, "--", path])
        if tree.exit_code != 0:
            return RecordedResponse(status=0)
        digests = {path: tree_listing_digest(tree.stdout)} if tree.stdout.strip() else {}
        return RecordedResponse(
            status=int(HTTPStatus.OK),
            payload={
                SOURCE_HOST_REPOSITORY_FIELD: request.identity,
                GIT_REF_FIELD: PLUGINS_DIST_REF,
                GIT_TIP_FIELD: tip,
                GIT_TREE_DIGESTS_FIELD: digests,
            },
        )


def _first_ref_sha(listing: bytes) -> str | None:
    """Return the object name of the first ``ls-remote`` row, if any.

    Args:
        listing: Raw ``git ls-remote`` stdout.

    Returns:
        The 40-character object name, or ``None`` when the remote
        listed no matching ref or answered in another shape.
    """
    for line in listing.decode("utf-8", errors="replace").splitlines():
        candidate = line.split("\t", 1)[0].strip()
        if _OBJECT_NAME_PATTERN.fullmatch(candidate):
            return candidate
    return None


__all__ = [
    "GIT_REF_FIELD",
    "GIT_TIMEOUT_SECONDS",
    "GIT_TIP_FIELD",
    "GIT_TREE_DIGESTS_FIELD",
    "NPM_REGISTRY_URL",
    "NPM_TARBALL_DIGESTS_FIELD",
    "PACKAGE_INDEX_URL",
    "PLUGINS_DIST_REF",
    "READ_TIMEOUT_SECONDS",
    "SOURCE_HOST_API_URL",
    "SOURCE_HOST_REPOSITORY_FIELD",
    "USER_AGENT",
    "GitRefReader",
    "GitReply",
    "GitRunner",
    "HttpOpener",
    "HttpReply",
    "NpmRegistryReader",
    "PackageIndexReader",
    "SourceHostReleaseReader",
    "SubprocessGitRunner",
    "UrlOpen",
    "UrllibOpener",
    "npm_packument_url",
    "package_index_url",
    "plugins_dist_path",
    "source_host_clone_url",
    "source_host_release_url",
    "tree_listing_digest",
]
