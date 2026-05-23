"""CLI surface for typed agent reports."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.state.enums import AgentSessionRole

if TYPE_CHECKING:
    from eawf.state.models import State

agent_report_app = typer.Typer(
    name="agent-report",
    help="Manage typed agent reports.",
    no_args_is_help=True,
)
operator_app = typer.Typer(
    name="operator",
    help="Operator report rollups.",
    no_args_is_help=True,
)


def _load_state(state_path: Path) -> State:
    from eawf.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


def _parse_role(raw: str | None) -> AgentSessionRole | None:
    if raw is None:
        return None
    try:
        return AgentSessionRole(raw.strip().lower())
    except ValueError as exc:
        raise cli_errors.UserError(
            f"--role must be one of {[role.value for role in AgentSessionRole]}; got {raw!r}",
            kind="InvalidInput",
        ) from exc


def _body_from_json(raw: str | None) -> dict[str, Any]:
    text = raw if raw is not None else sys.stdin.read()
    if not text.strip():
        raise cli_errors.UserError(
            "--body-json or stdin JSON body is required", kind="InvalidInput"
        )
    try:
        decoded = orjson.loads(text)
    except orjson.JSONDecodeError as exc:
        raise cli_errors.UserError(f"body JSON is invalid: {exc}", kind="InvalidInput") from exc
    if not isinstance(decoded, dict):
        raise cli_errors.UserError(
            f"body JSON must decode to an object; got {type(decoded).__name__}", kind="InvalidInput"
        )
    return decoded


@agent_report_app.command("add")
def add_report(
    ctx: typer.Context,
    session_id: Annotated[str, typer.Option("--session", help="Authoritative session id.")],
    base_id: Annotated[str, typer.Option("--base-id", help="Stable role/base id.")],
    body_json: Annotated[
        str | None,
        typer.Option("--body-json", help="Role body JSON. Defaults to stdin when omitted."),
    ] = None,
    artifact_ids: Annotated[
        list[str] | None,
        typer.Option("--artifact", help="Artifact id to attach; repeatable."),
    ] = None,
    blob_refs: Annotated[
        list[str] | None,
        typer.Option("--blob", help="Blob ref to attach; repeatable."),
    ] = None,
) -> None:
    """Append a typed agent report."""
    from pydantic import ValidationError

    from eawf.agent_report.store import (
        AgentReportRoleMismatchError,
        AgentReportScrubError,
        append_agent_report,
        parse_agent_report_body,
    )

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        state = _load_state(state_path)
        body = parse_agent_report_body(_body_from_json(body_json))
        result = append_agent_report(
            state=state,
            state_path=state_path,
            session_id=session_id,
            base_id=base_id,
            body=body,
            artifact_ids=list(artifact_ids or []),
            blob_refs=list(blob_refs or []),
        )
    except ValidationError as err:
        cli_errors.emit_error(
            cli_errors.UserError(
                f"agent report body rejected: {err.errors()[0]['msg']}", kind="InvalidInput"
            ),
            flags=flags,
        )
        return
    except KeyError as err:
        cli_errors.emit_error(cli_errors.UserError(str(err), kind="NotFound"), flags=flags)
        return
    except (AgentReportRoleMismatchError, AgentReportScrubError) as err:
        cli_errors.emit_error(cli_errors.ValidationError(str(err)), flags=flags)
        return
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "id": result.envelope.id,
        "attempt": result.attempt,
        "store_kind": result.store_kind,
        "urn": result.urn,
    }
    emit_json_or_text(payload, f"agent report added: {result.envelope.id}", flags=flags)


@agent_report_app.command("list")
def list_reports(
    ctx: typer.Context,
    role: Annotated[str | None, typer.Option("--role", help="Role alias filter.")] = None,
    base_id: Annotated[str | None, typer.Option("--base-id", help="Base id filter.")] = None,
    scope_id: Annotated[str | None, typer.Option("--scope-id", help="Scope id filter.")] = None,
) -> None:
    """List typed agent reports."""
    from eawf.agent_report.rollup import iter_agent_reports

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        role_filter = _parse_role(role)
        rows = iter_agent_reports(state_path, role=role_filter, base_id=base_id, scope_id=scope_id)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    payload = {"reports": [row.as_summary() for row in rows]}
    text = "\n".join(row.envelope.id for row in rows) or "no agent reports"
    emit_json_or_text(payload, text, flags=flags)


@agent_report_app.command("show")
def show_report(
    ctx: typer.Context,
    report_id: Annotated[str, typer.Argument(help="Agent report record id.")],
    role: Annotated[str | None, typer.Option("--role", help="Role alias hint.")] = None,
) -> None:
    """Show a typed agent report."""
    from eawf.agent_report.rollup import find_agent_report

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        row = find_agent_report(state_path, report_id, role=_parse_role(role))
        if row is None:
            raise cli_errors.UserError(f"agent report not found: {report_id!r}", kind="NotFound")
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    payload = {
        "envelope": row.envelope.model_dump(mode="json"),
        "report": row.payload.model_dump(mode="json"),
    }
    text = orjson.dumps(payload, option=orjson.OPT_INDENT_2).decode("utf-8")
    emit_json_or_text(payload, text, flags=flags)


@operator_app.command("rollup")
def rollup(
    ctx: typer.Context,
    phase_id: Annotated[str, typer.Argument(help="Phase id to roll up, e.g. P18.")],
) -> None:
    """Render a read-only operator rollup for *phase_id*."""
    from eawf.agent_report.rollup import operator_rollup

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        payload = operator_rollup(state_path, phase_id)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    text = f"{phase_id}: {payload['report_count']} report(s), roles={payload['by_role']}"
    emit_json_or_text(payload, text, flags=flags)
