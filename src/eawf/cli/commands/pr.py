"""``eawf pr render <scope-id>`` Typer sub-app.

Read-only renderer. Loads state, projects through
:func:`eawf.render.pr_body.build_pr_body`, emits Markdown body (default) or
JSON envelope.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.state.ids import is_iter_id, is_phase_id
from eawf.state.resolve import resolve_with_reason

if TYPE_CHECKING:
    from eawf.state.models import State

logger = logging.getLogger(__name__)


pr_app = typer.Typer(
    name="pr",
    help="Render a phase PR body from state.json.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    from eawf.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@pr_app.command("render")
def pr_render(
    ctx: typer.Context,
    scope_id: Annotated[str, typer.Argument(help="Phase or iter id (e.g. P11 or P11-I01).")],
    artifact: Annotated[
        list[Path] | None,
        typer.Option(
            "--artifact",
            help="Artifact markdown path that must validate before rendering.",
        ),
    ] = None,
    profile_blocks: Annotated[
        bool,
        typer.Option("--profile-blocks/--no-profile-blocks", help="Include pr.<kind> blocks."),
    ] = True,
    source: Annotated[
        str | None,
        typer.Option("--source", help="Optional source kind, e.g. docs-research."),
    ] = None,
) -> None:
    """Render the PR body for a phase or iter as Markdown (or JSON envelope)."""
    from eawf.artifacts.validation import validate_markdown_artifact
    from eawf.config.layered import merge_config
    from eawf.profiles.compose import compose
    from eawf.profiles.loader import load_profile
    from eawf.render.pr_body import (
        PrBodyNotFound,
        PrBodyValidationError,
        build_pr_body,
        collect_pr_report_inputs,
        infer_pr_kind,
        resolve_pr_phase_id,
    )

    flags: GlobalFlags = ctx.obj
    try:
        if not (is_phase_id(scope_id) or is_iter_id(scope_id)):
            raise cli_errors.InvalidInput(
                f"invalid PR scope id: {scope_id!r} (expected P<NN> or P<NN>-I<NN>)"
            )
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        for path in artifact or []:
            try:
                report = validate_markdown_artifact(path.read_text(encoding="utf-8"))
            except OSError as exc:
                raise cli_errors.InvalidInput(f"cannot read artifact {path}: {exc}") from exc
            if not report.ok:
                raise cli_errors.ValidationFailed(
                    f"artifact validation failed for {path}: {'; '.join(report.errors)}"
                )
        composed = None
        pr_kind = infer_pr_kind(scope_id, source=source)
        if profile_blocks:
            repo = Path.cwd()
            merged, _sources = merge_config(workspace=flags.workspace, repo=repo)
            enabled = [str(p) for p in (merged.get("profiles", {}).get("enabled") or [])]
            composed = compose([load_profile(pid) for pid in enabled])
        try:
            phase_id = resolve_pr_phase_id(state, scope_id)
            inputs = collect_pr_report_inputs(state_path, state, scope_id, kind=pr_kind)
            body = build_pr_body(
                state,
                phase_id,
                inputs=inputs,
                composed_profile=composed,
                kind=pr_kind,
            )
        except PrBodyNotFound as exc:
            raise cli_errors.NotFound(str(exc)) from exc
        except PrBodyValidationError as exc:
            raise cli_errors.ValidationFailed(str(exc)) from exc
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    payload = {
        "scope": scope_id,
        "phase": phase_id,
        "kind": pr_kind,
        "body": body,
    }
    emit_json_or_text(payload, body, flags=flags)
