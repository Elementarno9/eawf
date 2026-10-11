"""Derive the provider registry and certified runtime bindings from installed facts.

Native dispatch compiles a Run against three inputs nobody writes by hand: a
:class:`~eawf.kernel.config.providers.ProviderRegistry` of the drivers and
auth profiles installed, one :class:`~eawf.kernel.runtime.compiled.RuntimeBinding`
per driver this machine has certified, and the provider configuration layers.
Each is read here off what is installed:

- every launcher eawf ships carries a ``driver_manifest.json`` in its package,
  which declares the driver and is completed with the installed SDK
  distribution's own version and RECORD digest;
- a runtime is bound only when its latest machine certification row is
  certified and unexpired, and the binding cites that row by URN and digest;
- the built-in profiles and routes are filed under the global layer, beneath
  any record the operator declares there, and the tree's own config file is
  the repository layer, which the provider loader lets only narrow.

A machine certification row records neither a model listing nor an auth kind.
Neither is invented from the row: the auth kind is the local session, because
the certifier probes the installed binary under the session it holds and its
tuple digest commits to that; the model listing is the one the shipped
launcher manifest declares. The probe runs no model, so it certifies the
harness for whichever listed model it is asked to run.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Annotated, Any, Final, Literal

from pydantic import Field
from pydantic import ValidationError as PydanticValidationError

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.layered import global_config_path, repo_config_path
from eawf.kernel.config.providers import ProviderRegistry, read_runtime_section
from eawf.kernel.runtime.certification import CapabilityCertification
from eawf.kernel.runtime.compiled import (
    UNSEALED_DIGEST,
    CapabilityObservation,
    CapabilityReasonCode,
    CapabilityStatus,
    RuntimeBinding,
    canonical_digest,
)
from eawf.kernel.runtime.provider import (
    AuthKind,
    AuthProfileUrn,
    BoundedIdentifier,
    CapabilityDeclaration,
    DriverManifest,
    DriverManifestId,
    DriverManifestUrn,
    EventCodecDeclaration,
    ExecutableComponentUrn,
    LockedDistribution,
    NonEmptyUniqueModelIds,
    ProviderId,
    RuntimeRecord,
    SchemaUrn,
    SemVer,
)
from eawf.kernel.store.kinds.runtime_certification import MachineCertification
from eawf.runtime.mcp.grant import SEMANTIC_TOOLS_CAPABILITY
from eawf.runtime.runtimes.capabilities import CAPABILITY_NAMES
from eawf.workflow.evidence.machine_certification import read_machine_certifications

logger = logging.getLogger(__name__)

#: The packages whose ``driver_manifest.json`` declares a shipped launcher.
LAUNCHER_PACKAGES: Final[tuple[str, ...]] = (
    "eawf.runtime.runtimes.claude",
    "eawf.runtime.runtimes.codex",
)
_MANIFEST_FILENAME: Final = "driver_manifest.json"

#: The auth kind every machine certification is taken under: the certifier
#: probes the installed binary through the session it holds on this machine.
MACHINE_CERTIFIED_AUTH_KIND: Final = AuthKind.LOCAL_SESSION

#: Every capability a machine certification can attest: each one the probe has a
#: matrix row for, and the semantic tools, which only a dispatched canary observes.
CERTIFIABLE_CAPABILITIES: Final = frozenset((*CAPABILITY_NAMES, SEMANTIC_TOOLS_CAPABILITY))

#: How a certified capability's status reads as a negotiated observation. A
#: failed capability established nothing the compiler may rely on.
_OBSERVED_STATUS: Final[Mapping[str, tuple[CapabilityStatus, CapabilityReasonCode | None]]] = {
    "verified": ("verified", None),
    "degraded": ("degraded", None),
    "unsupported": ("unsupported", CapabilityReasonCode.PROVIDER_UNSUPPORTED),
    "failed": ("unknown", CapabilityReasonCode.OBSERVATION_UNKNOWN),
}


class ShippedDistribution(RuntimeRecord):
    """The SDK distribution a shipped launcher drives its runtime through."""

    kind: Literal["python"]
    package: BoundedIdentifier
    entrypoint_ref: ExecutableComponentUrn


class LauncherManifest(RuntimeRecord):
    """One shipped launcher's driver declaration, before installation facts.

    Attributes:
        runtime_id: The runtime its machine certification rows name.
        driver_ref: The reference profiles name the driver by.
        auth_profile_ref: The auth profile of the session the runtime holds.
        listed_models: The models the launcher declares it can run.
    """

    schema_version: Literal["launcher-manifest/v1"]
    runtime_id: Annotated[str, Field(min_length=1, max_length=64)]
    driver_ref: DriverManifestUrn
    auth_profile_ref: AuthProfileUrn
    listed_models: NonEmptyUniqueModelIds
    manifest_id: DriverManifestId
    provider_id: ProviderId
    driver_kind: Literal["native_sdk", "app_server", "registered_cli", "remote_api"]
    distribution: ShippedDistribution
    worker_protocol_versions: Annotated[tuple[SemVer, ...], Field(min_length=1)]
    semantic_protocol_versions: Annotated[tuple[SemVer, ...], Field(min_length=1)]
    event_codec: EventCodecDeclaration
    auth_kinds: Annotated[tuple[AuthKind, ...], Field(min_length=1)]
    control_schema_ref: SchemaUrn
    provider_options_schema_ref: SchemaUrn
    capabilities: Annotated[tuple[CapabilityDeclaration, ...], Field(min_length=1)]


@dataclass(frozen=True)
class InstalledProviderFacts:
    """What native dispatch compiles a Run against on this machine.

    Attributes:
        registry: The shipped drivers and auth profiles, and the capability
            ids a machine certification can attest.
        bindings: One binding per runtime whose latest certification is
            current, in launcher order.
        config: Each provider layer's config document, keyed by layer
            name, in the shape ``compile_task_request`` reads.
        unbound: Why each shipped provider has no binding, keyed by
            provider id, with the command that would bind it.
    """

    registry: ProviderRegistry
    bindings: tuple[RuntimeBinding, ...]
    config: Mapping[str, Mapping[str, Any]]
    unbound: Mapping[str, str]


class _UnboundError(Exception):
    """A runtime cannot be bound; the message leads with the reason code."""


def shipped_launcher_manifests() -> tuple[LauncherManifest, ...]:
    """Return the driver declaration of every launcher eawf ships.

    Returns:
        One manifest per :data:`LAUNCHER_PACKAGES` entry, in that order.

    Raises:
        FileNotFoundError: A launcher package carries no manifest.
        pydantic.ValidationError: A manifest does not validate.
    """
    return tuple(
        LauncherManifest.model_validate_json(
            files(package).joinpath(_MANIFEST_FILENAME).read_text(encoding="utf-8")
        )
        for package in LAUNCHER_PACKAGES
    )


def _installed_manifest(launcher: LauncherManifest) -> DriverManifest:
    """Complete *launcher* with its installed SDK distribution's facts.

    The distribution's RECORD lists a digest of every installed file, so its
    own digest stands for the installed artifact, and its modification time
    for when it was installed.

    Raises:
        _UnboundError: The distribution is not installed or records no RECORD.
        pydantic.ValidationError: An installed fact breaks the manifest
            grammar, such as a version that is not semantic.
    """
    package = launcher.distribution.package
    try:
        installed = importlib.metadata.distribution(package)
    except importlib.metadata.PackageNotFoundError as error:
        raise _UnboundError(f"driver_not_installed: {package} is not installed") from error
    record = next(
        (
            entry
            for entry in installed.files or ()
            if entry.name == "RECORD" and entry.parent.name.endswith(".dist-info")
        ),
        None,
    )
    if record is None:
        raise _UnboundError(f"driver_not_installed: {package} records no RECORD")
    record_path = Path(str(record.locate()))
    version = installed.version
    declared = launcher.model_dump(
        exclude={"schema_version", "runtime_id", "driver_ref", "auth_profile_ref", "listed_models"}
    )
    declared["distribution"] = LockedDistribution(
        **launcher.distribution.model_dump(),
        version=version,
        artifact_digest=f"sha256:{hashlib.sha256(record_path.read_bytes()).hexdigest()}",
        lockfile_ref=f"artifact://dist-record/{package}/{version}",
    ).model_dump()
    declared |= {
        "schema_version": "driver-manifest/v1",
        "installation_ref": f"installation://{package}/{version}",
        "installed_at": datetime.fromtimestamp(record_path.stat().st_mtime, UTC),
    }
    content = DriverManifest.model_validate(declared | {"manifest_digest": UNSEALED_DIGEST})
    digest = canonical_digest(content.model_dump(mode="json", exclude={"manifest_digest"}))
    return content.model_copy(update={"manifest_digest": digest})


def _observation(row: CapabilityCertification, certification_ref: str) -> CapabilityObservation:
    """Return the negotiated view of one certified capability."""
    status, reason = _OBSERVED_STATUS[row.status]
    return CapabilityObservation(
        capability_id=row.capability_id,
        effective_level=row.level,
        status=status,
        basis=row.basis,
        basis_refs=(certification_ref,),
        certification_evidence_ref=row.evidence_ref,
        verified_at=row.verified_at,
        expires_at=row.expires_at,
        degradation_workflow_ref=row.degradation_workflow_ref,
        reason_code=reason,
    )


def _binding(
    launcher: LauncherManifest, row: MachineCertification | None, now: datetime
) -> RuntimeBinding:
    """Bind *launcher* under its runtime's latest certification row *row*.

    Raises:
        _UnboundError: The runtime was never certified, its latest row is a
            quarantine or has expired, or its SDK is not installed.
        pydantic.ValidationError: An installed fact breaks a record grammar.
    """
    runtime_id = launcher.runtime_id
    if row is None:
        raise _UnboundError(f"never_certified: no certification row names {runtime_id}")
    if row.outcome != "certified" or row.expires_at is None:
        raise _UnboundError(f"quarantined: {row.certification_urn} quarantines {runtime_id}")
    if row.expires_at <= now:
        raise _UnboundError(
            f"expired: {row.certification_urn} expired at {row.expires_at:%Y-%m-%d}"
        )
    return RuntimeBinding(
        driver_ref=launcher.driver_ref,
        manifest=_installed_manifest(launcher),
        certification_ref=row.certification_urn,
        certification_digest=canonical_digest(row.model_dump(mode="json")),
        auth_kind=MACHINE_CERTIFIED_AUTH_KIND,
        listed_models=launcher.listed_models,
        certified_models=launcher.listed_models,
        capabilities=tuple(_observation(item, row.certification_urn) for item in row.capabilities),
    )


def _latest_rows(rows: Iterable[MachineCertification]) -> dict[str, MachineCertification]:
    """Return each runtime's most recently verified row; a tie keeps the later append."""
    latest: dict[str, MachineCertification] = {}
    for row in rows:
        held = latest.get(row.runtime_id)
        if held is None or row.verified_at >= held.verified_at:
            latest[row.runtime_id] = row
    return latest


def _merged_records(built_in: Iterable[Mapping[str, Any]], declared: object, id_key: str) -> object:
    """File the built-in records beneath the operator's, which win by id.

    A value that is not a list is returned unchanged for the provider loader
    to refuse with its own code.
    """
    if not isinstance(declared, list):
        return declared
    taken = {entry.get(id_key) for entry in declared if isinstance(entry, Mapping)}
    return [dict(entry) for entry in built_in if entry[id_key] not in taken] + declared


def _provider_config(repository_root: Path) -> dict[str, dict[str, Any]]:
    """Return the global and repository provider layers of the tree at *repository_root*.

    Raises:
        ProviderConfigError: A layer file is unreadable, or its ``runtime``
            entry is not a mapping.
    """
    defaults = built_in_defaults()["runtime"]
    global_runtime = dict(read_runtime_section("global", global_config_path()))
    global_runtime["profiles"] = _merged_records(
        defaults["profiles"], global_runtime.get("profiles", []), "profile_id"
    )
    global_runtime["routes"] = _merged_records(
        defaults["routes"], global_runtime.get("routes", []), "route_id"
    )
    repository_runtime = read_runtime_section("repository", repo_config_path(repository_root))
    return {
        "global": {"runtime": global_runtime},
        "repository": {"runtime": dict(repository_runtime)},
    }


def installed_provider_facts(tree_root: Path, *, now: datetime) -> InstalledProviderFacts:
    """Return the registry, bindings and provider layers of the tree at *tree_root*.

    Args:
        tree_root: The tree's ``.ea`` directory.
        now: The instant a certification's expiry is judged at.

    Returns:
        The facts a Run of this tree compiles against. A runtime with no
        current certification, or whose SDK is not installed, has no
        binding; the reason is logged and kept in ``unbound``.

    Raises:
        ValueError: *now* is naive.
        ProviderConfigError: A provider layer file is unreadable.
        pydantic.ValidationError: A shipped manifest or a certification row
            does not validate.
    """
    if now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    launchers = shipped_launcher_manifests()
    latest = _latest_rows(read_machine_certifications(tree_root / "state.json"))
    bindings: list[RuntimeBinding] = []
    unbound: dict[str, str] = {}
    for launcher in launchers:
        runtime_id = launcher.runtime_id
        package = launcher.distribution.package
        try:
            bindings.append(_binding(launcher, latest.get(runtime_id), now))
        except _UnboundError as reason:
            logger.info(f"installed_provider_facts unbound runtime={runtime_id} reason={reason}")
            remedy = (
                f"install the {package} package"
                if str(reason).startswith("driver_not_installed")
                else f"run `eawf runtime certify {runtime_id}`"
            )
            unbound[launcher.provider_id] = f"runtime {runtime_id} is unbound ({reason}); {remedy}"
        except PydanticValidationError as error:
            logger.warning(
                f"installed_provider_facts unbound runtime={runtime_id} "
                f"reason=installed_fact_invalid errors={error.error_count()}"
            )
            unbound[launcher.provider_id] = (
                f"runtime {runtime_id} is unbound (installed_fact_invalid: "
                f"{error.error_count()} errors); reinstall the {package} package"
            )
    registry = ProviderRegistry(
        driver_refs=frozenset(launcher.driver_ref for launcher in launchers),
        auth_profile_refs=frozenset(launcher.auth_profile_ref for launcher in launchers),
        certification_capabilities=CERTIFIABLE_CAPABILITIES,
    )
    logger.debug(
        f"installed_provider_facts bound={[b.manifest.provider_id for b in bindings]} "
        f"launchers={len(launchers)}"
    )
    return InstalledProviderFacts(
        registry=registry,
        bindings=tuple(bindings),
        config=_provider_config(tree_root.parent),
        unbound=unbound,
    )


__all__ = [
    "CERTIFIABLE_CAPABILITIES",
    "LAUNCHER_PACKAGES",
    "MACHINE_CERTIFIED_AUTH_KIND",
    "InstalledProviderFacts",
    "LauncherManifest",
    "ShippedDistribution",
    "installed_provider_facts",
    "shipped_launcher_manifests",
]
