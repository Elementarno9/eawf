"""Strict provider records: driver manifests, operator profiles, and routes.

Three owners write these records and none writes another's. Installation
writes :class:`DriverManifest`; operator configuration writes
:class:`AgentProviderProfile` and :class:`RoutePolicy`; the daemon alone
compiles them into :mod:`eawf.kernel.runtime.compiled`. Every record is
frozen and refuses unknown keys, because a provider record that absorbed
a misspelled field would hand an adapter authority nobody wrote down.

References are scheme URNs rather than paths. A field that promises an
executable component refuses ``/usr/local/bin/tool`` at the boundary, so
no ambient executable enters through a record with no room for one.
"""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose
from eawf.kernel.state.epoch2.urns import RepositoryUrn
from eawf.kernel.state.types import UtcDatetime


class RuntimeRecord(BaseModel):
    """Base of every provider and compiled record: frozen, no unknown key."""

    model_config = ConfigDict(extra="forbid", frozen=True)


def reject_repeats[T](values: tuple[T, ...]) -> tuple[T, ...]:
    """Refuse a set-valued list that names one member twice.

    A duplicate is refused rather than dropped: the author wrote two
    entries, and keeping one silently hides which of them was meant. The
    comparison keys on type as well as value so ``True`` and ``1`` stay
    distinct levels.

    Args:
        values: The declared members, in author order.

    Returns:
        *values* unchanged.

    Raises:
        ValueError: A member appears more than once.
    """
    seen: set[tuple[type, object]] = set()
    for value in values:
        key = (type(value), value)
        if key in seen:
            raise ValueError(f"{value!r} appears more than once")
        seen.add(key)
    return values


def _ref(scheme: str, prefix: str = "") -> StringConstraints:
    """Return the constraint of a ``scheme://segment/...`` reference.

    Segments start with a lowercase letter or digit, so neither a ``..``
    segment nor an empty one can appear, and a host path cannot parse.
    """
    segment = r"[a-z0-9][a-z0-9._-]*"
    pattern = rf"^{scheme}://{prefix}{segment}(?:/{segment})*$"
    return StringConstraints(strict=True, max_length=256, pattern=pattern)


def _grammar(pattern: str) -> StringConstraints:
    """Return a strict string constraint for *pattern*."""
    return StringConstraints(strict=True, pattern=pattern)


# ---- identifiers ------------------------------------------------------------

#: The shared grammar of provider, manifest, profile, route, and
#: certification identifiers.
ProviderRecordId = Annotated[str, _grammar(r"^[a-z][a-z0-9_-]{0,63}$")]
ProviderId = ProviderRecordId
DriverManifestId = ProviderRecordId
ProviderProfileId = ProviderRecordId
RoutePolicyId = ProviderRecordId

#: A secret-free identifier such as a package, codec, locale, or zone.
#: A leading separator is refused, so a host path cannot pose as one.
BoundedIdentifier = Annotated[str, _grammar(r"^[A-Za-z0-9@][A-Za-z0-9._@/+:-]{0,127}$")]
SemVer = Annotated[
    str,
    _grammar(
        r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
    ),
]
Digest = Sha256DigestStr
CapabilityId = Annotated[str, _grammar(r"^[a-z][a-z0-9_]{0,63}$")]
ToolCapabilityId = Annotated[str, _grammar(r"^[a-z][a-z0-9_]{0,63}$")]
CommandFamilyId = Annotated[str, _grammar(r"^[a-z][a-z0-9_-]{0,63}$")]
ModelId = Annotated[str, _grammar(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$")]
EnvironmentVariableName = Annotated[str, _grammar(r"^[A-Z_][A-Z0-9_]{0,127}$")]
#: A capability level. Strings are bounded so a level cannot smuggle a
#: payload; floats and null are not levels.
JsonScalar = StrictBool | StrictInt | Annotated[str, StringConstraints(strict=True, max_length=128)]

# ---- typed references -------------------------------------------------------

DriverManifestUrn = Annotated[str, _ref("driver")]
AuthProfileUrn = Annotated[str, _ref("auth")]
ConfigUrn = Annotated[str, _ref("config")]
PolicyUrn = Annotated[str, _ref("policy")]
FilesystemPolicyUrn = Annotated[str, _ref("policy", "filesystem/")]
NetworkPolicyUrn = Annotated[str, _ref("policy", "network/")]
SchemaUrn = Annotated[str, _ref("schema")]
InstallationUrn = Annotated[str, _ref("installation")]
ExecutableComponentUrn = Annotated[str, _ref("component")]
ArtifactUrn = Annotated[str, _ref("artifact")]
WorkflowUrn = Annotated[str, _ref("workflow")]
EnvironmentClassUrn = Annotated[str, _ref("environment")]
SecretBrokerUrn = Annotated[str, _ref("secrets")]
RegisteredServerProfileUrn = Annotated[str, _ref("server")]
ProviderProfileUrn = Annotated[str, _ref("profile")]
RoutePolicyUrn = Annotated[str, _ref("route")]
DriverCertificationUrn = Annotated[str, _ref("certification")]
#: Any reference of a scheme this module registers.
TypedUrn = Annotated[
    str,
    _ref(
        "(?:driver|auth|config|policy|schema|installation|component|artifact"
        "|workflow|environment|secrets|server|profile|route|certification)"
    ),
]

# ---- closed vocabularies ----------------------------------------------------


class AuthKind(StrEnum):
    """How a provider session authenticates. Only the kind is recorded."""

    SUBSCRIPTION = "subscription"
    API_KEY = "api_key"  # pragma: allowlist secret
    OAUTH = "oauth"
    WORKLOAD_IDENTITY = "workload_identity"
    LOCAL_SESSION = "local_session"


class ControlKind(StrEnum):
    """A control a capability may require the driver to honour."""

    STEER = "steer"
    ANSWER = "answer"
    INTERRUPT = "interrupt"
    CANCEL = "cancel"
    RESUME = "resume"
    RETRY = "retry"
    FORK = "fork"
    RECONCILE = "reconcile"


class FallbackCause(StrEnum):
    """Why a route may move to its next profile.

    Every member names a failure observed before the provider accepted
    the Run. There is deliberately no post-acceptance member: after
    acceptance the attempt identity exists, and moving it would fork one
    Run into two.
    """

    PRE_ACCEPTANCE_TRANSPORT_FAILURE = "pre_acceptance_transport_failure"
    PRE_ACCEPTANCE_CAPACITY = "pre_acceptance_capacity"
    PRE_ACCEPTANCE_PROVIDER_UNAVAILABLE = "pre_acceptance_provider_unavailable"


#: The ten scope kinds a route may match, spelled as the Run scopes spell
#: their discriminator.
RunScopeKind = Literal[
    "task",
    "batch",
    "milestone",
    "campaign",
    "release",
    "repository",
    "workspace",
    "evidence",
    "claim",
    "question",
]

#: Where a compiled field's value came from, least specific first.
SourceLayer = Literal[
    "safe_baseline",
    "global",
    "workspace",
    "repository",
    "track_policy",
    "milestone_policy",
    "batch_policy",
    "run_override",
]
SOURCE_LAYER_PRECEDENCE: Final[tuple[SourceLayer, ...]] = (
    "safe_baseline",
    "global",
    "workspace",
    "repository",
    "track_policy",
    "milestone_policy",
    "batch_policy",
    "run_override",
)

SandboxMode = Literal["read_only", "workspace_write"]
NetworkMode = Literal["deny", "brokered_allowlist"]
StateLevel = Literal["read_only", "proposal_only"]
WorkspaceLevel = Literal["none", "read_only", "scoped_write"]
GitLevel = Literal["none", "read_metadata"]
ExternalEffectsLevel = Literal["none", "proposal_only"]
CredentialsLevel = Literal["none", "reference_only"]
ConfigLevel = Literal["none", "read_effective"]

#: Each ordered axis from least to most authority. A merge keeps the
#: lowest level any layer declares, so this table defines "narrower".
AUTHORITY_LEVEL_ORDER: Final[Mapping[str, tuple[str, ...]]] = {
    "state": ("read_only", "proposal_only"),
    "workspace": ("none", "read_only", "scoped_write"),
    "git": ("none", "read_metadata"),
    "external_effects": ("none", "proposal_only"),
    "credentials": ("none", "reference_only"),
    "config": ("none", "read_effective"),
    "publish": ("none",),
    "merge": ("none",),
}
SANDBOX_MODE_ORDER: Final[tuple[SandboxMode, ...]] = ("read_only", "workspace_write")
NETWORK_MODE_ORDER: Final[tuple[NetworkMode, ...]] = ("deny", "brokered_allowlist")

#: Roles whose Runs may carry a mutating purpose.
MUTATING_ROLES: Final = frozenset({AgentSessionRole.EXECUTOR, AgentSessionRole.POLISHER})

UniqueAuthKinds = Annotated[tuple[AuthKind, ...], AfterValidator(reject_repeats)]
UniqueCapabilityIds = Annotated[tuple[CapabilityId, ...], AfterValidator(reject_repeats)]
UniqueToolIds = Annotated[tuple[ToolCapabilityId, ...], AfterValidator(reject_repeats)]
UniqueModelIds = Annotated[tuple[ModelId, ...], AfterValidator(reject_repeats)]
UniqueVariableNames = Annotated[tuple[EnvironmentVariableName, ...], AfterValidator(reject_repeats)]
NonEmptyUniqueModelIds = Annotated[
    tuple[ModelId, ...], Field(min_length=1), AfterValidator(reject_repeats)
]


def _same_scalar(left: object, right: object) -> bool:
    """Compare two levels without letting ``True`` equal ``1``."""
    return type(left) is type(right) and left == right


def semver_key(version: str) -> tuple[int, int, int, int, str]:
    """Return a sort key for a :data:`SemVer` string.

    A release sorts above its own pre-releases, and pre-release labels
    compare as text. Build metadata is ignored.

    Args:
        version: A string already admitted by :data:`SemVer`.

    Returns:
        ``(major, minor, patch, is_release, pre_release)``.
    """
    core, _, pre_release = version.split("+", 1)[0].partition("-")
    major, minor, patch = (int(part) for part in core.split("."))
    return major, minor, patch, 0 if pre_release else 1, pre_release


# ---- driver manifest --------------------------------------------------------


class LockedDistribution(RuntimeRecord):
    """The exact installed artifact a driver runs from.

    ``entrypoint_ref`` names a registered executable component. A raw path
    would let whichever binary sits there run under this certification.
    """

    kind: Literal["python", "npm", "binary", "bundled"]
    package: BoundedIdentifier
    version: SemVer
    artifact_digest: Digest
    entrypoint_ref: ExecutableComponentUrn
    lockfile_ref: ArtifactUrn
    signature_ref: ArtifactUrn | None = None


class EventCodecDeclaration(RuntimeRecord):
    """The event stream format a driver emits."""

    codec_id: BoundedIdentifier
    schema_version: SemVer
    schema_ref: SchemaUrn
    supports_replay: StrictBool
    provider_sequence_kind: Literal["integer", "opaque", "none"]
    max_raw_event_bytes: Annotated[StrictInt, Field(ge=1, le=4_194_304)] = 262_144


class CapabilityDeclaration(RuntimeRecord):
    """One capability a driver claims, with its admissible levels."""

    capability_id: CapabilityId
    level_kind: Literal["boolean", "integer", "enum", "set"]
    allowed_levels: Annotated[
        tuple[JsonScalar, ...], Field(min_length=1), AfterValidator(reject_repeats)
    ]
    default_level: JsonScalar
    required_auth_kinds: UniqueAuthKinds = ()
    required_control_refs: Annotated[tuple[ControlKind, ...], AfterValidator(reject_repeats)] = ()
    degradation_workflow_refs: tuple[WorkflowUrn, ...] = ()

    @model_validator(mode="after")
    def _default_is_allowed(self) -> Self:
        """Require the default level to be one of the allowed levels.

        Raises:
            ValueError: The default is not an allowed level.
        """
        if not any(_same_scalar(level, self.default_level) for level in self.allowed_levels):
            raise ValueError(
                f"default_level {self.default_level!r} of {self.capability_id!r} is not allowed"
            )
        return self


class DriverManifest(RuntimeRecord):
    """What an installed driver is and claims. Operators cannot patch it."""

    schema_version: Literal["driver-manifest/v1"]
    manifest_id: DriverManifestId
    provider_id: ProviderId
    driver_kind: Literal["native_sdk", "app_server", "registered_cli", "remote_api"]
    distribution: LockedDistribution
    manifest_digest: Digest
    worker_protocol_versions: Annotated[
        tuple[SemVer, ...], Field(min_length=1), AfterValidator(reject_repeats)
    ]
    semantic_protocol_versions: Annotated[
        tuple[SemVer, ...], Field(min_length=1), AfterValidator(reject_repeats)
    ]
    event_codec: EventCodecDeclaration
    auth_kinds: Annotated[tuple[AuthKind, ...], Field(min_length=1), AfterValidator(reject_repeats)]
    control_schema_ref: SchemaUrn
    provider_options_schema_ref: SchemaUrn
    capabilities: Annotated[tuple[CapabilityDeclaration, ...], Field(min_length=1)]
    installation_ref: InstallationUrn
    installed_at: UtcDatetime
    disabled: StrictBool = False

    @model_validator(mode="after")
    def _versions_descend_and_capabilities_unique(self) -> Self:
        """Require newest-first protocol versions and one row per capability.

        Raises:
            ValueError: Worker protocol versions are not in descending
                order, or two declarations share a capability id.
        """
        versions = self.worker_protocol_versions
        if list(versions) != sorted(versions, key=semver_key, reverse=True):
            raise ValueError("worker_protocol_versions must be sorted newest first")
        reject_repeats(tuple(row.capability_id for row in self.capabilities))
        return self


# ---- operator profile -------------------------------------------------------


class ModelPolicy(RuntimeRecord):
    """The operator's model request set, never the eligible set."""

    allowed: NonEmptyUniqueModelIds
    default: ModelId
    selection: Literal["fixed", "ordered_first_available"] = "fixed"
    reasoning_effort: Literal["low", "medium", "high", "xhigh", "max", "ultra"] | None = None
    minimum_context_tokens: Annotated[StrictInt, Field(ge=1024)] = 8192

    @model_validator(mode="after")
    def _default_is_allowed(self) -> Self:
        """Require the default model to be requested.

        Raises:
            ValueError: The default is not in the allowed list.
        """
        if self.default not in self.allowed:
            raise ValueError(f"default model {self.default!r} is not in allowed")
        return self


class ResponsePolicy(RuntimeRecord):
    """Bounds on what a provider may return."""

    max_text_chars: Annotated[StrictInt, Field(ge=1, le=1_000_000)] = 100_000
    require_typed_terminal_report: Literal[True] = True
    allow_hidden_reasoning: Literal[False] = False
    summary_style: Literal["normal", "compact"] = "normal"


class ContextPolicy(RuntimeRecord):
    """How much context a Run may hold and how it compacts."""

    max_input_tokens: Annotated[StrictInt, Field(ge=1024)] = 131_072
    compaction: Literal["deny", "provider_checkpoint", "eawf_checkpoint"] = "eawf_checkpoint"
    minimum_remaining_tokens: Annotated[StrictInt, Field(ge=512)] = 4096
    deferred_catalog: StrictBool = True
    reassert_contract_after_compaction: Literal[True] = True

    @model_validator(mode="after")
    def _floor_below_ceiling(self) -> Self:
        """Require the remaining-token floor to sit below the input ceiling.

        Raises:
            ValueError: The floor is not below the ceiling.
        """
        if self.minimum_remaining_tokens >= self.max_input_tokens:
            raise ValueError("minimum_remaining_tokens must be below max_input_tokens")
        return self


class ToolPolicy(RuntimeRecord):
    """Positive semantic tool grants. A deny entry wins over an allow."""

    allow: UniqueToolIds = ()
    deny: UniqueToolIds = ()
    command_family_refs: Annotated[tuple[CommandFamilyId, ...], AfterValidator(reject_repeats)] = ()
    max_parallel_calls: Annotated[StrictInt, Field(ge=1, le=32)] = 1
    ambient_provider_tools: Literal[False] = False


class SandboxPolicy(RuntimeRecord):
    """The managed isolation a Run executes inside."""

    mode: SandboxMode
    network: NetworkMode = "deny"
    filesystem_policy_ref: FilesystemPolicyUrn | None = None
    network_policy_ref: NetworkPolicyUrn | None = None
    managed: Literal[True] = True
    expose_canonical_git: Literal[False] = False
    expose_daemon_storage: Literal[False] = False

    @model_validator(mode="after")
    def _allowlist_names_its_policy(self) -> Self:
        """Require a brokered allowlist to name the policy that lists it.

        Raises:
            ValueError: Network is brokered but no network policy is named.
        """
        if self.network == "brokered_allowlist" and self.network_policy_ref is None:
            raise ValueError("network brokered_allowlist requires network_policy_ref")
        return self


class EnvironmentPolicy(RuntimeRecord):
    """The environment a provider process sees. Nothing is inherited."""

    environment_class_ref: EnvironmentClassUrn | None = None
    allowed_variable_names: UniqueVariableNames = ()
    secret_broker_ref: SecretBrokerUrn | None = None
    locale: BoundedIdentifier = "C.UTF-8"
    timezone: BoundedIdentifier = "UTC"
    inherit_ambient: Literal[False] = False


class SessionPolicy(RuntimeRecord):
    """Provider session reuse and liveness timing."""

    reuse: Literal["never", "same_run_continuity"] = "same_run_continuity"
    heartbeat_seconds: Annotated[StrictInt, Field(ge=1, le=300)] = 10
    lost_after_seconds: Annotated[StrictInt, Field(ge=2, le=900)] = 30
    resume_window_seconds: Annotated[StrictInt, Field(ge=0, le=3600)] = 120
    max_continuity_attempts: Annotated[StrictInt, Field(ge=0, le=3)] = 1

    @model_validator(mode="after")
    def _lost_after_exceeds_heartbeat(self) -> Self:
        """Require the loss threshold to exceed one heartbeat.

        Raises:
            ValueError: ``lost_after_seconds`` does not exceed the heartbeat.
        """
        if self.lost_after_seconds <= self.heartbeat_seconds:
            raise ValueError("lost_after_seconds must exceed heartbeat_seconds")
        return self


class RuntimeLimits(RuntimeRecord):
    """Hard ceilings on one Run. A null cost means no priced ceiling exists."""

    wall_seconds: Annotated[StrictInt, Field(ge=1, le=86_400)]
    output_bytes: Annotated[StrictInt, Field(ge=1, le=16_777_216)]
    child_runs: Annotated[StrictInt, Field(ge=0, le=32)] = 0
    tool_calls: Annotated[StrictInt, Field(ge=0, le=100_000)] = 1000
    cost_microusd: Annotated[StrictInt, Field(ge=0)] | None = None
    concurrency: Annotated[StrictInt, Field(ge=1, le=32)] = 1


class StreamPolicy(RuntimeRecord):
    """How provider events reach the daemon."""

    emit_semantic_events: Literal[True] = True
    capture_raw_diagnostics: StrictBool = False
    max_raw_event_bytes: Annotated[StrictInt, Field(ge=0, le=4_194_304)] = 262_144
    flush_interval_ms: Annotated[StrictInt, Field(ge=1, le=10_000)] = 100
    require_contiguous_sequence: Literal[True] = True


class AuthorityGrant(RuntimeRecord):
    """Authority per axis. No level grants a canonical write, merge, or publish."""

    state: StateLevel = "read_only"
    workspace: WorkspaceLevel = "none"
    git: GitLevel = "none"
    external_effects: ExternalEffectsLevel = "none"
    credentials: CredentialsLevel = "none"
    config: ConfigLevel = "none"
    publish: Literal["none"] = "none"
    merge: Literal["none"] = "none"


class CodexProviderOptions(RuntimeRecord):
    """Options the Codex driver accepts."""

    provider_kind: Literal["codex"]
    approval_mode: Literal["broker_only"] = "broker_only"
    reasoning_summary: Literal["none", "auto"] = "auto"


class ClaudeProviderOptions(RuntimeRecord):
    """Options the Claude driver accepts."""

    provider_kind: Literal["claude"]
    permission_mode: Literal["broker_only"] = "broker_only"
    max_thinking_tokens: Annotated[StrictInt, Field(ge=0)] | None = None


class OpenCodeProviderOptions(RuntimeRecord):
    """Options the OpenCode driver accepts."""

    provider_kind: Literal["opencode"]
    permission_mode: Literal["broker_only"] = "broker_only"
    server_profile_ref: RegisteredServerProfileUrn


ProviderOptions = Annotated[
    CodexProviderOptions | ClaudeProviderOptions | OpenCodeProviderOptions,
    Field(discriminator="provider_kind"),
]


class AgentProviderProfile(RuntimeRecord):
    """One operator-declared way to run an agent on one driver."""

    schema_version: Literal["provider-profile/v1"] = "provider-profile/v1"
    profile_id: ProviderProfileId
    driver_ref: DriverManifestUrn
    auth_profile_ref: AuthProfileUrn
    model_policy: ModelPolicy
    required_capabilities: UniqueCapabilityIds = ()
    optional_capabilities: UniqueCapabilityIds = ()
    response_policy: ResponsePolicy = Field(default_factory=ResponsePolicy)
    context_policy: ContextPolicy = Field(default_factory=ContextPolicy)
    tool_policy: ToolPolicy = Field(default_factory=ToolPolicy)
    sandbox: SandboxPolicy
    environment: EnvironmentPolicy = Field(default_factory=EnvironmentPolicy)
    session: SessionPolicy = Field(default_factory=SessionPolicy)
    limits: RuntimeLimits
    stream: StreamPolicy = Field(default_factory=StreamPolicy)
    authority_ceiling: AuthorityGrant
    provider_options: ProviderOptions
    disabled: StrictBool = False
    revision: Annotated[StrictInt, Field(gt=0)]
    source_ref: ConfigUrn

    @model_validator(mode="after")
    def _capability_sets_disjoint(self) -> Self:
        """Refuse a capability that is both required and optional.

        Raises:
            ValueError: The two sets overlap.
        """
        overlap = set(self.required_capabilities) & set(self.optional_capabilities)
        if overlap:
            raise ValueError(f"capabilities both required and optional: {sorted(overlap)}")
        return self


# ---- route policy -----------------------------------------------------------


class RouteMatch(RuntimeRecord):
    """Which Runs a route applies to. An empty field is a wildcard."""

    scope_kind: RunScopeKind | None = None
    purpose: Annotated[tuple[RunPurpose, ...], AfterValidator(reject_repeats)] = ()
    agent_role: Annotated[tuple[AgentSessionRole, ...], AfterValidator(reject_repeats)] = ()
    repository_refs: Annotated[tuple[RepositoryUrn, ...], AfterValidator(reject_repeats)] = ()
    regime: Annotated[
        tuple[Literal["steady", "fast", "experimental"], ...], AfterValidator(reject_repeats)
    ] = ()
    requires_mutation: StrictBool | None = None

    @property
    def mutating(self) -> bool:
        """Whether a Run this route matches may change a repository.

        An explicit ``requires_mutation`` decides. Without it, a match that
        lists no purpose also selects mutating Runs, so it counts as
        mutating; only a purpose list with no mutating member rules writes
        out.
        """
        if self.requires_mutation is not None:
            return self.requires_mutation
        return not self.purpose or not MUTATING_PURPOSES.isdisjoint(self.purpose)

    @model_validator(mode="after")
    def _not_wholly_wildcard(self) -> Self:
        """Refuse a match that constrains nothing.

        Raises:
            ValueError: Every field is a wildcard.
        """
        fields = (self.purpose, self.agent_role, self.repository_refs, self.regime)
        if self.scope_kind is None and self.requires_mutation is None and not any(fields):
            raise ValueError("a route match must constrain at least one field")
        return self


class RoutePolicy(RuntimeRecord):
    """An ordered choice of profiles for the Runs a match selects."""

    schema_version: Literal["route-policy/v1"] = "route-policy/v1"
    route_id: RoutePolicyId
    priority: Annotated[StrictInt, Field(ge=0, le=10_000)] = 100
    match: RouteMatch
    allowed_profiles: Annotated[
        tuple[ProviderProfileId, ...], Field(min_length=1), AfterValidator(reject_repeats)
    ]
    required_capabilities: UniqueCapabilityIds = ()
    optional_capabilities: UniqueCapabilityIds = ()
    fallback_causes: Annotated[tuple[FallbackCause, ...], AfterValidator(reject_repeats)] = ()
    protected_auth_change: Literal[True] = True
    protected_cost_basis_change: Literal[True] = True
    disabled: StrictBool = False
    revision: Annotated[StrictInt, Field(gt=0)]
    source_ref: ConfigUrn
