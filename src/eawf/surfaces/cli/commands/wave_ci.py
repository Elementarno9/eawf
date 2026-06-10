"""``eawf wave fix-ci`` / ``wave fix-ci-loop`` — CI-fix orchestration.

These verbs hang off ``wave_app`` (defined in
:mod:`eawf.surfaces.cli.commands.lifecycle`). They are registered here rather
than mutating ``lifecycle.py`` so the per-wave subsystems stay in
their own modules — W05 owns ``ci_loop`` + this CLI surface; the
``wave plan`` command itself lives in lifecycle.py.

Both verbs ingest a CI log file (no live GitHub fetch — that is out of
scope for this wave) and use :mod:`eawf.runtime.ci_loop` to:

1. Parse the log into pytest / ruff / mypy failures.
2. Compute the file-scope union via
   :func:`eawf.runtime.ci_loop.failure_to_file_scope`.
3. Allocate a new wave id under the parent's iter via
   :func:`eawf.workflow.lifecycle.allocator.allocate_wave_id`.
4. Plan the follow-up wave with ``deps=[parent_wave_id]``,
   ``file_scopes=<union>``, and a synthesised title.

``wave fix-ci`` is the single-shot form. ``wave fix-ci-loop`` is a
thin orchestrator that re-runs the single-shot form up to
``--max-iters`` times; on each iteration it expects the caller (or an
outer loop) to land the previous follow-up before re-invoking. The
loop refuses with exit 4 when the same failure-signature recurs (an
indication that the loop is not converging and human intervention is
required).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.ids import is_wave_id
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import wave_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.runtime.ci_loop.policy import Failure

logger = logging.getLogger(__name__)


# ---- helpers ----------------------------------------------------------------


def _resolve_state_path(flags: GlobalFlags) -> Path:
    """Resolve the active ``state.json`` path or raise :class:`UserError` (``kind="NotFound"``)."""
    try:
        return resolve_state_path(flags.workspace)
    except FileNotFoundError as exc:
        raise cli_errors.UserError(str(exc), kind="NotFound") from exc


def _read_log(log_path: Path) -> str:
    """Read the CI log file.

    Raises:
        UserError: When the log file is absent (``kind="NotFound"``) or
            cannot be read (``kind="InvalidInput"``).
    """
    if not log_path.exists():
        raise cli_errors.UserError(f"ci log not found: {log_path}", kind="NotFound")
    try:
        return log_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise cli_errors.UserError(
            f"could not read ci log {log_path}: {exc}", kind="InvalidInput"
        ) from exc


def _parse_all(log_text: str) -> list[Failure]:
    """Parse every supported diagnostic kind in declaration order."""
    from eawf.runtime.ci_loop import (
        parse_mypy_failures,
        parse_pytest_failures,
        parse_ruff_failures,
    )

    failures: list[Failure] = []
    failures.extend(parse_pytest_failures(log_text))
    failures.extend(parse_ruff_failures(log_text))
    failures.extend(parse_mypy_failures(log_text))
    return failures


# ---- wave fix-ci ------------------------------------------------------------


@wave_app.command(name="fix-ci")
def wave_fix_ci_cmd(
    ctx: typer.Context,
    parent_wave_id: Annotated[
        str,
        typer.Argument(help="Parent wave ID whose CI failures the follow-up should target."),
    ],
    log: Annotated[
        Path,
        typer.Option(
            "--log",
            help="Path to the CI log file to parse for failures.",
            exists=False,
        ),
    ],
    new_wave_id: Annotated[
        str | None,
        typer.Option(
            "--id",
            help=(
                "Explicit follow-up wave id (defaults to the next free wave id "
                "under the parent's iter)."
            ),
        ),
    ] = None,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Describe the wave that *would* be planned without mutating state.",
        ),
    ] = False,
) -> None:
    """Plan a follow-up wave that targets the failing files in *log*.

    On a clean log (no failures parsed), no wave is planned and the
    envelope reports ``planned_wave=null``. On any failures, a new wave
    is allocated under the parent's iter with ``deps=[parent_wave_id]``
    and ``file_scopes`` = the sorted-unique union of failing file paths.
    """
    from eawf.runtime.ci_loop import failure_to_file_scope, summarise_failures

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(parent_wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid parent wave id: {parent_wave_id!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    if new_wave_id is not None and not is_wave_id(new_wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid follow-up wave id: {new_wave_id!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return

    try:
        log_text = _read_log(log)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    failures = _parse_all(log_text)

    if not failures:
        envelope: dict[str, Any] = {
            "parent": parent_wave_id,
            "failures": 0,
            "planned_wave": None,
        }
        emit_json_or_text(
            envelope,
            f"wave fix-ci parent={parent_wave_id} failures=0 (no follow-up planned)",
            flags=flags,
        )
        return

    file_scope = failure_to_file_scope(failures)
    summary = summarise_failures(failures)
    title = f"CI fix follow-up: {summary}"

    if dry_run:
        envelope = {
            "parent": parent_wave_id,
            "failures": len(failures),
            "summary": summary,
            "planned_wave": {
                "id": new_wave_id or "<auto>",
                "iter": _parent_iter_id(parent_wave_id),
                "deps": [parent_wave_id],
                "file_scope": file_scope,
                "title": title,
            },
            "dry_run": True,
        }
        text = (
            f"wave fix-ci parent={parent_wave_id} failures={len(failures)} "
            f"summary={summary!r} (dry-run; no state change)"
        )
        emit_json_or_text(envelope, text, flags=flags)
        return

    result = _plan_follow_up(
        flags=flags,
        parent_wave_id=parent_wave_id,
        new_wave_id=new_wave_id,
        file_scope=file_scope,
        title=title,
    )
    if result is None:
        # ``_plan_follow_up`` already emitted the canonical error
        # envelope and called ``typer.Exit``; control should not reach
        # here in normal flow, but the early-return keeps mypy happy.
        return

    envelope = {
        "parent": parent_wave_id,
        "failures": len(failures),
        "summary": summary,
        "planned_wave": {
            "id": result,
            "iter": _parent_iter_id(parent_wave_id),
            "deps": [parent_wave_id],
            "file_scope": file_scope,
            "title": title,
        },
    }
    text = (
        f"wave fix-ci parent={parent_wave_id} failures={len(failures)} "
        f"planned={result} summary={summary!r}"
    )
    emit_json_or_text(envelope, text, flags=flags)


def _parent_iter_id(parent_wave_id: str) -> str:
    """Return the iter id segment of *parent_wave_id*.

    Uses the textual structure of the wave id (``P<NN>-I<MM>-W<KK>``)
    rather than a state lookup so the dry-run path stays read-free.
    Callers have already validated *parent_wave_id* via
    :func:`is_wave_id`.
    """
    parts = parent_wave_id.split("-")
    return f"{parts[0]}-{parts[1]}"


def _plan_follow_up(
    *,
    flags: GlobalFlags,
    parent_wave_id: str,
    new_wave_id: str | None,
    file_scope: list[str],
    title: str,
) -> str | None:
    """Plan the follow-up wave under the parent's iter; return its id.

    Returns ``None`` (after emitting the canonical error envelope) when
    the operation fails. On success returns the allocated / supplied
    wave id.

    Implementation note: we intentionally do *not* use
    :func:`eawf.surfaces.cli.commands.lifecycle._run_mutation` here. That helper
    is bound to the static / factory envelope pair used by the existing
    handlers; the fix-ci envelope is richer (it carries the summary,
    failure count, and file_scope), and reaching in to monkey-patch
    those is uglier than running our own state_transaction. The
    invariants the helper enforces — portalock + version-check + commit
    journal — are identical between the two paths via
    :func:`state_transaction`.
    """
    from pydantic import ValidationError as PydValidationError

    from eawf.kernel.spec.intent import IntentBrief
    from eawf.kernel.state.enums import EffortBucket
    from eawf.surfaces.cli._mutation import state_transaction
    from eawf.workflow.lifecycle.allocator import allocate_wave_id
    from eawf.workflow.lifecycle.transitions import LifecycleError, plan_wave

    try:
        state_path = _resolve_state_path(flags)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return None

    allocated_id: str | None = None
    try:
        with state_transaction(state_path) as state:
            parent_wave = state.waves.get(parent_wave_id)
            if parent_wave is None:
                raise cli_errors.UserError(
                    f"unknown parent wave: {parent_wave_id}", kind="NotFound"
                )
            iter_id = parent_wave.iter_id
            target_id = new_wave_id or allocate_wave_id(state, iter_id)
            # The fix-ci follow-up is an authored wave, so it carries an
            # IntentBrief like any other. The repair context is implicit
            # (a CI failure on the parent), so the brief is synthesised from
            # the parent id rather than operator-supplied flags. A non-blank
            # priority_rationale satisfies the authoring body-completeness
            # guard since the follow-up takes no operator-supplied body.
            follow_up_intent = IntentBrief(
                problem=f"CI failed on parent wave {parent_wave_id}",
                desired_outcome="the follow-up wave repairs the parent CI failure",
                priority_rationale=f"repair the CI failure on parent wave {parent_wave_id}",
            )
            try:
                plan_wave(
                    state,
                    wave_id=target_id,
                    iter_id=iter_id,
                    title=title,
                    file_scopes=file_scope,
                    deps=[parent_wave_id],
                    effort_bucket=EffortBucket.M,
                    intent=follow_up_intent,
                )
            except LifecycleError as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            except (PydValidationError, ValueError) as exc:
                raise cli_errors.UserError(str(exc), kind="InvalidInput") from exc
            allocated_id = target_id
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return None
    return allocated_id


# ---- wave fix-ci-loop -------------------------------------------------------


@wave_app.command(name="fix-ci-loop")
def wave_fix_ci_loop_cmd(
    ctx: typer.Context,
    parent_wave_id: Annotated[
        str,
        typer.Argument(help="Parent wave ID for the initial CI-fix iteration."),
    ],
    log: Annotated[
        Path,
        typer.Option(
            "--log",
            help="Path to the CI log file to parse for failures.",
        ),
    ],
    max_iters: Annotated[
        int,
        typer.Option(
            "--max-iters",
            help="Maximum number of follow-up waves to plan in this loop run.",
            min=1,
        ),
    ] = 3,
) -> None:
    """Plan a chain of CI-fix follow-up waves until convergence or *max_iters*.

    The caller is expected to land each follow-up before re-invoking
    the loop with a fresh CI log. This entry point performs at most
    *max_iters* iterations within a single invocation and refuses
    (exit 4) when consecutive iterations surface the same failure
    signature (``pytest:N ruff:N mypy:N``) — that is the canonical
    "loop not converging" signal.

    The persisted history is per-invocation only: nothing about the
    loop is written to ``state.json`` beyond the planned wave records
    themselves (each via :func:`plan_wave`).
    """
    from eawf.runtime.ci_loop import failure_to_file_scope, summarise_failures

    flags: GlobalFlags = ctx.obj
    if not is_wave_id(parent_wave_id):
        cli_errors.emit_error(
            cli_errors.UserError(
                f"invalid parent wave id: {parent_wave_id!r}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    history: list[dict[str, Any]] = []
    current_parent = parent_wave_id
    seen_signature: str | None = None

    for iter_index in range(1, max_iters + 1):
        # Re-read each iter: the caller is expected to land the previous
        # follow-up and re-run CI before the next loop tick, so the log
        # content evolves. A static read outside the loop would surface
        # the same signature on every iter and trip the "not converging"
        # refusal spuriously after iter 1.
        try:
            log_text = _read_log(log)
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return
        failures = _parse_all(log_text)
        if not failures:
            history.append(
                {
                    "iter": iter_index,
                    "parent": current_parent,
                    "failures": 0,
                    "planned_wave": None,
                }
            )
            break

        summary = summarise_failures(failures)
        # Signature is parser-kind counts only (see B040 spec). Parent
        # id is intentionally excluded so identical-failure logs across
        # consecutive iterations surface as a refusal: the loop is not
        # making progress, the operator needs to intervene.
        signature = summary
        if seen_signature is not None and signature == seen_signature:
            history.append(
                {
                    "iter": iter_index,
                    "parent": current_parent,
                    "failures": len(failures),
                    "summary": summary,
                    "planned_wave": None,
                    "refused": "ci-fix loop not converging",
                }
            )
            envelope: dict[str, Any] = {
                "parent": parent_wave_id,
                "iters": iter_index,
                "history": history,
                "converged": False,
            }
            text = (
                f"error: ci-fix loop not converging (same failure signature on iter {iter_index})"
            )
            emit_json_or_text(envelope, text, flags=flags)
            raise typer.Exit(cli_errors.ValidationError.exit_code)

        file_scope = failure_to_file_scope(failures)
        title = f"CI fix follow-up: {summary}"
        planned = _plan_follow_up(
            flags=flags,
            parent_wave_id=current_parent,
            new_wave_id=None,
            file_scope=file_scope,
            title=title,
        )
        if planned is None:
            # ``_plan_follow_up`` already emitted + raised typer.Exit;
            # control should not reach here, but the early-return keeps
            # mypy / runtime semantics aligned.
            return
        history.append(
            {
                "iter": iter_index,
                "parent": current_parent,
                "failures": len(failures),
                "summary": summary,
                "planned_wave": planned,
            }
        )
        seen_signature = signature
        # Outer loops are expected to land ``planned`` and re-invoke
        # with a refreshed log; within a single invocation we keep
        # parsing the same log, so subsequent iterations will either
        # converge (signature mismatch → keep going) or refuse on
        # repetition. The loop also halts naturally at max_iters.
        current_parent = planned

    converged = len(history) > 0 and history[-1].get("planned_wave") is None
    envelope = {
        "parent": parent_wave_id,
        "iters": len(history),
        "history": history,
        "converged": converged,
    }
    text = f"wave fix-ci-loop parent={parent_wave_id} iters={len(history)} converged={converged}"
    emit_json_or_text(envelope, text, flags=flags)


__all__ = [
    "wave_fix_ci_cmd",
    "wave_fix_ci_loop_cmd",
]
