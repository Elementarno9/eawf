"""A release's post-merge facts, derived from the remote at read time.

Whether a release's source commit reached the default branch, and
whether its ``v<version>`` tag exists, are facts about the remote. They
happen after a Milestone is accepted and its pull request is opened, so
they are never recorded as state: recording them would make a state
commit wait on a merge, and the merge wait on that commit. They are read
back from the remote whenever asked, and nothing under
``.ea/generations``, no ledger and no ``state.json`` is written.

The last answer is cached under :data:`FACTS_CACHE_DIRNAME`, which git
ignores, so a read made while the remote is unreachable still answers --
from the cache, marked as such, with the instant it was observed.

Publication is not derived here. Reading a registry back needs the
frozen manifest an approval bound, and ``eawf release observe`` already
does that read and records its verdict on the release record, so the
record's status is the published fact.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict

from eawf.kernel.spec.release import NormalizedVersionStr, normalize_version
from eawf.kernel.state.types import ShaStr
from eawf.workflow.release.registry_readers import SubprocessGitRunner

logger = logging.getLogger(__name__)

#: Remote the facts are read from: the one the pull request merges into.
REMOTE: Final[str] = "origin"

#: Where the last answer per version is cached, under the repo root.
FACTS_CACHE_DIRNAME: Final[str] = ".ea/local/release-facts"

#: ``merge-base --is-ancestor`` exit code -> the answer it gives. Any
#: other code (128: the commit is not in the local objects) is no answer.
_ANCESTRY_ANSWERS: Final[dict[int, bool]] = {0: True, 1: False}


class FactsSource(StrEnum):
    """Where a :class:`RemoteFacts` answer came from."""

    REMOTE = "remote"
    CACHE = "cache"


class RemoteFacts(BaseModel):
    """What the remote says about one release version.

    Attributes:
        version: The normalized release version.
        tag: The tag the version is released under, ``v<version>``.
        source_sha: The release's source commit, or ``None`` when no
            record pins one yet.
        default_branch: The branch the remote's ``HEAD`` names, or
            ``None`` for a remote with no commits.
        merged: Whether *source_sha* is an ancestor of the default
            branch's tip; ``None`` when there is no source commit, the
            remote has no default branch to merge into (it is empty), or
            git could not decide (the commit is not in the local objects).
        tagged: Whether the remote carries *tag*.
        tag_commit: The commit the remote's tag points at, peeled.
        observed_at: When the remote answered.
        source: Whether this answer was read now or is the cached one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: NormalizedVersionStr
    tag: str
    source_sha: ShaStr | None
    default_branch: str | None
    merged: bool | None
    tagged: bool
    tag_commit: ShaStr | None
    observed_at: AwareDatetime
    source: FactsSource = FactsSource.REMOTE


class RemoteUnreachableError(RuntimeError):
    """The remote did not answer and no earlier answer is cached."""


def facts_cache_path(repo_root: Path, version: str) -> Path:
    """Return the cache file of *version* under *repo_root*.

    Args:
        repo_root: The checkout.
        version: A normalized release version.

    Returns:
        The path the last answer for *version* is cached at.
    """
    return repo_root / FACTS_CACHE_DIRNAME / f"{version}.json"


def derive_remote_facts(
    repo_root: Path,
    *,
    version: str,
    source_sha: str | None,
) -> RemoteFacts:
    """Return the post-merge facts of *version*, read from the remote now.

    One ``ls-remote`` call reads the default branch, its tip and the tag.
    When a source commit is given, the default branch is fetched so the
    ancestry question is answered against the remote's tip, not a stale
    remote-tracking ref.

    Args:
        repo_root: The checkout whose ``origin`` is read.
        version: The release version.
        source_sha: The release's source commit, or ``None``.

    Returns:
        The facts as the remote states them, or the cached answer marked
        :attr:`FactsSource.CACHE` when the remote did not answer.

    Raises:
        ValueError: When *version* is not a train version.
        RemoteUnreachableError: When the remote did not answer and
            nothing is cached for *version*.
        pydantic.ValidationError: When the cache file is malformed, or
            *source_sha* is not a commit id.
    """
    version = normalize_version(version)
    tag = f"v{version}"
    runner = SubprocessGitRunner()
    git = ["-C", str(repo_root)]
    listing = runner(
        [*git, "ls-remote", "--symref", REMOTE, "HEAD", f"refs/tags/{tag}", f"refs/tags/{tag}^{{}}"]
    )
    if listing.exit_code != 0:
        return _cached(repo_root, version)
    branch, tip, refs = _parse_listing(listing.stdout)
    merged: bool | None = None
    if source_sha is not None and branch is not None and tip is not None:
        fetched = runner([*git, "fetch", "--quiet", "--no-tags", REMOTE, f"refs/heads/{branch}"])
        if fetched.exit_code != 0:
            return _cached(repo_root, version)
        ancestry = runner([*git, "merge-base", "--is-ancestor", source_sha, tip])
        merged = _ANCESTRY_ANSWERS.get(ancestry.exit_code)
    tag_commit = refs.get(f"refs/tags/{tag}^{{}}") or refs.get(f"refs/tags/{tag}")
    facts = RemoteFacts(
        version=version,
        tag=tag,
        source_sha=source_sha,
        default_branch=branch,
        merged=merged,
        tagged=tag_commit is not None,
        tag_commit=tag_commit,
        observed_at=datetime.now(UTC),
    )
    path = facts_cache_path(repo_root, version)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(facts.model_dump_json(), encoding="utf-8")
    logger.info(f"remote_facts version={version!r} merged={merged} tagged={facts.tagged}")
    return facts


def show_remote_facts(
    repo_root: Path, show_answer: Mapping[str, object]
) -> dict[str, object] | None:
    """Return the ``remote_facts`` a ``release show`` answer is reported with.

    The rung's version comes from the answer's ``checkpoint`` and the
    source commit from its ``record``; a rung nobody opened has no record,
    so its facts carry no source commit.

    Args:
        repo_root: The checkout whose ``origin`` is read and under which
            the facts are cached.
        show_answer: The daemon's ``release.show`` answer.

    Returns:
        The derived facts; ``source: unavailable`` and why when ``origin``
        did not answer and nothing is cached; ``None`` when the answer
        names no checkpoint, as ``show`` reports any other absent field.

    Raises:
        ValueError: When the checkpoint's version is not a train version.
        pydantic.ValidationError: When the cache file is malformed, or
            the record's source commit is not a commit id.
    """
    checkpoint = show_answer.get("checkpoint")
    release_key = checkpoint.get("release_key") if isinstance(checkpoint, Mapping) else None
    if not isinstance(release_key, str):
        return None
    record = show_answer.get("record")
    source_sha = record.get("source_sha") if isinstance(record, Mapping) else None
    try:
        facts = derive_remote_facts(
            repo_root,
            version=release_key.removeprefix("REL-"),
            source_sha=source_sha if isinstance(source_sha, str) else None,
        )
    except RemoteUnreachableError as exc:
        return {"source": "unavailable", "detail": str(exc)}
    return facts.model_dump(mode="json")


def _cached(repo_root: Path, version: str) -> RemoteFacts:
    """Return the cached answer for *version*, marked as cached.

    Args:
        repo_root: The checkout.
        version: A normalized release version.

    Returns:
        The last answer the remote gave, with ``source`` set to cache.

    Raises:
        RemoteUnreachableError: When nothing is cached for *version*.
        pydantic.ValidationError: When the cache file is malformed.
    """
    path = facts_cache_path(repo_root, version)
    if not path.exists():
        raise RemoteUnreachableError(
            f"remote {REMOTE!r} did not answer and no facts are cached for {version}"
        )
    cached = RemoteFacts.model_validate_json(path.read_bytes())
    logger.warning(f"remote_facts_from_cache version={version!r} observed_at={cached.observed_at}")
    return cached.model_copy(update={"source": FactsSource.CACHE})


def _parse_listing(stdout: bytes) -> tuple[str | None, str | None, dict[str, str]]:
    """Split ``ls-remote --symref`` output into branch, tip and refs.

    Args:
        stdout: Raw ``git ls-remote --symref`` output.

    Returns:
        The default branch name, its tip commit (both ``None`` for an
        empty remote) and every other listed ref mapped to its object.
    """
    branch: str | None = None
    refs: dict[str, str] = {}
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        left, _, ref = line.partition("\t")
        if left.startswith("ref: "):
            branch = left.removeprefix("ref: ").removeprefix("refs/heads/")
        elif ref:
            refs[ref.strip()] = left.strip()
    return branch, refs.pop("HEAD", None), refs


__all__ = [
    "FACTS_CACHE_DIRNAME",
    "REMOTE",
    "FactsSource",
    "RemoteFacts",
    "RemoteUnreachableError",
    "derive_remote_facts",
    "facts_cache_path",
    "show_remote_facts",
]
