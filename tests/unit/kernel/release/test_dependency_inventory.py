"""The lock-derived inventory and the ``dependencies.inventory`` component.

Every assertion here is about a manifest built from lock *text* rather
than from the repo's own ``uv.lock``: the checked-in lock changes with
every dependency bump, and a test that reads it would either drift or
pin the tree, neither of which says anything about the classifier.

The wiring cases at the end are the ones that matter most. A producer
that exists but is not in
:data:`~eawf.workflow.release.producers.DEFAULT_RELEASE_PROBES` leaves
the ``dependencies`` row exactly as ``unavailable`` as it was before the
producer was written.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from eawf.kernel.release.signals import (
    ReleaseSignalContext,
    ReleaseSignalFailureCode,
    ReleaseSignalName,
    ReleaseSignalStatus,
)
from eawf.workflow.release.dependencies import (
    DEFAULT_LICENSE_ALLOWLIST,
    INVENTORY_COMPONENT,
    DependencyFinding,
    DependencyInventoryInputs,
    LicenseDisposition,
    LockedPackage,
    ReleaseDependencyManifest,
    build_dependency_manifest,
    classify_license,
    compute_lock_digest,
    inventory_component,
    normalized_name,
)
from eawf.workflow.release.producers import (
    DEFAULT_RELEASE_PROBES,
    build_receipt_probes,
    dependencies_probe,
)
from eawf.workflow.release.receipts import receipt_path, write_receipt
from eawf.workflow.release.vulnerability import VulnerabilityReport
from tests.unit.kernel.release.conftest import dev1_config

pytestmark = pytest.mark.unit

#: A two-package lock: the smallest text that still exercises ordering,
#: normalization and the per-row license join.
LOCK = """\
version = 1
requires-python = ">=3.14"

[[package]]
name = "Pydantic_Core"
version = "2.33.2"
source = { registry = "https://pypi.org/simple" }

[[package]]
name = "annotated-types"
version = "0.7.0"
source = { registry = "https://pypi.org/simple" }
"""

SINGLE_LOCK = """\
version = 1

[[package]]
name = "typer"
version = "0.19.1"
"""


def _manifest(**overrides: object) -> ReleaseDependencyManifest:
    """Return a clean two-row manifest with *overrides* applied."""
    payload: dict[str, object] = {
        "lock_digest": f"sha256:{'a' * 64}",
        "packages": (
            LockedPackage(
                name="annotated-types",
                version="0.7.0",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
            LockedPackage(
                name="pydantic-core",
                version="2.33.2",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
        ),
        "imported_distributions": ("pydantic-core",),
    }
    payload.update(overrides)
    return ReleaseDependencyManifest(**payload)  # type: ignore[arg-type]


def _inputs(manifest: ReleaseDependencyManifest) -> DependencyInventoryInputs:
    """Return inputs whose wheel digest agrees with *manifest*."""
    return DependencyInventoryInputs(manifest=manifest, wheel_lock_digest=manifest.lock_digest)


# --- building the manifest ---------------------------------------------------


def test_build_dependency_manifest_rows_one_per_locked_package() -> None:
    """Every ``[[package]]`` table becomes exactly one normalized row."""
    manifest = build_dependency_manifest(LOCK, licenses={"pydantic-core": "MIT"})
    assert [(row.name, row.version) for row in manifest.packages] == [
        ("annotated-types", "0.7.0"),
        ("pydantic-core", "2.33.2"),
    ]
    assert manifest.lock_digest == compute_lock_digest(LOCK)
    assert manifest.schema_version == "release-dependency-manifest/v1"


def test_build_dependency_manifest_classifies_each_license_against_the_allowlist() -> None:
    """An allowed, a forbidden and an undeclared license land on three rows."""
    manifest = build_dependency_manifest(
        LOCK,
        licenses={"Pydantic-Core": "GPL-3.0-only", "annotated-types": "MIT"},
    )
    dispositions = {row.name: row.disposition for row in manifest.packages}
    assert dispositions["annotated-types"] is LicenseDisposition.ALLOWED
    assert dispositions["pydantic-core"] is LicenseDisposition.FORBIDDEN
    assert manifest.forbidden[0].name == "pydantic-core"
    missing = build_dependency_manifest(LOCK, licenses={})
    assert {row.name for row in missing.undeclared} == {"annotated-types", "pydantic-core"}


def test_build_dependency_manifest_matches_a_license_key_in_any_spelling() -> None:
    """``Pydantic_Core`` in the license map joins the ``pydantic-core`` row."""
    manifest = build_dependency_manifest(LOCK, licenses={"Pydantic_Core": "Apache-2.0"})
    assert manifest.packages[1].license_id == "Apache-2.0"
    assert manifest.packages[1].disposition is LicenseDisposition.ALLOWED


def test_build_dependency_manifest_accepts_a_single_package_lock() -> None:
    """One locked package is a valid inventory -- the minimum non-empty case."""
    manifest = build_dependency_manifest(SINGLE_LOCK, licenses={"typer": "MIT"})
    assert len(manifest.packages) == 1
    assert manifest.packages[0].name == "typer"


def test_build_dependency_manifest_normalizes_the_recorded_imports() -> None:
    """Imports are stored deduplicated, sorted and normalized."""
    manifest = build_dependency_manifest(
        LOCK,
        licenses={},
        imported_distributions=("Pydantic_Core", "pydantic-core", "annotated.types"),
    )
    assert manifest.imported_distributions == ("annotated-types", "pydantic-core")


def test_build_dependency_manifest_rejects_a_lock_with_no_packages() -> None:
    """An empty inventory would pass every check by describing nothing."""
    with pytest.raises(ValueError, match="no \\[\\[package\\]\\] tables"):
        build_dependency_manifest("version = 1\n", licenses={})


def test_build_dependency_manifest_rejects_unparsable_toml() -> None:
    """A lock that is not TOML is a repair task, not an empty inventory."""
    with pytest.raises(ValueError, match="not valid TOML"):
        build_dependency_manifest("[[package]\nname =", licenses={})


def test_build_dependency_manifest_rejects_a_package_without_a_version() -> None:
    """A nameless or versionless table cannot become a row."""
    with pytest.raises(ValueError, match="no name/version"):
        build_dependency_manifest('[[package]]\nname = "typer"\n', licenses={})


def test_build_dependency_manifest_skips_the_editable_workspace_package() -> None:
    """The project itself is not a dependency of itself.

    uv locks the released project with an editable source and no version,
    because its version is whatever the working tree says. Requiring one
    reds the producer on every real lock, which is what kept the
    dependency receipt from ever being written.
    """
    lock = (
        '[[package]]\nname = "eawf"\nsource = { editable = "." }\n\n'
        '[[package]]\nname = "typer"\nversion = "0.25.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    manifest = build_dependency_manifest(lock, licenses={})
    assert [package.name for package in manifest.packages] == ["typer"]


def test_build_dependency_manifest_skips_a_virtual_workspace_package() -> None:
    """A virtual workspace member is locked the same way and skipped too."""
    lock = (
        '[[package]]\nname = "eawf-workspace"\nsource = { virtual = "." }\n\n'
        '[[package]]\nname = "typer"\nversion = "0.25.1"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
    )
    manifest = build_dependency_manifest(lock, licenses={})
    assert [package.name for package in manifest.packages] == ["typer"]


def test_build_dependency_manifest_still_rejects_a_versionless_registry_package() -> None:
    """The skip is narrow: only an editable/virtual source excuses no version.

    A registry distribution with no resolved version is a broken lock, and
    silently dropping it would understate what the release ships.
    """
    lock = '[[package]]\nname = "typer"\nsource = { registry = "https://pypi.org/simple" }\n'
    with pytest.raises(ValueError, match="no name/version"):
        build_dependency_manifest(lock, licenses={})


def test_build_dependency_manifest_rejects_a_non_string_lock() -> None:
    """The lock arrives as decoded text; bytes are a caller bug."""
    with pytest.raises(TypeError, match="lock_text must be str"):
        build_dependency_manifest(b"version = 1", licenses={})  # type: ignore[arg-type]


# --- digest and classification helpers ---------------------------------------


def test_compute_lock_digest_ignores_the_checkout_line_endings() -> None:
    """A CRLF checkout must digest the same as an LF one."""
    assert compute_lock_digest("a\r\nb\n") == compute_lock_digest("a\nb\n")
    assert compute_lock_digest(LOCK).startswith("sha256:")


def test_compute_lock_digest_rejects_a_non_string() -> None:
    """Digesting bytes would silently produce a different digest."""
    with pytest.raises(TypeError, match="lock_text must be str"):
        compute_lock_digest(b"version = 1")  # type: ignore[arg-type]


def test_classify_license_is_case_insensitive_over_the_allowlist() -> None:
    """``mit`` and ``MIT`` are the same license."""
    assert classify_license("mit") is LicenseDisposition.ALLOWED
    assert classify_license("  MIT  ") is LicenseDisposition.ALLOWED
    assert classify_license("") is LicenseDisposition.UNDECLARED
    assert classify_license("SSPL-1.0") is LicenseDisposition.FORBIDDEN
    assert "MIT" in DEFAULT_LICENSE_ALLOWLIST


def test_classify_license_rejects_a_non_string() -> None:
    """A ``None`` license must not classify as undeclared by accident."""
    with pytest.raises(TypeError, match="license_id must be str"):
        classify_license(None)  # type: ignore[arg-type]


def test_normalized_name_rejects_a_non_string() -> None:
    """A non-string distribution name is a caller bug, not an empty name."""
    with pytest.raises(TypeError, match="name must be str"):
        normalized_name(None)  # type: ignore[arg-type]


# --- record validation -------------------------------------------------------


def test_locked_package_rejects_a_disposition_contradicting_its_license() -> None:
    """An undeclared license cannot carry a decided disposition."""
    with pytest.raises(ValidationError, match="classified"):
        LockedPackage(
            name="typer", version="0.19.1", license_id="", disposition=LicenseDisposition.ALLOWED
        )


def test_release_dependency_manifest_rejects_a_duplicated_package() -> None:
    """One package locked twice would be counted twice by every check."""
    row = LockedPackage(
        name="typer", version="0.19.1", license_id="MIT", disposition=LicenseDisposition.ALLOWED
    )
    with pytest.raises(ValidationError, match="more than once"):
        _manifest(packages=(row, row))


def test_release_dependency_manifest_rejects_unsorted_rows() -> None:
    """Row order is part of the record so two manifests can be diffed."""
    with pytest.raises(ValidationError, match="sorted by package name"):
        _manifest(packages=tuple(reversed(_manifest().packages)))


def test_release_dependency_manifest_rejects_an_empty_package_set() -> None:
    """Zero rows is the boundary an empty inventory would sneak through."""
    with pytest.raises(ValidationError):
        _manifest(packages=())


def test_release_dependency_manifest_forbids_an_unknown_key() -> None:
    """A typo in a written receipt must fail at load, not drift through."""
    with pytest.raises(ValidationError):
        _manifest(lock_sha="deadbeef")


# --- the inventory component -------------------------------------------------


def test_inventory_component_passes_on_a_clean_manifest() -> None:
    """Allowed licenses, locked imports and an agreeing digest are green."""
    outcome = inventory_component(_inputs(_manifest()))
    assert outcome.status is ReleaseSignalStatus.PASS
    assert outcome.component == INVENTORY_COMPONENT == "dependencies.inventory"
    assert outcome.failure_code is None
    assert outcome.findings == ()
    assert f"lock_digest:{'sha256:' + 'a' * 64}" in outcome.evidence_refs


def test_inventory_component_reports_an_undeclared_license_without_blocking() -> None:
    """An unknown license is surfaced in the evidence, not turned into a red."""
    manifest = _manifest(
        packages=(
            LockedPackage(
                name="annotated-types",
                version="0.7.0",
                license_id="",
                disposition=LicenseDisposition.UNDECLARED,
            ),
        ),
        imported_distributions=(),
    )
    outcome = inventory_component(_inputs(manifest))
    assert outcome.status is ReleaseSignalStatus.PASS
    assert "undeclared_license:annotated-types" in outcome.evidence_refs


def test_inventory_component_fails_on_a_forbidden_license() -> None:
    """A package outside the allowlist reds the component."""
    manifest = _manifest(
        packages=(
            LockedPackage(
                name="annotated-types",
                version="0.7.0",
                license_id="MIT",
                disposition=LicenseDisposition.ALLOWED,
            ),
            LockedPackage(
                name="pydantic-core",
                version="2.33.2",
                license_id="SSPL-1.0",
                disposition=LicenseDisposition.FORBIDDEN,
            ),
        )
    )
    outcome = inventory_component(_inputs(manifest))
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.failure_code is ReleaseSignalFailureCode.DEPENDENCY_GATE_FAILED
    assert outcome.findings == (DependencyFinding.FORBIDDEN_LICENSE,)
    assert "SSPL-1.0" in outcome.remediation
    assert "forbidden_license:pydantic-core:SSPL-1.0" in outcome.evidence_refs


def test_inventory_component_fails_on_an_unlocked_import() -> None:
    """An import the lock does not pin reds the component and is named."""
    manifest = _manifest(imported_distributions=("pydantic-core", "requests"))
    outcome = inventory_component(_inputs(manifest))
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.failure_code is ReleaseSignalFailureCode.DEPENDENCY_GATE_FAILED
    assert outcome.findings == (DependencyFinding.UNLOCKED_IMPORT,)
    assert "unlocked_import:requests" in outcome.evidence_refs


def test_inventory_component_fails_on_a_lock_digest_mismatch() -> None:
    """A wheel built from another lock reds before any license is judged.

    The forbidden row is deliberately present: a digest mismatch means
    the manifest describes a different build, so its license findings
    are about the wrong artifact and must not be reported instead.
    """
    manifest = _manifest(
        packages=(
            LockedPackage(
                name="pydantic-core",
                version="2.33.2",
                license_id="SSPL-1.0",
                disposition=LicenseDisposition.FORBIDDEN,
            ),
        )
    )
    outcome = inventory_component(
        DependencyInventoryInputs(manifest=manifest, wheel_lock_digest=f"sha256:{'b' * 64}")
    )
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.findings == (DependencyFinding.LOCK_DIGEST_MISMATCH,)
    assert outcome.failure_code is ReleaseSignalFailureCode.DEPENDENCY_GATE_FAILED


def test_inventory_component_rejects_an_untyped_argument() -> None:
    """Handing the component a bare manifest is a caller bug, not a pass."""
    with pytest.raises(TypeError, match="DependencyInventoryInputs"):
        inventory_component(_manifest())  # type: ignore[arg-type]


# --- wiring ------------------------------------------------------------------


def _context() -> ReleaseSignalContext:
    """Return a ``dependencies`` request against the authored dev1 config."""
    return ReleaseSignalContext(dev1_config(), ReleaseSignalName.DEPENDENCIES, None)


def test_default_release_probes_registers_the_dependencies_producer() -> None:
    """The sweep reads this producer without any injection."""
    assert DEFAULT_RELEASE_PROBES[ReleaseSignalName.DEPENDENCIES] is dependencies_probe


def test_dependencies_probe_is_unavailable_when_no_receipt_was_written() -> None:
    """A producer that did not run leaves the row unproven, not green."""
    probe = build_receipt_probes(Path("/nonexistent-checkout"))[ReleaseSignalName.DEPENDENCIES]
    outcome = probe(_context())
    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "dependency-manifest" in outcome.remediation
    assert "inventory-and-reproducibility" in outcome.remediation


def test_dependencies_probe_is_unavailable_when_only_the_manifest_landed(
    tmp_path: Path,
) -> None:
    """Half a dependency verdict is a different claim, not a weaker one."""
    write_receipt(tmp_path, "dependency-manifest", _manifest())
    outcome = dependencies_probe(_context(), repo_root=tmp_path)
    assert outcome.status is ReleaseSignalStatus.UNAVAILABLE
    assert "vulnerability-report" in outcome.remediation


def test_dependencies_probe_merges_both_components_from_the_receipts(
    tmp_path: Path,
) -> None:
    """Both receipts present and clean makes the shared row pass."""
    manifest = _manifest()
    write_receipt(tmp_path, "dependency-manifest", manifest)
    write_receipt(
        tmp_path,
        "vulnerability-report",
        VulnerabilityReport(lock_digest=manifest.lock_digest),
    )
    outcome = dependencies_probe(_context(), repo_root=tmp_path)
    assert outcome.status is ReleaseSignalStatus.PASS
    assert f"lock_digest:{manifest.lock_digest}" in outcome.evidence_refs


def test_dependencies_probe_names_the_failing_component(tmp_path: Path) -> None:
    """A red inventory reaches the row prefixed with the component it came from."""
    manifest = _manifest(imported_distributions=("requests",))
    write_receipt(tmp_path, "dependency-manifest", manifest)
    write_receipt(
        tmp_path,
        "vulnerability-report",
        VulnerabilityReport(lock_digest=manifest.lock_digest),
    )
    outcome = dependencies_probe(_context(), repo_root=tmp_path)
    assert outcome.status is ReleaseSignalStatus.FAIL
    assert outcome.remediation.startswith("dependencies.inventory:")
    assert "requests" in outcome.remediation


def test_dependencies_probe_refuses_a_malformed_receipt(tmp_path: Path) -> None:
    """A corrupt receipt is a repair task; reading it as absent would hide it."""
    path = receipt_path(tmp_path, "dependency-manifest")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"lock_digest": "not-a-digest"}), encoding="utf-8")
    with pytest.raises(ValueError, match="does not validate"):
        dependencies_probe(_context(), repo_root=tmp_path)


def test_classify_license_accepts_non_canonical_spellings() -> None:
    """Real metadata spells permissive licenses many ways.

    Package metadata is hand-written and predates PEP 639, so the same
    license arrives as "BSD", "Apache 2.0" or "Mozilla Public License
    2.0 (MPL 2.0)". Normalising here keeps the allowlist canonical SPDX
    instead of an ever-growing set of observed strings. Every id below
    was read off a real distribution in this project's own lock.
    """
    for declared in (
        "BSD",
        "BSD License",
        "Apache 2.0",
        "Apache Software License",
        "ISC License",
        "MIT License",
        "Mozilla Public License 2.0 (MPL 2.0)",
    ):
        assert classify_license(declared) is LicenseDisposition.ALLOWED, declared


def test_classify_license_reads_spdx_or_as_a_choice() -> None:
    """``A OR B`` offers a choice, so one permitted operand suffices."""
    assert classify_license("Apache-2.0 OR BSD-2-Clause") is LicenseDisposition.ALLOWED
    assert classify_license("MIT OR GPL-3.0-only") is LicenseDisposition.ALLOWED


def test_classify_license_reads_spdx_and_as_a_conjunction() -> None:
    """``A AND B`` imposes both, so every operand must be permitted."""
    assert classify_license("MPL-2.0 AND (Apache-2.0 OR MIT)") is LicenseDisposition.ALLOWED
    assert classify_license("MIT AND GPL-3.0-only") is LicenseDisposition.FORBIDDEN


def test_classify_license_still_refuses_copyleft_and_unknown() -> None:
    """The gate keeps its teeth: normalisation widened spelling, not policy.

    A classifier lenient enough to accept every spelling is worthless if
    it also waves through what the allowlist exists to stop.
    """
    for declared in ("GPL-3.0-only", "AGPL-3.0", "UNKNOWN", "SSPL-1.0"):
        assert classify_license(declared) is LicenseDisposition.FORBIDDEN, declared
