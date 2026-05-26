"""Wave mutator command handlers (plan / claim / close / show / fail / update).

Split out of :mod:`eawf.surfaces.cli.commands.lifecycle` (P27-W06). The ``wave_app``
Typer app, the shared transaction helpers, and the wave git/commit-ref
helpers (``_resolve_commit_sha``, ``_resolve_repo_root_for_drift``,
``_wave_close_via_daemon``) live in the parent module; this module attaches
the wave mutator command bodies via ``@wave_app.command(...)``. The wave
read / dispatch / budget verbs live in
:mod:`eawf.surfaces.cli.commands.lifecycle_wave_read`.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.enums import (
    AgentSessionRole,
    EffortBucket,
    WaveStatus,
)
from eawf.kernel.state.ids import (
    is_iter_id,
    is_wave_id,
)
from eawf.kernel.state.mutations import MutationKind
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import (
    _load_state_readonly,
    _resolve_commit_sha,
    _resolve_repo_root_for_drift,
    _run_mutation,
    _wave_close_via_daemon,
    wave_app,
)
from eawf.surfaces.cli.flags import GlobalFlags

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

logger = logging.getLogger(__name__)


def _wrap_no_return(_value: object) -> None:
    """Adapter so transition helpers can be passed directly to ``mutate=``."""
    return None


@wave_app.command("plan")
def wave_plan_cmd(
    ctx: typer.Context,
    iter_id: Annotated[str, typer.Argument(help="Parent iter ID.")],
    wave_id: Annotated[str, typer.Option("--id", help="Explicit wave ID.")],
    title: Annotated[str, typer.Option("--title", help="Wave title.")],
    files: Annotated[
        str,
        typer.Option(
            "--files",
            help="Comma-separated file globs that the wave covers.",
        ),
    ],
    deps: Annotated[
        str | None,
        typer.Option("--deps", help="Comma-separated dep wave IDs (must already exist)."),
    ] = None,
    success_criteria: Annotated[
        str | None,
        typer.Option("--success", help="Comma-separated success criteria."),
    ] = None,
    agent_role: Annotated[
        AgentSessionRole | None,
        typer.Option("--agent-role", help="Executor role expected for the wave."),
    ] = None,
    effort_bucket: Annotated[
        EffortBucket | None,
        typer.Option("--effort-bucket", help="XS/S/M/L/XL estimate bucket."),
    ] = None,
    description: Annotated[
        str | None,
        typer.Option("--description", help="Optional long-form wave description (≤500 chars)."),
    ] = None,
) -> None:
    """Plan a new pending wave under an open iter."""
    from eawf.workflow.lifecycle.transitions import plan_wave

    flags: GlobalFlags = ctx.obj
    if not is_iter_id(iter_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid iter id: {iter_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if not wave_id.startswith(f"{iter_id}-W"):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"wave id {wave_id!r} does not belong to iter {iter_id!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    file_list = [f.strip() for f in files.split(",") if f.strip()]
    deps_list = [d.strip() for d in (deps or "").split(",") if d.strip()]
    criteria_list = [c.strip() for c in (success_criteria or "").split(",") if c.strip()]
    _run_mutation(
        ctx,
        command="wave plan",
        args={
            "iter_id": iter_id,
            "id": wave_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
        },
        scope_id=wave_id,
        text=f"wave plan {wave_id} iter={iter_id} title={title!r}",
        envelope=lambda: {
            "wave": wave_id,
            "iter": iter_id,
            "title": title,
            "files": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
        },
        mutate=lambda state: _wrap_no_return(
            plan_wave(
                state,
                wave_id=wave_id,
                iter_id=iter_id,
                title=title,
                file_scopes=file_list,
                deps=deps_list,
                success_criteria=criteria_list,
                agent_role=agent_role,
                effort_bucket=effort_bucket,
                description=description,
            )
        ),
        mutation_kind=MutationKind.ROADMAP_REVISE,
        params={
            "op": "add_wave",
            "wave_id": wave_id,
            "iter_id": iter_id,
            "title": title,
            "file_scopes": file_list,
            "deps": deps_list,
            "success_criteria": criteria_list,
            "agent_role": agent_role.value if agent_role else None,
            "effort_bucket": effort_bucket.value if effort_bucket else None,
            "description": description,
        },
    )


@wave_app.command("claim")
def wave_claim_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to claim.")],
    session: Annotated[str, typer.Option("--session", help="Claiming agent/operator session id.")],
    worktree_policy: Annotated[
        str,
        typer.Option(
            "--worktree-policy",
            help="One of current_branch|fresh_branch|inline.",
        ),
    ] = "current_branch",
    out_of_order: Annotated[
        bool,
        typer.Option(
            "--out-of-order",
            help=(
                "Bypass the W## monotonic gate (P19-W02). Use only for parallel-"
                "worktree dispatch where multiple siblings of the same dep "
                "frontier are claimed at once."
            ),
        ),
    ] = False,
) -> None:
    """Claim a pending wave for *session*. Exactly-once across concurrent calls."""
    from eawf.workflow.lifecycle.transitions import claim_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if worktree_policy not in {"current_branch", "fresh_branch", "inline"}:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--worktree-policy must be current_branch|fresh_branch|inline; "
                f"got {worktree_policy!r}",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    def _claim_with_budget_gate(state: State) -> None:
        wave = state.waves.get(wave_id)
        if (
            wave is not None
            and wave.token_budget is not None
            and wave.tokens_consumed >= wave.token_budget
        ):
            raise cli_errors.ValidationError(
                f"wave {wave_id!r} is over token budget "
                f"({wave.tokens_consumed}/{wave.token_budget}); raise budget or split work"
            )
        claim_wave(
            state,
            wave_id=wave_id,
            session_id=session,
            out_of_order=out_of_order,
        )

    _run_mutation(
        ctx,
        command="wave claim",
        args={
            "id": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
            "out_of_order": out_of_order,
        },
        scope_id=wave_id,
        text=f"wave claim {wave_id} session={session}",
        envelope=lambda: {
            "wave": wave_id,
            "session": session,
            "worktree_policy": worktree_policy,
            "out_of_order": out_of_order,
        },
        mutate=_claim_with_budget_gate,
        mutation_kind=MutationKind.WAVE_CLAIM,
        params={
            "wave_id": wave_id,
            "session_id": session,
            "out_of_order": out_of_order,
        },
    )


@wave_app.command("close")
def wave_close_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to close.")],
    outcome: Annotated[
        str | None, typer.Option("--outcome", help="Outcome description (required).")
    ] = None,
    commit_ref: Annotated[
        str | None,
        typer.Option(
            "--commit",
            help=(
                "Optional commit ref to pin on the wave. Accepts full/short "
                "SHA, branch tip, tag, or HEAD-relative ref; normalised via "
                "``git rev-parse <ref>^{commit}`` to a 40-char hex SHA."
            ),
        ),
    ] = None,
) -> None:
    """Close a claimed/in-progress wave with an outcome string.

    When ``--commit`` is supplied the ref is resolved via
    ``git rev-parse <ref>^{commit}`` to a canonical 40-char hex SHA and
    persisted on the wave. ``eawf wave show --commit <wave-id>``
    prefers this stored value; absent it falls back to
    :func:`~eawf.workflow.lifecycle.wave_sha.derive_wave_sha` walking
    ``git log --grep "[P##-W##]"``.

    P24-W09 canary: when ``daemon.proxy_enabled=true`` the close
    proxies through the daemon's ``state.mutate`` RPC (typed
    :class:`~eawf.kernel.state.mutations.Mutation` payload with
    ``kind=WAVE_CLOSE``); otherwise the legacy in-process path runs.
    Both paths converge on the same ``state.json`` + ``event.jsonl``
    on-disk shape.
    """
    from eawf.workflow.lifecycle.criterion_drift import check_wave_criteria_drift
    from eawf.workflow.lifecycle.transitions import close_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if outcome is None or outcome == "":
        cli_errors.emit_error(
            cli_errors.UserError("--outcome is required for wave close", kind="InvalidInput"),
            flags=flags,
        )
        return
    # Resolve the commit ref BEFORE any state mutation so a bad ref
    # fails the precondition without touching state.json.
    resolved_sha: str | None = None
    if commit_ref is not None:
        try:
            resolved_sha = _resolve_commit_sha(commit_ref)
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return

    # W09 daemon-proxy canary: route the close through ``state.mutate``
    # when ``daemon.proxy_enabled=true`` in the merged config. Falls
    # back to the in-process ``_run_mutation`` path transparently when
    # the flag is False (W09 default) or the daemon refuses the kind.
    from eawf.surfaces.cli._mutation import _proxy_enabled

    if _proxy_enabled(flags.workspace):
        proxied = _wave_close_via_daemon(
            flags=flags,
            wave_id=wave_id,
            outcome=outcome,
            resolved_sha=resolved_sha,
        )
        if proxied:
            return

    drift_warnings: list[str] = []
    close_succeeded = [False]

    def _close_and_pin(state: State) -> None:
        wave = close_wave(state, wave_id=wave_id, outcome=outcome)
        if resolved_sha is not None:
            wave.commit = resolved_sha
        repo_root = _resolve_repo_root_for_drift(flags.workspace)
        if repo_root is not None:
            drift_warnings.extend(check_wave_criteria_drift(wave, repo_root))
        close_succeeded[0] = True

    _run_mutation(
        ctx,
        command="wave close",
        args={"id": wave_id, "outcome": outcome, "commit": resolved_sha},
        scope_id=wave_id,
        text=f"wave close {wave_id} outcome={outcome!r}",
        envelope=lambda: {
            "wave": wave_id,
            "outcome": outcome,
            "commit": resolved_sha,
        },
        mutate=_close_and_pin,
    )
    if close_succeeded[0]:
        for glob in drift_warnings:
            print(
                f"warning: wave {wave_id} success_criteria reference path glob "
                f"that resolves to zero files: {glob!r}",
                file=sys.stderr,
            )


@wave_app.command("show")
def wave_show_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to inspect.")],
    show_commit: Annotated[
        bool,
        typer.Option(
            "--commit",
            help=(
                "Print the wave's commit SHA. Prefers ``Wave.commit`` set "
                "by ``wave close --commit``; falls back to deriving via "
                "git log --grep '[P##-W##]'."
            ),
        ),
    ] = False,
) -> None:
    """Inspect a wave. ``--commit`` prints the pinned-or-derived SHA."""
    from eawf.workflow.lifecycle.wave_sha import derive_wave_sha

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if not show_commit:
        cli_errors.emit_error(
            cli_errors.UserError("wave show currently requires --commit", kind="InvalidInput"),
            flags=flags,
        )
        return
    # Prefer the pinned SHA (set by ``wave close --commit``) over the
    # derive-from-git-log fallback so closed waves round-trip the value
    # the operator provided rather than re-querying git on every read.
    loaded = _load_state_readonly(ctx)
    pinned_sha: str | None = None
    if loaded is not None:
        state, _ = loaded
        wave = state.waves.get(wave_id)
        if wave is not None:
            pinned_sha = wave.commit
    sha = pinned_sha if pinned_sha is not None else derive_wave_sha(wave_id)
    if sha is None:
        typer.echo("")
        return
    typer.echo(sha)


@wave_app.command("fail")
def wave_fail_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID to fail.")],
    reason: Annotated[
        str | None, typer.Option("--reason", help="Failure reason (required).")
    ] = None,
) -> None:
    """Mark a claimed/in-progress wave as failed with *reason*."""
    from eawf.workflow.lifecycle.transitions import fail_wave

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    if reason is None or reason == "":
        cli_errors.emit_error(
            cli_errors.UserError("--reason is required for wave fail", kind="InvalidInput"),
            flags=flags,
        )
        return
    _run_mutation(
        ctx,
        command="wave fail",
        args={"id": wave_id, "reason": reason},
        scope_id=wave_id,
        text=f"wave fail {wave_id} reason={reason!r}",
        envelope=lambda: {"wave": wave_id, "reason": reason},
        mutate=lambda state: _wrap_no_return(fail_wave(state, wave_id=wave_id, reason=reason)),
        mutation_kind=MutationKind.WAVE_FAIL,
        params={"wave_id": wave_id, "reason": reason},
    )


_WAVE_UPDATE_FILES_ALLOWED_STATUSES: frozenset[WaveStatus] = frozenset(
    {WaveStatus.PENDING, WaveStatus.CLAIMED}
)


@wave_app.command("update")
def wave_update_cmd(
    ctx: typer.Context,
    wave_id: Annotated[str, typer.Argument(help="Wave ID whose file_scopes are being updated.")],
    files: Annotated[
        str | None,
        typer.Option(
            "--files",
            help="Comma-separated file globs that REPLACE the wave's file_scopes.",
        ),
    ] = None,
    add_file: Annotated[
        str | None,
        typer.Option(
            "--add-file",
            help="Comma-separated file globs to append to file_scopes (dedup, preserve order).",
        ),
    ] = None,
    remove_file: Annotated[
        str | None,
        typer.Option(
            "--remove-file",
            help="Comma-separated file globs to drop from file_scopes (missing entries ignored).",
        ),
    ] = None,
) -> None:
    """Mutate a PENDING/CLAIMED wave's ``file_scopes``.

    Reactive scope shifts ("we found we need to touch X too") flow through
    this verb. Exactly one of ``--files`` / ``--add-file`` / ``--remove-file``
    must be passed. CLOSED waves are rejected with ``VALIDATION_FAILED`` (4)
    so historical scope cannot be rewritten.

    Exit codes:
        0: file_scopes updated.
        2: wave id is unknown (``NOT_FOUND``).
        3: invalid args — bad wave id, no mode selected, multiple modes, or
           empty file list (``INVALID_INPUT``).
        4: wave is not in {PENDING, CLAIMED} — typically CLOSED
           (``VALIDATION_FAILED``).
    """
    flags: GlobalFlags = ctx.obj
    if not is_wave_id(wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(f"invalid wave id: {wave_id!r}", kind="InvalidInput"),
            flags=flags,
        )
        return
    selected = [opt for opt in (files, add_file, remove_file) if opt is not None]
    if len(selected) != 1:
        cli_errors.emit_error(
            cli_errors.UserError(
                "exactly one of --files / --add-file / --remove-file must be passed",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return
    if files is not None:
        mode = "set"
        raw = files
    elif add_file is not None:
        mode = "add"
        raw = add_file
    else:
        assert remove_file is not None  # mutually-exclusive guard above
        mode = "remove"
        raw = remove_file
    file_list = [tok.strip() for tok in raw.split(",") if tok.strip()]
    if not file_list:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"--{'files' if mode == 'set' else f'{mode}-file'} requires at least one path",
                kind="InvalidInput",
            ),
            flags=flags,
        )
        return

    result: dict[str, Any] = {}

    def _mutator(state: State) -> None:
        wave = state.waves.get(wave_id)
        if wave is None:
            raise cli_errors.UserError(f"unknown wave: {wave_id!r}", kind="NotFound")
        if wave.status not in _WAVE_UPDATE_FILES_ALLOWED_STATUSES:
            raise cli_errors.ValidationError(
                f"wave {wave_id!r} is {wave.status.value!r}; "
                f"update --files only allowed on PENDING or CLAIMED waves"
            )
        before = list(wave.file_scopes)
        if mode == "set":
            after = list(file_list)
            added = [p for p in after if p not in before]
            removed = [p for p in before if p not in after]
        elif mode == "add":
            after = list(before)
            added = []
            for path in file_list:
                if path not in after:
                    after.append(path)
                    added.append(path)
            removed = []
        else:  # remove
            drop = set(file_list)
            after = [p for p in before if p not in drop]
            added = []
            removed = [p for p in before if p in drop]
        wave.file_scopes = after
        result["before"] = before
        result["after"] = after
        result["added"] = added
        result["removed"] = removed
        logger.info(
            f"update_wave_files wave={wave_id} mode={mode!r} added={added} removed={removed}"
        )

    _run_mutation(
        ctx,
        command="wave update",
        args={"id": wave_id, "mode": mode, "files": file_list},
        scope_id=wave_id,
        text_factory=lambda: (
            f"wave update {wave_id} mode={mode} "
            f"before={len(result['before'])} after={len(result['after'])} "
            f"added={len(result['added'])} removed={len(result['removed'])}"
        ),
        envelope_factory=lambda: {
            "wave": wave_id,
            "mode": mode,
            "file_scopes": result["after"],
            "added": result["added"],
            "removed": result["removed"],
        },
        mutate=_mutator,
    )
