"""``eawf wave policy set / show`` — sandbox policy CLI surface.

Sandbox-policy entries are state-resident :class:`~eawf.sandbox.policy.SandboxPolicy`
rows stored on :attr:`~eawf.state.models.State.sandbox_policies`. v0.2 ships
the populate + query verbs; enforcement (hard refusal at dispatch time) is
deferred to a follow-up wave.

Commands attach to the existing ``wave`` Typer app at import time so the
surface lives under ``eawf wave policy ...``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Annotated

import typer
from pydantic import ValidationError

from eawf.cli import errors as cli_errors
from eawf.cli._mutation import state_transaction
from eawf.cli.commands.lifecycle import wave_app
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.cli.scope import resolve_state_path
from eawf.sandbox.policy import (
    SANDBOX_SCOPE_KINDS,
    SandboxPolicy,
    allocate_policy_id,
)

logger = logging.getLogger(__name__)


wave_policy_app = typer.Typer(
    name="policy",
    help="Sandbox / permission policy (set, show).",
    no_args_is_help=True,
)
wave_app.add_typer(wave_policy_app, name="policy")


def _parse_csv(raw: str | None) -> list[str]:
    if raw is None or raw.strip() == "":
        return []
    return [tok.strip() for tok in raw.split(",") if tok.strip()]


def _policy_payload(policy: SandboxPolicy) -> dict[str, object]:
    return {
        "id": policy.id,
        "scope_kind": policy.scope_kind,
        "scope_id": policy.scope_id,
        "allowed_tools": list(policy.allowed_tools),
        "denied_tools": list(policy.denied_tools),
        "granted_at": policy.granted_at.isoformat(),
    }


@wave_policy_app.command("set")
def policy_set(
    ctx: typer.Context,
    scope_id: Annotated[
        str,
        typer.Argument(
            help="Wave id (e.g. P12-I01-W01), profile name, or 'global'.",
            metavar="SCOPE_ID",
        ),
    ],
    scope_kind: Annotated[
        str,
        typer.Option(
            "--scope-kind",
            help="Scope shape: wave | profile | global (default: wave).",
        ),
    ] = "wave",
    allow: Annotated[
        str | None,
        typer.Option(
            "--allow",
            help="Comma-separated list of tool names the policy grants.",
        ),
    ] = None,
    deny: Annotated[
        str | None,
        typer.Option(
            "--deny",
            help="Comma-separated list of tool names the policy refuses.",
        ),
    ] = None,
    policy_id: Annotated[
        str | None,
        typer.Option(
            "--policy-id",
            help="Override the auto-generated id (default: POL-<n>).",
        ),
    ] = None,
) -> None:
    """Bind allowed / denied tool lists to a wave / profile / global scope."""
    flags: GlobalFlags = ctx.obj
    try:
        if scope_kind not in SANDBOX_SCOPE_KINDS:
            raise cli_errors.InvalidInput(
                f"unknown scope_kind {scope_kind!r}; expected one of {list(SANDBOX_SCOPE_KINDS)}"
            )
        state_path = resolve_state_path(flags.workspace)
        with state_transaction(state_path) as state:
            policies = state.sandbox_policies if state.sandbox_policies is not None else {}
            resolved_id = policy_id if policy_id is not None else allocate_policy_id(policies)
            if resolved_id in policies:
                raise cli_errors.InvalidInput(
                    f"sandbox policy id {resolved_id!r} already exists; "
                    "pick another or run `eawf wave policy show` to inspect"
                )
            try:
                policy = SandboxPolicy(
                    id=resolved_id,
                    scope_kind=scope_kind,
                    scope_id=scope_id,
                    allowed_tools=_parse_csv(allow),
                    denied_tools=_parse_csv(deny),
                    granted_at=datetime.now(UTC),
                )
            except ValidationError as exc:
                raise cli_errors.InvalidInput(str(exc)) from exc
            policies[resolved_id] = policy
            state.sandbox_policies = policies
            state.updated_at = datetime.now(UTC)
        emit_json_or_text(
            payload=_policy_payload(policy),
            text=(
                f"policy set: {policy.id} ({policy.scope_kind}={policy.scope_id}) "
                f"allow={len(policy.allowed_tools)} deny={len(policy.denied_tools)}"
            ),
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


@wave_policy_app.command("show")
def policy_show(
    ctx: typer.Context,
    scope_id: Annotated[
        str,
        typer.Argument(
            help="Wave id, profile name, or 'global' to match against scope_id.",
            metavar="SCOPE_ID",
        ),
    ],
) -> None:
    """Show every sandbox policy whose ``scope_id`` matches *scope_id*."""
    import orjson

    from eawf.state.models import State
    from eawf.validate.strict import validate_state

    flags: GlobalFlags = ctx.obj
    try:
        state_path = resolve_state_path(flags.workspace)
        if not state_path.exists():
            raise cli_errors.NotFound(f"state file not found: {state_path}")
        payload = orjson.loads(state_path.read_bytes())
        report = validate_state(payload, strict_optional=False)
        if report.state is None:
            raise cli_errors.ValidationFailed(
                f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
            )
        state: State = report.state
        policies = state.sandbox_policies or {}
        matched = [p for p in policies.values() if p.scope_id == scope_id]
        rows = [_policy_payload(p) for p in matched]
        text = (
            "\n".join(
                f"{p.id}\t{p.scope_kind}\t{p.scope_id}\tallow={','.join(p.allowed_tools) or '-'}"
                f"\tdeny={','.join(p.denied_tools) or '-'}"
                for p in matched
            )
            or "(no policies)"
        )
        emit_json_or_text(
            payload={"scope_id": scope_id, "policies": rows, "count": len(rows)},
            text=text,
            flags=flags,
        )
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
    except FileNotFoundError as err:
        cli_errors.emit_error(cli_errors.NotFound(str(err)), flags=flags)


__all__ = ["wave_policy_app"]
