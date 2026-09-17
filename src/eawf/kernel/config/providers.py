"""Canonical provider configuration loader.

Provider profiles and routes live under ``runtime.profiles`` and
``runtime.routes`` of the global, workspace, and repository config layers.
The other ``runtime`` keys belong to the adapter selector and are not
read here.

Operators own profiles and routes, so the global and workspace layers
declare whole records, and a workspace record replaces the global record
with the same id. The repository layer may only narrow: each of its
``runtime.profiles`` entries is a :class:`PolicyOverlay` patch, and it
declares no routes. A patch that tries to add a profile, a tool, network
reach, or credential reach is refused rather than clamped, so the
operator learns the patch has no effect instead of reading it as policy.

Every rule raises :class:`ProviderConfigError` with one
:class:`ProviderConfigRejection` member, so callers and tests branch on
the code instead of matching message text. Driver executables and secret
bytes have no field to appear in.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.config.layered import global_config_path, repo_config_path, workspace_config_path
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.runtime.compiled import PolicyOverlay
from eawf.kernel.runtime.provider import (
    AUTHORITY_LEVEL_ORDER,
    NETWORK_MODE_ORDER,
    SANDBOX_MODE_ORDER,
    AgentProviderProfile,
    AuthProfileUrn,
    CapabilityId,
    DriverManifestUrn,
    RoutePolicy,
    RuntimeRecord,
)
from eawf.surfaces.cli.errors import ValidationError

logger = logging.getLogger(__name__)

OperatorLayer = Literal["global", "workspace"]
ProviderLayer = Literal["global", "workspace", "repository"]
_OPERATOR_LAYERS: Final[tuple[OperatorLayer, ...]] = ("global", "workspace")


class ProviderConfigRejection(StrEnum):
    """Which loader rule refused a provider configuration."""

    LAYER_UNREADABLE = "layer_unreadable"
    AMBIGUOUS_LAYER_PATH = "ambiguous_layer_path"
    SCHEMA_INVALID = "schema_invalid"
    DUPLICATE_RECORD_ID = "duplicate_record_id"
    UNREGISTERED_DRIVER_REF = "unregistered_driver_ref"
    UNREGISTERED_AUTH_REF = "unregistered_auth_ref"
    EMPTY_MODEL_ALLOWLIST = "empty_model_allowlist"
    CAPABILITY_NOT_CERTIFIABLE = "capability_not_certifiable"
    FALLBACK_AFTER_ACCEPTANCE = "fallback_after_acceptance"
    MUTATING_ROUTE_UNMANAGED = "mutating_route_unmanaged"
    ROUTE_PROFILE_UNKNOWN = "route_profile_unknown"
    REPOSITORY_ADDS_PROFILE = "repository_adds_profile"
    REPOSITORY_ADDS_ROUTE = "repository_adds_route"
    REPOSITORY_ADDS_TOOLS = "repository_adds_tools"
    REPOSITORY_ADDS_NETWORK = "repository_adds_network"
    REPOSITORY_ADDS_CREDENTIALS = "repository_adds_credentials"
    REPOSITORY_WIDENS_AUTHORITY = "repository_widens_authority"


class ProviderConfigError(ValidationError):
    """A provider configuration was refused.

    Subclasses the CLI validation bucket so a surface maps it to the
    validation exit code, and carries the rejection as ``kind`` so the
    error envelope names the rule.

    Attributes:
        code: Which rule refused the configuration.
    """

    def __init__(self, code: ProviderConfigRejection, message: str) -> None:
        """Store *code* and lead the message with it.

        Args:
            code: The rule that refused the configuration.
            message: Operator-facing explanation naming the offending entry.
        """
        super().__init__(f"{code.value}: {message}", kind=code.value)
        self.code = code


class ProviderRegistry(RuntimeRecord):
    """The installed facts a provider configuration is checked against.

    Attributes:
        driver_refs: Driver manifests installation has registered.
        auth_profile_refs: Auth profiles the operator has registered.
        certification_capabilities: Capability ids the certification
            schema can attest; a capability outside it can never verify.
    """

    driver_refs: frozenset[DriverManifestUrn]
    auth_profile_refs: frozenset[AuthProfileUrn]
    certification_capabilities: frozenset[CapabilityId]


class LayeredProfile(RuntimeRecord):
    """An operator profile and the layer that declared it."""

    layer: OperatorLayer
    profile: AgentProviderProfile


class LayeredRoute(RuntimeRecord):
    """An operator route and the layer that declared it."""

    layer: OperatorLayer
    route: RoutePolicy


class ProviderConfiguration(RuntimeRecord):
    """The validated provider configuration of one repository."""

    profiles: tuple[LayeredProfile, ...]
    routes: tuple[LayeredRoute, ...]
    repository_overlays: tuple[PolicyOverlay, ...]

    def profile(self, profile_id: str) -> LayeredProfile | None:
        """Return the profile declared under *profile_id*, if any.

        Args:
            profile_id: The profile to look up.

        Returns:
            The layered profile, or ``None`` when no layer declares it.
        """
        return next((p for p in self.profiles if p.profile.profile_id == profile_id), None)


#: Pydantic failures that have a more specific rule than a schema error,
#: keyed by the error's string path and pydantic error type.
_SPECIFIC_ERRORS: Final[Mapping[tuple[tuple[str, ...], str], ProviderConfigRejection]] = {
    (("model_policy", "allowed"), "too_short"): ProviderConfigRejection.EMPTY_MODEL_ALLOWLIST,
    (("fallback_causes",), "enum"): ProviderConfigRejection.FALLBACK_AFTER_ACCEPTANCE,
    (("auth_profile_ref",), "extra_forbidden"): ProviderConfigRejection.REPOSITORY_ADDS_CREDENTIALS,
    (("environment", "secret_broker_ref"), "extra_forbidden"): (
        ProviderConfigRejection.REPOSITORY_ADDS_CREDENTIALS
    ),
    (("environment", "environment_class_ref"), "extra_forbidden"): (
        ProviderConfigRejection.REPOSITORY_ADDS_CREDENTIALS
    ),
    (("sandbox", "network_policy_ref"), "extra_forbidden"): (
        ProviderConfigRejection.REPOSITORY_ADDS_NETWORK
    ),
    (("sandbox", "filesystem_policy_ref"), "extra_forbidden"): (
        ProviderConfigRejection.REPOSITORY_WIDENS_AUTHORITY
    ),
    (("tool_policy", "command_family_refs"), "extra_forbidden"): (
        ProviderConfigRejection.REPOSITORY_ADDS_TOOLS
    ),
    (("driver_ref",), "extra_forbidden"): ProviderConfigRejection.REPOSITORY_ADDS_PROFILE,
}


def _rejection_for(exc: PydanticValidationError) -> ProviderConfigRejection:
    """Return the most specific rule a pydantic failure maps to."""
    for error in exc.errors():
        path = tuple(part for part in error["loc"] if isinstance(part, str))
        code = _SPECIFIC_ERRORS.get((path, error["type"]))
        if code is not None:
            return code
    return ProviderConfigRejection.SCHEMA_INVALID


def _validate[RecordT: RuntimeRecord](model: type[RecordT], raw: object, *, where: str) -> RecordT:
    """Validate *raw* as *model*, mapping failure onto a typed rejection.

    Raises:
        ProviderConfigError: *raw* does not validate.
    """
    try:
        return model.model_validate(raw)
    except PydanticValidationError as exc:
        raise ProviderConfigError(_rejection_for(exc), f"{where}: {exc}") from exc


def _entries(document: Mapping[str, Any], key: str, *, layer: str) -> list[Any]:
    """Return the list under ``runtime.<key>``, or an empty list.

    Raises:
        ProviderConfigError: The key holds something other than a list.
    """
    value = document.get(key, [])
    if not isinstance(value, list):
        raise ProviderConfigError(
            ProviderConfigRejection.SCHEMA_INVALID,
            f"{layer} runtime.{key} must be a list, got {type(value).__name__}",
        )
    return value


def _reject_duplicate_ids(ids: Iterable[str], *, where: str) -> None:
    """Refuse two records with one id inside a single layer.

    Raises:
        ProviderConfigError: An id repeats.
    """
    seen: set[str] = set()
    for record_id in ids:
        if record_id in seen:
            raise ProviderConfigError(
                ProviderConfigRejection.DUPLICATE_RECORD_ID, f"{where} declares {record_id!r} twice"
            )
        seen.add(record_id)


def _check_certifiable(ids: Iterable[str], registry: ProviderRegistry, *, where: str) -> None:
    """Refuse capability ids the certification schema cannot attest.

    Raises:
        ProviderConfigError: An id is absent from the certification schema.
    """
    missing = sorted(set(ids) - registry.certification_capabilities)
    if missing:
        raise ProviderConfigError(
            ProviderConfigRejection.CAPABILITY_NOT_CERTIFIABLE,
            f"{where} names capabilities absent from the certification schema: {missing}",
        )


def _check_profile(profile: AgentProviderProfile, registry: ProviderRegistry, where: str) -> None:
    """Refuse a profile whose references or capabilities are unregistered.

    Raises:
        ProviderConfigError: The driver or auth ref is unregistered, or a
            capability is not certifiable.
    """
    if profile.driver_ref not in registry.driver_refs:
        raise ProviderConfigError(
            ProviderConfigRejection.UNREGISTERED_DRIVER_REF,
            f"{where} names unregistered driver {profile.driver_ref!r}",
        )
    if profile.auth_profile_ref not in registry.auth_profile_refs:
        raise ProviderConfigError(
            ProviderConfigRejection.UNREGISTERED_AUTH_REF,
            f"{where} names unregistered auth profile {profile.auth_profile_ref!r}",
        )
    _check_certifiable(
        (*profile.required_capabilities, *profile.optional_capabilities), registry, where=where
    )


def _check_route(
    route: RoutePolicy,
    profiles: Mapping[str, AgentProviderProfile],
    registry: ProviderRegistry,
    where: str,
) -> None:
    """Refuse a route naming unknown profiles or routing writes unmanaged.

    Raises:
        ProviderConfigError: A profile is unknown, a capability is not
            certifiable, or a mutating route allows a profile without a
            writable workspace under a filesystem policy.
    """
    unknown = [pid for pid in route.allowed_profiles if pid not in profiles]
    if unknown:
        raise ProviderConfigError(
            ProviderConfigRejection.ROUTE_PROFILE_UNKNOWN, f"{where} names unknown {unknown}"
        )
    _check_certifiable(
        (*route.required_capabilities, *route.optional_capabilities), registry, where=where
    )
    if not route.match.mutating:
        return
    for profile_id in route.allowed_profiles:
        sandbox = profiles[profile_id].sandbox
        if sandbox.mode != "workspace_write" or sandbox.filesystem_policy_ref is None:
            raise ProviderConfigError(
                ProviderConfigRejection.MUTATING_ROUTE_UNMANAGED,
                f"{where} routes writes to {profile_id!r}, which lacks a workspace_write "
                f"sandbox under a filesystem policy",
            )


def _operator_records[RecordT: RuntimeRecord](
    documents: Mapping[ProviderLayer, Mapping[str, Any]],
    key: str,
    model: type[RecordT],
    record_id: Callable[[RecordT], str],
) -> dict[str, tuple[OperatorLayer, RecordT]]:
    """Parse one record kind across the operator layers, workspace last.

    Returns:
        ``id -> (layer, record)`` where a workspace record replaces the
        global record with the same id.
    """
    merged: dict[str, tuple[OperatorLayer, RecordT]] = {}
    for layer in _OPERATOR_LAYERS:
        raw = _entries(documents.get(layer, {}), key, layer=layer)
        records = [
            _validate(model, entry, where=f"{layer} runtime.{key}[{index}]")
            for index, entry in enumerate(raw)
        ]
        _reject_duplicate_ids(map(record_id, records), where=f"{layer} runtime.{key}")
        merged.update((record_id(record), (layer, record)) for record in records)
    return merged


def _wider(order: Sequence[str], candidate: str | None, base: str) -> bool:
    """Whether *candidate* grants more than *base* on an ordered axis."""
    return candidate is not None and order.index(candidate) > order.index(base)


def _not_subset(candidate: Iterable[str] | None, base: Iterable[str]) -> list[str]:
    """Return the members of *candidate* that *base* lacks, sorted."""
    return [] if candidate is None else sorted(set(candidate) - set(base))


def _widened_ceiling(overlay: PolicyOverlay, profile: AgentProviderProfile) -> str | None:
    """Name the first limit, context, or tool ceiling *overlay* raises."""
    pairs = [
        *(
            (f"limits.{name}", value, getattr(profile.limits, name))
            for name, value in overlay.limits.model_dump().items()
        ),
        (
            "context_policy.max_input_tokens",
            overlay.context_policy.max_input_tokens,
            profile.context_policy.max_input_tokens,
        ),
        (
            "tool_policy.max_parallel_calls",
            overlay.tool_policy.max_parallel_calls,
            profile.tool_policy.max_parallel_calls,
        ),
    ]
    for name, value, base in pairs:
        if value is not None and base is not None and value > base:
            return name
    return None


def _widened_authority(overlay: PolicyOverlay, profile: AgentProviderProfile) -> str | None:
    """Name the first non-credential authority axis or model *overlay* widens."""
    ceiling = overlay.authority_ceiling
    for axis, order in AUTHORITY_LEVEL_ORDER.items():
        if axis != "credentials" and _wider(
            order, getattr(ceiling, axis), getattr(profile.authority_ceiling, axis)
        ):
            return f"authority_ceiling.{axis}"
    if _wider(SANDBOX_MODE_ORDER, overlay.sandbox.mode, profile.sandbox.mode):
        return "sandbox.mode"
    requested = overlay.model_policy
    if _not_subset(requested.allowed, profile.model_policy.allowed) or (
        requested.default is not None and requested.default not in profile.model_policy.allowed
    ):
        return "model_policy"
    return _widened_ceiling(overlay, profile)


def _check_narrows(overlay: PolicyOverlay, profile: AgentProviderProfile, where: str) -> None:
    """Refuse a repository patch that grants more than its profile.

    Raises:
        ProviderConfigError: The patch adds tools, network, or credential
            reach, or widens any other axis.
    """
    added_tools = _not_subset(overlay.tool_policy.allow, profile.tool_policy.allow)
    if added_tools:
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_ADDS_TOOLS, f"{where} adds tools {added_tools}"
        )
    if _wider(NETWORK_MODE_ORDER, overlay.sandbox.network, profile.sandbox.network):
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_ADDS_NETWORK, f"{where} widens sandbox.network"
        )
    added_names = _not_subset(
        overlay.environment.allowed_variable_names, profile.environment.allowed_variable_names
    )
    credentials = AUTHORITY_LEVEL_ORDER["credentials"]
    if added_names or _wider(
        credentials, overlay.authority_ceiling.credentials, profile.authority_ceiling.credentials
    ):
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_ADDS_CREDENTIALS,
            f"{where} widens credential reach {added_names or ['authority_ceiling.credentials']}",
        )
    widened = _widened_authority(overlay, profile)
    if widened is not None:
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_WIDENS_AUTHORITY, f"{where} widens {widened}"
        )


def _repository_overlay(
    entry: object,
    where: str,
    profiles: Mapping[str, AgentProviderProfile],
    registry: ProviderRegistry,
) -> PolicyOverlay:
    """Parse one repository patch and check it against its profiles.

    The loader files the patch under the repository layer itself, so an
    entry that names its own layer is refused rather than overwritten.

    Raises:
        ProviderConfigError: The patch is malformed, names its own layer or
            an unknown profile, allows no model, or widens its profile.
    """
    if not isinstance(entry, dict) or "layer" in entry:
        raise ProviderConfigError(
            ProviderConfigRejection.SCHEMA_INVALID,
            f"{where} must be a mapping without a layer key; the loader files it",
        )
    overlay = _validate(PolicyOverlay, {**entry, "layer": "repository"}, where=where)
    targets = [p for pid, p in profiles.items() if overlay.profile_id in (None, pid)]
    if not targets:
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_ADDS_PROFILE,
            f"{where} patches {overlay.profile_id!r}, which no operator layer declares",
        )
    if overlay.model_policy.allowed == ():
        raise ProviderConfigError(
            ProviderConfigRejection.EMPTY_MODEL_ALLOWLIST, f"{where} allows no model"
        )
    _check_certifiable(overlay.required_capabilities, registry, where=where)
    for profile in targets:
        _check_narrows(overlay, profile, where)
    return overlay


def _repository_overlays(
    document: Mapping[str, Any],
    profiles: Mapping[str, AgentProviderProfile],
    registry: ProviderRegistry,
) -> tuple[PolicyOverlay, ...]:
    """Parse and check the repository layer's narrowing patches.

    Raises:
        ProviderConfigError: The layer declares routes, or a patch is
            refused by :func:`_repository_overlay`.
    """
    if _entries(document, "routes", layer="repository"):
        raise ProviderConfigError(
            ProviderConfigRejection.REPOSITORY_ADDS_ROUTE,
            "repository runtime.routes is refused: routes are operator configuration",
        )
    entries = _entries(document, "profiles", layer="repository")
    return tuple(
        _repository_overlay(entry, f"repository runtime.profiles[{index}]", profiles, registry)
        for index, entry in enumerate(entries)
    )


def parse_provider_configuration(
    documents: Mapping[ProviderLayer, Mapping[str, Any]],
    *,
    registry: ProviderRegistry,
) -> ProviderConfiguration:
    """Validate the ``runtime`` sections of the provider layers.

    Args:
        documents: Each present layer's ``runtime`` mapping. A missing
            layer contributes nothing.
        registry: The installed driver, auth, and certification facts.

    Returns:
        The validated configuration.

    Raises:
        ProviderConfigError: Any loader rule refuses the configuration.
    """
    profiles = _operator_records(
        documents, "profiles", AgentProviderProfile, lambda p: p.profile_id
    )
    routes = _operator_records(documents, "routes", RoutePolicy, lambda r: r.route_id)
    by_id = {pid: profile for pid, (_, profile) in profiles.items()}
    for pid, (layer, profile) in profiles.items():
        _check_profile(profile, registry, f"{layer} profile {pid!r}")
    for rid, (layer, route) in routes.items():
        _check_route(route, by_id, registry, f"{layer} route {rid!r}")
    overlays = _repository_overlays(documents.get("repository", {}), by_id, registry)
    logger.debug(
        f"provider configuration loaded profiles={len(profiles)} routes={len(routes)} "
        f"repository_overlays={len(overlays)}"
    )
    return ProviderConfiguration(
        profiles=tuple(LayeredProfile(layer=layer, profile=p) for layer, p in profiles.values()),
        routes=tuple(LayeredRoute(layer=layer, route=r) for layer, r in routes.values()),
        repository_overlays=overlays,
    )


def _runtime_section(layer: ProviderLayer, path: Path) -> Mapping[str, Any]:
    """Return the ``runtime`` mapping of the config file at *path*.

    Raises:
        ProviderConfigError: The file is unreadable, or ``runtime`` is not
            a mapping.
    """
    try:
        document = load_yaml_layer(path)
    except ValidationError as exc:
        raise ProviderConfigError(ProviderConfigRejection.LAYER_UNREADABLE, str(exc)) from exc
    runtime = document.get("runtime", {})
    if not isinstance(runtime, dict):
        raise ProviderConfigError(
            ProviderConfigRejection.SCHEMA_INVALID, f"{layer} runtime must be a mapping"
        )
    return runtime


def load_provider_configuration(
    *,
    workspace: Path,
    repository: Path | None,
    registry: ProviderRegistry,
) -> ProviderConfiguration:
    """Read the provider layers from their config files and validate them.

    Args:
        workspace: The workspace root whose ``.ea/config.yaml`` is the
            workspace layer.
        repository: The repository root whose ``.ea/config.yaml`` is the
            repository layer, or ``None`` when the workspace has no
            separate repository root.
        registry: The installed driver, auth, and certification facts.

    Returns:
        The validated configuration.

    Raises:
        ProviderConfigError: The workspace and repository layers resolve to
            one file, a file is unreadable, or any loader rule refuses the
            configuration.
    """
    paths: dict[ProviderLayer, Path] = {
        "global": global_config_path(),
        "workspace": workspace_config_path(workspace),
    }
    if repository is not None:
        repository_path = repo_config_path(repository)
        if repository_path.resolve() == paths["workspace"].resolve():
            raise ProviderConfigError(
                ProviderConfigRejection.AMBIGUOUS_LAYER_PATH,
                "the workspace and repository layers resolve to one config file; pass "
                "repository=None when the workspace is the repository",
            )
        paths["repository"] = repository_path
    documents = {layer: _runtime_section(layer, path) for layer, path in paths.items()}
    return parse_provider_configuration(documents, registry=registry)
