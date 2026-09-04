"""Where the three release receipts are written, and how they are read back.

The inventory, the vulnerability report and the double-build receipt are
produced in CI -- they need a clean checkout, a resolved environment and
two builds, none of which a readiness sweep can conjure. So the sweep
does not compute them; it *reads* them, and this module is the one place
that knows where.

Naming the directory here rather than at each call site is what keeps
the producer and the consumer from drifting: the CI job writes
:data:`RECEIPT_FILENAMES` into :data:`RECEIPT_DIRNAME` and uploads them
under the same three names, and the probes in
:mod:`eawf.workflow.release.producers` look for exactly that. A receipt
that is absent reports ``unavailable`` rather than passing, which is the
honest reading of "the producer did not run".
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from eawf.kernel.spec.common import _StrictModel
from eawf.workflow.release.dependencies import ReleaseDependencyManifest
from eawf.workflow.release.reproducibility import ReproducibleBuildReceipt
from eawf.workflow.release.vulnerability import VulnerabilityReport

logger = logging.getLogger(__name__)

#: Directory the release receipts are written into and uploaded from,
#: relative to the repo root. Under ``dist/`` because the receipts
#: describe the artifacts that sit beside them, and because the tree is
#: already ignored -- a receipt is evidence about a build, not source.
RECEIPT_DIRNAME: Final[str] = "dist/release-receipts"

#: Workflow artifact name -> filename under :data:`RECEIPT_DIRNAME`. The
#: artifact name and the file stem are deliberately the same string: a CI
#: job that uploads one and writes the other cannot silently disagree.
RECEIPT_FILENAMES: Final[Mapping[str, str]] = {
    "dependency-manifest": "dependency-manifest.json",
    "vulnerability-report": "vulnerability-report.json",
    "reproducible-build-receipt": "reproducible-build-receipt.json",
}


def receipt_dir(repo_root: Path) -> Path:
    """Return the receipt directory of the checkout at *repo_root*."""
    return repo_root / RECEIPT_DIRNAME


def receipt_path(repo_root: Path, artifact_name: str) -> Path:
    """Return where the receipt uploaded as *artifact_name* lives.

    Args:
        repo_root: Checkout the receipts belong to.
        artifact_name: One key of :data:`RECEIPT_FILENAMES`.

    Returns:
        The absolute path.

    Raises:
        KeyError: When *artifact_name* is not a declared receipt.
    """
    try:
        filename = RECEIPT_FILENAMES[artifact_name]
    except KeyError as exc:
        raise KeyError(
            f"unknown release receipt {artifact_name!r}; have {sorted(RECEIPT_FILENAMES)}"
        ) from exc
    return receipt_dir(repo_root) / filename


def write_receipt(repo_root: Path, artifact_name: str, record: _StrictModel) -> Path:
    """Write *record* as the receipt uploaded under *artifact_name*.

    Args:
        repo_root: Checkout the receipts belong to.
        artifact_name: One key of :data:`RECEIPT_FILENAMES`.
        record: The typed receipt.

    Returns:
        The path written.

    Raises:
        KeyError: When *artifact_name* is not a declared receipt.
    """
    path = receipt_path(repo_root, artifact_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(record.model_dump_json(indent=2) + "\n", encoding="utf-8")
    logger.info(f"write_receipt artifact={artifact_name!r} path={path.name!r}")
    return path


def read_receipt[ReceiptT: _StrictModel](
    repo_root: Path,
    artifact_name: str,
    model: type[ReceiptT],
) -> ReceiptT | None:
    """Return the receipt uploaded under *artifact_name*, or ``None``.

    Args:
        repo_root: Checkout the receipts belong to.
        artifact_name: One key of :data:`RECEIPT_FILENAMES`.
        model: The record type the file must validate as.

    Returns:
        The validated receipt, or ``None`` when the producer has not run
        and the file is absent.

    Raises:
        KeyError: When *artifact_name* is not a declared receipt.
        ValueError: When the file exists but is not valid JSON, or does
            not validate as *model*. A malformed receipt is a repair
            task, not a missing producer, so it must not read as absent.
    """
    path = receipt_path(repo_root, artifact_name)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"release receipt {artifact_name!r} is not valid JSON: {exc}") from exc
    try:
        return model.model_validate(payload)
    except ValidationError as exc:
        raise ValueError(
            f"release receipt {artifact_name!r} does not validate as "
            f"{model.__name__}: {exc.error_count()} error(s); first: {exc.errors()[0]['msg']}"
        ) from exc


def read_dependency_manifest(repo_root: Path) -> ReleaseDependencyManifest | None:
    """Return the written dependency manifest, or ``None`` when absent."""
    return read_receipt(repo_root, "dependency-manifest", ReleaseDependencyManifest)


def read_vulnerability_report(repo_root: Path) -> VulnerabilityReport | None:
    """Return the written vulnerability report, or ``None`` when absent."""
    return read_receipt(repo_root, "vulnerability-report", VulnerabilityReport)


def read_build_receipt(repo_root: Path) -> ReproducibleBuildReceipt | None:
    """Return the written double-build receipt, or ``None`` when absent."""
    return read_receipt(repo_root, "reproducible-build-receipt", ReproducibleBuildReceipt)


__all__ = [
    "RECEIPT_DIRNAME",
    "RECEIPT_FILENAMES",
    "read_build_receipt",
    "read_dependency_manifest",
    "read_receipt",
    "read_vulnerability_report",
    "receipt_dir",
    "receipt_path",
    "write_receipt",
]
