"""``create_worktree`` — materialise a per-wave worktree branch.

Public API:
    create_worktree(state, *, repo_root, wave_id, ...) -> WorktreeRecord

The function mutates the supplied :class:`State` in place
(:attr:`State.worktrees` and the wave's ``worktree_id``) and returns the
freshly-built :class:`WorktreeRecord`. Caller holds
``portalock(state.json)`` via :func:`eawf.surfaces.cli._mutation.state_transaction`
and the worktree-registry lock via
:func:`eawf.runtime.worktree.locks.worktree_registry_lock`. The function never
opens its own locks — re-entry into ``portalock`` would deadlock.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

import eawf.runtime.worktree.git as git
from eawf.kernel.state.enums import WaveStatus, WorktreeStatus
from eawf.kernel.state.ids import is_wave_id
from eawf.kernel.state.models import State, Wave, WorktreeRecord
from eawf.runtime.sandbox.cwd_guard import is_path_inside
from eawf.surfaces.cli import errors as cli_errors

logger = logging.getLogger(__name__)


_DEFAULT_BRANCH_PREFIX: str = "feature/eawf-v0.1"
# ``.`` stays in the regex because the canonical default branch name
# ``feature/eawf-v0.1-pNN-wMM`` carries a dot in the version segment.
# Git's own ref-name rules already reject dangerous shapes (``..``,
# trailing ``.lock``, leading ``/``); the validator below piles on a
# whitespace check so the regex itself can stay permissive.
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _slugify_wave(wave_id: str) -> str:
    """Lower-case the wave id's phase/wave segments for use in branch names.

    ``P05-I01-W01`` -> ``p05-i01-w01``. Strips the iter segment per
    AGENTS.md rule 14 (``feature/eawf-v0.1-pNN-wMM``).
    """
    if not is_wave_id(wave_id):
        raise cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput")
    parts = wave_id.split("-")
    # parts == ["P05", "I01", "W01"] -> "p05-w01"
    return f"{parts[0].lower()}-{parts[2].lower()}"


def _default_branch_name(wave_id: str) -> str:
    """Compose the default per-wave branch name."""
    return f"{_DEFAULT_BRANCH_PREFIX}-{_slugify_wave(wave_id)}"


def _default_path(repo_root: Path, wave_id: str) -> Path:
    """Compose the default worktree path under ``.ea/worktrees/<suffix>/``.

    The "suffix" is the wave-id slug — e.g., ``p05-w01`` for wave
    ``P05-I01-W01``. Worktrees live under ``<repo_root>/.ea/worktrees/``
    rather than sibling repositories so the operator's working tree
    stays self-contained; ``.ea/worktrees/`` is gitignored alongside
    ``.ea/locks/`` and ``.ea/local/``.
    """
    suffix = _slugify_wave(wave_id)
    return repo_root / ".ea" / "worktrees" / suffix


def _validate_path_inside_repo(repo_root: Path, target: Path) -> None:
    """Refuse if *target* resolves outside *repo_root* (path-traversal guard).

    Thin adapter over :func:`eawf.runtime.sandbox.cwd_guard.is_path_inside`
    that maps the boolean predicate onto the worktree CLI's
    :class:`~eawf.surfaces.cli.errors.UserError` contract (``kind=
    "InvalidInput"``). The shared predicate handles ``..`` segments,
    absolute paths, and symlink resolution.
    """
    if is_path_inside(target, root=repo_root):
        return
    target_resolved = target.resolve(strict=False)
    repo_resolved = repo_root.resolve(strict=False)
    raise cli_errors.UserError(
        f"worktree path {target_resolved} resolves outside repo root {repo_resolved}",
        kind="InvalidInput",
    )


def _make_id(wave_id: str, *, now: datetime) -> str:
    """Build a :class:`IdStr`-compliant worktree id.

    Format: ``WT-<wave_id>-<UTC-epoch-seconds>``. The epoch suffix
    avoids collisions when an abandoned worktree is re-created without
    changing the wave id. ``IdStr`` is ``^\\S+$`` so dashes are fine.
    """
    return f"WT-{wave_id}-{int(now.timestamp())}"


def _validate_wave_for_worktree(state: State, *, wave_id: str) -> Wave:
    """Validate *wave_id* and return its (CLAIMED/IN_PROGRESS) wave record.

    Raises:
        UserError: when the wave id is malformed or the wave is not in a
            worktree-eligible status (``kind="InvalidInput"``); or when the
            wave id is absent from ``state.waves`` (``kind="NotFound"``).
    """
    if not is_wave_id(wave_id):
        raise cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput")
    wave = state.waves.get(wave_id)
    if wave is None:
        raise cli_errors.UserError(f"unknown wave: {wave_id}", kind="NotFound")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise cli_errors.UserError(
            f"wave {wave_id!r} must be CLAIMED or IN_PROGRESS to create a worktree "
            f"(current status: {wave.status.value})",
            kind="InvalidInput",
        )
    return wave


def _resolve_branch_name(branch: str | None, *, wave_id: str) -> str:
    """Return the validated branch name (explicit or default for *wave_id*).

    Raises:
        UserError: when the branch name is empty, carries whitespace, or
            contains characters outside :data:`_BRANCH_NAME_RE`
            (``kind="InvalidInput"``).
    """
    chosen_branch = branch or _default_branch_name(wave_id)
    if not chosen_branch.strip() or " " in chosen_branch or "\t" in chosen_branch:
        raise cli_errors.UserError(
            f"branch name must be non-empty and whitespace-free: {chosen_branch!r}",
            kind="InvalidInput",
        )
    if not _BRANCH_NAME_RE.fullmatch(chosen_branch):
        raise cli_errors.UserError(
            f"branch name contains illegal characters: {chosen_branch!r}", kind="InvalidInput"
        )
    return chosen_branch


def _guarded_default_branch(state: State, default_branch: str | None) -> str | None:
    """Resolve the branch the "refuse to branch from main" guard protects."""
    if default_branch is not None:
        return default_branch
    if state.project is not None:
        return state.project.default_branch
    return None


def _resolve_base_branch(
    state: State,
    *,
    repo_root: Path,
    base: str | None,
    explicit_base: bool,
    default_branch: str | None,
) -> str:
    """Resolve the base ref, refusing to branch from the default branch.

    When *base* is ``None`` the current branch is used (AGENTS.md rule 11);
    an explicit *base* bypasses the guard only when *explicit_base* is set.

    Raises:
        UserError: when the resolved base is the guarded default branch
            and the explicit-override opt-in was not given
            (``kind="InvalidInput"``).
    """
    guarded_default = _guarded_default_branch(state, default_branch)
    if base is None:
        # The current-branch resolver already refuses detached HEAD with
        # UserError (kind="InvalidInput").
        chosen_base = git.current_branch(repo_root)
        if guarded_default and chosen_base == guarded_default:
            raise cli_errors.UserError(
                f"worktree create refuses to branch from {guarded_default!r}; "
                f"switch to a feature branch first or pass --base explicitly",
                kind="InvalidInput",
            )
        return chosen_base
    if not explicit_base and guarded_default and base == guarded_default:
        # Library callers passing base= directly must opt into the
        # explicit-override semantics; otherwise the guard fires.
        raise cli_errors.UserError(
            f"worktree create refuses to branch from {guarded_default!r} "
            f"without explicit_base=True",
            kind="InvalidInput",
        )
    return base


def _resolve_worktree_path(
    repo_root: Path, *, path: Path | None, wave_id: str, force: bool
) -> Path:
    """Resolve + validate the on-disk worktree path, clearing an empty dir under *force*.

    Raises:
        UserError: when the path resolves outside *repo_root*, is a
            non-empty directory, or already exists (empty) without *force*
            (``kind="InvalidInput"``).
    """
    chosen_path = path or _default_path(repo_root, wave_id)
    _validate_path_inside_repo(repo_root, chosen_path)
    if chosen_path.exists():
        if any(chosen_path.iterdir()):
            raise cli_errors.UserError(
                f"worktree path {chosen_path} is non-empty; refuse to overwrite",
                kind="InvalidInput",
            )
        if not force:
            raise cli_errors.UserError(
                f"worktree path {chosen_path} already exists (empty); pass --force to reuse",
                kind="InvalidInput",
            )
        # Empty dir + --force: git worktree add will refuse if dir exists,
        # so unlink and let git create it fresh.
        chosen_path.rmdir()
    return chosen_path


def create_worktree(
    state: State,
    *,
    repo_root: Path,
    wave_id: str,
    branch: str | None = None,
    base: str | None = None,
    path: Path | None = None,
    session_id: str | None = None,
    force: bool = False,
    explicit_base: bool = False,
    default_branch: str | None = None,
) -> WorktreeRecord:
    """Materialise a per-wave worktree and append a :class:`WorktreeRecord`.

    Args:
        state: Mutated in place (``state.worktrees`` and the wave's
            ``worktree_id``). Caller holds ``portalock(state.json)``.
        repo_root: Repository root (the directory containing ``.git/``).
        wave_id: Target wave id (regex-validated).
        branch: Optional explicit branch name; defaults to
            ``feature/eawf-v0.1-<phase>-<wave>``.
        base: Optional explicit base ref. When ``None``, the helper
            uses :func:`git.current_branch` and refuses if it is
            ``main``/the project's ``default_branch``. *explicit_base*
            must be ``True`` when *base* is user-supplied.
        path: Optional explicit on-disk path; defaults to
            ``<repo_root>/.ea/worktrees/<branch-suffix>/``.
        session_id: Optional :class:`AgentSession` id to record as the
            owner for provenance.
        force: When ``True`` and an empty directory already exists at
            *path*, proceed; otherwise refuse on any pre-existing dir.
        explicit_base: Set by the CLI to ``True`` when the operator
            passed ``--base``. Bypasses the "refuse to branch from main"
            guard so deliberate off-main experiments stay possible.
        default_branch: Project's default branch (typically ``main``).
            Defaults to ``state.project.default_branch`` when present.

    Returns:
        The new :class:`WorktreeRecord`.

    Raises:
        UserError: Bad inputs (regex, detached HEAD, branch already
            exists, path outside repo, target dir non-empty)
            (``kind="InvalidInput"``); or wave id absent from
            ``state.waves`` or git repo absent (``kind="NotFound"``).
        StateConflict: ``git worktree add`` failed for an unmapped reason
            (``kind="IntegrityViolation"``); or git's own registry was
            contended (mapped from "already a working tree";
            ``kind="LockConflict"``).
    """
    # ---- 1. Validate wave id + presence -----------------------------------
    wave = _validate_wave_for_worktree(state, wave_id=wave_id)

    # ---- 2. Resolve branch + base ----------------------------------------
    chosen_branch = _resolve_branch_name(branch, wave_id=wave_id)
    chosen_base = _resolve_base_branch(
        state,
        repo_root=repo_root,
        base=base,
        explicit_base=explicit_base,
        default_branch=default_branch,
    )
    if git.branch_exists(repo_root, chosen_branch):
        raise cli_errors.UserError(
            f"branch {chosen_branch!r} already exists locally; pick another name "
            f"or delete the existing branch first",
            kind="InvalidInput",
        )

    # ---- 3. Resolve path + path-traversal guard --------------------------
    chosen_path = _resolve_worktree_path(repo_root, path=path, wave_id=wave_id, force=force)

    # ---- 4. Shell `git worktree add` -------------------------------------
    chosen_path.parent.mkdir(parents=True, exist_ok=True)
    git.worktree_add(repo_root, branch=chosen_branch, path=chosen_path, base=chosen_base)

    # ---- 5. Append WorktreeRecord ---------------------------------------
    now = datetime.now(UTC)
    record_id = _make_id(wave_id, now=now)
    if state.worktrees and record_id in state.worktrees:
        # Epoch collision (sub-second re-create on a fast loop). Fall back
        # to a microsecond-suffixed id so we always emit a unique key.
        record_id = f"{record_id}-{now.microsecond:06d}"
    rel_path = chosen_path.resolve().relative_to(repo_root.resolve())
    record = WorktreeRecord(
        id=record_id,
        wave_id=wave_id,
        branch=chosen_branch,
        path=str(rel_path),
        base_branch=chosen_base,
        status=WorktreeStatus.ACTIVE,
        owner_session_id=session_id,
        created_at=now,
        merged_commit=None,
    )
    if state.worktrees is None:
        state.worktrees = {}
    state.worktrees[record.id] = record
    wave.worktree_id = record.id
    state.updated_at = now
    logger.info(f"create_worktree wave={wave_id} branch={chosen_branch} path={chosen_path}")
    return record


__all__ = [
    "create_worktree",
]
