"""``eawf flow`` Typer subapp — operator surface for the ``/flow`` skill.

Phase 5 W02 acceptance: introduces ``run`` / ``status`` / ``abort``
subcommands that mirror the skill engine's envelope contract for ``run``
and emit a small structured JSON for ``status`` / ``abort`` (the latter
two are read-only / mutate-by-append, never going through the engine
themselves).

Exit-code contract:

- ``run`` and ``run --resume`` route through the skill engine; the
  envelope's ``header.status`` maps to an exit code per the same table
  as ``eawf skill run`` (see :mod:`eawf.cli.commands.skill`).
- ``run --resume`` returns ``NOT_FOUND`` (2) when no flow exists, and
  ``INVALID_INPUT`` (3) when more than one in-progress flow exists and
  ``--flow-id`` was not given.
- ``run --resume`` returns ``INTEGRITY_VIOLATION`` (8) when drift is
  detected against the latest ``last_safe=True`` checkpoint.
- ``abort`` of an unknown flow returns ``NOT_FOUND`` (2). Aborting an
  already-abandoned flow is idempotent and returns ``OK`` (0).
- ``status`` is read-only and never appends.
"""

from __future__ import annotations

import logging
import sys
import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, cast

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.render.envelope import to_markdown
from eawf.skills import (
    _bootstrap as _skills_bootstrap,  # noqa: F401 — import-side-effect registers skills
)
from eawf.skills._common import resolve_active_state_path
from eawf.skills.engine import SkillContext, run_skill
from eawf.skills.flow import (
    FlowSkill,
    in_progress_flow_ids,
    latest_active_flow_id,
    load_flow_records,
    load_latest_records_per_flow,
    load_latest_safe_checkpoint,
)
from eawf.state.enums import FlowStatus, StoreKind
from eawf.store.append import append_envelope
from eawf.store.envelope import Envelope
from eawf.store.kinds.flow import FlowCheckpointPayload, FlowPayload
from eawf.store.paths import store_path

logger = logging.getLogger(__name__)


flow_app = typer.Typer(
    name="flow",
    help="Operator surface for the /flow skill (run, status, abort).",
    no_args_is_help=True,
)


def _emit_envelope_via_skill_run_cmd(envelope: Any, *, as_json: bool) -> None:
    """Print *envelope* in JSON or markdown form on stdout."""
    if as_json:
        raw = orjson.dumps(
            envelope.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
        typer.echo(raw.decode("utf-8"))
        return
    typer.echo(to_markdown(envelope), nl=False)


def _exit_for_status(status: str) -> int:
    """Mirror :func:`eawf.cli.commands.skill._exit_for_status`."""
    if status in {"ok", "partial"}:
        return exit_codes.OK
    if status == "needs_user":
        return exit_codes.USER_DECLINED
    if status == "failed":
        return exit_codes.VALIDATION_FAILED
    return exit_codes.INSTRUMENT_MISSING


def _parse_stdin_args(stdin_text: str) -> dict[str, Any]:
    """Decode the optional stdin JSON args mapping.

    Mirrors :func:`eawf.cli.commands.skill._parse_stdin_args`. Empty
    stdin → empty dict. Non-mapping payloads raise
    :class:`InvalidInput`.
    """
    if not stdin_text.strip():
        return {}
    try:
        decoded: Any = orjson.loads(stdin_text)
    except orjson.JSONDecodeError as exc:
        raise cli_errors.InvalidInput(f"stdin is not valid JSON: {exc}") from exc
    if not isinstance(decoded, dict):
        raise cli_errors.InvalidInput(
            f"stdin payload must be a JSON object; got {type(decoded).__name__}"
        )
    return cast(dict[str, Any], decoded)


def _resolve_target_flow_id(
    state_path: Any,
    *,
    flow_id_flag: str | None,
) -> str:
    """Locate the flow_id to operate on for resume/abort.

    Precedence:

    1. Explicit ``--flow-id`` flag wins; the resolver verifies the
       ``FL-...`` prefix (so ``--flow-id=foo`` raises
       :class:`InvalidInput`).
    2. Otherwise, the unique in-progress flow id is used. Zero
       in-progress flows → :class:`NotFound`. More than one → ambiguous,
       raises :class:`InvalidInput` asking for ``--flow-id``.
    """
    if flow_id_flag is not None:
        if not flow_id_flag.startswith("FL-"):
            raise cli_errors.InvalidInput(
                f"--flow-id must look like 'FL-<uuid12>'; got {flow_id_flag!r}"
            )
        return flow_id_flag

    in_progress = in_progress_flow_ids(state_path)
    if not in_progress:
        raise cli_errors.NotFound(
            "no in-progress flow run found for the active scope; start one with 'eawf flow run'"
        )
    if len(in_progress) > 1:
        raise cli_errors.InvalidInput(
            f"multiple in-progress flow runs found ({sorted(in_progress)}); "
            "pass '--flow-id <FL-...>' to disambiguate"
        )
    return in_progress[0]


# ---- run / run --resume ----------------------------------------------------


@flow_app.command(name="run")
def run_cmd(
    ctx: typer.Context,
    topic: Annotated[
        str | None,
        typer.Option("--topic", help="Free-form description of the flow run."),
    ] = None,
    stop_after: Annotated[
        str | None,
        typer.Option(
            "--stop-after",
            help=(
                "Run only up to (and including) this step. "
                "One of research|prep|audit|ship|review|polish."
            ),
        ),
    ] = None,
    resume: Annotated[
        bool,
        typer.Option("--resume", help="Resume from the latest safe checkpoint."),
    ] = False,
    flow_id: Annotated[
        str | None,
        typer.Option(
            "--flow-id",
            help="Disambiguates which flow to resume when multiple exist.",
        ),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä state-scope URN passed to the SkillContext.",
        ),
    ] = "urn:eawf:v1:state:cli-flow-run",
    session: Annotated[
        str,
        typer.Option(
            "--session",
            help="Eä session URN passed to the SkillContext.",
        ),
    ] = "urn:eawf:v1:store:cli/sessions/SES-flow-run",
) -> None:
    """Run the ``/flow`` skill (fresh or resumed).

    Without ``--resume``: starts a fresh run with a new ``FL-<uuid12>``
    id and optional ``--topic`` / ``--stop-after``.

    With ``--resume``: locates the latest ``last_safe=True`` checkpoint
    for the active flow, computes drift, and replays the canonical step
    order from ``step_index + 1``. Drift refuses with exit
    ``INTEGRITY_VIOLATION`` (8); a missing flow raises ``NOT_FOUND`` (2);
    multiple in-progress flows without ``--flow-id`` raise
    ``INVALID_INPUT`` (3).
    """
    flags: GlobalFlags = ctx.obj

    try:
        # Skip stdin on a TTY so interactive runs don't block on EOF.
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        stdin_args = _parse_stdin_args(stdin_text)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # Precedence: stdin args populate first; CLI flags override on
    # conflict. Debug-log a no-op-side-effect line on override so the
    # operator can trace "why didn't my stdin --topic take effect"
    # without resorting to print statements.
    skill_args: dict[str, Any] = dict(stdin_args)
    if topic is not None:
        if "topic" in skill_args and skill_args["topic"] != topic:
            logger.debug(
                f"flow.run: --topic={topic!r} overrides stdin topic={skill_args['topic']!r}"
            )
        skill_args["topic"] = topic
    if stop_after is not None:
        if "stop_after" in skill_args and skill_args["stop_after"] != stop_after:
            logger.debug(
                f"flow.run: --stop-after={stop_after!r} overrides "
                f"stdin stop_after={skill_args['stop_after']!r}"
            )
        skill_args["stop_after"] = stop_after

    if resume:
        try:
            state_path = resolve_active_state_path(workspace=flags.workspace)
            target_flow_id = _resolve_target_flow_id(
                state_path,
                flow_id_flag=flow_id,
            )
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return

        # Locate the latest safe checkpoint.
        ckpt_lookup = load_latest_safe_checkpoint(state_path, target_flow_id)
        if ckpt_lookup is None:
            cli_errors.emit_error(
                cli_errors.IntegrityViolation(
                    f"flow {target_flow_id!r} has no last_safe=True checkpoint to resume to"
                ),
                flags=flags,
            )
            return
        else:
            # Explicit else so the unpack lives in the narrowed branch
            # rather than dangling under an early-return contract.
            ckpt_id, ckpt = ckpt_lookup

        # Compute drift against the live workspace.
        from eawf.skills.flow import compute_drift

        drift = compute_drift(
            ckpt,
            state_path,
            args_per_step=skill_args.get("args_per_step")
            if isinstance(skill_args.get("args_per_step"), dict)
            else None,
        )
        if drift is not None:
            payload = {
                "error": "IntegrityViolation",
                "message": f"drift detected on resume of flow {target_flow_id!r}",
                "exit_code": exit_codes.INTEGRITY_VIOLATION,
                "exit_name": exit_codes.name_for(exit_codes.INTEGRITY_VIOLATION),
                "flow_id": target_flow_id,
                "checkpoint_id": ckpt_id,
                "drift": drift,
                "repair_commands": [
                    "review the drift report; either run 'eawf flow run' fresh "
                    f"or 'eawf flow abort --flow-id {target_flow_id}'",
                ],
            }
            if flags.json_output:
                raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
                typer.echo(raw.decode("utf-8"))
            else:
                typer.echo(f"error: drift detected for flow {target_flow_id!r}; resume refused")
                for key, change in drift.items():
                    typer.echo(f"  {key}: {change}")
            raise typer.Exit(exit_codes.INTEGRITY_VIOLATION)

        # No drift — pack the checkpoint into the skill args so the
        # runner knows where to resume from.
        ckpt_dict = ckpt.model_dump(mode="json")
        ckpt_dict["__envelope_id__"] = ckpt_id
        skill_args["resume_from"] = ckpt_dict
        skill_args["flow_id"] = target_flow_id

    skill = FlowSkill()
    skill_ctx = SkillContext(scope=scope, session=session, args=skill_args)
    envelope = run_skill(skill, skill_ctx)
    _emit_envelope_via_skill_run_cmd(envelope, as_json=flags.json_output)
    code = _exit_for_status(envelope.header.status)
    if code != exit_codes.OK:
        raise typer.Exit(code)


# ---- status ----------------------------------------------------------------


def _latest_checkpoint_for(
    state_path: Any,
    flow_id: str,
) -> tuple[str, FlowCheckpointPayload] | None:
    """Return the most recent checkpoint (safe or not) for *flow_id*."""
    last: tuple[str, FlowCheckpointPayload] | None = None
    for envelope_id, payload in load_flow_records(state_path):
        if payload.get("kind") != "flow_checkpoint":
            continue
        if payload.get("flow_id") != flow_id:
            continue
        try:
            ckpt = FlowCheckpointPayload.model_validate(payload)
        except Exception:
            continue
        last = (envelope_id, ckpt)
    return last


def _safe_checkpoint_summary(
    safe: tuple[str, FlowCheckpointPayload] | None,
) -> dict[str, Any] | None:
    if safe is None:
        return None
    envelope_id, ckpt = safe
    return {
        "id": envelope_id,
        "step_index": ckpt.step_index,
        "step_name": ckpt.step_name,
    }


@flow_app.command(name="status")
def status_cmd(
    ctx: typer.Context,
    flow_id: Annotated[
        str | None,
        typer.Option(
            "--flow-id",
            help="Filter to this flow id (defaults to the most recent flow).",
        ),
    ] = None,
) -> None:
    """Print structured status for a flow run (read-only)."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_active_state_path(workspace=flags.workspace)
    except Exception as exc:
        cli_errors.emit_error(
            cli_errors.NotFound(f"could not resolve state.json: {exc}"),
            flags=flags,
        )
        return

    latest_records = load_latest_records_per_flow(state_path)
    if flow_id is None:
        # Pick the latest in-progress flow if any, else the latest record.
        in_progress = [
            fid for fid, r in latest_records.items() if r.status == FlowStatus.IN_PROGRESS
        ]
        if in_progress:
            target = in_progress[0]
        elif latest_records:
            # Pick the most recently-appended flow_record; falls back to
            # the alphabetically-first flow_id only if the helper
            # can't locate any flow_record envelope (defensive — the
            # records dict was non-empty so at least one envelope must
            # be present).
            recent = latest_active_flow_id(state_path)
            target = recent if recent in latest_records else sorted(latest_records.keys())[0]
        else:
            cli_errors.emit_error(
                cli_errors.NotFound("no flow records found for the active scope"),
                flags=flags,
            )
            return
    else:
        if flow_id not in latest_records:
            cli_errors.emit_error(
                cli_errors.NotFound(f"flow {flow_id!r} not found"),
                flags=flags,
            )
            return
        target = flow_id

    record = latest_records[target]
    last_ckpt = _latest_checkpoint_for(state_path, target)
    safe_ckpt = load_latest_safe_checkpoint(state_path, target)

    current_step_index: int | None = None
    current_step_name: str | None = None
    started_at: str | None = None
    updated_at: str | None = None
    if last_ckpt is not None:
        _eid, ckpt = last_ckpt
        current_step_index = ckpt.step_index
        current_step_name = ckpt.step_name
        started_at = ckpt.started_at.isoformat()
        updated_at = ckpt.completed_at.isoformat()

    payload = {
        "flow_id": target,
        "started_at": started_at,
        "updated_at": updated_at,
        "current_step_index": current_step_index,
        "current_step_name": current_step_name,
        "last_safe_checkpoint": _safe_checkpoint_summary(safe_ckpt),
        "status": record.status.value,
        "drift": None,
    }
    if flags.json_output:
        raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        typer.echo(raw.decode("utf-8"))
    else:
        typer.echo(f"flow_id: {target}")
        typer.echo(f"status:  {record.status.value}")
        typer.echo(f"current: step_index={current_step_index} step_name={current_step_name}")
        if safe_ckpt is not None:
            _eid, ckpt = safe_ckpt
            typer.echo(
                f"last_safe_checkpoint: id={_eid} step_index={ckpt.step_index} "
                f"step_name={ckpt.step_name}"
            )
        else:
            typer.echo("last_safe_checkpoint: <none>")


# ---- abort -----------------------------------------------------------------


@flow_app.command(name="abort")
def abort_cmd(
    ctx: typer.Context,
    flow_id: Annotated[
        str | None,
        typer.Option(
            "--flow-id",
            help="Disambiguates which flow to abort when multiple exist.",
        ),
    ] = None,
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="Operator-supplied reason recorded in the abort policy dict.",
        ),
    ] = None,
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä state-scope URN recorded on the abort envelope.",
        ),
    ] = "urn:eawf:v1:state:cli-flow-abort",
) -> None:
    """Abort a flow run by appending an ``abandoned`` flow_record.

    Idempotent: re-aborting an already-abandoned flow is a no-op exit
    ``OK`` and the JSON output reports
    ``previous_status="abandoned" new_status="abandoned"``.
    """
    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_active_state_path(workspace=flags.workspace)
    except Exception as exc:
        cli_errors.emit_error(
            cli_errors.NotFound(f"could not resolve state.json: {exc}"),
            flags=flags,
        )
        return

    latest_records = load_latest_records_per_flow(state_path)
    if flow_id is None:
        # Pick the unique in-progress flow.
        in_progress = [
            fid for fid, r in latest_records.items() if r.status == FlowStatus.IN_PROGRESS
        ]
        if not in_progress:
            cli_errors.emit_error(
                cli_errors.NotFound("no in-progress flow to abort"),
                flags=flags,
            )
            return
        if len(in_progress) > 1:
            cli_errors.emit_error(
                cli_errors.InvalidInput(
                    f"multiple in-progress flows ({sorted(in_progress)}); pass --flow-id"
                ),
                flags=flags,
            )
            return
        target = in_progress[0]
    else:
        if flow_id not in latest_records:
            cli_errors.emit_error(
                cli_errors.NotFound(f"flow {flow_id!r} not found"),
                flags=flags,
            )
            return
        target = flow_id

    previous = latest_records[target]
    new_status = FlowStatus.ABANDONED

    policy: dict[str, Any] = dict(previous.policy)
    if reason is not None:
        policy["abort_reason"] = reason

    # Build and append the new flow_record envelope.
    payload = FlowPayload(
        flow_id=target,
        goal=previous.goal,
        policy=policy,
        last_safe_checkpoint=previous.last_safe_checkpoint,
        next_action=None,
        status=new_status,
    )
    envelope_id = f"EV-{uuid.uuid4().hex[:12]}"
    envelope = Envelope(
        schema_version="1.0",
        id=envelope_id,
        kind=StoreKind.FLOW,
        scope_id=scope,
        created_at=datetime.now(UTC),
        updated_at=None,
        summary=f"flow: {target} abort previous={previous.status.value}",
        payload=payload.model_dump(mode="json"),
        blob_refs=[],
        artifact_ids=[],
    )
    append_envelope(store_path(state_path, StoreKind.FLOW), envelope)

    response = {
        "flow_id": target,
        "previous_status": previous.status.value,
        "new_status": new_status.value,
    }
    if flags.json_output:
        raw = orjson.dumps(response, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        typer.echo(raw.decode("utf-8"))
    else:
        typer.echo(f"flow_id={target} previous={previous.status.value} new={new_status.value}")


__all__ = [
    "abort_cmd",
    "flow_app",
    "run_cmd",
    "status_cmd",
]
