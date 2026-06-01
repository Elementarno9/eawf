"""Phase lifecycle command handlers.

Split out of :mod:`eawf.surfaces.cli.commands.lifecycle` (P27-W06). The ``phase_app``
Typer app and the shared transaction helpers live in the parent module;
this module attaches the phase command bodies via ``@phase_app.command(...)``
and owns the phase-activate git gates plus the phase-close checklist. The
``project init`` / ``subproject add·switch`` setup verbs live in
:mod:`eawf.surfaces.cli.commands.lifecycle_iter`.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.kernel.state.enums import (
    IterStatus,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.ids import is_phase_id, natural_key
from eawf.kernel.state.mutations import MutationKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import (
    _append_event,
    _load_state_readonly,
    _read_state_payload,
    _run_mutation,
    _state_version,
    phase_app,
)
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


def _wrap_no_return(_value: object) -> None:
    """Adapter so transition helpers can be passed directly to ``mutate=``."""
    return None


def _phase_close_requires_release_preflight(ctx: typer.Context) -> bool:
    """Return whether release cadence config gates ``phase close``."""
    from eawf.kernel.config.layered import merge_config
    from eawf.runtime.vcs.coauthor import VcsConfig

    flags: GlobalFlags = ctx.obj
    state_path = resolve_state_path(flags.workspace)
    anchor = state_path.parent.parent
    merged, _sources = merge_config(repo=anchor, workspace=anchor)
    vcs_config = VcsConfig.model_validate(merged.get("vcs", {}))
    return vcs_config.conventions.release.cadence == "per-phase"


# ---- Phase handlers ---------------------------------------------------------


@phase_app.command("open")
def phase_open_cmd(
    ctx: typer.Context,
    phase_id: Annotated[
        str | None,
        typer.Argument(help="Explicit phase ID like P03 (omit with --auto)."),
    ] = None,
    auto: Annotated[
        bool, typer.Option("--auto", help="Auto-allocate the next free P<NN>.")
    ] = False,
    title: Annotated[str | None, typer.Option("--title", help="Phase title.")] = None,
    scope: Annotated[
        str | None,
        typer.Option("--scope", help="Phase scope_id (defaults to project code)."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="Optional long-form phase description (≤500 chars)."),
    ] = None,
) -> None:
    """Open a new phase. Provide an explicit ID or use ``--auto``."""
    from eawf.workflow.lifecycle.allocator import allocate_phase_id
    from eawf.workflow.lifecycle.transitions import open_phase

    flags: GlobalFlags = ctx.obj
    if title is None:
        cli_errors.emit_error(
            cli_errors.UserError("--title required", kind="InvalidInput"), flags=flags
        )
        return
    if (phase_id is None) == (not auto):
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of <id> or --auto must be provided", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if phase_id is not None and not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return

    chosen: dict[str, str] = {}

    def _mutator(state: State) -> None:
        target = allocate_phase_id(state) if auto else phase_id
        assert target is not None  # validated above
        chosen["id"] = target
        open_phase(
            state,
            phase_id=target,
            title=title,
            scope_id=scope,
            description=description,
        )

    _run_mutation(
        ctx,
        command="phase open",
        args={
            "id": phase_id,
            "auto": auto,
            "title": title,
            "scope": scope,
            "description": description,
        },
        scope_id_factory=lambda: chosen["id"],
        text_factory=lambda: f"phase open {chosen['id']} title={title!r}",
        envelope_factory=lambda: {
            "phase": chosen["id"],
            "title": title,
            "description": description,
        },
        mutate=_mutator,
    )


@phase_app.command("close")
def phase_close_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase ID to close.")],
    audit: Annotated[str, typer.Option("--audit", help="Audit ID providing closure evidence.")],
    checkpoint: Annotated[
        str | None,
        typer.Option("--checkpoint", help="Optional commit SHA marking the close."),
    ] = None,
) -> None:
    """Close an active phase. Rejects when child iters are still open."""
    from eawf.workflow.lifecycle.transitions import LifecycleError, close_phase

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return

    # The ``close_phase`` transition is authoritative for every close
    # blocker (open iters, no closed wave, closed iters missing audit,
    # single-wave-without-decision) and enforces them atomically under the
    # write lock on both the daemon-proxy and in-process paths. The
    # read-only pre-flight below is a friendly early-exit: it surfaces the
    # aggregated checklist blockers before any wire traffic so the operator
    # sees all problems at once instead of the transition's first failure.
    loaded = _load_state_readonly(ctx)
    if loaded is None:
        return
    preflight_state, _ = loaded
    require_release_preflight = _phase_close_requires_release_preflight(ctx)
    try:
        checklist = _phase_prepare_close_checklist(
            preflight_state,
            phase_id=phase_id,
            audit_id=audit,
            require_audit=True,
            require_release_preflight=require_release_preflight,
        )
    except LifecycleError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    if checklist["blockers"]:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"phase close blocked: {'; '.join(checklist['blockers'])}"),
            flags=flags,
        )
        return

    def _mutator(state: State) -> None:
        close_phase(
            state,
            phase_id=phase_id,
            audit_id=audit,
            checkpoint=checkpoint,
            require_release_preflight=require_release_preflight,
        )

    _run_mutation(
        ctx,
        command="phase close",
        args={"id": phase_id, "audit": audit, "checkpoint": checkpoint},
        scope_id=phase_id,
        text=f"phase close {phase_id} audit={audit}",
        envelope=lambda: {
            "phase": phase_id,
            "audit": audit,
            "checkpoint": checkpoint,
        },
        mutate=_mutator,
        closure_kind=True,
        mutation_kind=MutationKind.PHASE_CLOSE,
        params={"phase_id": phase_id, "audit_id": audit, "checkpoint": checkpoint},
    )


# Timeout for git invocations in the phase-activate gate. A clean
# eawf checkout returns each of these subcommands in well under 5 s; the
# 30 s headroom absorbs cold caches, slow file systems, and a small
# ``git fetch`` of one branch from the configured remote.
_PHASE_ACTIVATE_GIT_TIMEOUT: float = 30.0


def _phase_activate_find_repo_root(state_path: Path) -> Path | None:
    """Locate the enclosing git repo for *state_path*, or ``None`` when there is none.

    Walks the ancestor chain looking for a ``.git`` directory or file (the
    file variant covers worktrees). Returns ``None`` when no enclosing
    repository is found — the git-touching gates then skip cleanly, which
    keeps non-repo workspaces (e.g. tests under ``tmp_path``) working.
    """
    start = state_path.parent.resolve()
    for candidate in (start, *start.parents):
        git_marker = candidate / ".git"
        if git_marker.exists():
            return candidate
    return None


def _phase_activate_run_git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    """Run ``git`` with the phase-activate timeout and captured stdio.

    Maps the timeout into :class:`cli_errors.StateConflict`
    (``kind="IntegrityViolation"``) so the operator gets exit 8 (transient
    infrastructure failure) instead of a surface-level rejection.
    """
    logger.info(f"_phase_activate_run_git args={args} cwd={cwd}")
    try:
        return subprocess.run(
            args,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=False,
            timeout=_PHASE_ACTIVATE_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise cli_errors.StateConflict(
            f"git command timed out after {_PHASE_ACTIVATE_GIT_TIMEOUT}s: {' '.join(args)}",
            kind="IntegrityViolation",
        ) from exc


def _phase_activate_dirty_lines(repo: Path) -> list[str]:
    """Return ``git status --porcelain`` lines from *repo* (empty list when clean).

    Surfaces a :class:`cli_errors.StateConflict` (``kind="IntegrityViolation"``)
    when the porcelain invocation itself fails (rc != 0); a clean tree
    returns ``[]`` and a dirty tree returns one entry per dirty path.
    """
    res = _phase_activate_run_git(["git", "-C", str(repo), "status", "--porcelain"], cwd=repo)
    if res.returncode != 0:
        stderr = (res.stderr or "").strip()
        raise cli_errors.StateConflict(
            f"git status failed (rc={res.returncode}): {stderr or 'unknown'}",
            kind="IntegrityViolation",
        )
    return [line for line in (res.stdout or "").splitlines() if line.strip()]


def _phase_activate_fetch_base(repo: Path, *, default_branch: str, remote: str = "origin") -> bool:
    """Fetch ``<remote>/<default_branch>`` so the currency gate sees fresh tips.

    Safe-degrades when the configured remote is missing or the fetch
    fails (offline, auth refused, branch not on remote). The currency
    gate downstream will then either find no ``<remote>/<default_branch>``
    ref and skip cleanly, or compare against the previously-cached tip.

    Args:
        repo: Repository root containing ``.git``.
        default_branch: Branch name on the remote to refresh.
        remote: Remote alias to fetch from (defaults to ``origin``).

    Returns:
        ``True`` when the fetch ran and exited zero; ``False`` when the
        remote is absent or the fetch failed. The boolean is logged but
        never raised — failure must not block activation on its own.
    """
    remotes = _phase_activate_run_git(["git", "-C", str(repo), "remote"], cwd=repo)
    if remotes.returncode != 0:
        logger.info(f"_phase_activate_fetch_base remote_list_rc={remotes.returncode}")
        return False
    remote_names = {line.strip() for line in (remotes.stdout or "").splitlines() if line.strip()}
    if remote not in remote_names:
        logger.info(f"_phase_activate_fetch_base no_remote remote={remote!r}")
        return False
    fetched = _phase_activate_run_git(
        ["git", "-C", str(repo), "fetch", "--quiet", remote, default_branch],
        cwd=repo,
    )
    if fetched.returncode != 0:
        logger.warning(
            f"_phase_activate_fetch_base fetch_failed remote={remote!r} "
            f"base={default_branch!r} rc={fetched.returncode} "
            f"stderr={(fetched.stderr or '').strip()!r}"
        )
        return False
    logger.info(f"_phase_activate_fetch_base ok remote={remote!r} base={default_branch!r}")
    return True


def _phase_activate_base_behind_count(repo: Path, *, default_branch: str) -> int | None:
    """Return commit count by which ``HEAD`` is behind ``origin/<default_branch>``.

    Returns ``None`` when the currency check should be skipped: no
    ``origin/<default_branch>`` ref is present locally. A non-negative
    integer is the count of commits in ``origin/<default_branch>..HEAD``'s
    reverse range — i.e. the number of commits the local branch is behind.

    Callers should invoke :func:`_phase_activate_fetch_base` first so the
    remote ref is fresh; this helper itself does not network.
    """
    ref = f"origin/{default_branch}"
    verify = _phase_activate_run_git(
        ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", ref],
        cwd=repo,
    )
    if verify.returncode != 0:
        logger.info(f"_phase_activate_base_behind_count missing_ref={ref!r}")
        return None
    count_res = _phase_activate_run_git(
        ["git", "-C", str(repo), "rev-list", "--count", f"HEAD..{ref}"],
        cwd=repo,
    )
    if count_res.returncode != 0:
        stderr = (count_res.stderr or "").strip()
        raise cli_errors.StateConflict(
            f"git rev-list --count failed (rc={count_res.returncode}): {stderr or 'unknown'}",
            kind="IntegrityViolation",
        )
    stdout = (count_res.stdout or "").strip()
    try:
        return int(stdout)
    except ValueError as exc:
        raise cli_errors.StateConflict(
            f"git rev-list --count returned non-int output: {stdout!r}", kind="IntegrityViolation"
        ) from exc


def _phase_activate_git_gates(
    state_path: Path,
    *,
    default_branch: str,
    allow_stale: bool = False,
) -> None:
    """Run the dirty-worktree + base-currency gates ahead of the mutation.

    Skips silently when ``state_path`` is not inside a git repository or
    when ``git`` is not installed on PATH — both are valid configurations
    (tmp test workspaces, non-git Eä deployments).

    Before the currency comparison runs, a best-effort
    :func:`_phase_activate_fetch_base` refreshes ``origin/<default_branch>``
    so the local cached tip cannot mask remote advances. Fetch failures
    (offline, missing remote, auth) are logged and skipped — the count
    step then either finds no remote ref and skips or compares against
    the stale-but-cached tip.

    When *allow_stale* is true, the currency gate is bypassed entirely
    (dirty-worktree gate still runs). The operator opted into landing
    on a behind base; the bypass is logged so audit reconstruction can
    see the decision.

    Raises:
        cli_errors.UserError: when the worktree is dirty (gate 3) or
            ``HEAD`` is behind ``origin/<default_branch>`` (gate 2)
            (``kind="InvalidInput"``).
        cli_errors.StateConflict: when an underlying ``git``
            invocation fails or times out unexpectedly
            (``kind="IntegrityViolation"``).
    """
    if shutil.which("git") is None:
        logger.info("_phase_activate_git_gates skip=no_git_binary")
        return
    repo = _phase_activate_find_repo_root(state_path)
    if repo is None:
        logger.info(f"_phase_activate_git_gates skip=not_a_git_repo state_path={state_path}")
        return
    dirty = _phase_activate_dirty_lines(repo)
    if dirty:
        logger.info(f"_phase_activate_git_gates dirty=True entries={len(dirty)} repo={repo}")
        raise cli_errors.UserError(
            f"refusing to activate phase: worktree is dirty ({len(dirty)} entries); "
            "commit, stash, or discard local changes first",
            kind="InvalidInput",
        )
    if allow_stale:
        logger.info(
            f"_phase_activate_git_gates currency=bypassed allow_stale=True "
            f"default_branch={default_branch!r} repo={repo}"
        )
        return
    fetched = _phase_activate_fetch_base(repo, default_branch=default_branch)
    logger.info(f"_phase_activate_git_gates fetched={fetched} default_branch={default_branch!r}")
    behind = _phase_activate_base_behind_count(repo, default_branch=default_branch)
    if behind is None:
        logger.info(
            f"_phase_activate_git_gates currency=skipped "
            f"default_branch={default_branch!r} repo={repo}"
        )
        return
    if behind > 0:
        logger.info(
            f"_phase_activate_git_gates behind={behind} "
            f"default_branch={default_branch!r} repo={repo}"
        )
        raise cli_errors.UserError(
            f"refusing to activate phase: HEAD is {behind} commits behind "
            f"origin/{default_branch}; rebase first (or pass --allow-stale to override)",
            kind="InvalidInput",
        )
    logger.info(f"_phase_activate_git_gates ok behind=0 default_branch={default_branch!r}")


def _phase_activate_project_default_branch(state_path: Path) -> str | None:
    """Peek at ``state.project.default_branch`` without acquiring the sibling lock.

    Returns ``None`` when the state file is missing, unreadable, or carries
    no ``project.default_branch`` field (e.g. a partially-initialised state).
    The currency gate is skipped when the default branch cannot be resolved.
    """
    if not state_path.exists():
        return None
    try:
        payload = orjson.loads(state_path.read_bytes())
    except orjson.JSONDecodeError:
        return None
    project = payload.get("project") if isinstance(payload, dict) else None
    if not isinstance(project, dict):
        return None
    default_branch = project.get("default_branch")
    if not isinstance(default_branch, str) or not default_branch.strip():
        return None
    return default_branch


@phase_app.command("activate")
def phase_activate_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="PLANNED phase id to activate.")],
    allow_stale: Annotated[
        bool,
        typer.Option(
            "--allow-stale",
            help=(
                "Bypass the base-currency gate (P19-W13). Operator opts into "
                "activating the phase even when HEAD is behind "
                "origin/<default_branch>. The dirty-worktree gate still runs."
            ),
        ),
    ] = False,
) -> None:
    """Flip a PLANNED phase to ACTIVE (P19-W07, P19-W11, P19-W13).

    Runs the V11 hard gate: the phase must already carry at least one
    planned wave, and every phase listed in ``Phase.depends_on`` must
    be CLOSED. Sets ``current.phase_id`` to *phase_id*.

    P19-W11 extends the gate with two pre-mutation git checks:

    - **Dirty worktree**: ``git status --porcelain`` must be empty.
      Operator-owned local edits get committed/stashed/discarded before
      a phase activates so the activation lands on a known tree.
    - **Stale base branch**: when an ``origin`` remote is configured and
      ``origin/<project.default_branch>`` resolves, ``git fetch`` then
      compare; reject when ``HEAD`` is behind. Skipped when no remote,
      no ref, or no git binary on PATH (the activation still falls
      through to the library no-waves gate in those configurations).

    P19-W13 wires an explicit ``git fetch origin <default_branch>`` step
    in front of the behind-count so the comparison sees fresh tips, and
    exposes an ``--allow-stale`` override that lets the operator
    deliberately bypass the currency gate (dirty-worktree gate still
    runs). Fetch failures degrade safely — a missing or unreachable
    remote logs and falls through to the count step, which then skips
    when the local cache also lacks the ref.
    """
    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    default_branch = _phase_activate_project_default_branch(state_path)
    if default_branch is not None:
        try:
            _phase_activate_git_gates(
                state_path,
                default_branch=default_branch,
                allow_stale=allow_stale,
            )
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return
    else:
        logger.info(f"phase_activate_cmd skip_git_gates=no_default_branch state_path={state_path}")
    from eawf.workflow.lifecycle.transitions import activate_phase

    _run_mutation(
        ctx,
        command="phase activate",
        args={"id": phase_id},
        scope_id=phase_id,
        text=f"phase activate {phase_id}",
        envelope=lambda: {"phase": phase_id, "status": "active"},
        mutate=lambda state: _wrap_no_return(activate_phase(state, phase_id=phase_id)),
        mutation_kind=MutationKind.PHASE_ACTIVATE,
        params={"phase_id": phase_id},
    )


@phase_app.command("reopen")
def phase_reopen_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase ID to reopen.")],
) -> None:
    """Reopen a closed phase. Used for follow-up iters after a phase close."""
    from eawf.workflow.lifecycle.transitions import reopen_phase

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="phase reopen",
        args={"id": phase_id},
        scope_id=phase_id,
        text=f"phase reopen {phase_id}",
        envelope=lambda: {"phase": phase_id},
        mutate=lambda state: _wrap_no_return(reopen_phase(state, phase_id=phase_id)),
    )


@phase_app.command("retro")
def phase_retro_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase ID to digest.")],
    md: Annotated[
        bool, typer.Option("--md", help="Render the digest as a markdown table.")
    ] = False,
) -> None:
    """Emit a closure digest joining the phase's waves to their agent reports.

    Read-only. For each wave under *phase_id*, the digest joins the wave to its
    agent report(s) by ``wave_id == base_id`` and surfaces per-wave status,
    outcome, and report verdict (``no report`` when none exists), flags failed
    waves, and prints a phase-level summary (closed / failed / reportless
    counts). ``--md`` emits a markdown table; ``--json`` (top-level flag) emits
    the JSON payload. Renders honestly empty when no reports exist.
    """
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.agent_report.rollup import (
        phase_retro_digest,
        render_phase_retro_markdown,
    )

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if md and flags.json_output:
        cli_errors.emit_error(
            cli_errors.UserError("--md and --json are contradictory", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    try:
        with state_transaction(state_path, read_only=True) as state:
            digest = phase_retro_digest(state, state_path, phase_id)
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    if md:
        emit_json_or_text(digest.as_payload(), render_phase_retro_markdown(digest), flags=flags)
        return
    text = (
        f"phase retro {phase_id}: {digest.wave_count} wave(s), "
        f"{digest.closed_count} closed, {digest.failed_count} failed, "
        f"{digest.reportless_count} reportless"
    )
    emit_json_or_text(digest.as_payload(), text, flags=flags)


def _closed_wave_commit_summary(
    state: State,
    *,
    iter_ids_in_phase: set[str],
    derive_wave_sha: Callable[[str], str | None],
) -> dict[str, Any]:
    """Summarise commit coverage across a phase's CLOSED waves.

    Returns:
        A dict with ``closed_wave_count``, ``unique_closed_wave_commit_count``,
        and ``closed_waves_missing_commit`` (sorted wave ids lacking a SHA).
    """
    closed_wave_ids = sorted(
        (
            wid
            for wid, w in state.waves.items()
            if w.iter_id in iter_ids_in_phase and w.status == WaveStatus.CLOSED
        ),
        key=natural_key,
    )
    closed_wave_shas = [derive_wave_sha(wid) for wid in closed_wave_ids]
    closed_waves_missing_commit = sorted(
        (wid for wid, sha in zip(closed_wave_ids, closed_wave_shas, strict=True) if not sha),
        key=natural_key,
    )
    unique_closed_wave_commits = sorted({sha for sha in closed_wave_shas if sha})
    return {
        "closed_wave_count": len(closed_wave_ids),
        "unique_closed_wave_commit_count": len(unique_closed_wave_commits),
        "closed_waves_missing_commit": closed_waves_missing_commit,
    }


def _phase_prepare_close_checklist(
    state: State,
    *,
    phase_id: str,
    audit_id: str | None = None,
    require_audit: bool = False,
    require_release_preflight: bool = False,
) -> dict[str, Any]:
    """Compute a structured pre-close checklist for *phase_id*.

    Items: open iters, open waves, audit linkage, waves missing commit/outcome.
    The handler renders ``ok=True`` only when every blocking item resolves to
    empty.
    """
    from eawf.workflow.lifecycle.transitions import (
        LifecycleError,
        has_scope_collapse_decision,
        phase_close_readiness,
    )
    from eawf.workflow.lifecycle.wave_sha import derive_wave_sha

    phase = state.phases.get(phase_id)
    if phase is None:
        raise LifecycleError(f"unknown phase: {phase_id!r}")
    iter_ids_in_phase = {iid for iid, it in state.iters.items() if it.phase_id == phase_id}
    open_iters = sorted(
        (
            iid
            for iid, it in state.iters.items()
            if it.phase_id == phase_id and it.status in {IterStatus.PLANNED, IterStatus.ACTIVE}
        ),
        key=natural_key,
    )
    open_waves = sorted(
        (
            wid
            for wid, w in state.waves.items()
            if w.iter_id in iter_ids_in_phase
            and w.status in {WaveStatus.PENDING, WaveStatus.CLAIMED, WaveStatus.IN_PROGRESS}
        ),
        key=natural_key,
    )
    iters_without_audit = sorted(
        (
            iid
            for iid, it in state.iters.items()
            if it.phase_id == phase_id and it.status == IterStatus.CLOSED and not it.audit_id
        ),
        key=natural_key,
    )

    commit_summary = _closed_wave_commit_summary(
        state, iter_ids_in_phase=iter_ids_in_phase, derive_wave_sha=derive_wave_sha
    )
    scope_collapse_decision = has_scope_collapse_decision(state, phase_id=phase_id)
    single_wave_without_decision = bool(
        commit_summary["closed_wave_count"] == 1 and not scope_collapse_decision
    )
    checklist = {
        "phase": phase_id,
        "phase_status": phase.status.value,
        "close_audit": audit_id,
        "open_iters": open_iters,
        "open_waves": open_waves,
        "closed_wave_count": commit_summary["closed_wave_count"],
        "unique_closed_wave_commit_count": commit_summary["unique_closed_wave_commit_count"],
        "closed_waves_missing_commit": commit_summary["closed_waves_missing_commit"],
        "iters_without_audit": iters_without_audit,
        "scope_collapse_decision": scope_collapse_decision,
        "single_wave_without_decision": single_wave_without_decision,
    }
    readiness = phase_close_readiness(
        state,
        phase_id=phase_id,
        audit_id=audit_id,
        require_audit=require_audit,
        require_release_preflight=require_release_preflight,
    )
    checklist["close_readiness_ready"] = readiness.ready
    checklist["close_readiness_warnings"] = list(readiness.warnings)
    checklist["close_readiness_warnings_count"] = len(readiness.warnings)
    checklist["close_audit_blockers"] = [
        warning for warning in readiness.warnings if warning.startswith("close audit")
    ]
    checklist["release_preflight_required"] = require_release_preflight
    checklist["release_preflight_blockers"] = [
        warning for warning in readiness.warnings if warning.startswith("release preflight")
    ]
    blockers = _phase_close_blockers(checklist)
    checklist["blockers"] = blockers
    checklist["ok"] = not blockers
    return checklist


def _phase_close_blockers(checklist: dict[str, Any]) -> list[str]:
    """Render actionable blocker strings from a phase-close checklist."""
    blockers: list[str] = []
    if checklist["open_iters"]:
        blockers.append(f"open iters: {', '.join(checklist['open_iters'])}")
    if checklist["open_waves"]:
        blockers.append(f"open waves: {', '.join(checklist['open_waves'])}")
    if checklist["iters_without_audit"]:
        blockers.append(
            f"closed iters missing audit: {', '.join(checklist['iters_without_audit'])}"
        )
    if checklist["single_wave_without_decision"]:
        blockers.append(
            "single closed wave requires an active phase decision documenting scope collapse"
        )
    for warning in checklist.get("close_readiness_warnings", []):
        if warning not in blockers:
            blockers.append(warning)
    return blockers


@phase_app.command("prepare-close")
def phase_prepare_close_cmd(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase ID to prepare for close.")],
    audit: Annotated[
        str | None,
        typer.Option("--audit", help="Optional close audit ID to validate."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run/--no-dry-run", help="Skip event emission.")
    ] = True,
    release_preflight: Annotated[
        bool,
        typer.Option(
            "--release-preflight/--no-release-preflight",
            help="Require the ship-gate audit to contain a passing release-preflight check.",
        ),
    ] = False,
) -> None:
    """Compute a pre-close checklist for *phase_id* without closing it.

    Read-only by default; pass ``--no-dry-run`` to emit a ``phase prepare-close``
    event into the JSONL store. ``state.json`` is never mutated by this command.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.state.models import State
    from eawf.kernel.store.paths import store_path
    from eawf.workflow.lifecycle.transitions import LifecycleError

    flags: GlobalFlags = ctx.obj
    if not is_phase_id(phase_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid phase id: {phase_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    try:
        state_path = resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    if not state_path.exists():
        cli_errors.emit_error(
            cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound"),
            flags=flags,
        )
        return
    try:
        payload = _read_state_payload(state_path)
        state = State.model_validate(payload)
    except PydValidationError as exc:
        cli_errors.emit_error(
            cli_errors.StateConflict(
                f"state at {state_path} fails schema validation: {exc}", kind="IntegrityViolation"
            ),
            flags=flags,
        )
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    try:
        checklist = _phase_prepare_close_checklist(
            state,
            phase_id=phase_id,
            audit_id=audit,
            require_audit=audit is not None,
            require_release_preflight=release_preflight,
        )
    except LifecycleError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    if not dry_run:
        version = _state_version(payload)
        events_path = store_path(state_path, StoreKind.EVENT)
        _append_event(
            events_path,
            command="phase prepare-close",
            args={"phase_id": phase_id, "audit": audit, "dry_run": False},
            scope_id=phase_id,
            before_version=version,
            after_version=version,
            summary=f"phase prepare-close {phase_id} ok={checklist['ok']}",
        )
    text = (
        f"phase prepare-close {phase_id} ok={checklist['ok']} "
        f"open_iters={len(checklist['open_iters'])} "
        f"open_waves={len(checklist['open_waves'])}"
    )
    emit_json_or_text(checklist, text, flags=flags)
