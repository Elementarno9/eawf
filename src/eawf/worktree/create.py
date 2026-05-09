"""``create_worktree`` — materialise a per-wave worktree branch.

Public API:
    create_worktree(state, *, repo_root, wave_id, ...) -> WorktreeRecord

The function mutates the supplied :class:`State` in place
(:attr:`State.worktrees` and the wave's ``worktree_id``) and returns the
freshly-built :class:`WorktreeRecord`. Caller holds
``portalock(state.json)`` via :func:`eawf.cli._mutation.state_transaction`
and the worktree-registry lock via
:func:`eawf.worktree.locks.worktree_registry_lock`. The function never
opens its own locks — re-entry into ``portalock`` would deadlock.
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from pathlib import Path

from eawf.cli import errors as cli_errors
from eawf.state.enums import WaveStatus, WorktreeStatus
from eawf.state.ids import is_wave_id
from eawf.state.models import State, WorktreeRecord
from eawf.worktree import git

logger = logging.getLogger(__name__)


_DEFAULT_BRANCH_PREFIX: str = "feature/eawf-v0.1"
_BRANCH_NAME_RE = re.compile(r"^[A-Za-z0-9._/\-]+$")


def _slugify_wave(wave_id: str) -> str:
    """Lower-case the wave id's phase/wave segments for use in branch names.

    ``P05-I01-W01`` -> ``p05-i01-w01``. Strips the iter segment per
    AGENTS.md rule 14 (``feature/eawf-v0.1-pNN-wMM``).
    """
    if not is_wave_id(wave_id):
        raise cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}")
    parts = wave_id.split("-")
    # parts == ["P05", "I01", "W01"] -> "p05-w01"
    return f"{parts[0].lower()}-{parts[2].lower()}"


def _default_branch_name(wave_id: str) -> str:
    """Compose the default per-wave branch name."""
    return f"{_DEFAULT_BRANCH_PREFIX}-{_slugify_wave(wave_id)}"


def _default_path(repo_root: Path, wave_id: str) -> Path:
    """Compose the default worktree path under ``.claude/worktrees/<suffix>/``.

    The "suffix" is the wave-id slug — e.g., ``p05-w01`` for wave
    ``P05-I01-W01``. Matches the canonical envelope shape in the wave
    spec §2 ("path": ".claude/worktrees/p05-w01"). Honours the
    :file:`feedback_worktree_location.md` user memo (worktrees live
    under the repo's :file:`.claude/worktrees/`, not in sibling dirs).
    """
    suffix = _slugify_wave(wave_id)
    return repo_root / ".claude" / "worktrees" / suffix


def _validate_path_inside_repo(repo_root: Path, target: Path) -> None:
    """Refuse if *target* resolves outside *repo_root* (path-traversal guard).

    Explicit absolute paths and ``..`` segments are honoured by
    :func:`pathlib.Path.resolve` then compared via
    :func:`pathlib.PurePath.is_relative_to`.
    """
    target_resolved = target.resolve(strict=False)
    repo_resolved = repo_root.resolve(strict=False)
    if not target_resolved.is_relative_to(repo_resolved):
        raise cli_errors.InvalidInput(
            f"worktree path {target_resolved} resolves outside repo root {repo_resolved}"
        )


def _make_id(wave_id: str, *, now: datetime) -> str:
    """Build a :class:`IdStr`-compliant worktree id.

    Format: ``WT-<wave_id>-<UTC-epoch-seconds>``. The epoch suffix
    avoids collisions when an abandoned worktree is re-created without
    changing the wave id. ``IdStr`` is ``^\\S+$`` so dashes are fine.
    """
    return f"WT-{wave_id}-{int(now.timestamp())}"


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
            ``<repo_root>/.claude/worktrees/<branch-suffix>/``.
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
        InvalidInput: Bad inputs (regex, detached HEAD, branch already
            exists, path outside repo, target dir non-empty).
        NotFound: Wave id absent from ``state.waves`` or git repo absent.
        IntegrityViolation: ``git worktree add`` failed for an unmapped
            reason.
        LockConflict: git's own registry was contended (mapped from
            "already a working tree").
    """
    # ---- 1. Validate wave id + presence -----------------------------------
    if not is_wave_id(wave_id):
        raise cli_errors.InvalidInput(f"invalid wave id: {wave_id!r}")
    wave = state.waves.get(wave_id)
    if wave is None:
        raise cli_errors.NotFound(f"unknown wave: {wave_id}")
    if wave.status not in {WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}:
        raise cli_errors.InvalidInput(
            f"wave {wave_id!r} must be CLAIMED or IN_PROGRESS to create a worktree "
            f"(current status: {wave.status.value})"
        )

    # ---- 2. Resolve branch + base ----------------------------------------
    chosen_branch = branch or _default_branch_name(wave_id)
    if not chosen_branch.strip() or " " in chosen_branch or "\t" in chosen_branch:
        raise cli_errors.InvalidInput(
            f"branch name must be non-empty and whitespace-free: {chosen_branch!r}"
        )
    if not _BRANCH_NAME_RE.fullmatch(chosen_branch):
        raise cli_errors.InvalidInput(f"branch name contains illegal characters: {chosen_branch!r}")

    if base is None:
        # AGENTS.md rule 11: branch from current feature branch HEAD,
        # not main. The current-branch resolver already refuses detached
        # HEAD with InvalidInput.
        chosen_base = git.current_branch(repo_root)
        guarded_default = default_branch
        if guarded_default is None and state.project is not None:
            guarded_default = state.project.default_branch
        if guarded_default and chosen_base == guarded_default:
            raise cli_errors.InvalidInput(
                f"worktree create refuses to branch from {guarded_default!r}; "
                f"switch to a feature branch first or pass --base explicitly"
            )
    else:
        chosen_base = base
        if not explicit_base:
            # Library callers passing base= directly must opt into the
            # explicit-override semantics; otherwise the guard fires.
            guarded_default = default_branch
            if guarded_default is None and state.project is not None:
                guarded_default = state.project.default_branch
            if guarded_default and chosen_base == guarded_default:
                raise cli_errors.InvalidInput(
                    f"worktree create refuses to branch from {guarded_default!r} "
                    f"without explicit_base=True"
                )

    if git.branch_exists(repo_root, chosen_branch):
        raise cli_errors.InvalidInput(
            f"branch {chosen_branch!r} already exists locally; pick another name "
            f"or delete the existing branch first"
        )

    # ---- 3. Resolve path + path-traversal guard --------------------------
    chosen_path = path or _default_path(repo_root, wave_id)
    _validate_path_inside_repo(repo_root, chosen_path)

    if chosen_path.exists():
        if any(chosen_path.iterdir()):
            raise cli_errors.InvalidInput(
                f"worktree path {chosen_path} is non-empty; refuse to overwrite"
            )
        if not force:
            raise cli_errors.InvalidInput(
                f"worktree path {chosen_path} already exists (empty); pass --force to reuse"
            )
        # Empty dir + --force: git worktree add will refuse if dir exists,
        # so unlink and let git create it fresh.
        chosen_path.rmdir()

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
    record = WorktreeRecord(
        id=record_id,
        wave_id=wave_id,
        branch=chosen_branch,
        path=str(chosen_path),
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
