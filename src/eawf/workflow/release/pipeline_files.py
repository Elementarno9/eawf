"""The two file chores of the post-merge release pipeline.

Both came out of walking a checkpoint by hand, and both failed quietly
the first time rather than refusing.

``gh run download`` nests every artifact in a directory named after it
once more than one artifact is fetched, so a receipt uploaded as
``dependency-manifest`` lands at
``<dir>/dependency-manifest/dependency-manifest.json``. The readers in
:mod:`eawf.workflow.release.pipeline_receipts` and
:mod:`eawf.workflow.release.publication_receipt` look for the flat name
only, so a nested download reads as "the producer never ran".
:func:`flatten_artifacts` puts every expected file where the readers
look and names the ones the download did not carry.

Evidence JSON pins bare 40-hex commits, which the secret scanner reads
as high-entropy strings. Rescanning the whole tree into the baseline
pulls in every file the hook excludes, so :func:`baseline_evidence`
scans only the files the pipeline wrote, merges their findings into the
committed baseline, and bumps the ``Baseline-hash`` acknowledgement in
the pre-commit configuration to match.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any, Final

logger = logging.getLogger(__name__)

#: The committed secret-scan baseline, relative to the repo root.
SECRETS_BASELINE: Final[str] = ".secrets.baseline"

#: The pre-commit configuration carrying the baseline acknowledgement.
PRE_COMMIT_CONFIG: Final[str] = ".pre-commit-config.yaml"

#: How many hex digits of the baseline's sha256 the acknowledgement quotes.
BASELINE_HASH_WIDTH: Final[int] = 16

_BASELINE_HASH_RE: Final[re.Pattern[str]] = re.compile(
    r"(# Baseline-hash: )([0-9a-f]+)( \(\.secrets\.baseline)"
)


def flatten_artifacts(raw_dir: Path, dest: Path, filenames: Iterable[str]) -> tuple[str, ...]:
    """Copy each of *filenames* found anywhere under *raw_dir* flat into *dest*.

    Args:
        raw_dir: Where ``gh run download`` unpacked the artifacts, nested
            or not.
        dest: The flat directory the receipt readers look in.
        filenames: The files expected, by basename.

    Returns:
        The basenames the download did not carry, in the order asked.

    Raises:
        ValueError: When one basename occurs more than once under
            *raw_dir*: two runs uploading the same receipt cannot be told
            apart, and picking one would pin an arbitrary leg.
    """
    dest.mkdir(parents=True, exist_ok=True)
    missing: list[str] = []
    for name in filenames:
        found = sorted(raw_dir.rglob(name)) if raw_dir.is_dir() else []
        if not found:
            missing.append(name)
            continue
        if len(found) > 1:
            raise ValueError(
                f"{len(found)} downloaded files are named {name!r}; expected one per artifact"
            )
        shutil.copy2(found[0], dest / name)
    logger.info(f"flatten_artifacts dest={dest.name!r} missing={missing}")
    return tuple(missing)


def baseline_hash(baseline_text: str) -> str:
    """Return the acknowledgement digest of a baseline's exact bytes.

    Args:
        baseline_text: The baseline file's content.

    Returns:
        The first :data:`BASELINE_HASH_WIDTH` hex digits of its sha256.
    """
    return hashlib.sha256(baseline_text.encode("utf-8")).hexdigest()[:BASELINE_HASH_WIDTH]


def _scan_findings(
    repo_root: Path, rel_paths: Sequence[str], baseline: dict[str, Any]
) -> dict[str, Any]:
    """Return the scanner's findings for *rel_paths* under the baseline's own settings.

    Args:
        repo_root: Root the relative paths resolve against.
        rel_paths: Files to scan, repo-relative.
        baseline: The decoded baseline, whose plugin and filter settings
            the scan runs under so its findings match what the hook sees.

    Returns:
        ``path -> findings`` for every path with at least one finding.

    Raises:
        ImportError: When the scanner is not installed; it ships with
            the development toolchain, not the runtime package.
    """
    from detect_secrets.core.secrets_collection import SecretsCollection
    from detect_secrets.settings import transient_settings

    config = {key: baseline[key] for key in ("plugins_used", "filters_used") if key in baseline}
    with transient_settings(config):
        collection = SecretsCollection(root=str(repo_root))
        for rel in rel_paths:
            collection.scan_file(rel)
        findings: dict[str, Any] = collection.json()
    # The scanner stamps each row with the path it opened, which is
    # absolute under a root; the baseline carries repo-relative paths only.
    for rel, rows in findings.items():
        for row in rows:
            row["filename"] = rel
    return findings


def baseline_evidence(repo_root: Path, rel_paths: Sequence[str]) -> int:
    """Baseline the scanner findings of exactly *rel_paths*, and re-acknowledge.

    Findings of every other file stay as the committed baseline holds
    them. When the merged baseline differs from the committed one, the
    ``Baseline-hash`` comment in the pre-commit configuration is bumped
    to the new digest; an unchanged baseline leaves both files alone.

    Args:
        repo_root: The checkout holding the baseline and the evidence.
        rel_paths: Evidence files the pipeline wrote, repo-relative.

    Returns:
        How many findings were baselined across *rel_paths*.

    Raises:
        ImportError: When the scanner is not installed.
        OSError: When a file cannot be read or written.
        ValueError: When the baseline is not a JSON object with a
            ``results`` map, or the configuration carries no
            ``Baseline-hash`` comment to bump.
    """
    baseline_path = repo_root / SECRETS_BASELINE
    before = baseline_path.read_text(encoding="utf-8")
    baseline = json.loads(before)
    if not isinstance(baseline, dict) or not isinstance(baseline.get("results"), dict):
        raise ValueError(f"{SECRETS_BASELINE} carries no results map")
    findings = _scan_findings(repo_root, rel_paths, baseline)
    results: dict[str, Any] = dict(baseline["results"])
    for rel in rel_paths:
        results.pop(rel, None)
    results.update(findings)
    baseline["results"] = {key: results[key] for key in sorted(results)}
    after = json.dumps(baseline, indent=2) + "\n"
    count = sum(len(rows) for rows in findings.values())
    if after == before:
        logger.info(f"baseline_evidence files={len(rel_paths)} findings={count} changed=False")
        return count
    config_path = repo_root / PRE_COMMIT_CONFIG
    config_text = config_path.read_text(encoding="utf-8")
    bumped, replaced = _BASELINE_HASH_RE.subn(
        lambda match: f"{match.group(1)}{baseline_hash(after)}{match.group(3)}", config_text
    )
    if replaced != 1:
        raise ValueError(f"{PRE_COMMIT_CONFIG} carries {replaced} Baseline-hash comments, not 1")
    baseline_path.write_text(after, encoding="utf-8")
    config_path.write_text(bumped, encoding="utf-8")
    logger.info(f"baseline_evidence files={len(rel_paths)} findings={count} changed=True")
    return count


__all__ = [
    "BASELINE_HASH_WIDTH",
    "PRE_COMMIT_CONFIG",
    "SECRETS_BASELINE",
    "baseline_evidence",
    "baseline_hash",
    "flatten_artifacts",
]
