"""Strict loader for one checkpoint's release configuration.

A release configuration is the single authored surface of a checkpoint:
which version and channel it tags, which authority epoch it runs under,
which targets it publishes to, and which gates its preflight must pass.
Everything else about the checkpoint -- the required signal set, the
gate-to-evidence binding -- is *derived* from this file plus the train
declaration, never authored a second time, so no two lists can drift.

The loader is the fail-fast boundary: :func:`load_release_config`
returns an already-validated :class:`ReleaseConfig` and every downstream
consumer accepts that typed object rather than re-checking a raw dict.
Each rejection carries a typed :class:`ReleaseConfigRejection` code, so
a caller can branch on the cause without matching prose.

Two classes of check live here for different reasons. Per-field shape
(enums, ranges, patterns) is Pydantic's, expressed on the models below.
Cross-field and cross-object agreement -- a channel that contradicts the
version, an epoch the train never declared for this rung, a membership
cardinality the checkpoint forbids -- is the loader's, because it needs
either two fields at once or the train the configuration belongs to.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Annotated, Any, Final

import yaml
from pydantic import ConfigDict, Field, ValidationError

from eawf.kernel.spec.common import _StrictModel
from eawf.kernel.spec.release import (
    NormalizedVersionStr,
    ReferenceStr,
    ReleaseChannel,
    ReleaseGateProfile,
    ReleaseTrain,
    TargetIdStr,
    channel_for_version,
    is_prerelease,
    release_key,
)

logger = logging.getLogger(__name__)

#: The npm distribution tag that resolves a bare ``install <pkg>``. A
#: prerelease that carries it becomes the default install, which is the
#: single most damaging misconfiguration this loader can catch.
DEFAULT_DIST_TAG: Final[str] = "latest"

#: Advertised platform id, e.g. ``linux-x86_64``, ``darwin-arm64``.
PlatformIdStr = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_-]{0,31}$")]


class ReleaseConfigRejection(StrEnum):
    """Closed vocabulary for why a release configuration was rejected.

    Values:
        SCHEMA_INVALID: The payload does not match the model shape.
        DUPLICATE_TARGET: Two target rows share a ``target_id``.
        CHANNEL_VERSION_DISAGREEMENT: ``channel`` contradicts ``version``.
        UNDECLARED_CHECKPOINT: The train declares no rung for this version.
        UNDECLARED_EPOCH: ``authority_epoch`` is not the rung's epoch.
        UNDECLARED_GATE_PROFILE: ``gates.profile`` is not the rung's profile.
        PRERELEASE_ON_DEFAULT_TAG: A prerelease resolves through the
            default distribution tag.
        INVALID_MEMBERSHIP_CARDINALITY: Membership bundles are present
            where the checkpoint forbids them, or absent where it
            requires them.
        MISSING_OBSERVATION_ADAPTER: A target declares no observation
            adapter, or names one the loader does not recognise.
        NONPOSITIVE_TIMEOUT: A target declares a timeout at or below zero.
        DUPLICATE_PLATFORM_CLAIM: Two platform claims share a
            ``platform_id``.
    """

    SCHEMA_INVALID = "schema_invalid"
    DUPLICATE_TARGET = "duplicate_target"
    CHANNEL_VERSION_DISAGREEMENT = "channel_version_disagreement"
    UNDECLARED_CHECKPOINT = "undeclared_checkpoint"
    UNDECLARED_EPOCH = "undeclared_epoch"
    UNDECLARED_GATE_PROFILE = "undeclared_gate_profile"
    PRERELEASE_ON_DEFAULT_TAG = "prerelease_on_default_tag"
    INVALID_MEMBERSHIP_CARDINALITY = "invalid_membership_cardinality"
    MISSING_OBSERVATION_ADAPTER = "missing_observation_adapter"
    NONPOSITIVE_TIMEOUT = "nonpositive_timeout"
    DUPLICATE_PLATFORM_CLAIM = "duplicate_platform_claim"


class ReleaseConfigError(ValueError):
    """A release configuration was rejected at load.

    Subclasses :class:`ValueError` so existing boundary handlers keep
    working; :attr:`code` is what a caller branches on.

    Attributes:
        code: Which rejection fired.
    """

    def __init__(self, code: ReleaseConfigRejection, message: str) -> None:
        """Store the typed *code* alongside the operator-facing *message*."""
        super().__init__(message)
        self.code = code


class ObservationAdapter(StrEnum):
    """Declared read-back adapter a publication target is observed through.

    The set is closed because an unrecognised adapter name would leave a
    target with no way to reach an observed state, which would silently
    make the release unbakeable rather than loudly unloadable.

    Values:
        PACKAGE_INDEX: A Python package index (PyPI).
        NPM_REGISTRY: The npm registry.
        SOURCE_HOST_RELEASE: The source host's release object.
    """

    PACKAGE_INDEX = "package_index"
    NPM_REGISTRY = "npm_registry"
    SOURCE_HOST_RELEASE = "source_host_release"


class ReleaseArtifactKind(StrEnum):
    """Artifact kind a publication target carries.

    Values:
        WHEEL: A Python wheel.
        SDIST: A Python source distribution.
        CODEX_PLUGIN: The runtime plugin package.
        PLUGIN_BUNDLE: The archived plugin bundle.
        RELEASE_NOTES: The rendered release notes.
        CHECKSUMS: The artifact checksum manifest.
    """

    WHEEL = "wheel"
    SDIST = "sdist"
    CODEX_PLUGIN = "codex_plugin"
    PLUGIN_BUNDLE = "plugin_bundle"
    RELEASE_NOTES = "release_notes"
    CHECKSUMS = "checksums"


class ReleaseGateName(StrEnum):
    """Gate names a checkpoint's ``gates.required`` list may draw from.

    Every name here reads exactly one piece of evidence -- a readiness
    row, a named component of a row, or a proof command run at the
    pinned source revision. The binding table itself is derived
    downstream; this enum only closes the vocabulary so a typo in the
    authored list fails at load rather than passing vacuously.

    Values:
        VERSION_CONSISTENCY: Version agreement across every manifest.
        CHANGELOG_ENTRY: A non-empty changelog section for the version.
        DEPENDENCY_INVENTORY: The lock-derived dependency inventory.
        ARTIFACT_REPRODUCIBILITY: Digest equality across clean builds.
        SECURITY_REVIEW: The vulnerability report and its blocking flag.
        EPOCH1_STABILIZATION: The epoch-1 stabilization proof command.
        TELEMETRY_PRODUCER: The telemetry-producer proof command.
        FRONT_DOOR_JOURNEY: The install-smoke journey proof command.
    """

    VERSION_CONSISTENCY = "version_consistency"
    CHANGELOG_ENTRY = "changelog_entry"
    DEPENDENCY_INVENTORY = "dependency_inventory"
    ARTIFACT_REPRODUCIBILITY = "artifact_reproducibility"
    SECURITY_REVIEW = "security_review"
    EPOCH1_STABILIZATION = "epoch1_stabilization"
    TELEMETRY_PRODUCER = "telemetry_producer"
    FRONT_DOOR_JOURNEY = "front_door_journey"


class ReleaseTargetConfig(_StrictModel):
    """One publication target of a checkpoint.

    Attributes:
        target_id: Target identity, e.g. ``pypi``.
        required: Whether the checkpoint cannot bake without this target.
        artifact_kinds: Non-empty set of artifact kinds published here.
        observe_adapter: Adapter the target is independently read back
            through. Mandatory: a target with no observer can report
            success but can never be observed, so the release could
            never legitimately bake.
        timeout_seconds: Wall budget for one publication attempt.
        retry_limit: Bounded retries of an unfinished leg.
        prerelease_dist_tag: Distribution tag a prerelease resolves
            through, where the target has the concept.
        stable_dist_tag: Distribution tag a stable release resolves
            through.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    target_id: TargetIdStr
    required: bool = True
    artifact_kinds: Annotated[tuple[ReleaseArtifactKind, ...], Field(min_length=1)]
    observe_adapter: ObservationAdapter
    timeout_seconds: Annotated[int, Field(gt=0)]
    retry_limit: Annotated[int, Field(ge=0)]
    prerelease_dist_tag: Annotated[str, Field(min_length=1)] | None = None
    stable_dist_tag: Annotated[str, Field(min_length=1)] | None = None


class ReleasePlatformClaim(_StrictModel):
    """One platform a checkpoint advertises, and the receipt proving it.

    The ``real_host`` flag is the whole point of the row. A journey that
    ran against an argv-shape stub or a masked container proves the code
    path was *entered*, not that the platform works, and that gap is how
    a backend stays dead on a platform while its checks stay green. A
    claim whose receipt did not come from a real host is unproven.

    Attributes:
        platform_id: Advertised platform, e.g. ``linux-x86_64``.
        receipt_ref: Reference to the CI receipt backing the claim.
        real_host: Whether the receipt came from a real host of that
            platform rather than a stub or an emulation shim.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    platform_id: PlatformIdStr
    receipt_ref: ReferenceStr
    real_host: bool = True


class ReleaseGatesConfig(_StrictModel):
    """The gate profile and required gate names of a checkpoint.

    Attributes:
        profile: Gate profile the checkpoint runs; must equal the one
            the train declared for this rung.
        required: Non-empty ordered list of gate names that must be
            green before the checkpoint may be approved.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    profile: ReleaseGateProfile
    required: Annotated[tuple[ReleaseGateName, ...], Field(min_length=1)]


class ReleaseConfig(_StrictModel):
    """One checkpoint's authored release configuration.

    Attributes:
        version: Normalized version this checkpoint tags.
        channel: Channel it publishes on; must agree with the version.
        authority_epoch: Authority epoch it runs under.
        source_branch: Branch the source commit must be reachable from.
        require_signed_tag: Whether the tag must carry a signature.
        require_clean_tree: Whether a dirty worktree blocks the release.
            When true, the tree-cleanliness signal joins the derived
            required set.
        require_ancestor_of_remote: Whether the source must be reachable
            from the remote branch. When true, the ancestry signal joins
            the derived required set.
        targets: Non-empty publication target set.
        gates: Gate profile and required gate names.
        membership_refs: Milestone acceptance bundle references; empty
            at the epoch-1 checkpoints.
        platform_claims: Platforms the checkpoint advertises, each with
            the receipt proving its journey. Empty means the checkpoint
            advertises no platform, which makes the ``platform``
            readiness row ``unavailable`` rather than passing vacuously.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: NormalizedVersionStr
    channel: ReleaseChannel
    authority_epoch: Annotated[int, Field(ge=1)]
    source_branch: Annotated[str, Field(min_length=1)]
    require_signed_tag: bool = True
    require_clean_tree: bool = True
    require_ancestor_of_remote: bool = True
    targets: Annotated[tuple[ReleaseTargetConfig, ...], Field(min_length=1)]
    gates: ReleaseGatesConfig
    membership_refs: tuple[ReferenceStr, ...] = ()
    platform_claims: tuple[ReleasePlatformClaim, ...] = ()

    @property
    def release_key(self) -> str:
        """Return the ``REL-<version>`` key this configuration targets."""
        return release_key(self.version)

    @property
    def required_target_ids(self) -> tuple[str, ...]:
        """Return the ids of the targets the checkpoint cannot bake without."""
        return tuple(target.target_id for target in self.targets if target.required)


#: Model-field names whose Pydantic failure maps onto a specific
#: rejection code. Anything else classifies as ``SCHEMA_INVALID``.
_FIELD_REJECTIONS: Final[Mapping[str, ReleaseConfigRejection]] = {
    "timeout_seconds": ReleaseConfigRejection.NONPOSITIVE_TIMEOUT,
    "observe_adapter": ReleaseConfigRejection.MISSING_OBSERVATION_ADAPTER,
}


def _classify(exc: ValidationError) -> ReleaseConfigError:
    """Map a Pydantic *exc* onto a typed :class:`ReleaseConfigError`.

    Args:
        exc: The validation error raised by :class:`ReleaseConfig`.

    Returns:
        A typed error whose code names the first field-specific cause,
        falling back to :attr:`ReleaseConfigRejection.SCHEMA_INVALID`.
    """
    for error in exc.errors():
        for part in error["loc"]:
            rejection = _FIELD_REJECTIONS.get(str(part))
            if rejection is not None:
                return ReleaseConfigError(
                    rejection,
                    f"release configuration rejected: {'.'.join(str(p) for p in error['loc'])}"
                    f" -- {error['msg']}",
                )
    return ReleaseConfigError(
        ReleaseConfigRejection.SCHEMA_INVALID,
        f"release configuration rejected: {exc.error_count()} schema error(s); "
        f"first: {exc.errors()[0]['msg']}",
    )


def _reject_duplicate_targets(config: ReleaseConfig) -> None:
    """Raise when two target rows share a ``target_id``.

    Args:
        config: Shape-validated configuration.

    Raises:
        ReleaseConfigError: With
            :attr:`ReleaseConfigRejection.DUPLICATE_TARGET`.
    """
    seen: set[str] = set()
    for target in config.targets:
        if target.target_id in seen:
            raise ReleaseConfigError(
                ReleaseConfigRejection.DUPLICATE_TARGET,
                f"target {target.target_id!r} is declared more than once",
            )
        seen.add(target.target_id)


def _reject_duplicate_platform_claims(config: ReleaseConfig) -> None:
    """Raise when two platform claims share a ``platform_id``.

    Args:
        config: Shape-validated configuration.

    Raises:
        ReleaseConfigError: With
            :attr:`ReleaseConfigRejection.DUPLICATE_PLATFORM_CLAIM`.
    """
    seen: set[str] = set()
    for claim in config.platform_claims:
        if claim.platform_id in seen:
            raise ReleaseConfigError(
                ReleaseConfigRejection.DUPLICATE_PLATFORM_CLAIM,
                f"platform {claim.platform_id!r} is claimed more than once",
            )
        seen.add(claim.platform_id)


def _reject_channel_disagreement(config: ReleaseConfig) -> None:
    """Raise when ``channel`` contradicts the normalized ``version``.

    Args:
        config: Shape-validated configuration.

    Raises:
        ReleaseConfigError: With
            :attr:`ReleaseConfigRejection.CHANNEL_VERSION_DISAGREEMENT`.
    """
    expected = channel_for_version(config.version)
    if config.channel is not expected:
        raise ReleaseConfigError(
            ReleaseConfigRejection.CHANNEL_VERSION_DISAGREEMENT,
            f"channel {config.channel.value!r} disagrees with version "
            f"{config.version!r} (expected {expected.value!r})",
        )


def _reject_prerelease_on_default_tag(config: ReleaseConfig) -> None:
    """Raise when a prerelease would resolve through the default tag.

    A prerelease reaches installers through its
    ``prerelease_dist_tag``; carrying the default tag there would make
    the next bare install pick up an unfinished checkpoint.

    Args:
        config: Shape-validated configuration.

    Raises:
        ReleaseConfigError: With
            :attr:`ReleaseConfigRejection.PRERELEASE_ON_DEFAULT_TAG`.
    """
    if not is_prerelease(config.version):
        return
    for target in config.targets:
        if target.prerelease_dist_tag == DEFAULT_DIST_TAG:
            raise ReleaseConfigError(
                ReleaseConfigRejection.PRERELEASE_ON_DEFAULT_TAG,
                f"target {target.target_id!r} routes prerelease {config.version!r} "
                f"through the default {DEFAULT_DIST_TAG!r} tag",
            )


def _reject_train_disagreement(config: ReleaseConfig, train: ReleaseTrain) -> None:
    """Raise when the configuration contradicts the train's rung declaration.

    Args:
        config: Shape-validated configuration.
        train: Train that declares the checkpoint ladder.

    Raises:
        ReleaseConfigError: With
            :attr:`ReleaseConfigRejection.UNDECLARED_CHECKPOINT`,
            :attr:`ReleaseConfigRejection.UNDECLARED_EPOCH`,
            :attr:`ReleaseConfigRejection.UNDECLARED_GATE_PROFILE`, or
            :attr:`ReleaseConfigRejection.INVALID_MEMBERSHIP_CARDINALITY`.
    """
    try:
        rung = train.checkpoint_for(config.release_key)
    except KeyError as exc:
        raise ReleaseConfigError(
            ReleaseConfigRejection.UNDECLARED_CHECKPOINT,
            f"train {train.train_id} declares no checkpoint {config.release_key!r}",
        ) from exc
    if config.authority_epoch != rung.authority_epoch:
        raise ReleaseConfigError(
            ReleaseConfigRejection.UNDECLARED_EPOCH,
            f"checkpoint {config.release_key!r} declares authority_epoch "
            f"{config.authority_epoch}; train {train.train_id} declares "
            f"{rung.authority_epoch}",
        )
    if config.gates.profile is not rung.gate_profile:
        raise ReleaseConfigError(
            ReleaseConfigRejection.UNDECLARED_GATE_PROFILE,
            f"checkpoint {config.release_key!r} declares gate profile "
            f"{config.gates.profile.value!r}; train {train.train_id} declares "
            f"{rung.gate_profile.value!r}",
        )
    if rung.requires_membership and not config.membership_refs:
        raise ReleaseConfigError(
            ReleaseConfigRejection.INVALID_MEMBERSHIP_CARDINALITY,
            f"checkpoint {config.release_key!r} requires non-empty membership_refs",
        )
    if not rung.requires_membership and config.membership_refs:
        raise ReleaseConfigError(
            ReleaseConfigRejection.INVALID_MEMBERSHIP_CARDINALITY,
            f"checkpoint {config.release_key!r} forbids membership_refs; "
            f"{len(config.membership_refs)} declared",
        )


def parse_release_config(source: str | Mapping[str, Any]) -> ReleaseConfig:
    """Parse *source* into a shape-validated :class:`ReleaseConfig`.

    Parsing is deliberately separate from the train-dependent
    validation: a caller that only needs the authored shape (a renderer,
    a diff) stops here, while :func:`load_release_config` continues into
    the cross-object checks.

    Args:
        source: YAML text or an already-decoded mapping. Either form may
            be wrapped in a top-level ``release:`` key, which is how the
            checkpoint file is written.

    Returns:
        The validated configuration.

    Raises:
        ReleaseConfigError: When the payload is not a mapping, is not
            valid YAML, or fails the model shape.
    """
    if isinstance(source, str):
        try:
            decoded = yaml.safe_load(source)
        except yaml.YAMLError as exc:
            raise ReleaseConfigError(
                ReleaseConfigRejection.SCHEMA_INVALID,
                f"release configuration is not valid YAML: {exc}",
            ) from exc
    else:
        decoded = source
    if not isinstance(decoded, Mapping):
        raise ReleaseConfigError(
            ReleaseConfigRejection.SCHEMA_INVALID,
            f"release configuration must be a mapping, got {type(decoded).__name__}",
        )
    body = decoded.get("release", decoded)
    if not isinstance(body, Mapping):
        raise ReleaseConfigError(
            ReleaseConfigRejection.SCHEMA_INVALID,
            f"release block must be a mapping, got {type(body).__name__}",
        )
    try:
        return ReleaseConfig.model_validate(dict(body))
    except ValidationError as exc:
        raise _classify(exc) from exc


def load_release_config(source: str | Mapping[str, Any], *, train: ReleaseTrain) -> ReleaseConfig:
    """Return the checkpoint configuration in *source*, fully validated.

    Runs the shape validation of :func:`parse_release_config` and then
    every cross-field and cross-object check in order: duplicate
    targets, channel/version agreement, the default-tag guard, and
    finally agreement with the rung *train* declares for this version.

    Args:
        source: YAML text or an already-decoded mapping.
        train: Train that declares the checkpoint ladder this
            configuration belongs to.

    Returns:
        The validated configuration.

    Raises:
        ReleaseConfigError: On any rejection; the typed
            :attr:`ReleaseConfigError.code` names which.
    """
    config = parse_release_config(source)
    _reject_duplicate_targets(config)
    _reject_duplicate_platform_claims(config)
    _reject_channel_disagreement(config)
    _reject_prerelease_on_default_tag(config)
    _reject_train_disagreement(config, train)
    logger.info(
        f"load_release_config version={config.version!r} "
        f"channel={config.channel.value!r} profile={config.gates.profile.value!r} "
        f"targets={len(config.targets)}"
    )
    return config


def gate_names(required: Sequence[ReleaseGateName]) -> tuple[str, ...]:
    """Return the string spellings of *required*, in order.

    Args:
        required: Gate names from a loaded configuration.

    Returns:
        The gate name values as plain strings, for rendering.
    """
    return tuple(name.value for name in required)


__all__ = [
    "DEFAULT_DIST_TAG",
    "ObservationAdapter",
    "PlatformIdStr",
    "ReleaseArtifactKind",
    "ReleaseConfig",
    "ReleaseConfigError",
    "ReleaseConfigRejection",
    "ReleaseGateName",
    "ReleaseGatesConfig",
    "ReleasePlatformClaim",
    "ReleaseTargetConfig",
    "gate_names",
    "load_release_config",
    "parse_release_config",
]
