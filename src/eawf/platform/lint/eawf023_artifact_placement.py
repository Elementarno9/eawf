"""EAWF023 — artifact placement + mandatory date-stem rule.

Durable artifacts under ``.ea/artifacts/`` are addressable by a stable,
sortable URI. Two conventions keep that URI honest:

1. Each artifact lives under its canonical kind sub-directory (an audit
   under ``audits/``, a research brief under ``research/``, an incident
   under ``incidents/``, ...). The kind sub-directory IS the artifact's
   type, so a misfiled artifact silently breaks the per-kind URI router
   and any consumer that resolves a kind from the path.
2. Each artifact's filename stem starts with a ``YYYY-MM-DD-`` date
   prefix so the artifact sorts chronologically within its kind and the
   slug never collides with a same-named sibling from another date.

This rule walks the git-tracked ``.ea/artifacts/**/*.md`` set and flags
any file that violates either convention. A grandfather baseline carries
the pre-convention legacy artifacts (named before the date-stem rule
landed) so the clean tree passes while every new artifact is held to the
convention. The same per-kind contract is enforced at the model boundary
by :data:`eawf.kernel.spec.common.ArtifactPathStr`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

RULE_CODE = "EAWF023"

#: Repo-relative prefix every durable artifact lives beneath.
ARTIFACTS_ROOT = ".ea/artifacts/"

#: Canonical kind sub-directories under ``.ea/artifacts/``. Mirrors the
#: ``_KIND_SUBDIR`` router values in
#: :mod:`eawf.surfaces.cli.commands.draft` (the promote-side source of
#: truth for kind -> sub-directory placement) so the lint and the
#: promoter never drift on which sub-directory a kind files into.
ARTIFACT_KIND_SUBDIRS: frozenset[str] = frozenset(
    {
        "audits",
        "research",
        "plans",
        "hypotheses",
        "decisions",
        "incidents",
        # Recorded validation-run evidence (live-drive recordings etc.,
        # P30-I23-W33) — machine-checked excerpt bundles, not prose briefs.
        "evidence",
    }
)

#: A conforming artifact filename stem leads with a ``YYYY-MM-DD-`` date.
DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

#: Pre-convention legacy artifacts named before the date-stem + canonical
#: sub-directory rule landed. These are grandfathered so the clean tree
#: passes; every NEW artifact is still held to the convention. A legacy
#: file drops off this baseline when a wave renames it to the canonical
#: form (rename-on-touch).
GRANDFATHERED_ARTIFACTS: frozenset[str] = frozenset(
    {
        ".ea/artifacts/audits/A09-P08-ship-gate.md",
        ".ea/artifacts/audits/A10-P09-ship-gate.md",
        ".ea/artifacts/audits/A11-P10-ship-gate.md",
        ".ea/artifacts/audits/A12-P11-ship-gate.md",
        ".ea/artifacts/audits/A13-P12-ship-gate.md",
        ".ea/artifacts/audits/A14-P13-ship-gate.md",
        ".ea/artifacts/audits/A15-P14-ship-gate.md",
        ".ea/artifacts/audits/A16-P14-ship-gate.md",
        ".ea/artifacts/audits/A17-P14-ship-gate.md",
        ".ea/artifacts/audits/A18-P14-ship-gate.md",
        ".ea/artifacts/audits/A19-P14-ship-gate.md",
        ".ea/artifacts/audits/A20-P15-p15-skills-audit.md",
        ".ea/artifacts/audits/A21-P16-ship-gate.md",
        ".ea/artifacts/audits/A22-P17-ship-gate.md",
        ".ea/artifacts/audits/A23-P18-ship-gate.md",
        ".ea/artifacts/audits/A24-P19-ship-gate.md",
        ".ea/artifacts/audits/A25-P19-I02-W16-backlog-bulk-close.md",
        ".ea/artifacts/audits/A26-P19-I02-ship-gate.md",
        ".ea/artifacts/audits/A27-P20-ship-gate.md",
        ".ea/artifacts/audits/A28-P22-ship-gate.md",
        ".ea/artifacts/audits/A29-P23-ship-gate.md",
        ".ea/artifacts/audits/A30-P24-ship-gate.md",
        ".ea/artifacts/audits/A31-P25-ship-gate.md",
        ".ea/artifacts/audits/audit-2026-05-28-p28-i03-deep-audit.md",
        ".ea/artifacts/audits/v0.1-closeout.md",
        ".ea/artifacts/research/long-term/c04a-workflow.md",
        ".ea/artifacts/research/long-term/c04b-skills.md",
        ".ea/artifacts/research/long-term/c04c-agent.md",
        ".ea/artifacts/research/long-term/c04d-runtime.md",
        ".ea/artifacts/research/research-2026-05-24-python-package-structure.md",
        ".ea/artifacts/research/research-2026-05-30-tui-chassis.md",
    }
)


@dataclass(frozen=True)
class ArtifactPlacementViolation:
    """One EAWF023 finding.

    Attributes:
        path: Repo-relative path of the offending artifact.
        reason: Why the path violates the placement convention.
    """

    path: str
    reason: str

    @property
    def code(self) -> str:
        """Return the rule code."""
        return RULE_CODE

    def render(self) -> str:
        """Return a ``CODE reason`` one-liner body (the path is the row prefix)."""
        return f"{RULE_CODE} {self.reason}"


def _normalize(path: str) -> str:
    """Return ``path`` with back-slashes folded to forward-slashes."""
    return path.replace("\\", "/")


def check_artifact_path(
    path: str,
    *,
    grandfather: frozenset[str] = GRANDFATHERED_ARTIFACTS,
) -> ArtifactPlacementViolation | None:
    """Return the placement violation for a single artifact path, or ``None``.

    A path is checked only when it is a Markdown file under
    :data:`ARTIFACTS_ROOT`; anything else is outside this rule's surface
    and returns ``None``. A path in ``grandfather`` is exempt.

    The two binary checks, in order:

    1. The first path segment after ``.ea/artifacts/`` must be a
       canonical kind sub-directory in :data:`ARTIFACT_KIND_SUBDIRS`.
    2. The filename stem must lead with a ``YYYY-MM-DD-`` date prefix
       (:data:`DATE_STEM_RE`).

    Args:
        path: Repo-relative artifact path.
        grandfather: Paths exempt from the convention (the legacy
            baseline). Defaults to :data:`GRANDFATHERED_ARTIFACTS`.

    Returns:
        The first :class:`ArtifactPlacementViolation` found, or ``None``
        when the path conforms (or is out of surface / grandfathered).
    """
    norm = _normalize(path)
    if not norm.startswith(ARTIFACTS_ROOT) or not norm.endswith(".md"):
        return None
    if norm in grandfather:
        return None
    relative = norm[len(ARTIFACTS_ROOT) :]
    head, _, tail = relative.partition("/")
    if not tail:
        return ArtifactPlacementViolation(
            path=norm,
            reason=(
                f"artifact must live under a kind sub-directory, not directly in {ARTIFACTS_ROOT}"
            ),
        )
    if head not in ARTIFACT_KIND_SUBDIRS:
        allowed = ", ".join(sorted(ARTIFACT_KIND_SUBDIRS))
        return ArtifactPlacementViolation(
            path=norm,
            reason=f"sub-directory {head!r} is not a canonical artifact kind (allowed: {allowed})",
        )
    stem = tail.rsplit("/", 1)[-1]
    if not DATE_STEM_RE.match(stem):
        return ArtifactPlacementViolation(
            path=norm,
            reason=f"filename {stem!r} must lead with a YYYY-MM-DD- date stem",
        )
    return None


def check_artifact_paths(
    paths: list[str],
    *,
    grandfather: frozenset[str] = GRANDFATHERED_ARTIFACTS,
) -> list[ArtifactPlacementViolation]:
    """Return placement violations across a list of artifact paths.

    Non-artifact paths (outside :data:`ARTIFACTS_ROOT` or non-``.md``)
    and grandfathered paths are skipped. Findings are returned in input
    order.

    Args:
        paths: Repo-relative candidate paths (typically the git-tracked
            ``.ea/artifacts/**/*.md`` set).
        grandfather: Paths exempt from the convention. Defaults to
            :data:`GRANDFATHERED_ARTIFACTS`.

    Returns:
        The violations, in the order their paths appear in ``paths``.
    """
    violations: list[ArtifactPlacementViolation] = []
    for path in paths:
        violation = check_artifact_path(path, grandfather=grandfather)
        if violation is not None:
            violations.append(violation)
    return violations
