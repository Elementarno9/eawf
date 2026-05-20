"""``eawf hook run <event>`` Typer command.

Surface contract:

- ``eawf hook run <event_type> [--runtime <claude|opencode|generic>]
  [--scope <ID>] [--command <str>]`` reads a JSON payload from stdin,
  builds a typed :class:`~eawf.hooks.event.HookEvent`, dispatches it
  through a fresh :class:`~eawf.hooks.runner.HookRunner`, and emits an
  :class:`~eawf.render.envelope.OutputEnvelope` to stdout.
- Exit ``0`` when no registered hook returns ``block=True``.
- Exit ``9`` (``HOOK_BLOCKED``) when at least one hook reports a block.
- Exit ``3`` (``INVALID_INPUT``) when the stdin payload is not valid JSON
  or is not a mapping.

The runner mounted by this command starts empty: registration is the
runtime adapter's job (W05 wires up the Claude-installed hook
callables; the v1 surface here is the CLI dispatch primitive). When no
hook is registered the result list is empty and the exit code is
``0``.
"""

from __future__ import annotations

import logging
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, cast

import orjson
import typer
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from eawf.cli import errors as cli_errors
from eawf.cli import exit_codes
from eawf.cli.flags import GlobalFlags
from eawf.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.hooks.event import HookEvent, HookEventType, HookRuntime
    from eawf.hooks.runner import HookResult
    from eawf.render.envelope import EnvelopeStatus, OutputEnvelope
    from eawf.state.models import State

logger = logging.getLogger(__name__)


class AgentEndPayload(BaseModel):
    """Payload accepted by ``eawf hook run agent_end``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str
    base_id: str
    body: dict[str, Any]
    artifact_ids: list[str] = Field(default_factory=list)
    blob_refs: list[str] = Field(default_factory=list)


hook_app = typer.Typer(
    name="hook",
    help="Dispatch hook events through the Eä hook runner.",
    no_args_is_help=True,
)


def _parse_event_type(raw: str) -> HookEventType:
    """Resolve *raw* (CLI argument string) into a :class:`HookEventType`.

    Accepts the canonical lowercase value ("pre_commit", "post_commit",
    …) — matches the StrEnum value verbatim. Unknown values raise
    :class:`~eawf.cli.errors.InvalidInput` so the handler can surface
    exit code 3.
    """
    from eawf.hooks.event import HookEventType

    try:
        return HookEventType(raw)
    except ValueError as exc:
        valid = sorted(t.value for t in HookEventType)
        raise cli_errors.InvalidInput(
            f"unknown event type {raw!r}; expected one of {valid}"
        ) from exc


def _parse_payload(stdin_text: str) -> dict[str, Any]:
    """Decode and shape-check the stdin JSON payload.

    Returns a mapping. Empty input is treated as ``{}`` so callers may
    invoke ``eawf hook run pre_commit`` with no piped payload during
    smoke checks.

    Raises:
        InvalidInput: ``stdin_text`` is non-empty but not valid JSON,
            or decodes to something other than a JSON object.
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


def _build_event(
    *,
    event_type: HookEventType,
    payload: dict[str, Any],
    scope: str,
    command: str,
    runtime: HookRuntime,
    occurred_at: datetime,
) -> HookEvent:
    """Build the typed :class:`HookEvent` from CLI args + decoded stdin.

    The decoded stdin mapping is folded into ``payloads[<event_type>]``
    so downstream hooks see the original shape under a stable key.
    """
    from eawf.hooks.event import HookEvent

    return HookEvent(
        event_type=event_type,
        scope_id=scope,
        command=command,
        args={},
        runtime=runtime,
        occurred_at=occurred_at,
        payloads={event_type.value: dict(payload)} if payload else {},
    )


def _envelope_for(
    *,
    event: HookEvent,
    results: list[HookResult],
    started_at: datetime,
    finished_at: datetime,
) -> OutputEnvelope:
    """Assemble the output envelope for a finished dispatch.

    The envelope is the same shape every Eä CLI command emits; we use
    ``/audit`` as the carrier ``skill`` because there is no dedicated
    skill for the CLI hook surface yet (W05 may add ``/hook`` if an
    operator-facing skill is required). Status is ``ok`` when no hook
    blocked, ``blocked`` otherwise.
    """
    from eawf.render.envelope import (
        EnvelopeFooter,
        EnvelopeHeader,
        EnvelopeWarning,
        OutputEnvelope,
    )

    blocked = any(r.block for r in results)
    status: EnvelopeStatus = "blocked" if blocked else "ok"
    body: dict[str, object] = {
        "event_type": event.event_type.value,
        "scope_id": event.scope_id,
        "runtime": event.runtime,
        "occurred_at": event.occurred_at.isoformat(),
        "results": [r.model_dump(mode="json") for r in results],
        "blocked": blocked,
    }
    warnings = [
        EnvelopeWarning(code="hook_raised", detail=f"hook {r.name!r}: {r.output}")
        for r in results
        if r.raised
    ]
    repair_commands = (
        [f"review hook output for {r.name!r}" for r in results if r.block] if blocked else None
    )
    header = EnvelopeHeader(
        skill="/audit",
        scope_id=event.scope_id or "urn:eawf:v1:state:hook-run",
        session="urn:eawf:v1:store:hook/sessions/SES-cli",
        started_at=started_at,
        finished_at=finished_at,
        status=status,
        instrument_probe={},
    )
    footer = EnvelopeFooter(
        persisted_artifacts=[],
        persisted_store_records=[],
        state_mutations=[],
        evidence_refs=[],
        next_valid_actions=[],
        warnings=warnings,
        repair_commands=repair_commands,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _load_state(state_path: Path) -> State:
    """Load and validate state from *state_path*."""
    from eawf.validate.strict import validate_state

    path = state_path
    if not path.exists():
        raise cli_errors.NotFound(f"state file not found: {path}")
    payload = orjson.loads(path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


def _envelope_for_agent_report(
    *,
    event: HookEvent,
    report_id: str,
    store_kind: str,
    attempt: int,
    store_urn: str,
    started_at: datetime,
    finished_at: datetime,
) -> OutputEnvelope:
    """Assemble the output envelope for an agent_end report append."""
    from eawf.render.envelope import EnvelopeFooter, EnvelopeHeader, OutputEnvelope

    body: dict[str, object] = {
        "event_type": event.event_type.value,
        "scope_id": event.scope_id,
        "runtime": event.runtime,
        "report_id": report_id,
        "store_kind": store_kind,
        "attempt": attempt,
        "persisted_store_record": store_urn,
        "blocked": False,
        "results": [],
    }
    header = EnvelopeHeader(
        skill="/audit",
        scope_id=event.scope_id or "urn:eawf:v1:state:hook-run",
        session="urn:eawf:v1:store:hook/sessions/SES-cli",
        started_at=started_at,
        finished_at=finished_at,
        status="ok",
        instrument_probe={},
    )
    footer = EnvelopeFooter(
        persisted_artifacts=[],
        persisted_store_records=[store_urn],
        state_mutations=[],
        evidence_refs=[store_urn],
        next_valid_actions=[],
        warnings=[],
        repair_commands=None,
    )
    return OutputEnvelope(header=header, body=body, footer=footer)


def _emit_envelope(env: OutputEnvelope) -> None:
    """Write the JSON envelope to stdout, newline-terminated."""
    raw = orjson.dumps(
        env.model_dump(mode="json"),
        option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
    )
    typer.echo(raw.decode("utf-8"))


def _runtime_default() -> HookRuntime:
    """Return the v1 default runtime label.

    Pulled into a helper so tests can monkeypatch the default cleanly.
    """
    return "generic"


@hook_app.command(name="run")
def run(
    ctx: typer.Context,
    event_type: Annotated[
        str,
        typer.Argument(
            help="HookEventType value (e.g., pre_commit, post_commit, "
            "session_start). See docs/hook-events.md.",
        ),
    ],
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Runtime label recorded on the event (claude/opencode/generic).",
            case_sensitive=False,
        ),
    ] = _runtime_default(),
    scope: Annotated[
        str,
        typer.Option(
            "--scope",
            help="Eä scope ID (wave/iter/phase) the event was raised inside.",
        ),
    ] = "",
    command: Annotated[
        str,
        typer.Option(
            "--command",
            help="Originating Eä CLI command string for the event record.",
        ),
    ] = "",
) -> None:
    """Dispatch a hook event read from stdin and emit the result envelope."""
    from eawf.agent_report.store import (
        AgentReportRoleMismatchError,
        AgentReportScrubError,
        append_agent_report,
        parse_agent_report_body,
    )
    from eawf.hooks.event import HookEventType
    from eawf.hooks.runner import HookRunner

    flags: GlobalFlags = ctx.obj
    started_at = datetime.now(UTC)

    if runtime.lower() not in {"claude", "codex", "opencode", "generic"}:
        cli_errors.emit_error(
            cli_errors.InvalidInput(
                f"--runtime must be one of claude/codex/opencode/generic; got {runtime!r}"
            ),
            flags=flags,
        )
        return

    try:
        resolved_event_type = _parse_event_type(event_type)
        # Skip stdin read on a TTY so interactive smoke runs don't block
        # waiting for EOF; piped/redirected stdin reads normally.
        stdin_text = "" if sys.stdin.isatty() else sys.stdin.read()
        payload = _parse_payload(stdin_text)
        event = _build_event(
            event_type=resolved_event_type,
            payload=payload,
            scope=scope,
            command=command,
            runtime=cast("HookRuntime", runtime.lower()),
            occurred_at=started_at,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return
    except ValidationError as err:
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"event payload rejected: {err.errors()[0]['msg']}"),
            flags=flags,
        )
        return

    if event.event_type == HookEventType.AGENT_END:
        try:
            agent_payload = AgentEndPayload.model_validate(payload)
            state_path = resolve_state_path(flags.workspace)
            state = _load_state(state_path)
            body = parse_agent_report_body(agent_payload.body)
            result = append_agent_report(
                state=state,
                state_path=state_path,
                session_id=agent_payload.session_id,
                base_id=agent_payload.base_id,
                body=body,
                runtime=event.runtime,
                generated_at=started_at,
                artifact_ids=agent_payload.artifact_ids,
                blob_refs=agent_payload.blob_refs,
            )
        except ValidationError as err:
            cli_errors.emit_error(
                cli_errors.InvalidInput(f"agent_end payload rejected: {err.errors()[0]['msg']}"),
                flags=flags,
            )
            return
        except KeyError as err:
            cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)
            return
        except (AgentReportRoleMismatchError, AgentReportScrubError) as err:
            cli_errors.emit_error(cli_errors.ValidationFailed(str(err)), flags=flags)
            return
        except cli_errors.CliError as err:
            cli_errors.emit_error(err, flags=flags)
            return
        finished_at = datetime.now(UTC)
        envelope = _envelope_for_agent_report(
            event=event,
            report_id=result.envelope.id,
            store_kind=result.store_kind,
            attempt=result.attempt,
            store_urn=result.urn,
            started_at=started_at,
            finished_at=finished_at,
        )
        _emit_envelope(envelope)
        return

    runner = HookRunner()
    # The v1 CLI surface dispatches with no registered hooks — runtime
    # adapters (W05) will register hooks via a sidecar config. The
    # empty-bucket path is the documented success case (no-op exit 0).
    results = runner.run_event(event)

    finished_at = datetime.now(UTC)
    envelope = _envelope_for(
        event=event,
        results=results,
        started_at=started_at,
        finished_at=finished_at,
    )
    _emit_envelope(envelope)
    if any(r.block for r in results):
        raise typer.Exit(exit_codes.HOOK_BLOCKED)


__all__ = [
    "hook_app",
]
