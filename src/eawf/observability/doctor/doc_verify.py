"""``eawf doc verify`` — drift + state-vs-doc cross-checks.

Consolidates two complementary surfaces:

1. **Region drift** — for every managed target in ``.ea/indexes/generated.json``,
   recompute the on-disk region body hash and compare against the recorded
   manifest hash. Drift kinds: ``hand-edited``, ``missing``, ``ok``. The
   underlying check lives in :mod:`eawf.surfaces.render.drift`; this module just
   batches it across every target referenced by the manifest.

2. **Artifact cross-checks** — every artifact of the selected generation
   whose ``uri`` starts with ``repo:`` resolves to an existing file under the
   workspace root, and a ``#fragment`` on a JSONL file names a row id in it.

The function is read-only — no locks, no writes — so callers can invoke it
freely inside other commands (``doctor``, CI checks).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from eawf.kernel.store.ledger import effective_records, read_ledger_records
from eawf.surfaces.render.drift import DriftReport, detect_drift
from eawf.surfaces.render.manifest import load as load_manifest

logger = logging.getLogger(__name__)


_MANIFEST_RELPATH: str = ".ea/indexes/generated.json"


@dataclass(frozen=True)
class CrossCheckViolation:
    """One cross-check finding (state-vs-doc)."""

    code: str
    target: str
    message: str


@dataclass(frozen=True)
class DocVerifyReport:
    """Result of a :func:`verify_docs` invocation."""

    drift_reports: list[DriftReport]
    cross_check_violations: list[CrossCheckViolation]
    status: str  # "ok" | "drift"
    manifest_targets: int
    manifest_entries: int
    extras: dict[str, int] = field(default_factory=dict)

    @property
    def has_drift(self) -> bool:
        return any(r.kind != "ok" for r in self.drift_reports)

    @property
    def has_cross_check_failures(self) -> bool:
        return bool(self.cross_check_violations)


@dataclass(frozen=True)
class ResolvedRepoUri:
    """Resolved repo artifact URI components."""

    path: Path
    fragment: str | None = None


def _resolve_artifact_uri(repo_root: Path, uri: str) -> ResolvedRepoUri | None:
    """Resolve a ``repo:<relpath>`` URI. Returns None on malformed/non-repo URI."""
    parsed = urlsplit(uri)
    if parsed.scheme != "repo":
        return None
    relpath = unquote(parsed.path)
    if not relpath:
        return None
    fragment = unquote(parsed.fragment) if parsed.fragment else None
    return ResolvedRepoUri(path=repo_root / relpath, fragment=fragment)


def _jsonl_contains_id(path: Path, fragment: str) -> bool:
    """Return whether JSONL *path* has a dict row whose ``id`` matches *fragment*."""
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if isinstance(row, dict) and row.get("id") == fragment:
            return True
    return False


def read_artifact_uris(artifact_ledger: Path) -> dict[str, str]:
    """Return the ``uri`` of every artifact a generation's artifact ledger holds.

    A line the cutover imported carries the epoch-1 artifact row under its
    ``payload``; a native line carries its fields at the top. A line with
    no string ``uri`` -- a content artifact, say -- names no file to check.

    Args:
        artifact_ledger: The generation's ``ledger/artifact.jsonl``.

    Returns:
        ``record_key -> uri``, the standing line of each artifact; empty
        when the ledger is absent.

    Raises:
        LedgerTornTailError: The ledger ends mid-line.
        pydantic.ValidationError: A line does not satisfy its envelope.
    """
    uris: dict[str, str] = {}
    for record in effective_records(read_ledger_records(artifact_ledger)):
        row = record.payload.get("payload", record.payload)
        uri = row.get("uri") if isinstance(row, dict) else None
        if isinstance(uri, str):
            uris[record.record_key] = uri
    return uris


def _cross_check_artifacts(
    artifact_uris: Mapping[str, str], repo_root: Path
) -> list[CrossCheckViolation]:
    """Check that every ``repo:`` artifact uri resolves under *repo_root*."""
    findings: list[CrossCheckViolation] = []
    for artifact_id, uri in artifact_uris.items():
        if not uri.startswith("repo:"):
            continue
        resolved = _resolve_artifact_uri(repo_root, uri)
        if resolved is None or not resolved.path.exists():
            findings.append(
                CrossCheckViolation(
                    code="DOC.ARTIFACT_URI_MISSING",
                    target=f"/artifacts/{artifact_id}",
                    message=(
                        f"artifact {artifact_id!r} uri {uri!r} does not resolve to a "
                        f"file under {repo_root}"
                    ),
                )
            )
            continue
        if (
            resolved.fragment is not None
            and resolved.path.suffix == ".jsonl"
            and not _jsonl_contains_id(resolved.path, resolved.fragment)
        ):
            findings.append(
                CrossCheckViolation(
                    code="DOC.ARTIFACT_URI_FRAGMENT_MISSING",
                    target=f"/artifacts/{artifact_id}",
                    message=(
                        f"artifact {artifact_id!r} uri {uri!r} does not resolve to a "
                        f"JSONL row id under {repo_root}"
                    ),
                )
            )
    return findings


def verify_docs(artifact_uris: Mapping[str, str], repo_root: Path) -> DocVerifyReport:
    """Run the drift + cross-check pass against the artifacts and the repo tree.

    Args:
        artifact_uris: The generation's artifact uris, as
            :func:`read_artifact_uris` reads them.
        repo_root: Workspace root containing ``.ea/indexes/generated.json``.

    Returns:
        A :class:`DocVerifyReport`. ``status`` is ``"ok"`` when there is no
        drift and no cross-check violation; ``"drift"`` otherwise.
    """
    manifest_path = repo_root / _MANIFEST_RELPATH
    manifest = load_manifest(manifest_path)

    targets = sorted({Path(entry.target) for entry in manifest.generated.values()})
    drift_reports: list[DriftReport] = []
    for target in targets:
        drift_reports.extend(detect_drift(target, manifest))

    cross_check = _cross_check_artifacts(artifact_uris, repo_root)
    has_any = any(r.kind != "ok" for r in drift_reports) or bool(cross_check)
    status = "drift" if has_any else "ok"
    return DocVerifyReport(
        drift_reports=drift_reports,
        cross_check_violations=cross_check,
        status=status,
        manifest_targets=len(targets),
        manifest_entries=len(manifest.generated),
        extras={
            "drift_count": sum(1 for r in drift_reports if r.kind != "ok"),
            "cross_check_count": len(cross_check),
        },
    )


__all__ = [
    "CrossCheckViolation",
    "DocVerifyReport",
    "read_artifact_uris",
    "verify_docs",
]
