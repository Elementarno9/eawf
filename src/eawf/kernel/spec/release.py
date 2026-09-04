"""Typed release-train records: :class:`Release` and :class:`ReleaseTrain`.

A *release train* is an ordered ladder of checkpoints that walks one
target version from its first development tag to stable. Each rung is a
distinct immutable :class:`Release`; the train orders them and never
mutates one rung into the next, so a published version can never be
retroactively re-pointed at different source.

Three properties carry the weight here and each is enforced at the model
boundary rather than by a downstream caller:

* the record key, the version and the channel agree -- a
  ``REL-0.7.0.dev1`` record cannot claim channel ``stable``, because the
  channel is a pure function of the normalized version
  (:func:`channel_for_version`);
* the checkpoint's authority epoch matches what the train declared for
  that rung (:func:`validate_release_against_train`), so a checkpoint
  cannot quietly promote itself onto a later authority;
* membership cardinality follows the checkpoint, not the author --
  ``dev1`` and ``dev2`` carry no acceptance bundles because epoch-2
  Milestone bundles cannot exist yet, and every later checkpoint
  requires them.

Every record is frozen and ``extra="forbid"``. A Release is a historical
claim about what was built from which source; mutating one after the
fact would rewrite evidence, so transitions return a NEW record with an
incremented :attr:`Release.revision` (see
:mod:`eawf.workflow.release.lifecycle`).
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.state.models import ShaStr
from eawf.kernel.state.types import UtcDatetime

logger = logging.getLogger(__name__)


class ReleaseChannel(StrEnum):
    """Distribution channel a release version maps onto.

    The channel is derived, never authored independently: see
    :func:`channel_for_version`. It exists as a stored field so a
    consumer reading a persisted record does not have to re-parse the
    version string, and as a validated field so the two can never drift.

    Values:
        DEV: A ``X.Y.Z.devN`` development checkpoint.
        RC: A ``X.Y.ZrcN`` release candidate.
        STABLE: A bare ``X.Y.Z`` stable release.
    """

    DEV = "dev"
    RC = "rc"
    STABLE = "stable"


class ReleaseStatus(StrEnum):
    """Lifecycle state of one :class:`Release` record.

    The legal moves between these states live in
    :data:`eawf.workflow.release.lifecycle.RELEASE_TRANSITIONS`; this
    enum is only the vocabulary. Four states are terminal
    (:attr:`CANCELLED`, :attr:`BAKED`, :attr:`RELEASED`,
    :attr:`PARTIALLY_RELEASED`) and none of them returns to
    :attr:`DRAFT` -- a burned version is corrected by a new version
    linked through :attr:`Release.supersedes_release_ref`.

    Values:
        DRAFT: Inputs still mutable; nothing pinned.
        CANDIDATE: Manifest pinned; preflight may run.
        PREFLIGHT_FAILED: A required readiness signal came back non-pass.
        APPROVED: Operator approved against an exact manifest digest.
        CANCELLED: Abandoned before any external effect.
        PUBLISHING: External effect in flight.
        PUBLISH_TIMEOUT: A target's final external state is unknown.
        VERIFYING: Every target call reported success; observation pending.
        RECOVERING: A hard failure landed after a possible side effect.
        BAKED: Prerelease independently observed on every required target.
        RELEASED: Stable independently observed on every required target.
        PARTIALLY_RELEASED: Recovery exhausted; the version is burned.
    """

    DRAFT = "draft"
    CANDIDATE = "candidate"
    PREFLIGHT_FAILED = "preflight_failed"
    APPROVED = "approved"
    CANCELLED = "cancelled"
    PUBLISHING = "publishing"
    PUBLISH_TIMEOUT = "publish_timeout"
    VERIFYING = "verifying"
    RECOVERING = "recovering"
    BAKED = "baked"
    RELEASED = "released"
    PARTIALLY_RELEASED = "partially_released"


class ReleaseTargetStatus(StrEnum):
    """Per-target publication state inside one :class:`Release`.

    The split between a *reported* and an *observed* state is the point
    of the vocabulary: an adapter's own success report is never enough
    to bake a release. Only the observation verb, reading back from the
    target's declared adapter, may write :attr:`OBSERVED_SUCCESS` or
    :attr:`OBSERVED_MISMATCH`.

    Values:
        NOT_STARTED: No publication operation has opened for this target.
        QUEUED: The publication operation opened this leg.
        IN_FLIGHT: The adapter call was dispatched.
        REPORTED_SUCCESS: The adapter claimed success; unverified.
        REPORTED_FAILURE: The adapter claimed failure.
        UNKNOWN: The deadline elapsed with no final result.
        OBSERVED_SUCCESS: Independent read-back matched the frozen digests.
        OBSERVED_MISMATCH: Independent read-back contradicted them.
    """

    NOT_STARTED = "not_started"
    QUEUED = "queued"
    IN_FLIGHT = "in_flight"
    REPORTED_SUCCESS = "reported_success"
    REPORTED_FAILURE = "reported_failure"
    UNKNOWN = "unknown"
    OBSERVED_SUCCESS = "observed_success"
    OBSERVED_MISMATCH = "observed_mismatch"


class ReleaseGateProfile(StrEnum):
    """Named gate profile a checkpoint runs its preflight under.

    Each checkpoint of a train declares exactly one profile; the release
    configuration must name the same one or the load is rejected. The
    profile selects which gate names are legal, not which signals exist.

    Values:
        DEV1: Epoch-1 stabilization floor.
        DEV2: Epoch-2 schema and migration tooling.
        NATIVE_CANARY: Disposable epoch-2 canary repositories.
        PRODUCT_CANARY: Opted-in canary with the full product surface.
        FLAG_DAY: Release candidate that refuses epoch-1 state.
        CROSS_PROVIDER: Provider, platform and recovery evidence.
        STABLE: The full stable gate set.
    """

    DEV1 = "dev1"
    DEV2 = "dev2"
    NATIVE_CANARY = "native_canary"
    PRODUCT_CANARY = "product_canary"
    FLAG_DAY = "flag_day"
    CROSS_PROVIDER = "cross_provider"
    STABLE = "stable"


class ReleaseInvalidationCause(StrEnum):
    """Why a :class:`Release` was returned to :attr:`ReleaseStatus.DRAFT`.

    Recorded on :attr:`Release.last_invalidation` so the return to DRAFT
    is a typed fact rather than an inference from a status history.

    Values:
        INPUTS_REPLACED: Candidate inputs changed before any effect.
        PREFLIGHT_FAILED: A required readiness signal came back non-pass.
        APPROVAL_INVALIDATED: Approved inputs changed before any effect.
    """

    INPUTS_REPLACED = "inputs_replaced"
    PREFLIGHT_FAILED = "preflight_failed"
    APPROVAL_INVALIDATED = "approval_invalidated"


#: PEP 440 grammar this train accepts: ``X.Y.Z``, ``X.Y.ZrcN`` or
#: ``X.Y.Z.devN``. Deliberately narrower than PEP 440 at large -- epoch
#: segments, post-releases and local versions are not part of the train
#: vocabulary, so admitting them would create versions no channel maps.
_VERSION_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<base>\d+\.\d+\.\d+)(?:rc(?P<rc>\d+)|\.dev(?P<dev>\d+))?$"
)

#: A normalized release version string (see :data:`_VERSION_RE`).
NormalizedVersionStr = Annotated[str, Field(pattern=r"^\d+\.\d+\.\d+(?:rc\d+|\.dev\d+)?$")]

#: Release record key: ``REL-`` plus the normalized version.
ReleaseKeyStr = Annotated[str, Field(pattern=r"^REL-\d+\.\d+\.\d+(?:rc\d+|\.dev\d+)?$")]

#: Release-train id: ``TRAIN-`` plus the stable target version.
ReleaseTrainIdStr = Annotated[str, Field(pattern=r"^TRAIN-\d+\.\d+\.\d+$")]

#: A ``sha256:``-prefixed content digest.
Sha256DigestStr = Annotated[str, Field(pattern=r"^sha256:[0-9a-f]{64}$")]

#: A reference string (URN, receipt locator, artifact pointer) carried
#: opaquely at ``dev1``. The typed URN leaf objects land with the
#: epoch-2 domain chassis; until then the field is a non-blank string so
#: a record round-trips without inventing a second URN parser.
ReferenceStr = Annotated[str, Field(min_length=1, max_length=500)]

#: Publication target id, e.g. ``pypi``, ``npm``, ``github``.
TargetIdStr = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


def normalize_version(value: str) -> str:
    """Return *value* unchanged after asserting it is a train version.

    Args:
        value: Candidate version string.

    Returns:
        The same string, once it matches :data:`_VERSION_RE`.

    Raises:
        ValueError: When *value* is not ``X.Y.Z``, ``X.Y.ZrcN`` or
            ``X.Y.Z.devN``.
    """
    if _VERSION_RE.fullmatch(value) is None:
        raise ValueError(
            f"version {value!r} is not a train version (X.Y.Z, X.Y.ZrcN or X.Y.Z.devN)"
        )
    return value


def channel_for_version(version: str) -> ReleaseChannel:
    """Return the channel *version* maps onto.

    The mapping is total over the accepted grammar and has no default
    branch: a ``.devN`` suffix is :attr:`ReleaseChannel.DEV`, an ``rcN``
    suffix is :attr:`ReleaseChannel.RC`, and a bare ``X.Y.Z`` is
    :attr:`ReleaseChannel.STABLE`.

    Args:
        version: Normalized version string.

    Returns:
        The channel that version belongs to.

    Raises:
        ValueError: When *version* is not a train version.
    """
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(
            f"version {version!r} is not a train version (X.Y.Z, X.Y.ZrcN or X.Y.Z.devN)"
        )
    if match.group("dev") is not None:
        return ReleaseChannel.DEV
    if match.group("rc") is not None:
        return ReleaseChannel.RC
    return ReleaseChannel.STABLE


def is_prerelease(version: str) -> bool:
    """Return whether *version* is a development or release-candidate tag.

    Args:
        version: Normalized version string.

    Returns:
        ``True`` for ``devN`` and ``rcN`` versions, ``False`` for stable.

    Raises:
        ValueError: When *version* is not a train version.
    """
    return channel_for_version(version) is not ReleaseChannel.STABLE


def semver_equivalent(version: str) -> str:
    """Return the SemVer spelling of a PEP 440 train *version*.

    PyPI carries ``0.7.0.dev1``; the npm artifact for the same
    checkpoint is ``0.7.0-dev.1``. Publication adapters need both
    spellings of one checkpoint, so the mapping lives here rather than
    being re-derived per adapter.

    Args:
        version: Normalized PEP 440 version string.

    Returns:
        The SemVer-equivalent string.

    Raises:
        ValueError: When *version* is not a train version.
    """
    match = _VERSION_RE.fullmatch(version)
    if match is None:
        raise ValueError(
            f"version {version!r} is not a train version (X.Y.Z, X.Y.ZrcN or X.Y.Z.devN)"
        )
    base = match.group("base")
    if match.group("dev") is not None:
        return f"{base}-dev.{int(match.group('dev'))}"
    if match.group("rc") is not None:
        return f"{base}-rc.{int(match.group('rc'))}"
    return base


def release_key(version: str) -> str:
    """Return the ``REL-<version>`` key for *version*.

    Args:
        version: Normalized version string.

    Returns:
        The record key.

    Raises:
        ValueError: When *version* is not a train version.
    """
    return f"REL-{normalize_version(version)}"


class ReleaseInvalidation(_StrictModel):
    """The typed record of one return to :attr:`ReleaseStatus.DRAFT`.

    Attributes:
        cause: Which of the three invalidation edges fired.
        invalidated_at: When the edge fired (timezone-aware UTC).
        detail: Non-blank prose naming what changed, dense enough that a
            reader can tell which input moved.
        prior_status: The status the record left.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    cause: ReleaseInvalidationCause
    invalidated_at: UtcDatetime
    detail: Annotated[str, Field(min_length=1, max_length=1000)]
    prior_status: ReleaseStatus


class ReleaseCheckpoint(_StrictModel):
    """One declared rung of a :class:`ReleaseTrain`.

    The train's ladder is data, not schema: a train declares any number
    of development and release-candidate rungs and exactly one stable
    rung at the end. Each rung fixes the authority epoch and the gate
    profile that its :class:`Release` and its release configuration must
    both agree with.

    Attributes:
        release_key: ``REL-<version>`` key of the Release at this rung.
        authority_epoch: Authority epoch the rung runs under; at least 1.
        gate_profile: Gate profile the rung's preflight runs.
        requires_membership: Whether the rung requires a non-empty
            :attr:`Release.membership_refs`. ``False`` for the epoch-1
            rungs, whose epoch-2 acceptance bundles cannot exist yet.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    release_key: ReleaseKeyStr
    authority_epoch: Annotated[int, Field(ge=1)]
    gate_profile: ReleaseGateProfile
    requires_membership: bool = False

    @property
    def version(self) -> str:
        """Return the normalized version this rung tags."""
        return self.release_key.removeprefix("REL-")

    @property
    def channel(self) -> ReleaseChannel:
        """Return the channel this rung publishes on."""
        return channel_for_version(self.version)


class ReleaseTrain(_StrictModel):
    """The ordered checkpoint ladder that walks one version to stable.

    Attributes:
        schema_version: Record schema tag.
        train_id: ``TRAIN-<target_version>``; the immutable target line.
        target_version: The stable version this train ends at.
        checkpoints: Ordered non-empty ladder. Keys are unique and the
            last rung is the stable release of :attr:`target_version`.
        current_checkpoint_index: Index of the rung currently open. It
            advances only when the Release at the current index is
            terminal, which the train-advance guard enforces; the model
            only bounds it to the ladder.
        gate_receipt_refs: Per-checkpoint gate receipt references,
            keyed by release key. Receipts stay bound to exact source.
        revision: Compare-and-swap revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-train/v1"] = "release-train/v1"
    train_id: ReleaseTrainIdStr
    target_version: NormalizedVersionStr
    checkpoints: Annotated[tuple[ReleaseCheckpoint, ...], Field(min_length=1)]
    current_checkpoint_index: Annotated[int, Field(ge=0)] = 0
    gate_receipt_refs: Mapping[ReleaseKeyStr, tuple[ReferenceStr, ...]] = {}
    revision: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _ladder_is_coherent(self) -> ReleaseTrain:
        """Reject an id, ladder or index that contradicts the target.

        Raises:
            ValueError: When the id does not spell the target version,
                the target version is not stable, a checkpoint key
                repeats, the ladder does not end at the stable target,
                the index is off the end of the ladder, or a receipt key
                names a rung the ladder does not declare.
        """
        if channel_for_version(self.target_version) is not ReleaseChannel.STABLE:
            raise ValueError(f"target_version {self.target_version!r} must be a stable version")
        if self.train_id != f"TRAIN-{self.target_version}":
            raise ValueError(f"train_id {self.train_id!r} must be TRAIN-{self.target_version}")
        keys = [rung.release_key for rung in self.checkpoints]
        if len(set(keys)) != len(keys):
            raise ValueError("checkpoint release keys must be unique")
        terminal = self.checkpoints[-1]
        if terminal.release_key != release_key(self.target_version):
            raise ValueError(
                f"ladder must end at {release_key(self.target_version)!r}, "
                f"not {terminal.release_key!r}"
            )
        if self.current_checkpoint_index >= len(self.checkpoints):
            raise ValueError(
                f"current_checkpoint_index {self.current_checkpoint_index} is off a "
                f"ladder of {len(self.checkpoints)} checkpoints"
            )
        unknown = sorted(set(self.gate_receipt_refs) - set(keys))
        if unknown:
            raise ValueError(f"gate receipts name undeclared checkpoints: {unknown}")
        return self

    @property
    def current_checkpoint(self) -> ReleaseCheckpoint:
        """Return the rung at :attr:`current_checkpoint_index`."""
        return self.checkpoints[self.current_checkpoint_index]

    def checkpoint_for(self, key: str) -> ReleaseCheckpoint:
        """Return the rung whose :attr:`ReleaseCheckpoint.release_key` is *key*.

        Args:
            key: ``REL-<version>`` key to look up.

        Returns:
            The matching checkpoint declaration.

        Raises:
            KeyError: When this train declares no such rung.
        """
        for rung in self.checkpoints:
            if rung.release_key == key:
                return rung
        raise KeyError(f"train {self.train_id} declares no checkpoint {key!r}")

    def checkpoint_for_version(self, version: str) -> ReleaseCheckpoint:
        """Return the rung tagging *version*.

        Args:
            version: Normalized version string.

        Returns:
            The matching checkpoint declaration.

        Raises:
            ValueError: When *version* is not a train version.
            KeyError: When this train declares no rung for it.
        """
        return self.checkpoint_for(release_key(version))


class Release(_StrictModel):
    """One immutable checkpoint record of a :class:`ReleaseTrain`.

    Attributes:
        schema_version: Record schema tag.
        uid: Stable identity across revisions of the same checkpoint.
        key: ``REL-<version>``; agrees with :attr:`version`.
        version: Normalized version; immutable for the record's life.
        channel: Derived from :attr:`version`; stored so a reader need
            not re-parse, validated so the two cannot drift.
        authority_epoch: Epoch this checkpoint runs under; must equal
            the train's declaration (:func:`validate_release_against_train`).
        membership_refs: Milestone acceptance bundle references. Empty
            at ``dev1``/``dev2``; required from ``dev3`` onward.
        source_sha: Commit the artifacts were built from. Required from
            :attr:`ReleaseStatus.CANDIDATE` onward.
        source_tree_sha: Tree of that commit; required alongside it.
        manifest_ref: Pointer to the pinned ReleaseManifest artifact.
        manifest_digest: Digest of that manifest; the approval binds it.
        target_statuses: Per-target publication state. The target set is
            immutable after :attr:`ReleaseStatus.APPROVED`.
        policy_revision: Policy generation the approval binds.
        status: Lifecycle state (see :class:`ReleaseStatus`).
        approval_ref: The single approval receipt; required from
            :attr:`ReleaseStatus.APPROVED` onward.
        publication_operation_ref: Set once external effect starts.
        supersedes_release_ref: Correction lineage for a burned version.
        last_invalidation: Typed record of the last return to DRAFT.
        revision: Compare-and-swap transition revision.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release/v1"] = "release/v1"
    uid: UUID
    key: ReleaseKeyStr
    version: NormalizedVersionStr
    channel: ReleaseChannel
    authority_epoch: Annotated[int, Field(ge=1)]
    membership_refs: tuple[ReferenceStr, ...] = ()
    source_sha: ShaStr | None = None
    source_tree_sha: ShaStr | None = None
    manifest_ref: ReferenceStr | None = None
    manifest_digest: Sha256DigestStr | None = None
    target_statuses: Mapping[TargetIdStr, ReleaseTargetStatus] = {}
    policy_revision: Annotated[int, Field(ge=1)] = 1
    status: ReleaseStatus = ReleaseStatus.DRAFT
    approval_ref: ReferenceStr | None = None
    publication_operation_ref: ReferenceStr | None = None
    supersedes_release_ref: ReleaseKeyStr | None = None
    last_invalidation: ReleaseInvalidation | None = None
    revision: Annotated[int, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _identity_is_coherent(self) -> Release:
        """Reject a key, channel or membership that contradicts the version.

        Raises:
            ValueError: When the key does not spell the version, the
                channel disagrees with the version, a ``dev1``/``dev2``
                checkpoint carries membership bundles, or the record
                supersedes itself.
        """
        if self.key != release_key(self.version):
            raise ValueError(
                f"key {self.key!r} must be {release_key(self.version)!r} for "
                f"version {self.version!r}"
            )
        expected = channel_for_version(self.version)
        if self.channel is not expected:
            raise ValueError(
                f"channel {self.channel.value!r} disagrees with version "
                f"{self.version!r} (expected {expected.value!r})"
            )
        if self.membership_refs and _is_epoch1_dev_checkpoint(self.version):
            raise ValueError(
                f"membership_refs must be empty at {self.version!r}; epoch-2 "
                f"milestone acceptance bundles cannot exist yet"
            )
        if self.supersedes_release_ref == self.key:
            raise ValueError(f"release {self.key!r} cannot supersede itself")
        return self

    @model_validator(mode="after")
    def _pin_is_complete(self) -> Release:
        """Require the pinned facts every post-DRAFT state depends on.

        Raises:
            ValueError: When a record at :attr:`ReleaseStatus.CANDIDATE`
                or later is missing its source binding or its manifest
                digest, or when a record at
                :attr:`ReleaseStatus.APPROVED` or later carries no
                approval reference.
        """
        if self.status in _PINNED_STATUSES:
            missing = [
                name
                for name, value in (
                    ("source_sha", self.source_sha),
                    ("source_tree_sha", self.source_tree_sha),
                    ("manifest_ref", self.manifest_ref),
                    ("manifest_digest", self.manifest_digest),
                )
                if value is None
            ]
            if missing:
                raise ValueError(f"status {self.status.value!r} requires pinned fields: {missing}")
        if self.status in _APPROVED_STATUSES and self.approval_ref is None:
            raise ValueError(f"status {self.status.value!r} requires approval_ref")
        return self


def _is_epoch1_dev_checkpoint(version: str) -> bool:
    """Return whether *version* is ``dev1`` or ``dev2`` of its base line.

    Those two checkpoints run on epoch-1 authority, where epoch-2
    Milestone acceptance bundles do not exist, so a membership reference
    on one of them is premature rather than merely unusual.

    Args:
        version: Normalized version string.

    Returns:
        ``True`` for ``X.Y.Z.dev1`` and ``X.Y.Z.dev2``.
    """
    match = _VERSION_RE.fullmatch(version)
    if match is None or match.group("dev") is None:
        return False
    return int(match.group("dev")) in (1, 2)


#: Statuses whose invariants require a pinned source + manifest. Every
#: state from CANDIDATE onward except CANCELLED, which is reachable from
#: DRAFT-adjacent states that never pinned.
_PINNED_STATUSES: Final[frozenset[ReleaseStatus]] = frozenset(
    {
        ReleaseStatus.CANDIDATE,
        ReleaseStatus.PREFLIGHT_FAILED,
        ReleaseStatus.APPROVED,
        ReleaseStatus.PUBLISHING,
        ReleaseStatus.PUBLISH_TIMEOUT,
        ReleaseStatus.VERIFYING,
        ReleaseStatus.RECOVERING,
        ReleaseStatus.BAKED,
        ReleaseStatus.RELEASED,
        ReleaseStatus.PARTIALLY_RELEASED,
    }
)

#: Statuses that require a bound approval receipt.
_APPROVED_STATUSES: Final[frozenset[ReleaseStatus]] = frozenset(
    {
        ReleaseStatus.APPROVED,
        ReleaseStatus.PUBLISHING,
        ReleaseStatus.PUBLISH_TIMEOUT,
        ReleaseStatus.VERIFYING,
        ReleaseStatus.RECOVERING,
        ReleaseStatus.BAKED,
        ReleaseStatus.RELEASED,
        ReleaseStatus.PARTIALLY_RELEASED,
    }
)


def validate_release_against_train(release: Release, train: ReleaseTrain) -> ReleaseCheckpoint:
    """Return the train rung *release* occupies, asserting they agree.

    This is the cross-object half of the record contract: the Release
    model alone cannot know which epoch its checkpoint was declared at,
    so the epoch equality and the membership cardinality of the later
    checkpoints are checked here, against the train that declares them.

    Args:
        release: Record to place on the ladder.
        train: Train that declares the ladder.

    Returns:
        The checkpoint declaration *release* occupies.

    Raises:
        KeyError: When *train* declares no rung for the release key.
        ValueError: When the authority epoch disagrees with the rung, or
            the rung requires membership bundles the release lacks.
    """
    rung = train.checkpoint_for(release.key)
    if release.authority_epoch != rung.authority_epoch:
        raise ValueError(
            f"release {release.key!r} declares authority_epoch "
            f"{release.authority_epoch} but train {train.train_id} declares "
            f"{rung.authority_epoch}"
        )
    if rung.requires_membership and not release.membership_refs:
        raise ValueError(f"checkpoint {rung.release_key!r} requires non-empty membership_refs")
    logger.debug(
        f"validate_release_against_train key={release.key!r} "
        f"train_id={train.train_id!r} epoch={release.authority_epoch}"
    )
    return rung


__all__ = [
    "NormalizedVersionStr",
    "ReferenceStr",
    "Release",
    "ReleaseChannel",
    "ReleaseCheckpoint",
    "ReleaseGateProfile",
    "ReleaseInvalidation",
    "ReleaseInvalidationCause",
    "ReleaseKeyStr",
    "ReleaseStatus",
    "ReleaseTargetStatus",
    "ReleaseTrain",
    "ReleaseTrainIdStr",
    "Sha256DigestStr",
    "TargetIdStr",
    "channel_for_version",
    "is_prerelease",
    "normalize_version",
    "release_key",
    "semver_equivalent",
    "validate_release_against_train",
]
