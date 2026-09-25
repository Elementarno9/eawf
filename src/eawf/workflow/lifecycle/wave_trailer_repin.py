"""Repin wave commits by their ``Eawf-Wave`` trailer after main was rewritten.

A phase normally lands by fast-forward, so every wave pin stays reachable on
``main``. When the commits were re-created anyway (a hosted rebase merge, a
rebase onto a moved ``main``), each old pin names a commit that is no longer
on first-parent ``main``, and the wave's new commit has to be found again.

The trailer is the identity that survives a rewrite: each pin is resolved to
the first-parent ``main`` commit whose message names the wave. A commit can
carry several trailers (a batch), and every wave it names maps to it. When
several commits name the same wave, the trailer alone cannot choose, so the
old pin's ``git patch-id`` breaks the tie: a rewrite that replays a commit
keeps its patch, so exactly one candidate should share it. Anything still
undecided is reported as ambiguous rather than guessed.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from eawf.platform.subprocess_detach import detached_subprocess_kwargs
from eawf.workflow.lifecycle.wave_sha import (
    _BODY_PLACEHOLDER,
    _FIELD_SEP,
    _FIELD_SEP_PLACEHOLDER,
    _REC_SEP,
    _REC_SEP_PLACEHOLDER,
    _body_wave_ids,
    _run_git,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_TARGET_REF: str = "refs/heads/main"
_LOG_TIMEOUT_SECONDS: float = 20.0
_PATCH_ID_TIMEOUT_SECONDS: float = 10.0

TrailerRepinOutcome = Literal["unchanged", "unique_trailer", "patch_id", "ambiguous", "unresolved"]


class TrailerRepinError(RuntimeError):
    """Git could not walk the first-parent history of the target ref."""


@dataclass(frozen=True)
class TrailerRepin:
    """How one wave pin resolved against the rewritten first-parent history.

    Attributes:
        wave_id: The wave whose pin was resolved.
        old_commit: The pin before the rewrite.
        new_commit: The commit the wave now resolves to; ``None`` when the
            outcome is ``ambiguous`` or ``unresolved``.
        outcome: ``unchanged`` when the old pin is still on first-parent
            history, ``unique_trailer`` when one commit names the wave,
            ``patch_id`` when the old pin's patch picked one of several,
            ``ambiguous`` when several remain, ``unresolved`` when none
            names the wave.
        candidates: Every first-parent commit naming the wave, newest first.
    """

    wave_id: str
    old_commit: str
    new_commit: str | None
    outcome: TrailerRepinOutcome
    candidates: tuple[str, ...] = ()


def first_parent_trailer_index(
    *, target_ref: str = DEFAULT_TARGET_REF, repo_root: Path | None = None
) -> tuple[set[str], dict[str, list[str]]]:
    """Index the first-parent history of *target_ref* by ``Eawf-Wave`` trailer.

    Args:
        target_ref: The ref whose first-parent chain is the landed history.
        repo_root: Repository working directory; ``None`` uses the cwd.

    Returns:
        ``(first_parent_shas, wave_id -> commits naming it, newest first)``.

    Raises:
        TrailerRepinError: When git cannot walk *target_ref*; an empty index
            would report every pin unresolved instead of failing.
    """
    fmt = _REC_SEP_PLACEHOLDER + _FIELD_SEP_PLACEHOLDER.join(("%H", _BODY_PLACEHOLDER))
    out = _run_git(
        ["log", "--first-parent", f"--format={fmt}", target_ref, "--"],
        repo_root=repo_root,
        timeout=_LOG_TIMEOUT_SECONDS,
    )
    if out is None or out.returncode != 0:
        detail = "" if out is None else out.stderr.strip()
        raise TrailerRepinError(f"cannot walk first-parent {target_ref}: {detail or 'git failed'}")
    shas: set[str] = set()
    index: dict[str, list[str]] = {}
    for record in out.stdout.split(_REC_SEP):
        fields = record.strip("\n").split(_FIELD_SEP, 1)
        if len(fields) != 2:
            continue
        sha, body = fields
        shas.add(sha)
        for wave_id in _body_wave_ids(body):
            index.setdefault(wave_id, []).append(sha)
    return shas, index


def patch_id(commit: str, *, repo_root: Path | None = None) -> str | None:
    """Return the stable ``git patch-id`` of *commit*, or ``None`` when it has none.

    An empty commit, a missing object and a failed probe all yield ``None``,
    which never equals another patch id and so never breaks a tie.
    """
    show = _run_git(
        ["show", "--format=", "--no-color", "--no-ext-diff", commit],
        repo_root=repo_root,
        timeout=_PATCH_ID_TIMEOUT_SECONDS,
    )
    if show is None or show.returncode != 0 or not show.stdout.strip():
        return None
    try:
        out = subprocess.run(
            ["git", "patch-id", "--stable"],
            cwd=str(repo_root) if repo_root else None,
            input=show.stdout,
            capture_output=True,
            text=True,
            timeout=_PATCH_ID_TIMEOUT_SECONDS,
            check=False,
            **detached_subprocess_kwargs(),
        )
    except subprocess.TimeoutExpired, FileNotFoundError, OSError:
        return None
    fields = out.stdout.split()
    return fields[0] if out.returncode == 0 and fields else None


def resolve_trailer_repins(
    pins: Mapping[str, str],
    *,
    target_ref: str = DEFAULT_TARGET_REF,
    repo_root: Path | None = None,
) -> list[TrailerRepin]:
    """Resolve each wave pin in *pins* to its commit on first-parent *target_ref*.

    Args:
        pins: Full wave id to its pinned 40-hex commit, as recorded before
            the rewrite.
        target_ref: The ref whose first-parent chain is the landed history.
        repo_root: Repository working directory; ``None`` uses the cwd.

    Returns:
        One :class:`TrailerRepin` per pin, ordered by wave id.

    Raises:
        TrailerRepinError: When git cannot walk *target_ref*.
    """
    landed, index = first_parent_trailer_index(target_ref=target_ref, repo_root=repo_root)
    patch_ids: dict[str, str | None] = {}

    def cached_patch_id(sha: str) -> str | None:
        if sha not in patch_ids:
            patch_ids[sha] = patch_id(sha, repo_root=repo_root)
        return patch_ids[sha]

    rows: list[TrailerRepin] = []
    for wave_id in sorted(pins):
        old = pins[wave_id]
        candidates = tuple(index.get(wave_id, ()))
        new: str | None = None
        outcome: TrailerRepinOutcome
        if old in landed:
            new, outcome = old, "unchanged"
        elif not candidates:
            outcome = "unresolved"
        elif len(candidates) == 1:
            new, outcome = candidates[0], "unique_trailer"
        else:
            old_patch = cached_patch_id(old)
            same_patch = [c for c in candidates if old_patch and cached_patch_id(c) == old_patch]
            if len(same_patch) == 1:
                new, outcome = same_patch[0], "patch_id"
            else:
                outcome = "ambiguous"
        rows.append(
            TrailerRepin(
                wave_id=wave_id,
                old_commit=old,
                new_commit=new,
                outcome=outcome,
                candidates=candidates,
            )
        )
    logger.info(
        f"resolve_trailer_repins target={target_ref} pins={len(pins)} "
        f"moved={sum(r.outcome in ('unique_trailer', 'patch_id') for r in rows)} "
        f"undecided={sum(r.new_commit is None for r in rows)}"
    )
    return rows
