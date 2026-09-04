"""REL-018 records: the frozen manifest and one independent read-back.

A publication *report* is what an adapter said it did; an *observation*
is what a third party can still see afterwards. Only the second can
bake a release, so the two are different records with different
evidence requirements, and this module holds the second.

An observation is a claim about three things at once, and all three are
fields rather than prose because a later reader has to be able to
re-derive the verdict:

* **what was queried** -- :attr:`PublicationObservation.queried_identity`
  is the external name the adapter asked the registry about (the PyPI
  project, the npm package, the ``owner/repo`` release). A read-back
  that queried the wrong identity proves nothing about this release;
* **what came back** -- :attr:`PublicationObservation.observed_digests`
  are the digests the registry advertises, keyed by filename, and
  :attr:`PublicationObservation.adapter_digest` pins the exact response
  they were read out of, so the verdict is reproducible from the
  recorded response alone;
* **what it was compared against** -- the frozen manifest. The release
  record pins only ``manifest_digest``; :class:`FrozenManifest` is the
  artifact behind it, and :func:`assert_manifest_binds` refuses a
  manifest whose digest is not the one the release approved. Without
  that check an observation could quietly compare against a manifest
  nobody approved and still read as a match.

:class:`ObservationCode` is the closed vocabulary of verdicts and
:data:`CODE_RESULTS` maps each one onto its
:class:`ObservationResult`, so the coarse result can never disagree
with the specific code.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Any, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    NormalizedVersionStr,
    ReferenceStr,
    Release,
    ReleaseKeyStr,
    Sha256DigestStr,
    TargetIdStr,
    is_prerelease,
)
from eawf.kernel.spec.release_config import (
    ObservationAdapter,
    ReleaseArtifactKind,
    ReleaseConfig,
    ReleaseTargetConfig,
)
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)

#: External identity a registry is queried by: a PyPI project name, an
#: npm package (scoped or bare), or an ``owner/repo`` pair. Bounded and
#: non-blank because an empty identity would make every query vacuous.
ExternalIdentityStr = Annotated[str, Field(min_length=1, max_length=200)]

#: A published artifact filename as the registry advertises it.
ArtifactFilenameStr = Annotated[str, Field(min_length=1, max_length=300)]


class ObservationResult(StrEnum):
    """Coarse verdict of one independent read-back.

    Values:
        MATCH: The registry exposes exactly the frozen artifact set.
        MISSING: The registry does not expose this version at all.
        MISMATCH: The registry exposes something that contradicts the
            frozen manifest -- different digests, a different identity,
            or a prerelease sitting on the stable default channel.
        UNKNOWN: The read-back learned nothing; the registry could not
            be queried or answered unreadably.
    """

    MATCH = "match"
    MISSING = "missing"
    MISMATCH = "mismatch"
    UNKNOWN = "unknown"


class ObservationCode(StrEnum):
    """Stable code naming exactly why a read-back reached its result.

    The code is what an operator, a dashboard and a recovery decision
    all branch on, so it is a closed vocabulary rather than prose. Each
    code maps onto exactly one :class:`ObservationResult` through
    :data:`CODE_RESULTS`.

    Values:
        MATCHED: Every frozen artifact is exposed at its frozen digest.
        VERSION_ABSENT: The registry exposes no such version.
        IDENTITY_MISMATCH: The registry answered about a different
            project, package or repository than the one queried.
        DIGEST_MISMATCH: An exposed artifact carries a digest the
            frozen manifest does not pin.
        ARTIFACT_ABSENT: The version exists but a frozen artifact is
            not exposed under it.
        PRERELEASE_ON_DEFAULT_CHANNEL: A prerelease resolves through the
            stable default channel -- the npm ``latest`` tag pointing at
            it, or a source-host release not flagged prerelease.
        REGISTRY_UNREACHABLE: The registry could not be queried.
        RESPONSE_UNREADABLE: The registry answered in a shape the
            adapter cannot read.
    """

    MATCHED = "matched"
    VERSION_ABSENT = "version_absent"
    IDENTITY_MISMATCH = "identity_mismatch"
    DIGEST_MISMATCH = "digest_mismatch"
    ARTIFACT_ABSENT = "artifact_absent"
    PRERELEASE_ON_DEFAULT_CHANNEL = "prerelease_on_default_channel"
    REGISTRY_UNREACHABLE = "registry_unreachable"
    RESPONSE_UNREADABLE = "response_unreadable"


#: The result each code implies. Total over :class:`ObservationCode`, so
#: an observation's coarse result is derived from its code rather than
#: authored beside it, and the two can never disagree.
CODE_RESULTS: Final[Mapping[ObservationCode, ObservationResult]] = {
    ObservationCode.MATCHED: ObservationResult.MATCH,
    ObservationCode.VERSION_ABSENT: ObservationResult.MISSING,
    ObservationCode.IDENTITY_MISMATCH: ObservationResult.MISMATCH,
    ObservationCode.DIGEST_MISMATCH: ObservationResult.MISMATCH,
    ObservationCode.ARTIFACT_ABSENT: ObservationResult.MISMATCH,
    ObservationCode.PRERELEASE_ON_DEFAULT_CHANNEL: ObservationResult.MISMATCH,
    ObservationCode.REGISTRY_UNREACHABLE: ObservationResult.UNKNOWN,
    ObservationCode.RESPONSE_UNREADABLE: ObservationResult.UNKNOWN,
}


class FrozenArtifact(_StrictModel):
    """One artifact the manifest pins, at the digest it was built with.

    Attributes:
        kind: Artifact kind, matching the target's ``artifact_kinds``.
        filename: Name the registry is expected to advertise it under.
        digest: ``sha256:``-prefixed digest of the built artifact.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ReleaseArtifactKind
    filename: ArtifactFilenameStr
    digest: Sha256DigestStr


class FrozenTarget(_StrictModel):
    """The identity and artifact set one target is observed against.

    Attributes:
        identity: External name the registry is queried by.
        artifacts: Non-empty frozen artifact set for this target.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    identity: ExternalIdentityStr
    artifacts: Annotated[tuple[FrozenArtifact, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _filenames_are_unique(self) -> FrozenTarget:
        """Reject two frozen artifacts sharing a filename.

        Raises:
            ValueError: When a filename repeats. Observed digests are
                keyed by filename, so a repeat would make one of the
                two pins unobservable.
        """
        names = [artifact.filename for artifact in self.artifacts]
        if len(set(names)) != len(names):
            raise ValueError(f"identity {self.identity!r} pins a filename more than once: {names}")
        return self


class FrozenManifest(_StrictModel):
    """The artifact set a release approved, per publication target.

    The release record pins only this manifest's digest; this is the
    artifact behind that pin. :attr:`digest` recomputes from the content
    rather than being stored, so a manifest cannot carry a digest that
    disagrees with what it says.

    Attributes:
        schema_version: Record schema tag.
        release_key: ``REL-<version>`` this manifest was frozen for.
        version: Normalized version the registries are queried for.
        targets: Frozen identity and artifact set per target id.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["frozen-manifest/v1"] = "frozen-manifest/v1"
    release_key: ReleaseKeyStr
    version: NormalizedVersionStr
    targets: Annotated[Mapping[TargetIdStr, FrozenTarget], Field(min_length=1)]

    @model_validator(mode="after")
    def _key_spells_the_version(self) -> FrozenManifest:
        """Reject a key that does not spell the version it freezes.

        Raises:
            ValueError: When ``release_key`` is not ``REL-<version>``.
        """
        if self.release_key != f"REL-{self.version}":
            raise ValueError(
                f"release_key {self.release_key!r} must be 'REL-{self.version}' "
                f"for version {self.version!r}"
            )
        return self

    @property
    def digest(self) -> str:
        """Return the ``sha256:`` digest of this manifest's content."""
        body = json.dumps(
            {
                "release_key": self.release_key,
                "version": self.version,
                "targets": {
                    target_id: {
                        "identity": frozen.identity,
                        "artifacts": sorted(
                            (
                                {
                                    "kind": artifact.kind.value,
                                    "filename": artifact.filename,
                                    "digest": artifact.digest,
                                }
                                for artifact in frozen.artifacts
                            ),
                            key=lambda row: row["filename"],
                        ),
                    }
                    for target_id, frozen in sorted(self.targets.items())
                },
            },
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"

    def target_for(self, target_id: str) -> FrozenTarget:
        """Return the frozen row for *target_id*.

        Args:
            target_id: Publication target to look up.

        Returns:
            The frozen identity and artifact set.

        Raises:
            KeyError: When the manifest freezes no such target.
        """
        try:
            return self.targets[target_id]
        except KeyError as exc:
            raise KeyError(
                f"manifest {self.release_key} freezes no target {target_id!r}; "
                f"frozen: {sorted(self.targets)}"
            ) from exc


def assert_manifest_binds(release: Release, manifest: FrozenManifest) -> None:
    """Raise unless *manifest* is the one *release* approved.

    An observation is only evidence about the approved artifact set, so
    the manifest it compares against has to be the exact one the
    approval bound. Checking it here means a caller cannot hand the
    observer a re-frozen manifest and collect a match.

    Args:
        release: The record being observed.
        manifest: The frozen manifest offered as the comparison basis.

    Returns:
        ``None`` when the manifest binds.

    Raises:
        ValueError: When the keys differ, or the manifest's recomputed
            digest is not the digest the release pinned.
    """
    if manifest.release_key != release.key:
        raise ValueError(
            f"manifest for {manifest.release_key!r} cannot observe release {release.key!r}"
        )
    if release.manifest_digest is None:
        raise ValueError(f"release {release.key!r} pins no manifest_digest to observe against")
    if manifest.digest != release.manifest_digest:
        raise ValueError(
            f"manifest digest {manifest.digest!r} is not the digest release "
            f"{release.key!r} approved ({release.manifest_digest!r})"
        )


class RecordedResponse(_StrictModel):
    """One registry answer, recorded exactly as it was read.

    Adapters are pure functions of this record, which is what lets the
    same fixtures drive the match, missing, mismatch and unknown paths
    of every adapter without a network.

    Attributes:
        status: Transport status of the query. ``200`` carries a
            payload, ``404`` means the registry has no such version, and
            anything else is a read that learned nothing.
        payload: The decoded response body, or ``None`` when the query
            produced no body.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Annotated[int, Field(ge=0, le=599)]
    payload: Mapping[str, Any] | None = None

    @property
    def digest(self) -> str:
        """Return the ``sha256:`` digest pinning this exact response."""
        body = json.dumps(
            {"status": self.status, "payload": self.payload},
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


@dataclass(frozen=True, slots=True)
class ObservationRequest:
    """Everything one adapter needs to judge one target's read-back.

    Attributes:
        target: The target's configuration; supplies the adapter to run
            and the distribution tags the channel check reads.
        version: Normalized version the registry is queried for.
        identity: External name the registry is queried by.
        artifacts: The frozen artifact set the read-back is compared to.
        manifest_digest: Digest of the manifest those artifacts came
            from, carried so the observation names its comparison basis.
    """

    target: ReleaseTargetConfig
    version: str
    identity: str
    artifacts: tuple[FrozenArtifact, ...]
    manifest_digest: str

    @property
    def prerelease(self) -> bool:
        """Return whether the queried version is a dev or rc checkpoint."""
        return is_prerelease(self.version)


def observation_request(
    config: ReleaseConfig,
    manifest: FrozenManifest,
    *,
    target_id: str,
) -> ObservationRequest:
    """Return the read-back request for one configured target.

    Args:
        config: Loaded checkpoint configuration naming every target.
        manifest: The frozen manifest the release approved.
        target_id: The leg to build a request for.

    Returns:
        The request the target's adapter judges.

    Raises:
        KeyError: When the checkpoint configures no such target, or the
            manifest freezes nothing for it.
        ValueError: When the manifest freezes a different version than
            the configuration publishes.
    """
    if manifest.version != config.version:
        raise ValueError(
            f"manifest freezes version {manifest.version!r}, configuration "
            f"publishes {config.version!r}"
        )
    target = configured_target(config, target_id)
    frozen = manifest.target_for(target_id)
    return ObservationRequest(
        target=target,
        version=manifest.version,
        identity=frozen.identity,
        artifacts=frozen.artifacts,
        manifest_digest=manifest.digest,
    )


def configured_target(config: ReleaseConfig, target_id: str) -> ReleaseTargetConfig:
    """Return the configured target named *target_id*.

    Args:
        config: Loaded checkpoint configuration.
        target_id: Target to look up.

    Returns:
        The matching target configuration.

    Raises:
        KeyError: When the checkpoint configures no such target.
    """
    for target in config.targets:
        if target.target_id == target_id:
            return target
    raise KeyError(
        f"checkpoint {config.release_key!r} configures no target {target_id!r}; "
        f"configured: {[t.target_id for t in config.targets]}"
    )


class PublicationObservation(_StrictModel):
    """One independent read-back of one publication target.

    Attributes:
        schema_version: Record schema tag.
        target_id: The leg that was read back.
        adapter: Which declared adapter performed the read-back.
        result: Coarse verdict; derived from :attr:`code`.
        code: Stable code naming exactly why.
        queried_identity: External name the registry was queried by.
        version: Normalized version the registry was queried for.
        observed_digests: Digests the registry advertises, keyed by
            filename. Empty when nothing was exposed to read.
        manifest_digest: Digest of the frozen manifest compared against.
        adapter_digest: Digest pinning the exact registry response the
            verdict was read out of.
        evidence_ref: Reconstructible locator of this read-back; the
            receipt the observed target row carries.
        observed_at: When the read-back was judged.
        detail: Non-blank prose naming what was seen, dense enough that
            an operator can act on it without re-querying.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["publication-observation/v1"] = "publication-observation/v1"
    target_id: TargetIdStr
    adapter: ObservationAdapter
    result: ObservationResult
    code: ObservationCode
    queried_identity: ExternalIdentityStr
    version: NormalizedVersionStr
    observed_digests: Mapping[ArtifactFilenameStr, Sha256DigestStr] = {}
    manifest_digest: Sha256DigestStr
    adapter_digest: Sha256DigestStr
    evidence_ref: ReferenceStr
    observed_at: UtcDatetime
    detail: Annotated[str, Field(min_length=1, max_length=1000)]

    @model_validator(mode="after")
    def _result_follows_the_code(self) -> PublicationObservation:
        """Reject a coarse result that contradicts the specific code.

        Raises:
            ValueError: When :attr:`result` is not the result
                :data:`CODE_RESULTS` declares for :attr:`code`.
        """
        expected = CODE_RESULTS[self.code]
        if self.result is not expected:
            raise ValueError(
                f"result {self.result.value!r} disagrees with code {self.code.value!r} "
                f"(expected {expected.value!r})"
            )
        return self

    @property
    def matched(self) -> bool:
        """Return whether the read-back reproduced the frozen manifest."""
        return self.result is ObservationResult.MATCH

    @property
    def conclusive(self) -> bool:
        """Return whether the read-back settled anything about the leg."""
        return self.result is not ObservationResult.UNKNOWN


def evidence_reference(
    *,
    adapter: ObservationAdapter,
    target_id: str,
    identity: str,
    version: str,
    adapter_digest: str,
) -> str:
    """Return the locator an observation is filed and cited under.

    The reference is derived rather than supplied so it names the four
    facts that make a read-back re-runnable -- which adapter, which
    leg, which external identity at which version -- and pins the exact
    response through the adapter digest. A caller cannot hand in a
    reference that points at a different read-back than the one judged.

    Args:
        adapter: The declared adapter that performed the read-back.
        target_id: The leg that was read back.
        identity: External name the registry was queried by.
        version: Normalized version queried.
        adapter_digest: Digest pinning the response.

    Returns:
        The ``observation://`` locator.
    """
    return f"observation://{adapter.value}/{target_id}/{identity}@{version}#{adapter_digest}"


def build_observation(
    request: ObservationRequest,
    response: RecordedResponse,
    *,
    code: ObservationCode,
    observed_digests: Mapping[str, str],
    detail: str,
    observed_at: datetime,
) -> PublicationObservation:
    """Return the observation record for one adapter's verdict.

    Every adapter funnels through here so the derived fields -- the
    coarse result, the adapter digest and the evidence reference -- are
    computed once rather than three times with three chances to drift.

    Args:
        request: The read-back request that was judged.
        response: The recorded registry answer the verdict came from.
        code: The stable code the adapter reached.
        observed_digests: Digests read out of the response, by filename.
        detail: Operator-facing prose naming what was seen.
        observed_at: Timezone-aware UTC instant of the judgement.

    Returns:
        The observation record.

    Raises:
        ValidationError: When the assembled record violates an
            invariant, e.g. a blank detail.
    """
    adapter_digest = response.digest
    observation = PublicationObservation(
        target_id=request.target.target_id,
        adapter=request.target.observe_adapter,
        result=CODE_RESULTS[code],
        code=code,
        queried_identity=request.identity,
        version=request.version,
        observed_digests=dict(observed_digests),
        manifest_digest=request.manifest_digest,
        adapter_digest=adapter_digest,
        evidence_ref=evidence_reference(
            adapter=request.target.observe_adapter,
            target_id=request.target.target_id,
            identity=request.identity,
            version=request.version,
            adapter_digest=adapter_digest,
        ),
        observed_at=observed_at,
        detail=detail,
    )
    logger.info(
        f"build_observation target={observation.target_id!r} "
        f"adapter={observation.adapter.value!r} code={observation.code.value!r} "
        f"identity={observation.queried_identity!r} version={observation.version!r}"
    )
    return observation


__all__ = [
    "CODE_RESULTS",
    "ArtifactFilenameStr",
    "ExternalIdentityStr",
    "FrozenArtifact",
    "FrozenManifest",
    "FrozenTarget",
    "ObservationCode",
    "ObservationRequest",
    "ObservationResult",
    "PublicationObservation",
    "RecordedResponse",
    "assert_manifest_binds",
    "build_observation",
    "configured_target",
    "evidence_reference",
    "observation_request",
]
