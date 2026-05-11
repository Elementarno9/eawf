"""``eawf doc verify`` — drift + state-vs-doc cross-checks.

Consolidates two complementary surfaces:

1. **Region drift** — for every managed target in ``.ea/indexes/generated.json``,
   recompute the on-disk region body hash and compare against the recorded
   manifest hash. Drift kinds: ``hand-edited``, ``missing``, ``ok``. The
   underlying check lives in :mod:`eawf.render.drift`; this module just
   batches it across every target referenced by the manifest.

2. **State-vs-doc cross-checks** — facts about ``state.json`` that should be
   reflected in the workspace:

   - Every closed phase has an ``audit_id`` set.
   - Every artifact whose ``uri`` starts with ``repo:`` resolves to an
     existing file under the workspace root.

The function is read-only — no locks, no writes — so callers can invoke it
freely inside other commands (``doctor``, CI checks).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from eawf.render.drift import DriftReport, detect_drift
from eawf.render.manifest import load as load_manifest
from eawf.state.enums import PhaseStatus
from eawf.state.models import State

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


def _resolve_artifact_uri(repo_root: Path, uri: str) -> Path | None:
    """Resolve a ``repo:<relpath>`` URI to a filesystem path. Returns None on miss."""
    if not uri.startswith("repo:"):
        return None
    relpath = uri[len("repo:") :]
    if not relpath:
        return None
    return repo_root / relpath


def _cross_check_state(state: State, repo_root: Path) -> list[CrossCheckViolation]:
    """Run the state-vs-doc cross-checks against *state* and the repo tree."""
    findings: list[CrossCheckViolation] = []
    for phase_id, phase in state.phases.items():
        if phase.status == PhaseStatus.CLOSED and phase.audit_id is None:
            findings.append(
                CrossCheckViolation(
                    code="DOC.PHASE_MISSING_AUDIT",
                    target=f"/phases/{phase_id}",
                    message=f"closed phase {phase_id!r} has no audit_id",
                )
            )
    artifacts = state.artifacts or {}
    for artifact_id, artifact in artifacts.items():
        if not artifact.uri.startswith("repo:"):
            continue
        resolved = _resolve_artifact_uri(repo_root, artifact.uri)
        if resolved is None or not resolved.exists():
            findings.append(
                CrossCheckViolation(
                    code="DOC.ARTIFACT_URI_MISSING",
                    target=f"/artifacts/{artifact_id}",
                    message=(
                        f"artifact {artifact_id!r} uri {artifact.uri!r} does not resolve to a "
                        f"file under {repo_root}"
                    ),
                )
            )
    return findings


def verify_docs(state: State, repo_root: Path) -> DocVerifyReport:
    """Run the drift + cross-check pass against *state* and the repo tree.

    Args:
        state: Loaded, validated :class:`State`.
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

    cross_check = _cross_check_state(state, repo_root)
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
    "verify_docs",
]
