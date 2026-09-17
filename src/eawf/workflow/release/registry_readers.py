"""Live readers for the three publication observation adapters.

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
"""

from __future__ import annotations

import hashlib
import http.client
import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
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


__all__ = [
    "NPM_REGISTRY_URL",
    "NPM_TARBALL_DIGESTS_FIELD",
    "PACKAGE_INDEX_URL",
    "READ_TIMEOUT_SECONDS",
    "SOURCE_HOST_API_URL",
    "SOURCE_HOST_REPOSITORY_FIELD",
    "USER_AGENT",
    "HttpOpener",
    "HttpReply",
    "NpmRegistryReader",
    "PackageIndexReader",
    "SourceHostReleaseReader",
    "UrlOpen",
    "UrllibOpener",
    "npm_packument_url",
    "package_index_url",
    "source_host_release_url",
]
