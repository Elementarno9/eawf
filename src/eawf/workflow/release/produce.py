"""The CI producer that writes all three release receipts.

The inventory, the audit and the double-build receipt each need
something a readiness sweep does not have: a resolved environment to
read licenses from, an advisory database to query, and two clean builds.
This module is what the ``inventory-and-reproducibility`` job runs to
get all three, in one pass, against one commit.

It is deliberately a ``python -m`` entry point rather than a CLI verb.
The producer has exactly one caller -- a CI job -- and giving it a
command would put a surface in front of operators that nobody should be
running by hand: a receipt produced from a dirty laptop tree is the
thing the ``artifacts`` signal exists to reject.

The import closure is read from the source with :mod:`ast` rather than
from the declared dependency list, because the two can disagree and only
the disagreement is interesting: a package declared but never imported
is dead weight, while a module imported but never locked is the defect
:attr:`~eawf.workflow.release.dependencies.DependencyFinding.UNLOCKED_IMPORT`
names.
"""

from __future__ import annotations

import argparse
import ast
import logging
import sys
from collections.abc import Iterable, Sequence
from importlib.metadata import distributions, packages_distributions
from pathlib import Path
from typing import Final

from eawf.workflow.release.dependencies import (
    LOCK_FILENAME,
    ReleaseDependencyManifest,
    build_dependency_manifest,
    normalized_name,
)
from eawf.workflow.release.receipts import write_receipt
from eawf.workflow.release.reproducibility import (
    ReproducibleBuildReceipt,
    double_build_receipt,
    source_date_epoch,
)
from eawf.workflow.release.vulnerability import VulnerabilityReport, run_pip_audit

logger = logging.getLogger(__name__)

#: Package whose imports are inventoried, relative to the repo root.
SOURCE_PACKAGE: Final[str] = "src/eawf"

#: Distribution metadata keys carrying an SPDX identifier, in the order
#: they are trusted. ``License-Expression`` is PEP 639 and is the only
#: one guaranteed to be an SPDX expression; ``License`` is free text
#: that is *often* an identifier and is accepted only when it is short
#: enough to be one rather than a pasted license body.
LICENSE_KEYS: Final[tuple[str, ...]] = ("License-Expression", "License")

#: Longest ``License`` value still treated as an identifier rather than
#: as an inlined license text.
MAX_LICENSE_ID_LENGTH: Final[int] = 64

#: Prefix of the trove classifiers that name a license.
LICENSE_CLASSIFIER_PREFIX: Final[str] = "License :: "


def _license_from_classifiers(classifiers: Iterable[str]) -> str:
    """Return the SPDX-ish tail of the first license classifier, or ``""``."""
    for classifier in classifiers:
        if classifier.startswith(LICENSE_CLASSIFIER_PREFIX):
            tail = classifier.rsplit("::", 1)[-1].strip()
            if tail and tail.lower() != "osi approved":
                return tail.removesuffix(" License").strip()
    return ""


def installed_licenses() -> dict[str, str]:
    """Return the SPDX identifier of every installed distribution.

    Returns:
        Normalized distribution name -> identifier. A distribution whose
        metadata carries none is absent from the map, which the manifest
        records as :attr:`LicenseDisposition.UNDECLARED` rather than
        guessing.
    """
    found: dict[str, str] = {}
    for dist in distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        declared = ""
        for key in LICENSE_KEYS:
            value = dist.metadata.get(key) or ""
            if value and len(value) <= MAX_LICENSE_ID_LENGTH and "\n" not in value:
                declared = value.strip()
                break
        declared = declared or _license_from_classifiers(dist.metadata.get_all("Classifier") or [])
        if declared:
            found[normalized_name(name)] = declared
    return found


def _top_level_imports(source: str) -> set[str]:
    """Return the root module of every absolute import in *source*.

    Args:
        source: Python source text.

    Returns:
        Root module names; empty when the text does not parse, since a
        file the inventory cannot read is a syntax problem the test
        suite already owns rather than a dependency finding.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".", 1)[0])
    return roots


def imported_distributions(repo_root: Path, *, first_party: str = "eawf") -> tuple[str, ...]:
    """Return the third-party distributions the source package imports.

    Args:
        repo_root: Checkout whose source package is walked.
        first_party: Top-level package that is this project's own.

    Returns:
        Normalized distribution names, sorted. A module that maps to no
        installed distribution is returned under its own name, so an
        import of something nobody installed shows up as unlocked rather
        than vanishing.
    """
    provided = packages_distributions()
    roots: set[str] = set()
    for path in sorted((repo_root / SOURCE_PACKAGE).rglob("*.py")):
        roots.update(_top_level_imports(path.read_text(encoding="utf-8")))
    third_party = roots - set(sys.stdlib_module_names) - {first_party, "__future__"}
    names: set[str] = set()
    for module in third_party:
        names.update(normalized_name(dist) for dist in provided.get(module, [module]))
    return tuple(sorted(names))


def produce_dependency_manifest(repo_root: Path) -> ReleaseDependencyManifest:
    """Return the inventory of the checkout at *repo_root*.

    Args:
        repo_root: Checkout whose lock and environment are read.

    Returns:
        The manifest.

    Raises:
        FileNotFoundError: When the checkout carries no lock file.
    """
    lock = repo_root / LOCK_FILENAME
    manifest = build_dependency_manifest(
        lock.read_text(encoding="utf-8"),
        licenses=installed_licenses(),
        imported_distributions=imported_distributions(repo_root),
    )
    logger.info(
        f"produce_dependency_manifest packages={len(manifest.packages)} "
        f"imports={len(manifest.imported_distributions)}"
    )
    return manifest


def produce_vulnerability_report(repo_root: Path, *, lock_digest: str) -> VulnerabilityReport:
    """Return the audit of the exported lock at *repo_root*.

    Args:
        repo_root: Checkout whose lock is exported and audited.
        lock_digest: Digest of that lock, carried onto the report.

    Returns:
        The report.

    Raises:
        ValueError: When the export or the audit does not produce a
            parsable report.
    """
    exported = repo_root / "dist" / "release-requirements.txt"
    exported.parent.mkdir(parents=True, exist_ok=True)
    return run_pip_audit(repo_root, lock_digest=lock_digest, requirements_path=exported)


def produce_build_receipt(repo_root: Path, *, source_sha: str) -> ReproducibleBuildReceipt:
    """Return the double-build receipt for *source_sha*.

    Args:
        repo_root: Checkout that is built twice.
        source_sha: The commit being built, whose committer timestamp
            becomes ``SOURCE_DATE_EPOCH``.

    Returns:
        The receipt.

    Raises:
        ValueError: When git cannot resolve *source_sha*, or a build
            produces no wheel or sdist.
    """
    epoch = source_date_epoch(repo_root, source_sha)
    return double_build_receipt(
        repo_root,
        source_sha=source_sha,
        epoch=epoch,
        out_root=repo_root / "dist" / "reproducibility",
    )


def produce_release_receipts(repo_root: Path, *, source_sha: str) -> tuple[Path, ...]:
    """Write all three receipts for *source_sha* and return their paths.

    Args:
        repo_root: Checkout the receipts describe.
        source_sha: The 40-hex commit being released.

    Returns:
        The written paths, in inventory / audit / build order.

    Raises:
        ValueError: When any producer cannot establish its fact.
    """
    manifest = produce_dependency_manifest(repo_root)
    report = produce_vulnerability_report(repo_root, lock_digest=manifest.lock_digest)
    receipt = produce_build_receipt(repo_root, source_sha=source_sha)
    written = (
        write_receipt(repo_root, "dependency-manifest", manifest),
        write_receipt(repo_root, "vulnerability-report", report),
        write_receipt(repo_root, "reproducible-build-receipt", receipt),
    )
    logger.info(
        f"produce_release_receipts source_sha={source_sha[:12]!r} "
        f"blocking_advisories={len(report.blocking)} reproduced={receipt.reproduced}"
    )
    return written


def main(argv: Sequence[str] | None = None) -> int:
    """Write the three receipts for the commit named on the command line.

    Args:
        argv: Argument vector; defaults to the process arguments.

    Returns:
        ``0`` when all three receipts were written, ``1`` when a
        producer could not establish its fact. The exit code does not
        report whether the receipts are *green*: judging them is the
        readiness sweep's job, and a producer that refused to write a
        red receipt would be hiding the finding.
    """
    parser = argparse.ArgumentParser(prog="eawf-release-receipts")
    parser.add_argument("--source-sha", required=True, help="commit the receipts are for")
    parser.add_argument("--repo-root", default=".", help="checkout to produce from")
    args = parser.parse_args(argv)
    try:
        written = produce_release_receipts(
            Path(args.repo_root).resolve(), source_sha=args.source_sha
        )
    except (OSError, ValueError) as exc:
        print(f"release receipt producer failed: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


__all__ = [
    "LICENSE_KEYS",
    "SOURCE_PACKAGE",
    "imported_distributions",
    "installed_licenses",
    "main",
    "produce_build_receipt",
    "produce_dependency_manifest",
    "produce_release_receipts",
    "produce_vulnerability_report",
]


if __name__ == "__main__":
    raise SystemExit(main())
