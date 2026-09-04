"""Working-copy probes the release tag chokepoint feeds to the sweep.

:func:`~eawf.workflow.verify.release_readiness.compute_readiness` is
total but producer-less: every signal reports ``unavailable`` until a
probe registers for it, so a probe-free sweep is twelve open questions
rather than a verdict. This module is the producer for the five facts a
working copy can answer at tag time -- version consistency, the
changelog section, the migration note, ancestry against the publishing
remote, and tree cleanliness -- plus one fact it can only *refute*: a
module-length exemption that has outlived its grant reds the realization
row, but a clean check leaves that row unproven rather than green.

The probes read their subject out of a frozen
:class:`TagPreflightInputs` record instead of out of the running process
(its own ``__version__``, its own cwd), so the tag chokepoint, the
``release preflight`` verb and the tests all drive one code path over a
repository each of them names.

A probe answers only what a checkout can prove. Dependency inventory,
artifact reproducibility and credential availability need producers that
run outside the working copy, so they stay unregistered here and keep
reporting ``unavailable`` -- an unproven signal, not a passing one.
"""

from __future__ import annotations

import logging
import re
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from functools import partial
from pathlib import Path
from typing import Final

from eawf.platform.lint import load_lint_config
from eawf.platform.lint.exclusion_expiry import (
    ExclusionConfigError,
    decision_ids_from_state,
    expired_exclusions,
    validate_renewals,
)
from eawf.workflow.verify.release_readiness import (
    ReleaseSignalContext,
    ReleaseSignalName,
    ReleaseSignalOutcome,
    ReleaseSignalProbe,
    ReleaseSignalStatus,
)

logger = logging.getLogger(__name__)

#: Changelog the release section is mined from, relative to the repo root.
CHANGELOG_FILENAME: Final[str] = "CHANGELOG.md"

#: Wall-clock ceiling on one git invocation. A probe that hangs would
#: stall the whole sweep, which is the one way this module could stop
#: being total.
GIT_TIMEOUT_SECONDS: Final[int] = 30

#: Matches a ``## [0.7.0.dev1]`` / ``## 0.7.0.dev1 - 2026-09-04`` heading.
_SECTION_HEADING = "^##\\s+\\[?{version}\\]?\\s*(?:-.*)?$"

#: A line that names the release's migration outcome. A release needing
#: no migration says so in one line; silence is not a claim.
_MIGRATION_RE: Final[re.Pattern[str]] = re.compile(r"\bmigrat(?:e|ed|ion|ions)\b", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class TagPreflightInputs:
    """What the tag chokepoint knows before it asks whether it may push.

    Attributes:
        repo_root: Working copy the facts are read from.
        version: Version being tagged, e.g. ``0.7.0.dev1``.
        tag: Tag the version spells, e.g. ``v0.7.0.dev1``.
        package_version: ``eawf.__version__`` of the tagging checkout.
        remote: Remote whose branch the source must be reachable from.
        today: Date the calendar-sensitive probes judge against.
            Injected rather than read at the point of use so a sweep is
            reproducible against a pinned day.
    """

    repo_root: Path
    version: str
    tag: str
    package_version: str
    remote: str
    today: date = field(default_factory=lambda: datetime.now(UTC).date())

    def __post_init__(self) -> None:
        """Reject inputs no probe could produce a verdict from.

        Raises:
            ValueError: When any field is blank.
        """
        for name in ("version", "tag", "package_version", "remote"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"TagPreflightInputs.{name} must not be blank")


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run one read-only git command inside *repo_root*.

    Args:
        repo_root: Working copy to run against.
        *args: Arguments after ``git``.

    Returns:
        The completed process; a non-zero return code is a fact the
        caller interprets, not an exception.
    """
    return subprocess.run(
        ["git", "-C", str(repo_root), *args],
        capture_output=True,
        text=True,
        check=False,
        timeout=GIT_TIMEOUT_SECONDS,
    )


def _passing(*evidence: str) -> ReleaseSignalOutcome:
    """Return a passing outcome carrying *evidence*."""
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.PASS, evidence_refs=tuple(evidence))


def _failing(remediation: str, *evidence: str) -> ReleaseSignalOutcome:
    """Return a failing outcome whose *remediation* names the next action."""
    return ReleaseSignalOutcome(
        status=ReleaseSignalStatus.FAIL,
        remediation=remediation,
        evidence_refs=tuple(evidence),
    )


def _unproven(remediation: str) -> ReleaseSignalOutcome:
    """Return an ``unavailable`` outcome whose *remediation* names the gap.

    The shape a probe uses when it can clear one named component of a
    signal but not the whole row. Reporting ``pass`` there would claim
    the unmeasured components hold too, which is the one thing this
    sweep is built not to do.
    """
    return ReleaseSignalOutcome(status=ReleaseSignalStatus.UNAVAILABLE, remediation=remediation)


def _changelog_section(text: str, version: str) -> tuple[str, ...]:
    """Return the body lines of the ``## [<version>]`` section of *text*.

    Args:
        text: Full changelog body.
        version: Version whose section to mine.

    Returns:
        The section's non-blank lines in order, empty when the changelog
        carries no section for *version*.
    """
    heading = re.compile(_SECTION_HEADING.format(version=re.escape(version)))
    body: list[str] = []
    inside = False
    for line in text.splitlines():
        if heading.match(line.strip()):
            inside = True
            continue
        if inside and line.startswith("## "):
            break
        if inside and line.strip():
            body.append(line.rstrip())
    return tuple(body)


def _probe_tree_cleanliness(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether the working copy has uncommitted release inputs.

    Args:
        inputs: The chokepoint's inputs.
        context: The sweep's per-signal context (unused; the fact is a
            property of the checkout, not of the checkpoint).

    Returns:
        Passing when ``git status --porcelain`` is empty.
    """
    del context
    status = _git(inputs.repo_root, "status", "--porcelain")
    dirty = tuple(line for line in status.stdout.splitlines() if line.strip())
    if status.returncode != 0:
        return _failing(f"git status failed in {inputs.repo_root.name}: {status.stderr.strip()}")
    if dirty:
        return _failing(
            f"{len(dirty)} uncommitted path(s) in the release tree; commit or stash them, "
            f"or record a waiver with `--waive-dirty-tree <reason>`",
            f"git-status:{len(dirty)}-dirty-paths",
        )
    return _passing("git-status:clean")


def _probe_ancestry(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether HEAD is reachable from the configured source branch.

    Args:
        inputs: The chokepoint's inputs.
        context: The sweep's context; its configuration names the
            source branch the remote must already carry.

    Returns:
        Passing when ``<remote>/<source_branch>`` exists and HEAD is an
        ancestor of it.
    """
    branch = context.config.source_branch
    ref = f"{inputs.remote}/{branch}"
    if _git(inputs.repo_root, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode:
        return _failing(
            f"remote-tracking ref {ref!r} is absent; run `git fetch {inputs.remote} {branch}` "
            f"so the source can be proven publishable"
        )
    if _git(inputs.repo_root, "merge-base", "--is-ancestor", "HEAD", ref).returncode:
        return _failing(
            f"HEAD is not an ancestor of {ref!r}; publish from a commit the remote "
            f"branch already carries"
        )
    return _passing(f"ancestor-of:{ref}")


def _probe_version_consistency(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether the tag, the request, the package and the config agree.

    Args:
        inputs: The chokepoint's inputs.
        context: The sweep's context; its configuration carries the
            checkpoint version the tag claims to cut.

    Returns:
        Passing when all four spellings of the version match.
    """
    configured = context.config.version
    mismatches: list[str] = []
    if inputs.version != configured:
        mismatches.append(f"requested {inputs.version!r} != configured {configured!r}")
    if inputs.package_version != configured:
        mismatches.append(f"package {inputs.package_version!r} != configured {configured!r}")
    if inputs.tag != f"v{configured}":
        mismatches.append(f"tag {inputs.tag!r} != 'v{configured}'")
    if mismatches:
        return _failing(
            f"version sources disagree ({'; '.join(mismatches)}); bump the version module "
            f"or tag the checkpoint the configuration declares"
        )
    return _passing(f"version:{configured}")


def _probe_changelog(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether the changelog carries a non-empty section for the version.

    Args:
        inputs: The chokepoint's inputs.
        context: The sweep's context (unused; the section is keyed on
            the version being tagged).

    Returns:
        Passing when the section exists and carries at least one bullet.
    """
    del context
    path = inputs.repo_root / CHANGELOG_FILENAME
    if not path.exists():
        return _failing(f"{CHANGELOG_FILENAME} is absent from {inputs.repo_root.name}; add it")
    section = _changelog_section(path.read_text(encoding="utf-8"), inputs.version)
    if not section:
        return _failing(
            f"{CHANGELOG_FILENAME} has no section for {inputs.version!r}; add "
            f"`## [{inputs.version}]` with the release's entries"
        )
    if not any(line.lstrip().startswith(("-", "*")) for line in section):
        return _failing(
            f"the {inputs.version!r} changelog section carries no entry; a release with "
            f"nothing to say is not a release"
        )
    return _passing(f"{CHANGELOG_FILENAME}:{inputs.version}")


def _probe_migration(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether the changelog section states the migration outcome.

    A release that needs no migration says so in one line. Silence is
    indistinguishable from a forgotten migration note, so it fails.

    Args:
        inputs: The chokepoint's inputs.
        context: The sweep's context (unused; the note lives beside the
            changelog entries).

    Returns:
        Passing when a line of the section names the migration outcome.
    """
    del context
    path = inputs.repo_root / CHANGELOG_FILENAME
    if not path.exists():
        return _failing(f"{CHANGELOG_FILENAME} is absent; the migration outcome is unstated")
    section = _changelog_section(path.read_text(encoding="utf-8"), inputs.version)
    if not any(_MIGRATION_RE.search(line) for line in section):
        return _failing(
            f"the {inputs.version!r} changelog section states no migration outcome; name the "
            f"migration or say none is required"
        )
    return _passing(f"{CHANGELOG_FILENAME}:{inputs.version}:migration")


def _probe_module_length_exclusion(
    inputs: TagPreflightInputs, context: ReleaseSignalContext
) -> ReleaseSignalOutcome:
    """Report whether any module-length exemption has outlived its grant.

    A grandfathered oversized module is a promise to split it later. Once
    the grant's expiry date passes, the promise is broken and the release
    is the wrong thing to let through silently: shipping renews the
    exemption by inaction, which is exactly what the expiry exists to
    prevent. The red row names every lapsed module so the operator can
    split it, or renew the grant against a typed decision, before tagging.

    The renewal escape hatch is checked here too, because the release is
    where an unratified extension would otherwise take effect: a grant
    renewed against a decision id that no ``Decision`` row in state.json
    carries is an extension nobody signed, and reds the row the same way
    a lapsed grant does.

    A clean check reports ``unavailable`` rather than ``pass``: this is
    one named component of the realization row, and the remaining
    realization assertions still have no producer.

    Args:
        inputs: The chokepoint's inputs, naming the working copy and the
            date the grants are judged against.
        context: The sweep's context for this signal.

    Returns:
        A failing outcome naming each lapsed module or unratified
        renewal, or an unavailable outcome recording that the component
        is clean.

    Raises:
        ExclusionConfigError: When the exclusion list itself is
            malformed, which the sweep converts into a blocked row.
    """
    config = load_lint_config(inputs.repo_root / "pyproject.toml")
    try:
        validate_renewals(
            config.eawf010.exclusions,
            decision_ids=decision_ids_from_state(inputs.repo_root / ".ea" / "state.json"),
        )
    except ExclusionConfigError as exc:
        logger.warning(
            f"_probe_module_length_exclusion signal={context.signal.value!r} "
            f"unratified_renewal=1 version={inputs.version!r}"
        )
        return _failing(
            f"module_length_exclusion: {exc}",
            "module_length_exclusion:unratified-renewal",
        )
    expired = expired_exclusions(config.eawf010.exclusions, today=inputs.today)
    if expired:
        named = ", ".join(
            f"{entry.path} (expired {entry.expires.isoformat()})" for entry in expired
        )
        logger.warning(
            f"_probe_module_length_exclusion signal={context.signal.value!r} "
            f"expired={len(expired)} version={inputs.version!r}"
        )
        return _failing(
            f"module_length_exclusion: {len(expired)} EAWF010 exemption(s) outlived their grant: "
            f"{named}; split the module, or renew the grant with a typed decision and a named "
            f"owner, before tagging {inputs.tag}",
            *(
                f"module_length_exclusion:{entry.path}:expired:{entry.expires.isoformat()}"
                for entry in expired
            ),
        )
    return _unproven(
        f"module_length_exclusion: all {len(config.eawf010.exclusions)} EAWF010 exemption(s) are "
        f"within their grant, but the remaining realization assertions have no producer at this "
        f"checkpoint; register one or drop the requirement"
    )


def build_tag_probes(inputs: TagPreflightInputs) -> dict[ReleaseSignalName, ReleaseSignalProbe]:
    """Return the probe registry the tag chokepoint sweeps *inputs* with.

    Args:
        inputs: The chokepoint's inputs, bound into every probe.

    Returns:
        A registry over the six signals a working copy can speak to. The
        other six stay absent so the sweep reports them ``unavailable``
        rather than silently green. The realization probe is the one
        partial member: it can red the row on a lapsed module-length
        exemption but never greens it, since the rest of the realization
        assertions have no producer.
    """
    logger.info(
        f"build_tag_probes version={inputs.version!r} tag={inputs.tag!r} remote={inputs.remote!r}"
    )
    return {
        ReleaseSignalName.VERSION_CONSISTENCY: partial(_probe_version_consistency, inputs),
        ReleaseSignalName.CHANGELOG: partial(_probe_changelog, inputs),
        ReleaseSignalName.ANCESTRY: partial(_probe_ancestry, inputs),
        ReleaseSignalName.TREE_CLEANLINESS: partial(_probe_tree_cleanliness, inputs),
        ReleaseSignalName.MIGRATION: partial(_probe_migration, inputs),
        ReleaseSignalName.PERFECT_REALIZATION: partial(_probe_module_length_exclusion, inputs),
    }


__all__ = [
    "CHANGELOG_FILENAME",
    "GIT_TIMEOUT_SECONDS",
    "TagPreflightInputs",
    "build_tag_probes",
]
