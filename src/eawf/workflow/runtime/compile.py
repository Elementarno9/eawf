"""Compile an immutable ``CompiledRunSpec`` for one Run.

The compiler is the gate a Run passes before anything is persisted or
spawned. It refuses a mutating purpose outside a task scope, selects the
highest-priority matching route and its first eligible profile, merges
the layer stack under least authority (:mod:`eawf.workflow.runtime.merge`),
pins the model, negotiates capabilities, and seals the result. Every
refusal carries one :class:`CompileRejection` member.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import replace
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.config.providers import LayeredProfile, LayeredRoute, ProviderConfiguration
from eawf.kernel.runtime.compiled import (
    CapabilityObservation,
    CapabilityReasonCode,
    CompiledCapability,
    CompiledRunSpec,
    ModelPinState,
    PolicyOverlay,
    ResolvedFieldSource,
    RunCompileRequest,
    RuntimeBinding,
    resolve_model_pin,
)
from eawf.kernel.runtime.provider import (
    MUTATING_ROLES,
    AgentProviderProfile,
    AuthorityGrant,
    CapabilityDeclaration,
    ContextPolicy,
    EnvironmentPolicy,
    RouteMatch,
    RuntimeLimits,
    RuntimeRecord,
    SandboxPolicy,
    SourceLayer,
    ToolPolicy,
)
from eawf.kernel.state.epoch2.run import MUTATING_PURPOSES, TaskScope
from eawf.workflow.runtime.merge import (
    FieldProvenance,
    MergedPolicy,
    merge_policy_layers,
    profile_layer,
    safe_baseline,
)

logger = logging.getLogger(__name__)

COMPILER_VERSION: Final = "1.0.0"

#: Layers a caller supplies overlays for. The repository layer comes from
#: provider configuration and the operator layers from profiles.
CALLER_OVERLAY_LAYERS: Final[frozenset[SourceLayer]] = frozenset(
    {"track_policy", "milestone_policy", "batch_policy", "run_override"}
)


class CompileRejection(StrEnum):
    """Why the compiler refused to produce a spec."""

    MUTATING_PURPOSE_OUTSIDE_TASK_SCOPE = "mutating_purpose_outside_task_scope"
    PURPOSE_SCOPE_MISMATCH = "purpose_scope_mismatch"
    ROLE_CANNOT_MUTATE = "role_cannot_mutate"
    OVERLAY_LAYER_INVALID = "overlay_layer_invalid"
    NO_MATCHING_ROUTE = "no_matching_route"
    NO_ELIGIBLE_PROFILE = "no_eligible_profile"
    NO_ELIGIBLE_MODEL = "no_eligible_model"
    MODEL_UNKNOWN = "model_unknown"
    MODEL_UNCERTIFIED = "model_uncertified"
    REQUIRED_CAPABILITY_UNAVAILABLE = "required_capability_unavailable"
    POLICY_BOUNDS_CONFLICT = "policy_bounds_conflict"


class RunCompileError(ValueError):
    """The compiler refused a request.

    Attributes:
        code: Which rule refused it.
    """

    def __init__(self, code: CompileRejection, message: str) -> None:
        """Store *code* and lead the message with it.

        Args:
            code: The rule that refused the request.
            message: Operator-facing explanation.
        """
        super().__init__(f"{code.value}: {message}")
        self.code = code


def _check_request(request: RunCompileRequest) -> None:
    """Refuse a request whose purpose its scope or role cannot carry.

    Raises:
        RunCompileError: A mutating purpose outside a task scope, a purpose
            the scope does not declare, or a mutating purpose under a
            read-only role.
    """
    mutating = request.purpose in MUTATING_PURPOSES
    if mutating and not isinstance(request.run_scope, TaskScope):
        raise RunCompileError(
            CompileRejection.MUTATING_PURPOSE_OUTSIDE_TASK_SCOPE,
            f"purpose {request.purpose.value!r} mutates, but the scope is "
            f"{request.run_scope.scope_kind!r}; only a task scope may write",
        )
    if request.purpose is not request.run_scope.purpose:
        raise RunCompileError(
            CompileRejection.PURPOSE_SCOPE_MISMATCH,
            f"purpose {request.purpose.value!r} differs from the scope's "
            f"{request.run_scope.purpose.value!r}",
        )
    if mutating and request.agent_role not in MUTATING_ROLES:
        raise RunCompileError(
            CompileRejection.ROLE_CANNOT_MUTATE,
            f"role {request.agent_role.value!r} cannot run purpose {request.purpose.value!r}",
        )


def _check_overlay_layers(
    repository: Iterable[PolicyOverlay], caller: Iterable[PolicyOverlay]
) -> None:
    """Refuse an overlay filed under a layer its source does not own.

    Raises:
        RunCompileError: A configuration overlay is not a repository
            layer, or a caller overlay is not a track, milestone, batch, or
            Run override layer.
    """
    misfiled: list[str] = [o.layer for o in repository if o.layer != "repository"]
    misfiled += [o.layer for o in caller if o.layer not in CALLER_OVERLAY_LAYERS]
    if misfiled:
        raise RunCompileError(
            CompileRejection.OVERLAY_LAYER_INVALID, f"overlays filed under {misfiled}"
        )


def _matches(match: RouteMatch, request: RunCompileRequest) -> bool:
    """Whether *request* falls inside *match*; an empty field matches all."""
    mutating = request.purpose in MUTATING_PURPOSES
    return all(
        (
            match.scope_kind in (None, request.run_scope.scope_kind),
            not match.purpose or request.purpose in match.purpose,
            not match.agent_role or request.agent_role in match.agent_role,
            not match.repository_refs or request.repository_ref in match.repository_refs,
            not match.regime or request.regime in match.regime,
            match.requires_mutation in (None, mutating),
        )
    )


def select_route(request: RunCompileRequest, configuration: ProviderConfiguration) -> LayeredRoute:
    """Return the enabled route with the highest priority matching *request*.

    Ties break on ``route_id`` so selection never depends on file order.

    Args:
        request: The Run being compiled.
        configuration: The validated provider configuration.

    Returns:
        The selected route.

    Raises:
        RunCompileError: No enabled route matches.
    """
    candidates = [
        entry
        for entry in configuration.routes
        if not entry.route.disabled and _matches(entry.route.match, request)
    ]
    if not candidates:
        raise RunCompileError(
            CompileRejection.NO_MATCHING_ROUTE,
            f"no enabled route matches {request.run_scope.scope_kind}/{request.purpose.value}",
        )
    return min(candidates, key=lambda entry: (-entry.route.priority, entry.route.route_id))


def _select_profile(
    route: LayeredRoute,
    configuration: ProviderConfiguration,
    bindings: Sequence[RuntimeBinding],
) -> tuple[LayeredProfile, RuntimeBinding]:
    """Return the first allowed profile with an enabled, bound driver.

    Raises:
        RunCompileError: No allowed profile is eligible; the message names
            each one and why.
    """
    by_driver = {binding.driver_ref: binding for binding in bindings}
    excluded: list[str] = []
    for profile_id in route.route.allowed_profiles:
        entry = configuration.profile(profile_id)
        binding = by_driver.get(entry.profile.driver_ref) if entry is not None else None
        if entry is None or entry.profile.disabled:
            excluded.append(f"{profile_id}: profile missing or disabled")
        elif binding is None or binding.manifest.disabled:
            excluded.append(f"{profile_id}: driver not bound or disabled")
        else:
            return entry, binding
    raise RunCompileError(
        CompileRejection.NO_ELIGIBLE_PROFILE,
        f"route {route.route.route_id!r} has no eligible profile: {excluded}",
    )


def _pin_model(merged: MergedPolicy, binding: RuntimeBinding, request: RunCompileRequest) -> str:
    """Resolve the merged model against the binding and the request mode.

    Raises:
        RunCompileError: No model survives the merge, the model is unknown
            to the provider listing, or an unattended Run pins an
            uncertified model.
    """
    model_id = merged.values.get("/model_id")
    if model_id is None:
        raise RunCompileError(
            CompileRejection.NO_ELIGIBLE_MODEL, "the model allowlists intersect to nothing"
        )
    pin = resolve_model_pin(
        model_id,
        requested=merged.model_allowed,
        listed=binding.listed_models,
        certified=binding.certified_models,
    )
    if pin.state is ModelPinState.UNKNOWN:
        raise RunCompileError(
            CompileRejection.MODEL_UNKNOWN, f"model {model_id!r} is not in the provider listing"
        )
    if request.unattended and not pin.unattended_dispatch:
        raise RunCompileError(
            CompileRejection.MODEL_UNCERTIFIED,
            f"model {model_id!r} is uncertified; certified alternative: "
            f"{pin.certified_alternative}",
        )
    return str(model_id)


def _capability_facts(
    declaration: CapabilityDeclaration | None,
    observation: CapabilityObservation | None,
    binding: RuntimeBinding,
    compiled_at: datetime,
) -> dict[str, Any]:
    """Return the negotiated facts of one capability.

    A capability the manifest does not declare is unsupported and is
    requested at level ``True``, meaning present. A declared capability is
    requested at its default level; with no observation it is unknown,
    and a verified observation whose evidence has expired is expired.
    """
    if declaration is None:
        return {
            "requested_level": True,
            "effective_level": None,
            "status": "unsupported",
            "basis_refs": (binding.driver_ref,),
            "reason_code": CapabilityReasonCode.NOT_DECLARED_BY_DRIVER,
        }
    if observation is None:
        return {
            "requested_level": declaration.default_level,
            "effective_level": None,
            "status": "unknown",
            "basis_refs": (binding.driver_ref, binding.certification_ref),
            "reason_code": CapabilityReasonCode.NOT_OBSERVED,
        }
    facts = observation.model_dump(exclude={"capability_id"})
    facts["requested_level"] = declaration.default_level
    if observation.status == "verified" and facts["expires_at"] <= compiled_at:
        facts.update(status="expired", reason_code=CapabilityReasonCode.EVIDENCE_EXPIRED)
    return facts


def _compile_capabilities(
    stack: Sequence[PolicyOverlay],
    merged: MergedPolicy,
    route: LayeredRoute,
    profile: AgentProviderProfile,
    binding: RuntimeBinding,
    compiled_at: datetime,
) -> tuple[CompiledCapability, ...]:
    """Compile every requested capability and refuse an unavailable required one.

    Raises:
        RunCompileError: A required capability is not verified.
    """
    required = set(merged.values.get("/capabilities", ()))
    refs: dict[str, list[str]] = {}
    for overlay in stack:
        for capability_id in overlay.required_capabilities:
            refs.setdefault(capability_id, []).append(overlay.source_ref)
    optional_sources = (
        (profile.source_ref, profile.optional_capabilities),
        (route.route.source_ref, route.route.optional_capabilities),
    )
    for source_ref, capability_ids in optional_sources:
        for capability_id in set(capability_ids) - required:
            refs.setdefault(capability_id, []).append(source_ref)
    declared = {row.capability_id: row for row in binding.manifest.capabilities}
    observed = {row.capability_id: row for row in binding.capabilities}
    rows = tuple(
        CompiledCapability.model_validate(
            {
                **_capability_facts(
                    declared.get(capability_id), observed.get(capability_id), binding, compiled_at
                ),
                "capability_id": capability_id,
                "requested": "required" if capability_id in required else "optional",
                "constraint_source_refs": tuple(dict.fromkeys(refs[capability_id])),
            }
        )
        for capability_id in sorted(refs)
    )
    unavailable = [
        r.capability_id for r in rows if r.requested == "required" and r.status != "verified"
    ]
    if unavailable:
        raise RunCompileError(
            CompileRejection.REQUIRED_CAPABILITY_UNAVAILABLE,
            f"required capabilities not verified: {unavailable}",
        )
    return rows


#: Compiled section, the profile attribute it starts from, and its model.
_SECTIONS: Final[tuple[tuple[str, str, type[RuntimeRecord]], ...]] = (
    ("limits", "limits", RuntimeLimits),
    ("sandbox", "sandbox", SandboxPolicy),
    ("environment", "environment", EnvironmentPolicy),
    ("authority", "authority_ceiling", AuthorityGrant),
    ("tool_policy", "tool_policy", ToolPolicy),
    ("context_policy", "context_policy", ContextPolicy),
)


def _sections(profile: AgentProviderProfile, merged: MergedPolicy) -> dict[str, RuntimeRecord]:
    """Build each compiled policy section from the profile and the merge.

    Raises:
        RunCompileError: The merged values violate a section's own rule,
            such as a remaining-token floor at or above the input ceiling.
    """
    built: dict[str, RuntimeRecord] = {}
    for field, attribute, model in _SECTIONS:
        prefix = f"/{field}/"
        document = getattr(profile, attribute).model_dump()
        document.update(
            (path.removeprefix(prefix), value)
            for path, value in merged.values.items()
            if path.startswith(prefix)
        )
        try:
            built[field] = model.model_validate(document)
        except PydanticValidationError as exc:
            raise RunCompileError(
                CompileRejection.POLICY_BOUNDS_CONFLICT, f"merged {field} is inconsistent: {exc}"
            ) from exc
    return built


def _source_map(
    merged: MergedPolicy, profile: LayeredProfile, route: LayeredRoute
) -> tuple[ResolvedFieldSource, ...]:
    """Name a source for every field resolved from configuration layers.

    Installation and certification facts are bound by their digests, not
    by a layer, so they have no row.
    """
    from_profile = FieldProvenance(profile.layer, profile.profile.source_ref, "replace")
    provenance = dict(merged.provenance)
    provenance.setdefault("/capabilities", replace(from_profile, merge_operation="maximum"))
    for field, _, model in _SECTIONS:
        for name in model.model_fields:
            provenance.setdefault(f"/{field}/{name}", from_profile)
    for path in (
        "/profile_ref",
        "/driver_manifest_ref",
        "/auth_profile_ref",
        "/session",
        "/stream",
        "/provider_options",
    ):
        provenance[path] = from_profile
    provenance["/route_policy_ref"] = FieldProvenance(
        route.layer, route.route.source_ref, "replace"
    )
    return tuple(provenance[path].row(path) for path in sorted(provenance))


def compile_run_spec(
    request: RunCompileRequest,
    *,
    configuration: ProviderConfiguration,
    bindings: Sequence[RuntimeBinding],
    compiled_at: datetime,
    policy_overlays: Sequence[PolicyOverlay] = (),
) -> CompiledRunSpec:
    """Compile *request* into an immutable, sealed spec.

    Args:
        request: The Run to compile.
        configuration: The validated provider configuration.
        bindings: The installed and certified drivers available to bind.
        compiled_at: The compilation stamp; capability evidence expiring
            at or before it counts as expired.
        policy_overlays: Track, milestone, batch, and Run override layers.

    Returns:
        The sealed spec, whose source map names a layer and merge
        operation for every configuration-resolved field.

    Raises:
        ValueError: *compiled_at* is naive.
        RunCompileError: Any compile rule refuses the request.
    """
    if compiled_at.utcoffset() is None:
        raise ValueError("compiled_at must be timezone-aware")
    _check_request(request)
    _check_overlay_layers(configuration.repository_overlays, policy_overlays)
    route = select_route(request, configuration)
    entry, binding = _select_profile(route, configuration, bindings)
    profile = entry.profile
    overlays = (*configuration.repository_overlays, *policy_overlays)
    stack = (
        safe_baseline(request),
        profile_layer(entry),
        PolicyOverlay(
            layer=route.layer,
            source_ref=route.route.source_ref,
            required_capabilities=route.route.required_capabilities,
        ),
        *(o for o in overlays if o.profile_id in (None, profile.profile_id)),
    )
    merged = merge_policy_layers(stack)
    model_id = _pin_model(merged, binding, request)
    capabilities = _compile_capabilities(stack, merged, route, profile, binding, compiled_at)
    fields: dict[str, Any] = {
        "run_ref": request.run_ref,
        "run_scope": request.run_scope,
        "purpose": request.purpose,
        "agent_role": request.agent_role,
        "route_policy_ref": f"route://{route.route.route_id}",
        "route_policy_revision": route.route.revision,
        "profile_ref": f"profile://{profile.profile_id}",
        "profile_revision": profile.revision,
        "driver_manifest_ref": profile.driver_ref,
        "driver_manifest_digest": binding.manifest.manifest_digest,
        "certification_ref": binding.certification_ref,
        "certification_digest": binding.certification_digest,
        "auth_profile_ref": profile.auth_profile_ref,
        "auth_kind": binding.auth_kind,
        "model_id": model_id,
        "capabilities": capabilities,
        **_sections(profile, merged),
        "session": profile.session,
        "stream": profile.stream,
        "provider_options": profile.provider_options,
        "source_map": _source_map(merged, entry, route),
        "compiled_at": compiled_at,
        "compiler_version": COMPILER_VERSION,
    }
    spec = CompiledRunSpec.seal(fields)
    logger.info(
        f"run spec compiled run={request.run_ref} route={route.route.route_id} "
        f"profile={profile.profile_id} model={model_id} contract={spec.contract_digest}"
    )
    return spec
