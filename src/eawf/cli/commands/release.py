"""``eawf release`` Typer sub-app."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import orjson
import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.render.release_notes import (
    ReleaseNotesValidationError,
    build_release_notes,
    mine_unreleased_changelog,
    release_slug,
)
from eawf.state.models import State
from eawf.state.resolve import resolve_with_reason
from eawf.validate.strict import validate_state

release_app = typer.Typer(
    name="release",
    help="Render release notes and changelog mining reports.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    if not state_path.exists():
        raise cli_errors.NotFound(f"state file not found: {state_path}")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationFailed(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@release_app.command("changelog")
def release_changelog(ctx: typer.Context) -> None:
    """Mine the current ``CHANGELOG.md`` unreleased section."""
    flags: GlobalFlags = ctx.obj
    path = Path("CHANGELOG.md")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        cli_errors.emit_error(cli_errors.NotFound(f"cannot read CHANGELOG.md: {exc}"), flags=flags)
        return
    lines = mine_unreleased_changelog(text)
    payload = {"entries": lines, "count": len(lines)}
    out = "\n".join(lines) if lines else "(no unreleased changelog entries)"
    emit_json_or_text(payload, out, flags=flags)


@release_app.command("notes")
def release_notes(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Release version label.")],
    from_phase: Annotated[
        str | None,
        typer.Option("--from-phase", help="First phase to include, e.g. P14."),
    ] = None,
    to_phase: Annotated[
        str | None,
        typer.Option("--to-phase", help="Last phase to include, e.g. P17."),
    ] = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", help="Optional output path for the draft."),
    ] = None,
) -> None:
    """Render a scrubbed release-notes draft."""
    flags: GlobalFlags = ctx.obj
    try:
        state_path, _reason = resolve_with_reason(flags.workspace)
        state = _load_state(state_path)
        changelog_text = None
        changelog_path = Path("CHANGELOG.md")
        if changelog_path.exists():
            changelog_text = changelog_path.read_text(encoding="utf-8")
        body = build_release_notes(
            state,
            from_phase=from_phase,
            to_phase=to_phase,
            changelog_text=changelog_text,
        )
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(body, encoding="utf-8")
    except ReleaseNotesValidationError as exc:
        cli_errors.emit_error(cli_errors.ValidationFailed(str(exc)), flags=flags)
        return
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.InvalidInput(f"cannot write release notes: {exc}"),
            flags=flags,
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    payload = {
        "version": version,
        "slug": release_slug(version),
        "body": body,
        "output": str(output) if output is not None else None,
    }
    emit_json_or_text(payload, body, flags=flags)


__all__ = ["release_app"]
