"""The lock-derived dependency inventory behind ``dependencies.inventory``.

The ``dependency_inventory`` gate of the ``dev1`` profile reads the
``dependencies.inventory`` component, and until this module landed that
component had no producer: the gate reported ``unavailable``, which is
honest but means the release ships whatever the resolver happened to
pick, under whatever licenses those packages happen to carry.

This module produces the fact the component reads. It turns ``uv.lock``
into a :class:`ReleaseDependencyManifest` -- a digest of the lock plus
one row per locked package carrying its version and its license
classified against a declared allowlist -- and then judges that manifest
in :func:`inventory_component`.

Three things red the component, and they are three different failures:

* a **forbidden license** -- the release would ship a package whose terms
  the project has not agreed to;
* an **unlocked import** -- the source imports a distribution the lock
  does not pin, so the built wheel depends on whatever is installed;
* a **lock digest mismatch** -- the wheel was built from a different lock
  than the one inventoried, so the inventory describes a different
  artifact than the one about to be published.

A package whose license metadata is absent is classified
:attr:`LicenseDisposition.UNDECLARED` and *reported* rather than
blocking. An unknown license is a question for the project's own
research, not a defect in this release, and reddening on it would make
the gate un-passable for reasons the release cannot fix.
"""

from __future__ import annotations

import hashlib
import logging
import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
    component_ref,
)
from eawf.kernel.spec.common import _StrictModel

logger = logging.getLogger(__name__)

#: Lock file the inventory is derived from, relative to the repo root.
LOCK_FILENAME: Final[str] = "uv.lock"

#: The qualified component this module produces.
INVENTORY_COMPONENT: Final[str] = component_ref(ReleaseSignalName.DEPENDENCIES, "inventory")

#: SPDX identifiers the project has agreed to ship. Spelled here rather
#: than read from the checkpoint file because adding a license is a legal
#: decision about the whole project, not a per-release configuration
#: knob: a release that needs a new license needs this constant edited
#: and reviewed, which is exactly the friction the allowlist is for.
DEFAULT_LICENSE_ALLOWLIST: Final[frozenset[str]] = frozenset(
    {
        "0BSD",
        "APACHE-2.0",
        "BSD-2-CLAUSE",
        "BSD-3-CLAUSE",
        "ISC",
        "MIT",
        "MIT-CMU",
        "MPL-2.0",
        "PSF-2.0",
        "PYTHON-2.0",
        "UNLICENSE",
    }
)

#: Matches a ``sha256:<64 hex>`` digest reference.
_DIGEST_PATTERN: Final[str] = r"^sha256:[0-9a-f]{64}$"

#: Collapses the PEP 503 separator run so ``Foo_Bar.Baz`` and
#: ``foo-bar-baz`` compare equal.
_SEPARATOR_RUN: Final[re.Pattern[str]] = re.compile(r"[-_.]+")


class LicenseDisposition(StrEnum):
    """How one package's license compares to the declared allowlist.

    Values:
        ALLOWED: The license is on the allowlist.
        FORBIDDEN: The license is declared and is not on the allowlist.
        UNDECLARED: The package carries no license metadata, so nothing
            can be said either way.
    """

    ALLOWED = "allowed"
    FORBIDDEN = "forbidden"
    UNDECLARED = "undeclared"


class DependencyFinding(StrEnum):
    """Closed vocabulary for why the ``dependencies`` row went red.

    Values:
        FORBIDDEN_LICENSE: A locked package's license is not allowed.
        UNLOCKED_IMPORT: The source imports a distribution the lock does
            not pin.
        LOCK_DIGEST_MISMATCH: The built wheel records a different lock
            than the one inventoried.
        BLOCKING_ADVISORY: A blocking vulnerability advisory is open.
    """

    FORBIDDEN_LICENSE = "forbidden_license"
    UNLOCKED_IMPORT = "unlocked_import"
    LOCK_DIGEST_MISMATCH = "lock_digest_mismatch"
    BLOCKING_ADVISORY = "blocking_advisory"


@dataclass(frozen=True, slots=True)
class ComponentOutcome:
    """One component's verdict on the row it shares with another.

    The ``dependencies`` row carries two independent checks, so neither
    can return a whole-row :class:`ReleaseSignalOutcome` without
    overwriting the other's verdict. Each returns this instead and the
    probe merges them.

    Attributes:
        component: Qualified ``<signal>.<component>`` reference.
        status: The component's verdict.
        failure_code: The row's failure code; present exactly when the
            component is not passing.
        findings: Which named failures fired, in detection order.
        remediation: Operator next action; non-empty when not passing.
        evidence_refs: References backing the verdict.
    """

    component: str
    status: ReleaseSignalStatus
    failure_code: ReleaseSignalFailureCode | None
    findings: tuple[DependencyFinding, ...]
    remediation: str
    evidence_refs: tuple[str, ...]

    @property
    def passing(self) -> bool:
        """Return whether this component cleared its check."""
        return self.status is ReleaseSignalStatus.PASS


def normalized_name(name: str) -> str:
    """Return *name* in PEP 503 normalized form.

    Args:
        name: A distribution name in any spelling.

    Returns:
        Lowercase with every separator run collapsed to ``-``.

    Raises:
        TypeError: When *name* is not a string.
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be str; got {type(name).__name__}")
    return _SEPARATOR_RUN.sub("-", name).strip().lower()


def classify_license(
    license_id: str,
    *,
    allowlist: frozenset[str] = DEFAULT_LICENSE_ALLOWLIST,
) -> LicenseDisposition:
    """Return how *license_id* compares to *allowlist*.

    Args:
        license_id: SPDX identifier as the package declares it; the
            empty string means the package declared none.
        allowlist: Identifiers the project has agreed to ship, compared
            case-insensitively.

    Returns:
        The disposition.

    Raises:
        TypeError: When *license_id* is not a string.
    """
    if not isinstance(license_id, str):
        raise TypeError(f"license_id must be str; got {type(license_id).__name__}")
    declared = license_id.strip()
    if not declared:
        return LicenseDisposition.UNDECLARED
    permitted = {entry.upper() for entry in allowlist}
    return (
        LicenseDisposition.ALLOWED
        if declared.upper() in permitted
        else LicenseDisposition.FORBIDDEN
    )


class LockedPackage(_StrictModel):
    """One row of the inventory: a pinned package and its license.

    Attributes:
        name: PEP 503 normalized distribution name.
        version: The pinned version, verbatim from the lock.
        license_id: SPDX identifier, or the empty string when the
            package declares none.
        disposition: How :attr:`license_id` compares to the allowlist.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: Annotated[str, Field(pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")]
    version: Annotated[str, Field(min_length=1)]
    license_id: str = ""
    disposition: LicenseDisposition

    @model_validator(mode="after")
    def _disposition_matches_the_license(self) -> LockedPackage:
        """Reject a row whose disposition contradicts its license text.

        Raises:
            ValueError: When an undeclared license carries a decided
                disposition, or a declared one is called undeclared.
        """
        undeclared = not self.license_id.strip()
        if undeclared is not (self.disposition is LicenseDisposition.UNDECLARED):
            raise ValueError(
                f"package {self.name!r} declares license {self.license_id!r} but is "
                f"classified {self.disposition.value!r}"
            )
        return self


class ReleaseDependencyManifest(_StrictModel):
    """Every package the release locks, with its license disposition.

    Attributes:
        schema_version: Record schema tag.
        lock_digest: ``sha256:<hex>`` of the lock the rows came from.
            The pin is the point: an inventory that does not name the
            lock it describes cannot be checked against the wheel that
            was actually built.
        packages: One row per locked package, sorted by name.
        imported_distributions: Third-party distributions the source
            tree imports, as the producer that walked the tree found
            them. Carried here because "what is locked" is only half an
            inventory: the other half is what the code actually reaches
            for, and only their difference exposes an unlocked import.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-dependency-manifest/v1"] = "release-dependency-manifest/v1"
    lock_digest: Annotated[str, Field(pattern=_DIGEST_PATTERN)]
    packages: Annotated[tuple[LockedPackage, ...], Field(min_length=1)]
    imported_distributions: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _rows_are_unique_and_ordered(self) -> ReleaseDependencyManifest:
        """Reject a duplicated or unsorted row set.

        Raises:
            ValueError: When a package appears twice, or the rows are
                not in name order.
        """
        names = [package.name for package in self.packages]
        if len(names) != len(set(names)):
            duplicated = sorted({name for name in names if names.count(name) > 1})
            raise ValueError(f"dependency manifest locks {duplicated} more than once")
        if names != sorted(names):
            raise ValueError("dependency manifest rows must be sorted by package name")
        return self

    @property
    def names(self) -> frozenset[str]:
        """Return the normalized name of every locked package."""
        return frozenset(package.name for package in self.packages)

    @property
    def forbidden(self) -> tuple[LockedPackage, ...]:
        """Return the rows whose license is not on the allowlist."""
        return tuple(
            package
            for package in self.packages
            if package.disposition is LicenseDisposition.FORBIDDEN
        )

    @property
    def undeclared(self) -> tuple[LockedPackage, ...]:
        """Return the rows that declare no license at all."""
        return tuple(
            package
            for package in self.packages
            if package.disposition is LicenseDisposition.UNDECLARED
        )


def compute_lock_digest(lock_text: str) -> str:
    """Return the ``sha256:<hex>`` digest of *lock_text*.

    Newlines are normalized to ``\\n`` first so a lock checked out with
    CRLF endings digests the same as one checked out with LF -- the
    inventory would otherwise diverge from the wheel's recorded digest
    on Windows for a reason that has nothing to do with dependencies.

    Args:
        lock_text: The lock file's decoded text.

    Returns:
        The digest reference.

    Raises:
        TypeError: When *lock_text* is not a string.
    """
    if not isinstance(lock_text, str):
        raise TypeError(f"lock_text must be str; got {type(lock_text).__name__}")
    normalized = lock_text.replace("\r\n", "\n").replace("\r", "\n")
    return f"sha256:{hashlib.sha256(normalized.encode('utf-8')).hexdigest()}"


def _package_rows(lock_text: str) -> Sequence[Mapping[str, object]]:
    """Return the ``[[package]]`` tables of *lock_text*.

    Args:
        lock_text: The lock file's decoded text.

    Returns:
        The raw package tables in lock order.

    Raises:
        ValueError: When the lock is not valid TOML or declares no
            packages at all -- an empty inventory would pass every check
            by describing nothing.
    """
    try:
        decoded = tomllib.loads(lock_text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"{LOCK_FILENAME} is not valid TOML: {exc}") from exc
    rows = decoded.get("package")
    if not isinstance(rows, list) or not rows:
        raise ValueError(
            f"{LOCK_FILENAME} declares no [[package]] tables; an empty inventory "
            f"would pass the dependency gate by describing nothing"
        )
    return [row for row in rows if isinstance(row, Mapping)]


def build_dependency_manifest(
    lock_text: str,
    *,
    licenses: Mapping[str, str],
    imported_distributions: Sequence[str] = (),
    allowlist: frozenset[str] = DEFAULT_LICENSE_ALLOWLIST,
) -> ReleaseDependencyManifest:
    """Return the inventory *lock_text* and *licenses* describe.

    The license map is passed in rather than read here because a lock
    file records no license: the identifiers come from the installed
    distributions' metadata, which only the environment that resolved
    the lock can read. Keeping the parse pure of that lookup is what
    lets the whole classification be tested without an install.

    Args:
        lock_text: Contents of ``uv.lock``.
        licenses: SPDX identifier per distribution name, in any
            spelling; a name absent from the map is undeclared.
        imported_distributions: Third-party distributions the source
            imports, in any spelling.
        allowlist: Identifiers the project has agreed to ship.

    Returns:
        The manifest, rows sorted by normalized name.

    Raises:
        TypeError: When *lock_text* is not a string.
        ValueError: When the lock is not valid TOML, declares no
            packages, or carries a package table with no name or no
            version.
    """
    digest = compute_lock_digest(lock_text)
    by_name = {normalized_name(name): value for name, value in licenses.items()}
    packages: list[LockedPackage] = []
    for row in _package_rows(lock_text):
        name = row.get("name")
        version = row.get("version")
        if not isinstance(name, str) or not isinstance(version, str):
            raise ValueError(
                f"{LOCK_FILENAME} carries a [[package]] table with no name/version "
                f"pair: {dict(row)}"
            )
        key = normalized_name(name)
        declared = by_name.get(key, "")
        packages.append(
            LockedPackage(
                name=key,
                version=version,
                license_id=declared,
                disposition=classify_license(declared, allowlist=allowlist),
            )
        )
    manifest = ReleaseDependencyManifest(
        lock_digest=digest,
        packages=tuple(sorted(packages, key=lambda package: package.name)),
        imported_distributions=tuple(sorted({normalized_name(n) for n in imported_distributions})),
    )
    logger.info(
        f"build_dependency_manifest packages={len(manifest.packages)} "
        f"forbidden={len(manifest.forbidden)} undeclared={len(manifest.undeclared)} "
        f"lock_digest={manifest.lock_digest!r}"
    )
    return manifest


@dataclass(frozen=True, slots=True)
class DependencyInventoryInputs:
    """What the inventory component judges.

    Attributes:
        manifest: The lock-derived inventory.
        wheel_lock_digest: The lock digest recorded for the wheel that
            was actually built, which must be the digest the inventory
            carries. The one fact the inventory cannot establish about
            itself: a manifest is always self-consistent, so only an
            outside reading catches one describing a different build.
    """

    manifest: ReleaseDependencyManifest
    wheel_lock_digest: str


def _unlocked_imports(manifest: ReleaseDependencyManifest) -> tuple[str, ...]:
    """Return the imported distributions the manifest does not lock."""
    imported = {normalized_name(name) for name in manifest.imported_distributions}
    return tuple(sorted(imported - manifest.names))


def _red(
    component: str,
    findings: Sequence[DependencyFinding],
    remediation: str,
    evidence: Sequence[str],
) -> ComponentOutcome:
    """Return a failing component outcome carrying the row's failure code."""
    return ComponentOutcome(
        component=component,
        status=ReleaseSignalStatus.FAIL,
        failure_code=ReleaseSignalFailureCode.DEPENDENCY_GATE_FAILED,
        findings=tuple(findings),
        remediation=remediation,
        evidence_refs=tuple(evidence),
    )


def inventory_component(inputs: DependencyInventoryInputs) -> ComponentOutcome:
    """Return the ``dependencies.inventory`` verdict for *inputs*.

    The three checks run in repair order rather than all at once: a lock
    digest mismatch means the manifest describes a different build, so
    its license and import findings are about the wrong artifact and
    reporting them would send the operator to fix the wrong thing.

    Args:
        inputs: The manifest and the two facts it is checked against.

    Returns:
        A passing outcome whose evidence names the lock digest and the
        undeclared-license rows, or a failing one carrying
        ``dependency_gate_failed`` and the finding that fired.

    Raises:
        TypeError: When *inputs* is not a
            :class:`DependencyInventoryInputs`.
    """
    if not isinstance(inputs, DependencyInventoryInputs):
        raise TypeError(f"inputs must be DependencyInventoryInputs; got {type(inputs).__name__}")
    manifest = inputs.manifest
    if inputs.wheel_lock_digest != manifest.lock_digest:
        return _red(
            INVENTORY_COMPONENT,
            [DependencyFinding.LOCK_DIGEST_MISMATCH],
            f"the built wheel records lock digest {inputs.wheel_lock_digest!r} but the "
            f"inventory describes {manifest.lock_digest!r}; rebuild the wheel from the "
            f"inventoried lock, or re-run the inventory against the lock that was built",
            [
                f"lock_digest:{manifest.lock_digest}",
                f"wheel_lock_digest:{inputs.wheel_lock_digest}",
            ],
        )
    forbidden = manifest.forbidden
    if forbidden:
        named = ", ".join(f"{row.name}=={row.version} ({row.license_id})" for row in forbidden)
        return _red(
            INVENTORY_COMPONENT,
            [DependencyFinding.FORBIDDEN_LICENSE],
            f"{len(forbidden)} locked package(s) carry a license outside the allowlist: "
            f"{named}; drop the dependency, replace it, or add the license to the "
            f"declared allowlist with review",
            [f"forbidden_license:{row.name}:{row.license_id}" for row in forbidden],
        )
    unlocked = _unlocked_imports(manifest)
    if unlocked:
        return _red(
            INVENTORY_COMPONENT,
            [DependencyFinding.UNLOCKED_IMPORT],
            f"the source imports {list(unlocked)}, which the lock does not pin; the built "
            f"wheel would depend on whatever happens to be installed -- declare the "
            f"dependency and re-lock before publishing",
            [f"unlocked_import:{name}" for name in unlocked],
        )
    logger.info(
        f"inventory_component packages={len(manifest.packages)} "
        f"imports={len(manifest.imported_distributions)} status='pass'"
    )
    return ComponentOutcome(
        component=INVENTORY_COMPONENT,
        status=ReleaseSignalStatus.PASS,
        failure_code=None,
        findings=(),
        remediation="",
        evidence_refs=(
            f"lock_digest:{manifest.lock_digest}",
            f"locked_packages:{len(manifest.packages)}",
            *(f"undeclared_license:{row.name}" for row in manifest.undeclared),
        ),
    )


__all__ = [
    "DEFAULT_LICENSE_ALLOWLIST",
    "INVENTORY_COMPONENT",
    "LOCK_FILENAME",
    "ComponentOutcome",
    "DependencyFinding",
    "DependencyInventoryInputs",
    "LicenseDisposition",
    "LockedPackage",
    "ReleaseDependencyManifest",
    "build_dependency_manifest",
    "classify_license",
    "compute_lock_digest",
    "inventory_component",
    "normalized_name",
]
