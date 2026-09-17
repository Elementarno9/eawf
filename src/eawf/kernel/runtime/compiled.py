"""Compile inputs, compiled Run records, and the three-state model pin.

:class:`PolicyOverlay` is the only shape a layer below the operator
contributes. It carries narrowing fields and no reference, provider
option, or executable, so a repository or Run override has no field
through which to name new path, network, or secret authority.

:class:`CompiledRunSpec` is the exact, immutable input a provider adapter
receives. It verifies its own digests: ``scope_digest``, ``policy_digest``
and ``contract_digest`` are recomputed on every validation, so a persisted
spec whose fields were edited after compilation fails to load instead of
running under authority nobody compiled.

Every resolved field carries a :class:`ResolvedFieldSource` naming the
layer it came from and the merge operation that produced it, so "why
does this Run have this limit" is answered by the record itself.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Annotated, Any, ClassVar, Final, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

from eawf.kernel.runtime.provider import (
    MUTATING_ROLES,
    ArtifactUrn,
    AuthKind,
    AuthorityGrant,
    AuthProfileUrn,
    CapabilityId,
    ConfigLevel,
    ConfigUrn,
    ContextPolicy,
    CredentialsLevel,
    Digest,
    DriverCertificationUrn,
    DriverManifest,
    DriverManifestUrn,
    EnvironmentPolicy,
    ExternalEffectsLevel,
    GitLevel,
    JsonScalar,
    ModelId,
    NetworkMode,
    PolicyUrn,
    ProviderOptions,
    ProviderProfileId,
    ProviderProfileUrn,
    RoutePolicyUrn,
    RuntimeLimits,
    RuntimeRecord,
    SandboxMode,
    SandboxPolicy,
    SemVer,
    SessionPolicy,
    SourceLayer,
    StateLevel,
    StreamPolicy,
    ToolPolicy,
    TypedUrn,
    UniqueCapabilityIds,
    UniqueModelIds,
    UniqueToolIds,
    UniqueVariableNames,
    WorkflowUrn,
    WorkspaceLevel,
    reject_repeats,
)
from eawf.kernel.state.enums import AgentSessionRole
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, RunPurpose, RunScope, TaskScope
from eawf.kernel.state.epoch2.urns import RepositoryUrn, RunUrn
from eawf.kernel.state.types import UtcDatetime

#: An RFC 6901 pointer restricted to lowercase member names, which is
#: every path a compiled record has.
JsonPointer = Annotated[str, StringConstraints(strict=True, pattern=r"^(?:/[a-z0-9_]+)+$")]
BoundedText = Annotated[
    str, StringConstraints(strict=True, strip_whitespace=True, min_length=1, max_length=500)
]
MergeOperation = Literal[
    "replace", "intersection", "union_deny", "minimum", "maximum", "keyed_patch"
]
CapabilityStatus = Literal["verified", "degraded", "unsupported", "unknown", "expired", "denied"]

#: Stands in for a digest that :meth:`CompiledRunSpec.seal` computes.
UNSEALED_DIGEST: Final = f"sha256:{'0' * 64}"
_DIGEST_FIELDS: Final = ("scope_digest", "policy_digest", "contract_digest")


class CapabilityReasonCode(StrEnum):
    """Why a capability is unavailable to a compiled Run."""

    NOT_DECLARED_BY_DRIVER = "not_declared_by_driver"
    NOT_OBSERVED = "not_observed"
    PROVIDER_UNSUPPORTED = "provider_unsupported"
    EVIDENCE_EXPIRED = "evidence_expired"
    DENIED_BY_POLICY = "denied_by_policy"
    OBSERVATION_UNKNOWN = "observation_unknown"


def canonical_digest(payload: Any) -> str:
    """Return the ``sha256:`` digest of *payload* in canonical JSON form.

    Args:
        payload: A JSON-compatible value.

    Returns:
        ``sha256:`` followed by 64 lowercase hex characters. Structurally
        equal payloads digest identically regardless of key order.

    Raises:
        TypeError: *payload* holds a value JSON cannot encode.
    """
    body = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def json_pointer_value(payload: Any, pointer: str) -> Any:
    """Return the member of *payload* that *pointer* addresses.

    Args:
        payload: A JSON-compatible document.
        pointer: A :data:`JsonPointer`.

    Returns:
        The addressed value.

    Raises:
        KeyError: A segment names no member of the document.
    """
    value = payload
    for segment in pointer.split("/")[1:]:
        if isinstance(value, dict) and segment in value:
            value = value[segment]
        elif isinstance(value, list) and segment.isdigit() and int(segment) < len(value):
            value = value[int(segment)]
        else:
            raise KeyError(pointer)
    return value


class CapabilityObservation(RuntimeRecord):
    """One capability as negotiated for a bound runtime tuple."""

    capability_id: CapabilityId
    effective_level: JsonScalar | None
    status: CapabilityStatus
    basis: Literal["native", "emulated"] | None = None
    basis_refs: Annotated[tuple[TypedUrn, ...], Field(min_length=1)]
    certification_evidence_ref: ArtifactUrn | None = None
    verified_at: UtcDatetime | None = None
    expires_at: UtcDatetime | None = None
    degradation_workflow_ref: WorkflowUrn | None = None
    reason_code: CapabilityReasonCode | None = None

    @model_validator(mode="after")
    def _status_carries_its_facts(self) -> Self:
        """Bind evidence, workflow, basis, and reason to the status.

        Raises:
            ValueError: A verified row lacks evidence or times, a degraded
                row lacks its workflow, an available row lacks its basis,
                or an unavailable row lacks its reason.
        """
        available = self.status in ("verified", "degraded")
        if available and self.basis is None:
            raise ValueError(f"a {self.status} capability requires basis")
        if self.status == "verified" and None in (
            self.certification_evidence_ref,
            self.verified_at,
            self.expires_at,
        ):
            raise ValueError("a verified capability requires evidence, verified_at and expires_at")
        if self.status == "degraded" and self.degradation_workflow_ref is None:
            raise ValueError("a degraded capability requires degradation_workflow_ref")
        if not available and self.reason_code is None:
            raise ValueError(f"a {self.status} capability requires reason_code")
        return self


class CompiledCapability(CapabilityObservation):
    """A negotiated capability together with how the Run requested it."""

    requested: Literal["required", "optional"]
    requested_level: JsonScalar
    constraint_source_refs: tuple[ConfigUrn | PolicyUrn, ...] = ()


class ResolvedFieldSource(RuntimeRecord):
    """Where one compiled field came from and how it was merged."""

    field_path: JsonPointer
    effective_value_digest: Digest
    source_layer: SourceLayer
    source_ref: ConfigUrn | PolicyUrn
    merge_operation: MergeOperation
    superseded_source_refs: Annotated[tuple[TypedUrn, ...], AfterValidator(reject_repeats)] = ()
    constraint_reason: BoundedText | None = None


# ---- narrowing overlay ------------------------------------------------------


class ModelNarrowing(RuntimeRecord):
    """A layer's cut of the model request set and its preferred default."""

    allowed: UniqueModelIds | None = None
    default: ModelId | None = None


class ToolNarrowing(RuntimeRecord):
    """A layer's cut of tool grants and its additional denials."""

    allow: UniqueToolIds | None = None
    deny: UniqueToolIds = ()
    max_parallel_calls: Annotated[StrictInt, Field(ge=1, le=32)] | None = None


class SandboxNarrowing(RuntimeRecord):
    """A layer's isolation ceiling."""

    mode: SandboxMode | None = None
    network: NetworkMode | None = None


class EnvironmentNarrowing(RuntimeRecord):
    """A layer's cut of the variable names a provider may read."""

    allowed_variable_names: UniqueVariableNames | None = None


class ContextNarrowing(RuntimeRecord):
    """A layer's input-token ceiling and remaining-token floor."""

    max_input_tokens: Annotated[StrictInt, Field(ge=1024)] | None = None
    minimum_remaining_tokens: Annotated[StrictInt, Field(ge=512)] | None = None


class LimitCeilings(RuntimeRecord):
    """A layer's ceilings on Run limits."""

    wall_seconds: Annotated[StrictInt, Field(ge=1, le=86_400)] | None = None
    output_bytes: Annotated[StrictInt, Field(ge=1, le=16_777_216)] | None = None
    child_runs: Annotated[StrictInt, Field(ge=0, le=32)] | None = None
    tool_calls: Annotated[StrictInt, Field(ge=0, le=100_000)] | None = None
    cost_microusd: Annotated[StrictInt, Field(ge=0)] | None = None
    concurrency: Annotated[StrictInt, Field(ge=1, le=32)] | None = None


class AuthorityCeiling(RuntimeRecord):
    """A layer's ceiling on each authority axis."""

    state: StateLevel | None = None
    workspace: WorkspaceLevel | None = None
    git: GitLevel | None = None
    external_effects: ExternalEffectsLevel | None = None
    credentials: CredentialsLevel | None = None
    config: ConfigLevel | None = None
    publish: Literal["none"] | None = None
    merge: Literal["none"] | None = None


class PolicyOverlay(RuntimeRecord):
    """One layer's narrowing contribution to a compiled Run.

    Field names mirror :class:`AgentProviderProfile` so a repository patch
    reads like the profile it narrows. ``profile_id`` confines the overlay
    to one profile; ``None`` applies it to every profile.
    """

    layer: SourceLayer
    source_ref: ConfigUrn | PolicyUrn
    profile_id: ProviderProfileId | None = None
    model_policy: ModelNarrowing = Field(default_factory=ModelNarrowing)
    tool_policy: ToolNarrowing = Field(default_factory=ToolNarrowing)
    sandbox: SandboxNarrowing = Field(default_factory=SandboxNarrowing)
    environment: EnvironmentNarrowing = Field(default_factory=EnvironmentNarrowing)
    context_policy: ContextNarrowing = Field(default_factory=ContextNarrowing)
    limits: LimitCeilings = Field(default_factory=LimitCeilings)
    authority_ceiling: AuthorityCeiling = Field(default_factory=AuthorityCeiling)
    required_capabilities: UniqueCapabilityIds = ()


class RunCompileRequest(RuntimeRecord):
    """What the daemon asks the compiler to compile for one Run.

    ``purpose`` is the purpose the Run was dispatched for and selects the
    route; the compiler binds it to ``run_scope`` rather than trusting the
    two to agree.
    """

    run_ref: RunUrn
    run_scope: RunScope
    purpose: RunPurpose
    agent_role: AgentSessionRole
    repository_ref: RepositoryUrn | None = None
    regime: Literal["steady", "fast", "experimental"] = "steady"
    unattended: StrictBool = True


class RuntimeBinding(RuntimeRecord):
    """The installed and certified facts of one driver a Run can bind to.

    Attributes:
        driver_ref: The reference profiles name this driver by.
        manifest: The installed driver manifest.
        certification_ref: The certification the Run would bind.
        certification_digest: Digest of that certification record.
        auth_kind: The auth kind the certification covers.
        listed_models: The provider's live model listing.
        certified_models: The models the certification covers.
        capabilities: The negotiated capability observations.
    """

    driver_ref: DriverManifestUrn
    manifest: DriverManifest
    certification_ref: DriverCertificationUrn
    certification_digest: Digest
    auth_kind: AuthKind
    listed_models: UniqueModelIds
    certified_models: UniqueModelIds
    capabilities: tuple[CapabilityObservation, ...] = ()

    @model_validator(mode="after")
    def _facts_agree(self) -> Self:
        """Require a manifest-supported auth kind and one row per capability.

        Raises:
            ValueError: The manifest does not support the auth kind, or two
                observations share a capability id.
        """
        if self.auth_kind not in self.manifest.auth_kinds:
            raise ValueError(f"manifest does not support auth kind {self.auth_kind.value!r}")
        reject_repeats(tuple(row.capability_id for row in self.capabilities))
        return self


class _CompiledRunContent(RuntimeRecord):
    """Every compiled field and every rule except the digest check.

    :meth:`CompiledRunSpec.seal` validates content through this class
    first, so digests are computed over validated values without any
    switch that could skip verification on a real spec.
    """

    #: The compiled fields ``policy_digest`` is taken over.
    POLICY_FIELDS: ClassVar[tuple[str, ...]] = (
        "capabilities",
        "limits",
        "sandbox",
        "environment",
        "session",
        "stream",
        "authority",
        "tool_policy",
        "context_policy",
        "provider_options",
    )

    schema_version: Literal["compiled-run/v1"] = "compiled-run/v1"
    run_ref: RunUrn
    run_scope: RunScope
    purpose: RunPurpose
    agent_role: AgentSessionRole
    route_policy_ref: RoutePolicyUrn
    route_policy_revision: Annotated[StrictInt, Field(gt=0)]
    profile_ref: ProviderProfileUrn
    profile_revision: Annotated[StrictInt, Field(gt=0)]
    driver_manifest_ref: DriverManifestUrn
    driver_manifest_digest: Digest
    certification_ref: DriverCertificationUrn
    certification_digest: Digest
    auth_profile_ref: AuthProfileUrn
    auth_kind: AuthKind
    model_id: ModelId
    capabilities: tuple[CompiledCapability, ...]
    limits: RuntimeLimits
    sandbox: SandboxPolicy
    environment: EnvironmentPolicy
    session: SessionPolicy
    stream: StreamPolicy
    authority: AuthorityGrant
    tool_policy: ToolPolicy
    context_policy: ContextPolicy
    provider_options: ProviderOptions
    source_map: Annotated[tuple[ResolvedFieldSource, ...], Field(min_length=1)]
    scope_digest: Digest
    contract_digest: Digest
    policy_digest: Digest
    compiled_at: UtcDatetime
    compiler_version: SemVer

    def policy_payload(self) -> dict[str, Any]:
        """Return the JSON form of the fields ``policy_digest`` covers."""
        return self.model_dump(mode="json", include=set(self.POLICY_FIELDS))

    def contract_payload(self) -> dict[str, Any]:
        """Return the JSON form ``contract_digest`` covers: all but the stamp."""
        return self.model_dump(mode="json", exclude={"contract_digest", "compiled_at"})

    @model_validator(mode="after")
    def _purpose_matches_scope_and_role(self) -> Self:
        """Keep one purpose, and keep mutation inside a task-scoped Run.

        Raises:
            ValueError: The purpose disagrees with the scope's purpose, a
                mutating purpose runs under a read-only role, or write
                authority is granted outside a mutating task-scoped Run.
        """
        if self.purpose is not self.run_scope.purpose:
            raise ValueError("purpose disagrees with run_scope.purpose")
        mutating = self.purpose in MUTATING_PURPOSES
        if mutating and self.agent_role not in MUTATING_ROLES:
            raise ValueError(f"role {self.agent_role.value!r} cannot run a mutating purpose")
        writes = self.authority.workspace == "scoped_write" or self.sandbox.mode != "read_only"
        if writes and not (mutating and isinstance(self.run_scope, TaskScope)):
            raise ValueError("write authority belongs to a mutating task-scoped Run")
        return self

    @model_validator(mode="after")
    def _rows_are_canonical(self) -> Self:
        """Require capability and source-map rows in canonical unique order.

        Raises:
            ValueError: Rows are out of order or repeat a key.
        """
        ids = [row.capability_id for row in self.capabilities]
        if ids != sorted(set(ids)):
            raise ValueError("capabilities must be unique and sorted by capability_id")
        paths = [row.field_path for row in self.source_map]
        if paths != sorted(set(paths)):
            raise ValueError("source_map must be unique and sorted by field_path")
        return self


class CompiledRunSpec(_CompiledRunContent):
    """The immutable, self-verifying provider and policy input of one Run."""

    @classmethod
    def seal(cls, fields: Mapping[str, Any]) -> Self:
        """Validate *fields* into a spec whose digests are all computed here.

        The digest rules live on this class alone, so a compiler supplies
        content and never spells a digest itself. Each source-map row's
        ``effective_value_digest`` is recomputed from the value its path
        addresses; callers pass :data:`UNSEALED_DIGEST` there.

        Args:
            fields: Every field except ``scope_digest``, ``policy_digest``
                and ``contract_digest``.

        Returns:
            The validated, sealed spec.

        Raises:
            KeyError: A source-map row names no compiled field.
            pydantic.ValidationError: The content breaks a record rule.
        """
        content = _CompiledRunContent.model_validate(
            {**fields, **dict.fromkeys(_DIGEST_FIELDS, UNSEALED_DIGEST)}
        )
        payload = content.model_dump(mode="json")
        rows = tuple(
            row.model_copy(
                update={
                    "effective_value_digest": canonical_digest(
                        json_pointer_value(payload, row.field_path)
                    )
                }
            )
            for row in content.source_map
        )
        content = content.model_copy(update={"source_map": rows})
        digests = {
            "scope_digest": canonical_digest(payload["run_scope"]),
            "policy_digest": canonical_digest(content.policy_payload()),
        }
        contract = canonical_digest({**content.contract_payload(), **digests})
        return cls.model_validate({**content.model_dump(), **digests, "contract_digest": contract})

    @model_validator(mode="after")
    def _digests_match_content(self) -> Self:
        """Recompute every content digest and refuse a mismatch.

        Raises:
            ValueError: A stored digest does not match the fields it
                covers, or a source-map row names no compiled field.
        """
        expected = {
            "scope_digest": canonical_digest(self.run_scope.model_dump(mode="json")),
            "policy_digest": canonical_digest(self.policy_payload()),
            "contract_digest": canonical_digest(self.contract_payload()),
        }
        for field, digest in expected.items():
            if getattr(self, field) != digest:
                raise ValueError(f"{field} does not match the compiled content")
        payload = self.model_dump(mode="json")
        for row in self.source_map:
            try:
                value = json_pointer_value(payload, row.field_path)
            except KeyError as exc:
                raise ValueError(f"source_map row {row.field_path} names no field") from exc
            if canonical_digest(value) != row.effective_value_digest:
                raise ValueError(f"source_map digest of {row.field_path} does not match")
        return self


class ModelPinState(StrEnum):
    """The three outcomes of resolving a model pin."""

    CERTIFIED = "certified"
    UNCERTIFIED = "uncertified"
    UNKNOWN = "unknown"


class ModelPinResolution(RuntimeRecord):
    """What a model pin resolves to and what that permits."""

    model_id: ModelId
    state: ModelPinState
    certified_alternative: ModelId | None = None

    @property
    def selectable(self) -> bool:
        """A pin unknown to the provider's listing cannot be saved."""
        return self.state is not ModelPinState.UNKNOWN

    @property
    def unattended_dispatch(self) -> bool:
        """Only a certified pin may run without an operator present."""
        return self.state is ModelPinState.CERTIFIED


def resolve_model_pin(
    model_id: str,
    *,
    requested: Iterable[str],
    listed: Iterable[str],
    certified: Iterable[str],
) -> ModelPinResolution:
    """Resolve *model_id* against the live listing and the certified set.

    A model the listing does not carry is unknown, whatever certification
    says, because nothing can run it. A listed model is certified when
    certification covers it and uncertified otherwise; an uncertified pin
    names the first requested model that is both listed and certified.

    Args:
        model_id: The pinned model.
        requested: The operator's request set, in preference order.
        listed: The provider's live model listing.
        certified: The models the bound certification covers.

    Returns:
        The resolution, in exactly one of the three states.
    """
    listed_set = frozenset(listed)
    certified_set = frozenset(certified)
    if model_id not in listed_set:
        state = ModelPinState.UNKNOWN
    elif model_id in certified_set:
        state = ModelPinState.CERTIFIED
    else:
        state = ModelPinState.UNCERTIFIED
    alternative = None
    if state is ModelPinState.UNCERTIFIED:
        alternative = next(
            (m for m in requested if m in listed_set and m in certified_set and m != model_id),
            None,
        )
    return ModelPinResolution(model_id=model_id, state=state, certified_alternative=alternative)
