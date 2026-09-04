"""REL-018/REL-016: the three publication observation adapters.

Each adapter answers one question about one registry: *is the exact
frozen artifact set of this checkpoint visible there right now, on the
channel this checkpoint is allowed to occupy?* Nothing else. An adapter
never publishes, never retries and never decides what the release does
next -- it turns one recorded registry answer into one
:class:`~eawf.workflow.release.observation.PublicationObservation`.

The split between *reading* and *judging* is deliberate and is what
makes the adapters testable at all. Judging is a pure function of a
:class:`~eawf.workflow.release.observation.RecordedResponse`, so the
match, missing, mismatch and unknown paths of every adapter are driven
by recorded fixtures. Reading is a separate seam
(:data:`DEFAULT_REGISTRY_READERS`): no HTTP client ships in this
distribution, so every default reader answers
``registry_unreachable`` and the operator supplies the recorded
response. That is the honest current state -- an adapter that fabricated
a match rather than reporting an unread registry would be exactly the
failure this whole record exists to prevent.

Two rules shape all three adapters:

* **Absence and contradiction are different verdicts.** A registry that
  exposes no such version is ``version_absent``; one that exposes the
  version with digests the manifest does not pin is ``digest_mismatch``.
  Collapsing them would lose the distinction between "the publish never
  landed" and "something else landed under this name".
* **REL-016: the default channel is checked before the digests.** A
  prerelease that the npm ``latest`` tag resolves, or a source-host
  release not flagged prerelease, is a mismatch *even when every digest
  matches* -- the artifacts are right and their exposure is wrong, and
  the exposure is what installers see.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Protocol

from eawf.kernel.spec.release import semver_equivalent
from eawf.kernel.spec.release_config import (
    DEFAULT_DIST_TAG,
    ObservationAdapter,
    ReleaseTargetConfig,
)
from eawf.workflow.release.observation import (
    ObservationCode,
    ObservationRequest,
    PublicationObservation,
    RecordedResponse,
    build_observation,
)

logger = logging.getLogger(__name__)

#: Transport status meaning the registry has no such version.
NOT_FOUND_STATUS: Final[int] = 404

#: Transport status carrying a readable body.
OK_STATUS: Final[int] = 200


class ObservationAdapterFn(Protocol):
    """One registry's read-back judge.

    Implementations are pure: same request and same recorded response
    give the same observation, which is what lets one fixture set cover
    every verdict of every adapter.
    """

    def __call__(
        self,
        request: ObservationRequest,
        response: RecordedResponse,
        *,
        observed_at: datetime,
    ) -> PublicationObservation:
        """Return the observation *response* supports for *request*."""
        ...


class RegistryReader(Protocol):
    """The seam that fetches one registry's answer.

    Separate from the adapter so the network stays out of the judgement:
    a reader that cannot reach its registry returns a non-``200``
    response and the adapter reports ``registry_unreachable`` rather
    than guessing.
    """

    def __call__(self, request: ObservationRequest) -> RecordedResponse:
        """Return the registry's recorded answer to *request*."""
        ...


class UndeclaredObservationAdapterError(KeyError):
    """A target named an adapter this build has no implementation for.

    Raised rather than defaulted: a target whose adapter silently fell
    back to some other registry would produce an observation about the
    wrong thing, and the release would bake on it.

    Attributes:
        adapter: The adapter that has no implementation.
        target_id: The leg that declared it.
    """

    def __init__(self, adapter: ObservationAdapter, target_id: str) -> None:
        """Store the unimplemented *adapter* alongside the leg naming it."""
        super().__init__(
            f"undeclared_observation_adapter: target {target_id!r} declares adapter "
            f"{adapter.value!r}, which has no implementation; implemented: "
            f"{sorted(known.value for known in OBSERVATION_ADAPTERS)}"
        )
        self.adapter = adapter
        self.target_id = target_id


def _mapping(value: Any) -> Mapping[str, Any] | None:
    """Return *value* when it is a mapping, else ``None``.

    Args:
        value: Any decoded JSON fragment.

    Returns:
        The mapping, or ``None`` when the fragment has another shape.
    """
    return value if isinstance(value, Mapping) else None


def _digest_verdict(
    request: ObservationRequest,
    observed: Mapping[str, str],
) -> tuple[ObservationCode, str]:
    """Return the verdict comparing *observed* digests to the frozen set.

    Args:
        request: The read-back request carrying the frozen artifacts.
        observed: Digests the registry advertises, keyed by filename.

    Returns:
        ``(MATCHED, "")`` when every frozen artifact is exposed at its
        frozen digest, or the first disagreement's code and its detail.
    """
    for artifact in request.artifacts:
        seen = observed.get(artifact.filename)
        if seen is None:
            return (
                ObservationCode.ARTIFACT_ABSENT,
                f"{request.identity}@{request.version} exposes no {artifact.filename!r}; "
                f"exposed: {sorted(observed)}",
            )
        if seen != artifact.digest:
            return (
                ObservationCode.DIGEST_MISMATCH,
                f"{artifact.filename!r} is exposed at {seen} but the frozen manifest "
                f"pins {artifact.digest}",
            )
    return ObservationCode.MATCHED, ""


def _transport_verdict(
    request: ObservationRequest,
    response: RecordedResponse,
) -> tuple[ObservationCode, str] | None:
    """Return the verdict the transport status alone decides, if any.

    Args:
        request: The read-back request, for the detail prose.
        response: The recorded registry answer.

    Returns:
        The code and detail when the status settles the read-back on its
        own, or ``None`` when the payload has to be read.
    """
    if response.status == NOT_FOUND_STATUS:
        return (
            ObservationCode.VERSION_ABSENT,
            f"{request.identity} exposes no {request.version}; the registry answered 404",
        )
    if response.status != OK_STATUS:
        return (
            ObservationCode.REGISTRY_UNREACHABLE,
            f"querying {request.identity} for {request.version} answered status "
            f"{response.status}; the read-back settled nothing",
        )
    if _mapping(response.payload) is None:
        return (
            ObservationCode.RESPONSE_UNREADABLE,
            f"the answer for {request.identity}@{request.version} carries no readable body",
        )
    return None


def observe_package_index(
    request: ObservationRequest,
    response: RecordedResponse,
    *,
    observed_at: datetime,
) -> PublicationObservation:
    """Judge a Python package index read-back of one checkpoint.

    Reads the project-and-version projection of the index response:
    ``info.name`` is the project the index answered about and each row
    of ``urls`` carries a ``filename`` plus a ``digests.sha256``. A
    package index has no default-tag concept -- installers resolve a
    prerelease only when asked -- so REL-016's channel check does not
    apply here and no exposure is inferred from silence.

    Args:
        request: The read-back request for this leg.
        response: The recorded index answer.
        observed_at: Timezone-aware UTC instant of the judgement.

    Returns:
        The observation the answer supports.
    """
    settled = _transport_verdict(request, response)
    if settled is not None:
        return build_observation(
            request,
            response,
            code=settled[0],
            observed_digests={},
            detail=settled[1],
            observed_at=observed_at,
        )
    payload = _mapping(response.payload) or {}
    info = _mapping(payload.get("info")) or {}
    name = str(info.get("name", ""))
    if name != request.identity:
        return build_observation(
            request,
            response,
            code=ObservationCode.IDENTITY_MISMATCH,
            observed_digests={},
            detail=f"the index answered about project {name!r}, not {request.identity!r}",
            observed_at=observed_at,
        )
    if str(info.get("version", "")) != request.version:
        return build_observation(
            request,
            response,
            code=ObservationCode.VERSION_ABSENT,
            observed_digests={},
            detail=(f"{request.identity} exposes {info.get('version')!r}, not {request.version!r}"),
            observed_at=observed_at,
        )
    observed = _index_digests(payload)
    code, detail = _digest_verdict(request, observed)
    return build_observation(
        request,
        response,
        code=code,
        observed_digests=observed,
        detail=detail or f"{request.identity}@{request.version} exposes the frozen artifact set",
        observed_at=observed_at,
    )


def _index_digests(payload: Mapping[str, Any]) -> dict[str, str]:
    """Return the ``urls`` digests of an index response, by filename.

    Args:
        payload: The decoded index response body.

    Returns:
        Filename to ``sha256:``-prefixed digest, skipping rows with no
        readable filename or sha256.
    """
    rows = payload.get("urls")
    observed: dict[str, str] = {}
    if not isinstance(rows, list):
        return observed
    for row in rows:
        entry = _mapping(row)
        if entry is None:
            continue
        filename = entry.get("filename")
        sha256 = (_mapping(entry.get("digests")) or {}).get("sha256")
        if isinstance(filename, str) and isinstance(sha256, str):
            observed[filename] = f"sha256:{sha256}" if not sha256.startswith("sha256:") else sha256
    return observed


def observe_npm_registry(
    request: ObservationRequest,
    response: RecordedResponse,
    *,
    observed_at: datetime,
) -> PublicationObservation:
    """Judge an npm registry read-back of one checkpoint.

    Reads the packument projection: ``name`` is the package the registry
    answered about, ``dist-tags`` maps each distribution tag onto a
    version, and ``versions.<semver>.dist.files`` carries the published
    filenames with their ``sha256`` digests. The version key is the
    SemVer spelling of the checkpoint, because npm carries
    ``0.7.0-dev.1`` where the index carries ``0.7.0.dev1``.

    REL-016 lands here: the ``latest`` tag is what a bare ``npm install``
    resolves, so a prerelease sitting on it is a mismatch regardless of
    the digests -- the artifacts are right and their exposure is wrong.

    Args:
        request: The read-back request for this leg.
        response: The recorded packument answer.
        observed_at: Timezone-aware UTC instant of the judgement.

    Returns:
        The observation the answer supports.
    """
    settled = _transport_verdict(request, response)
    if settled is not None:
        return build_observation(
            request,
            response,
            code=settled[0],
            observed_digests={},
            detail=settled[1],
            observed_at=observed_at,
        )
    payload = _mapping(response.payload) or {}
    if str(payload.get("name", "")) != request.identity:
        return build_observation(
            request,
            response,
            code=ObservationCode.IDENTITY_MISMATCH,
            observed_digests={},
            detail=(
                f"the registry answered about package {payload.get('name')!r}, "
                f"not {request.identity!r}"
            ),
            observed_at=observed_at,
        )
    semver = semver_equivalent(request.version)
    entry = _mapping((_mapping(payload.get("versions")) or {}).get(semver))
    if entry is None:
        return build_observation(
            request,
            response,
            code=ObservationCode.VERSION_ABSENT,
            observed_digests={},
            detail=(
                f"{request.identity} exposes no {semver}; exposed: "
                f"{sorted(_mapping(payload.get('versions')) or {})}"
            ),
            observed_at=observed_at,
        )
    dist_tags = _mapping(payload.get("dist-tags")) or {}
    if request.prerelease and dist_tags.get(DEFAULT_DIST_TAG) == semver:
        return build_observation(
            request,
            response,
            code=ObservationCode.PRERELEASE_ON_DEFAULT_CHANNEL,
            observed_digests=_npm_digests(entry),
            detail=(
                f"prerelease {semver} is resolved by the default {DEFAULT_DIST_TAG!r} tag; "
                f"it belongs on {request.target.prerelease_dist_tag!r}"
            ),
            observed_at=observed_at,
        )
    observed = _npm_digests(entry)
    code, detail = _digest_verdict(request, observed)
    return build_observation(
        request,
        response,
        code=code,
        observed_digests=observed,
        detail=detail or f"{request.identity}@{semver} exposes the frozen artifact set",
        observed_at=observed_at,
    )


def _npm_digests(entry: Mapping[str, Any]) -> dict[str, str]:
    """Return the published file digests of one packument version.

    Args:
        entry: The ``versions.<semver>`` object.

    Returns:
        Filename to ``sha256:``-prefixed digest, skipping unreadable
        rows.
    """
    files = (_mapping(entry.get("dist")) or {}).get("files")
    observed: dict[str, str] = {}
    if not isinstance(files, list):
        return observed
    for row in files:
        record = _mapping(row)
        if record is None:
            continue
        name = record.get("name")
        sha256 = record.get("sha256")
        if isinstance(name, str) and isinstance(sha256, str):
            observed[name] = f"sha256:{sha256}" if not sha256.startswith("sha256:") else sha256
    return observed


def observe_source_host_release(
    request: ObservationRequest,
    response: RecordedResponse,
    *,
    observed_at: datetime,
) -> PublicationObservation:
    """Judge a source-host release read-back of one checkpoint.

    Reads the release-object projection: ``repository`` is the
    ``owner/repo`` the host answered about, ``tag_name`` is the tag the
    release object hangs off, ``prerelease`` is the flag that keeps it
    off the "latest release" surface, and each row of ``assets`` carries
    a ``name`` plus a ``digest``.

    REL-016 lands here too, in the flag: a source host advertises the
    newest non-prerelease release as *the* release, so a checkpoint tag
    published with ``prerelease: false`` is exposed on the stable
    default channel exactly as an npm ``latest`` tag would be.

    Args:
        request: The read-back request for this leg.
        response: The recorded release-object answer.
        observed_at: Timezone-aware UTC instant of the judgement.

    Returns:
        The observation the answer supports.
    """
    settled = _transport_verdict(request, response)
    if settled is not None:
        return build_observation(
            request,
            response,
            code=settled[0],
            observed_digests={},
            detail=settled[1],
            observed_at=observed_at,
        )
    payload = _mapping(response.payload) or {}
    if str(payload.get("repository", "")) != request.identity:
        return build_observation(
            request,
            response,
            code=ObservationCode.IDENTITY_MISMATCH,
            observed_digests={},
            detail=(
                f"the host answered about repository {payload.get('repository')!r}, "
                f"not {request.identity!r}"
            ),
            observed_at=observed_at,
        )
    expected_tag = f"v{request.version}"
    if str(payload.get("tag_name", "")) != expected_tag:
        return build_observation(
            request,
            response,
            code=ObservationCode.IDENTITY_MISMATCH,
            observed_digests={},
            detail=(
                f"the release object hangs off tag {payload.get('tag_name')!r}, "
                f"not {expected_tag!r}"
            ),
            observed_at=observed_at,
        )
    observed = _asset_digests(payload)
    if request.prerelease and payload.get("prerelease") is not True:
        return build_observation(
            request,
            response,
            code=ObservationCode.PRERELEASE_ON_DEFAULT_CHANNEL,
            observed_digests=observed,
            detail=(
                f"release {expected_tag} is not flagged prerelease, so the host "
                f"advertises this checkpoint as the latest release"
            ),
            observed_at=observed_at,
        )
    code, detail = _digest_verdict(request, observed)
    return build_observation(
        request,
        response,
        code=code,
        observed_digests=observed,
        detail=detail or f"{request.identity}@{expected_tag} exposes the frozen artifact set",
        observed_at=observed_at,
    )


def _asset_digests(payload: Mapping[str, Any]) -> dict[str, str]:
    """Return the release object's asset digests, by asset name.

    Args:
        payload: The decoded release-object body.

    Returns:
        Asset name to ``sha256:``-prefixed digest, skipping unreadable
        rows.
    """
    assets = payload.get("assets")
    observed: dict[str, str] = {}
    if not isinstance(assets, list):
        return observed
    for row in assets:
        record = _mapping(row)
        if record is None:
            continue
        name = record.get("name")
        digest = record.get("digest")
        if isinstance(name, str) and isinstance(digest, str):
            observed[name] = digest if digest.startswith("sha256:") else f"sha256:{digest}"
    return observed


#: The implementation behind each declared adapter. Total over
#: :class:`~eawf.kernel.spec.release_config.ObservationAdapter` -- the
#: completeness assertion below runs at import, so adding an adapter
#: value without an implementation fails the process rather than
#: leaving one target silently unobservable.
OBSERVATION_ADAPTERS: Final[Mapping[ObservationAdapter, ObservationAdapterFn]] = {
    ObservationAdapter.PACKAGE_INDEX: observe_package_index,
    ObservationAdapter.NPM_REGISTRY: observe_npm_registry,
    ObservationAdapter.SOURCE_HOST_RELEASE: observe_source_host_release,
}

_UNIMPLEMENTED = sorted(
    adapter.value for adapter in ObservationAdapter if adapter not in OBSERVATION_ADAPTERS
)
if _UNIMPLEMENTED:  # pragma: no cover - a build with this defect cannot import
    raise RuntimeError(
        f"observation adapters are incomplete: {_UNIMPLEMENTED} have no implementation"
    )


def resolve_observe_adapter(target: ReleaseTargetConfig) -> ObservationAdapterFn:
    """Return the adapter *target* declares, or refuse.

    The adapter comes from the target's declared
    :attr:`~eawf.kernel.spec.release_config.ReleaseTargetConfig.observe_adapter`
    and nowhere else. There is no default: a leg whose adapter cannot be
    resolved is unobservable, and saying so is the only honest answer.

    Args:
        target: The target's configuration.

    Returns:
        The adapter implementation for that target.

    Raises:
        UndeclaredObservationAdapterError: When the declared adapter has
            no implementation in this build.
    """
    try:
        return OBSERVATION_ADAPTERS[target.observe_adapter]
    except KeyError as exc:
        raise UndeclaredObservationAdapterError(target.observe_adapter, target.target_id) from exc


def _unreachable_reader(request: ObservationRequest) -> RecordedResponse:
    """Return the answer of a registry this build cannot query.

    No HTTP client ships in this distribution, so there is nothing to
    query with. Reporting status ``0`` makes every default read-back
    ``registry_unreachable``, which refuses to settle the leg -- the
    alternative, inferring a verdict from an unmade query, is how a
    release bakes on evidence nobody collected.

    Args:
        request: The read-back request that cannot be dispatched.

    Returns:
        A response carrying no status and no body.
    """
    logger.warning(
        f"registry_reader_unavailable target={request.target.target_id!r} "
        f"adapter={request.target.observe_adapter.value!r} identity={request.identity!r}; "
        f"supply the recorded response instead"
    )
    return RecordedResponse(status=0)


#: The reader behind each adapter. Every entry is the unreachable reader
#: until a live client lands; see :func:`_unreachable_reader`.
DEFAULT_REGISTRY_READERS: Final[Mapping[ObservationAdapter, RegistryReader]] = dict.fromkeys(
    ObservationAdapter, _unreachable_reader
)


def observe_publication(
    request: ObservationRequest,
    *,
    observed_at: datetime,
    response: RecordedResponse | None = None,
    readers: Mapping[ObservationAdapter, RegistryReader] | None = None,
) -> PublicationObservation:
    """Return the read-back observation for one configured leg.

    Args:
        request: The read-back request for this leg.
        observed_at: Timezone-aware UTC instant of the judgement.
        response: A registry answer already in hand. When ``None`` the
            leg's reader is asked for one.
        readers: Reader registry to ask. Defaults to
            :data:`DEFAULT_REGISTRY_READERS`.

    Returns:
        The observation the answer supports.

    Raises:
        UndeclaredObservationAdapterError: When the leg's declared
            adapter has no implementation.
        ValueError: When *observed_at* is naive.
    """
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    adapter = resolve_observe_adapter(request.target)
    if response is None:
        registry = readers if readers is not None else DEFAULT_REGISTRY_READERS
        response = registry[request.target.observe_adapter](request)
    return adapter(request, response, observed_at=observed_at)


__all__ = [
    "DEFAULT_REGISTRY_READERS",
    "NOT_FOUND_STATUS",
    "OBSERVATION_ADAPTERS",
    "OK_STATUS",
    "ObservationAdapterFn",
    "RegistryReader",
    "UndeclaredObservationAdapterError",
    "observe_npm_registry",
    "observe_package_index",
    "observe_publication",
    "observe_source_host_release",
    "resolve_observe_adapter",
]
