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

Artifacts are local unless promoted, so this rule walks the committed
``.ea/artifacts/**/*.md`` set -- the git-tracked paths the commit policy
classifies as committed -- and flags any file that violates either
convention. A grandfather baseline may exempt a promoted path from the
convention; it names none today. The kind sub-directories are read from
:data:`eawf.kernel.spec.common.ARTIFACT_KIND_SUBDIR`, the one declaration
the draft promoter and :data:`eawf.kernel.spec.common.ArtifactPathStr`
read as well.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from eawf.kernel.spec.common import ARTIFACT_KIND_SUBDIR
from eawf.kernel.store.commit_policy import CommitPolicy, classify_path

RULE_CODE = "EAWF023"

#: Repo-relative prefix every durable artifact lives beneath.
ARTIFACTS_ROOT = ".ea/artifacts/"

#: A conforming artifact filename stem leads with a ``YYYY-MM-DD-`` date.
DATE_STEM_RE = re.compile(r"^\d{4}-\d{2}-\d{2}-")

#: Artifacts exempt from the convention. Only a committed artifact is in
#: the rule's surface, and every committed artifact is a promoted one, so an
#: exemption may name only a promoted path; none needs one today.
GRANDFATHERED_ARTIFACTS: frozenset[str] = frozenset()


def committed_artifact_paths(paths: Iterable[str]) -> list[str]:
    """Return the paths the commit policy keeps in git, in input order.

    Artifacts are local unless promoted, so a tracked artifact the policy
    does not commit is awaiting removal from the index -- the census
    reports it -- and its placement is no longer this rule's concern.

    Args:
        paths: Repo-relative candidate paths, typically the git-tracked
            ``.ea/artifacts/**/*.md`` set.

    Returns:
        The paths under :data:`ARTIFACTS_ROOT` that classify as committed.
    """
    return [
        path
        for path in paths
        if _normalize(path).startswith(ARTIFACTS_ROOT)
        and classify_path(path).policy is CommitPolicy.COMMITTED
    ]


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
       canonical kind sub-directory, a value of
       :data:`eawf.kernel.spec.common.ARTIFACT_KIND_SUBDIR`.
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
    subdirs = frozenset(ARTIFACT_KIND_SUBDIR.values())
    if head not in subdirs:
        allowed = ", ".join(sorted(subdirs))
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
