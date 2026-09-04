"""The double-build receipt behind the ``artifacts`` signal.

``artifact_reproducibility`` reads the whole ``artifacts`` row, and the
row's claim is narrow and strong: *building this source twice produces
byte-identical artifacts*. Nothing weaker is worth asserting. A single
build proves only that a build succeeded, which every release already
knows; a rebuild that merely produces artifacts of the same *names*
proves less than nothing, because it looks like the strong claim.

So the receipt this module produces has three parts, and all three are
load-bearing:

* **two builds**, each from a clean tree, of the same source commit;
* **``SOURCE_DATE_EPOCH`` pinned to that commit's timestamp**, because
  the archive members' mtimes are otherwise the wall clock and no build
  would ever reproduce;
* a **recomputation immediately before upload**, because the artifacts
  that get published are the files on disk at upload time, not the files
  the build wrote -- anything that touched them in between is exactly
  what this check is for.

A divergence names the artifact, not just the fact. "The build is not
reproducible" sends an operator to read two whole trees; "``eawf-0.7.0
-py3-none-any.whl`` differs between attempt 1 and attempt 2" sends them
to one file.
"""

from __future__ import annotations

import hashlib
import logging
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import ConfigDict, Field, model_validator

from eawf.kernel.release.signals import (
    ReleaseSignalFailureCode,
    ReleaseSignalStatus,
)
from eawf.kernel.spec.common import _StrictModel

logger = logging.getLogger(__name__)

#: How many clean builds a receipt is made of. Two: one build is not a
#: comparison, and a third would only re-answer a question two already
#: settled.
BUILD_ATTEMPTS: Final[int] = 2

#: Environment variable the build backend reads to pin archive mtimes.
SOURCE_DATE_EPOCH_ENV: Final[str] = "SOURCE_DATE_EPOCH"

#: Wall budget for reading one commit's timestamp.
GIT_TIMEOUT_SECONDS: Final[int] = 30


class ArtifactKind(StrEnum):
    """Which distribution shape one built artifact is.

    Values:
        WHEEL: A ``.whl`` binary distribution.
        SDIST: A ``.tar.gz`` source distribution.
    """

    WHEEL = "wheel"
    SDIST = "sdist"


class ReproducibilityFinding(StrEnum):
    """Closed vocabulary for why the ``artifacts`` row went red.

    Values:
        DIGEST_DIVERGED: An artifact differs between the two builds.
        ARTIFACT_SET_DIVERGED: The two builds produced different files.
        UPLOAD_DRIFT: The pre-upload recomputation disagrees with the
            build the receipt attests.
        EPOCH_UNPINNED: The builds ran under different (or no)
            ``SOURCE_DATE_EPOCH``, so agreement would prove nothing.
    """

    DIGEST_DIVERGED = "digest_diverged"
    ARTIFACT_SET_DIVERGED = "artifact_set_diverged"
    UPLOAD_DRIFT = "upload_drift"
    EPOCH_UNPINNED = "epoch_unpinned"


class ArtifactDigest(_StrictModel):
    """One built file and its content digest.

    Attributes:
        filename: Base name of the artifact, without any directory.
        kind: Which distribution shape it is.
        sha256: Hex digest of the file's bytes.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    filename: Annotated[str, Field(min_length=1, pattern=r"^[^/\\]+$")]
    kind: ArtifactKind
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class BuildAttempt(_StrictModel):
    """One clean build of the source, with what it produced.

    Attributes:
        attempt: 1-based index of this build within the receipt.
        source_date_epoch: The epoch the build ran under.
        artifacts: What it wrote, sorted by filename.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    attempt: Annotated[int, Field(ge=1, le=BUILD_ATTEMPTS)]
    source_date_epoch: Annotated[int, Field(ge=0)]
    artifacts: Annotated[tuple[ArtifactDigest, ...], Field(min_length=1)]

    @model_validator(mode="after")
    def _artifacts_are_unique_and_ordered(self) -> BuildAttempt:
        """Reject a build that names one artifact twice or out of order.

        Raises:
            ValueError: When two artifacts share a filename, or the rows
                are not sorted.
        """
        names = [artifact.filename for artifact in self.artifacts]
        if len(names) != len(set(names)):
            raise ValueError(f"build attempt {self.attempt} reports an artifact more than once")
        if names != sorted(names):
            raise ValueError(f"build attempt {self.attempt} artifacts must be sorted by filename")
        return self

    @property
    def by_filename(self) -> Mapping[str, ArtifactDigest]:
        """Return this build's artifacts keyed by filename."""
        return {artifact.filename: artifact for artifact in self.artifacts}


class ReproducibleBuildReceipt(_StrictModel):
    """What two clean builds of one commit produced, and whether they agree.

    Attributes:
        schema_version: Record schema tag.
        source_sha: The 40-hex commit both builds were made from.
        source_date_epoch: The epoch both builds were pinned to.
        attempts: The two builds, in attempt order.
        divergent_artifacts: Filenames that differ between the builds or
            drifted before upload, sorted; empty exactly when the
            receipt reproduces.
        findings: Which named failures fired.
        reproduced: Whether the receipt clears the ``artifacts`` row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["release-build-receipt/v1"] = "release-build-receipt/v1"
    source_sha: Annotated[str, Field(pattern=r"^[0-9a-f]{40}$")]
    source_date_epoch: Annotated[int, Field(ge=0)]
    attempts: Annotated[tuple[BuildAttempt, ...], Field(min_length=BUILD_ATTEMPTS)]
    divergent_artifacts: tuple[str, ...] = ()
    findings: tuple[ReproducibilityFinding, ...] = ()
    reproduced: bool

    @model_validator(mode="after")
    def _verdict_matches_the_divergences(self) -> ReproducibleBuildReceipt:
        """Reject a receipt whose verdict disagrees with its own rows.

        Raises:
            ValueError: When the attempt indices are not ``1..n``, or
                ``reproduced`` does not match the divergence set.
        """
        indices = [attempt.attempt for attempt in self.attempts]
        if indices != list(range(1, len(self.attempts) + 1)):
            raise ValueError(f"build receipt attempts must be numbered 1..n, got {indices}")
        clean = not self.divergent_artifacts and not self.findings
        if self.reproduced is not clean:
            raise ValueError(
                f"receipt claims reproduced={self.reproduced} while reporting "
                f"{list(self.divergent_artifacts)} and {[f.value for f in self.findings]}"
            )
        return self


def digest_file(path: Path, *, kind: ArtifactKind) -> ArtifactDigest:
    """Return the digest row for the artifact at *path*.

    Args:
        path: File to read.
        kind: Which distribution shape it is.

    Returns:
        The digest row, filenamed by the path's base name.

    Raises:
        FileNotFoundError: When *path* does not exist.
    """
    return ArtifactDigest(
        filename=path.name,
        kind=kind,
        sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
    )


def classify_artifact(path: Path) -> ArtifactKind | None:
    """Return which distribution shape *path* is, or ``None``.

    Args:
        path: A file in a build output directory.

    Returns:
        The kind, or ``None`` for anything that is neither a wheel nor
        an sdist (a ``.txt`` checksum listing, an editor backup) so a
        stray file in ``dist/`` cannot enter the receipt.
    """
    name = path.name
    if name.endswith(".whl"):
        return ArtifactKind.WHEEL
    if name.endswith(".tar.gz"):
        return ArtifactKind.SDIST
    return None


def digest_build_output(out_dir: Path, *, attempt: int, source_date_epoch: int) -> BuildAttempt:
    """Return the attempt record for the artifacts in *out_dir*.

    Args:
        out_dir: Directory a build wrote into.
        attempt: 1-based index of this build.
        source_date_epoch: The epoch the build ran under.

    Returns:
        The attempt, artifacts sorted by filename.

    Raises:
        ValueError: When *out_dir* holds no wheel or sdist -- a build
            that produced nothing must not read as a reproducible one.
    """
    digests = [
        digest_file(path, kind=kind)
        for path in sorted(out_dir.iterdir())
        if path.is_file() and (kind := classify_artifact(path)) is not None
    ]
    if not digests:
        raise ValueError(f"build attempt {attempt} produced no wheel or sdist in {out_dir.name}")
    return BuildAttempt(
        attempt=attempt,
        source_date_epoch=source_date_epoch,
        artifacts=tuple(sorted(digests, key=lambda artifact: artifact.filename)),
    )


def source_date_epoch(repo_root: Path, source_sha: str) -> int:
    """Return the committer timestamp of *source_sha*.

    The commit's own timestamp is used rather than "now" because it is
    the one clock reading that is a property of the source rather than
    of the machine: two builds of the same commit on different days must
    pin the same epoch or they cannot reproduce.

    Args:
        repo_root: Working copy the commit is read from.
        source_sha: The commit being built.

    Returns:
        Seconds since the Unix epoch.

    Raises:
        ValueError: When git cannot resolve *source_sha*.
    """
    completed = subprocess.run(
        ["git", "show", "-s", "--format=%ct", source_sha],
        cwd=repo_root,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_SECONDS,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"cannot read the timestamp of {source_sha!r}: {completed.stderr.strip()[:200]}"
        )
    return int(completed.stdout.strip())


def _divergences(first: BuildAttempt, second: BuildAttempt) -> tuple[list[str], list[str]]:
    """Return the (missing-or-extra, differing) filenames between two builds."""
    left, right = first.by_filename, second.by_filename
    set_diff = sorted(set(left) ^ set(right))
    digest_diff = sorted(
        name for name in set(left) & set(right) if left[name].sha256 != right[name].sha256
    )
    return set_diff, digest_diff


def compare_builds(
    attempts: Sequence[BuildAttempt],
    *,
    source_sha: str,
    pre_upload: Sequence[ArtifactDigest] = (),
) -> ReproducibleBuildReceipt:
    """Return the receipt *attempts* justify.

    Args:
        attempts: The clean builds, in attempt order; exactly
            :data:`BUILD_ATTEMPTS` of them.
        source_sha: The 40-hex commit both were built from.
        pre_upload: Digests recomputed from the files as they sit
            immediately before upload. Empty means the recomputation has
            not run yet, which is checked separately by
            :func:`artifacts_component` rather than silently passing.

    Returns:
        The receipt, ``reproduced`` exactly when nothing diverged.

    Raises:
        ValueError: When *attempts* is not exactly
            :data:`BUILD_ATTEMPTS` long.
    """
    if len(attempts) != BUILD_ATTEMPTS:
        raise ValueError(
            f"a build receipt is made of exactly {BUILD_ATTEMPTS} attempts, got {len(attempts)}"
        )
    first, second = attempts[0], attempts[1]
    findings: list[ReproducibilityFinding] = []
    divergent: set[str] = set()
    if first.source_date_epoch != second.source_date_epoch:
        findings.append(ReproducibilityFinding.EPOCH_UNPINNED)
    set_diff, digest_diff = _divergences(first, second)
    if set_diff:
        findings.append(ReproducibilityFinding.ARTIFACT_SET_DIVERGED)
        divergent.update(set_diff)
    if digest_diff:
        findings.append(ReproducibilityFinding.DIGEST_DIVERGED)
        divergent.update(digest_diff)
    drifted = _upload_drift(second, pre_upload)
    if drifted:
        findings.append(ReproducibilityFinding.UPLOAD_DRIFT)
        divergent.update(drifted)
    receipt = ReproducibleBuildReceipt(
        source_sha=source_sha,
        source_date_epoch=first.source_date_epoch,
        attempts=tuple(attempts),
        divergent_artifacts=tuple(sorted(divergent)),
        findings=tuple(findings),
        reproduced=not findings and not divergent,
    )
    logger.info(
        f"compare_builds source_sha={source_sha[:12]!r} epoch={receipt.source_date_epoch} "
        f"artifacts={len(first.artifacts)} reproduced={receipt.reproduced} "
        f"divergent={list(receipt.divergent_artifacts)}"
    )
    return receipt


def _upload_drift(
    build: BuildAttempt,
    pre_upload: Sequence[ArtifactDigest],
) -> list[str]:
    """Return the filenames whose pre-upload digest left the build behind.

    Args:
        build: The attempt whose output would be uploaded.
        pre_upload: Digests recomputed just before upload; empty skips
            the comparison, which the component reports separately.

    Returns:
        The drifted or vanished filenames, sorted.
    """
    if not pre_upload:
        return []
    built = build.by_filename
    recomputed = {artifact.filename: artifact for artifact in pre_upload}
    return sorted(
        name
        for name in set(built) | set(recomputed)
        if name not in built
        or name not in recomputed
        or built[name].sha256 != recomputed[name].sha256
    )


@dataclass(frozen=True, slots=True)
class ArtifactsOutcome:
    """The ``artifacts`` row's verdict on one receipt.

    Attributes:
        status: The row's verdict.
        failure_code: ``artifact_nonreproducible``; present exactly when
            the row is not passing.
        remediation: Operator next action, naming the artifact.
        evidence_refs: References backing the verdict.
    """

    status: ReleaseSignalStatus
    failure_code: ReleaseSignalFailureCode | None
    remediation: str
    evidence_refs: tuple[str, ...]


def artifacts_component(
    receipt: ReproducibleBuildReceipt,
    *,
    pre_upload_ran: bool = True,
) -> ArtifactsOutcome:
    """Return the ``artifacts`` verdict for *receipt*.

    Args:
        receipt: The double-build receipt.
        pre_upload_ran: Whether digests were recomputed immediately
            before upload. A receipt that skipped the recomputation is
            ``unavailable`` rather than passing: it attests two builds
            that agreed, which says nothing about the files that would
            actually be uploaded.

    Returns:
        The outcome.

    Raises:
        TypeError: When *receipt* is not a
            :class:`ReproducibleBuildReceipt`.
    """
    if not isinstance(receipt, ReproducibleBuildReceipt):
        raise TypeError(f"receipt must be ReproducibleBuildReceipt; got {type(receipt).__name__}")
    if not receipt.reproduced:
        named = ", ".join(receipt.divergent_artifacts) or "the build environment"
        codes = [finding.value for finding in receipt.findings]
        return ArtifactsOutcome(
            status=ReleaseSignalStatus.FAIL,
            failure_code=ReleaseSignalFailureCode.ARTIFACT_NONREPRODUCIBLE,
            remediation=(
                f"rebuilding {receipt.source_sha[:12]} did not reproduce {named} ({codes}); "
                f"find what the build reads outside the source tree, pin it, and rebuild "
                f"before publishing"
            ),
            evidence_refs=tuple(
                f"artifact_nonreproducible:{name}" for name in receipt.divergent_artifacts
            )
            or (f"artifact_nonreproducible:{receipt.source_sha}",),
        )
    if not pre_upload_ran:
        return ArtifactsOutcome(
            status=ReleaseSignalStatus.UNAVAILABLE,
            failure_code=ReleaseSignalFailureCode.ARTIFACT_NONREPRODUCIBLE,
            remediation=(
                "the two builds agree but no digest was recomputed before upload, so the "
                "receipt does not cover the files that would be published; recompute the "
                "inventory immediately before upload and re-attach the receipt"
            ),
            evidence_refs=(f"source_sha:{receipt.source_sha}",),
        )
    logger.info(
        f"artifacts_component source_sha={receipt.source_sha[:12]!r} "
        f"epoch={receipt.source_date_epoch} status='pass'"
    )
    return ArtifactsOutcome(
        status=ReleaseSignalStatus.PASS,
        failure_code=None,
        remediation="",
        evidence_refs=(
            f"source_sha:{receipt.source_sha}",
            f"source_date_epoch:{receipt.source_date_epoch}",
            *(
                f"artifact:{artifact.filename}:{artifact.sha256}"
                for artifact in receipt.attempts[-1].artifacts
            ),
        ),
    )


#: A builder: given the source tree, an output directory and the epoch
#: to pin, write the distribution artifacts. Injected rather than called
#: directly so the reproducibility machinery can be exercised over a
#: hermetic archive writer without a network-reachable build backend.
Builder = Callable[[Path, Path, int], None]


def uv_build(repo_root: Path, out_dir: Path, epoch: int) -> None:
    """Build a wheel and an sdist of *repo_root* into *out_dir*.

    Args:
        repo_root: Source tree to build.
        out_dir: Where the artifacts are written.
        epoch: Value exported as ``SOURCE_DATE_EPOCH``.

    Raises:
        ValueError: When the build exits non-zero.
    """
    completed = subprocess.run(
        ["uv", "build", "--out-dir", str(out_dir)],
        cwd=repo_root,
        capture_output=True,
        text=True,
        env={**os.environ, SOURCE_DATE_EPOCH_ENV: str(epoch)},
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(
            f"uv build exited {completed.returncode}: {completed.stderr.strip()[-400:]}"
        )


def double_build_receipt(
    repo_root: Path,
    *,
    source_sha: str,
    epoch: int,
    out_root: Path,
    builder: Builder = uv_build,
    recompute_before_upload: bool = True,
) -> ReproducibleBuildReceipt:
    """Build *repo_root* twice under *epoch* and return the receipt.

    Each build gets its own empty output directory under *out_root*, so
    the second build cannot pass by finding the first one's artifacts
    already there -- the failure mode that would make every receipt
    green regardless of the source.

    Args:
        repo_root: Source tree to build.
        source_sha: The 40-hex commit being built.
        epoch: ``SOURCE_DATE_EPOCH`` both builds are pinned to.
        out_root: Parent directory the two output directories are made
            under.
        builder: How to build; defaults to :func:`uv_build`.
        recompute_before_upload: Whether to re-digest the second build's
            output after both builds finish, which is the pre-upload
            reading the receipt attests.

    Returns:
        The receipt, ``reproduced`` exactly when nothing diverged.

    Raises:
        ValueError: When a build produces no wheel or sdist.
    """
    attempts: list[BuildAttempt] = []
    out_dirs: list[Path] = []
    for index in range(1, BUILD_ATTEMPTS + 1):
        out_dir = out_root / f"build-{index}"
        out_dir.mkdir(parents=True, exist_ok=False)
        builder(repo_root, out_dir, epoch)
        attempts.append(digest_build_output(out_dir, attempt=index, source_date_epoch=epoch))
        out_dirs.append(out_dir)
    pre_upload: tuple[ArtifactDigest, ...] = ()
    if recompute_before_upload:
        pre_upload = digest_build_output(
            out_dirs[-1], attempt=BUILD_ATTEMPTS, source_date_epoch=epoch
        ).artifacts
    return compare_builds(attempts, source_sha=source_sha, pre_upload=pre_upload)


__all__ = [
    "BUILD_ATTEMPTS",
    "GIT_TIMEOUT_SECONDS",
    "SOURCE_DATE_EPOCH_ENV",
    "ArtifactDigest",
    "ArtifactKind",
    "ArtifactsOutcome",
    "BuildAttempt",
    "Builder",
    "ReproducibilityFinding",
    "ReproducibleBuildReceipt",
    "artifacts_component",
    "classify_artifact",
    "compare_builds",
    "digest_build_output",
    "digest_file",
    "double_build_receipt",
    "source_date_epoch",
    "uv_build",
]
