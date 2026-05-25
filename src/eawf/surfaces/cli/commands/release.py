"""``eawf release`` Typer sub-app."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Annotated

import orjson
import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.state.models import State

release_app = typer.Typer(
    name="release",
    help="Tag releases and render release notes / changelog reports.",
    no_args_is_help=True,
    add_completion=False,
)


def _load_state(state_path: Path) -> State:
    from eawf.kernel.validate.strict import validate_state

    if not state_path.exists():
        raise cli_errors.UserError(f"state file not found: {state_path}", kind="NotFound")
    payload = orjson.loads(state_path.read_bytes())
    report = validate_state(payload, strict_optional=False)
    if report.state is None:
        raise cli_errors.ValidationError(
            f"state schema invalid: {'; '.join(report.schema_errors[:3])}"
        )
    return report.state


@release_app.command("tag")
def release_tag(
    ctx: typer.Context,
    version: Annotated[
        str | None,
        typer.Argument(help="Version to tag (default: the current package version)."),
    ] = None,
    push: Annotated[
        bool,
        typer.Option("--push", help="Push the tag to the remote, triggering the release pipeline."),
    ] = False,
    remote: Annotated[
        str,
        typer.Option("--remote", help="Remote to push the tag to."),
    ] = "origin",
    force: Annotated[
        bool,
        typer.Option("--force", help="Allow a dirty tree and overwrite an existing tag."),
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Print the tag/push plan without running git."),
    ] = False,
) -> None:
    """Create the ``v<version>`` release tag and (with ``--push``) trigger the pipeline.

    The release workflow (``.github/workflows/release.yaml``) fires on a
    ``v0.*`` tag push: it builds the wheel + sdist, runs the wheel-size
    gate and ``twine check``, then publishes to PyPI behind the tag-push
    condition. ``eawf release tag --push`` is the operator entry point
    that starts that chain. Without ``--push`` the tag is created locally
    and the push command is echoed so the operator triggers it manually.
    """
    import subprocess

    from eawf import __version__

    flags: GlobalFlags = ctx.obj
    resolved = version or __version__
    tag = f"v{resolved}"
    try:
        if not force and not dry_run:
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                capture_output=True,
                text=True,
                check=True,
            )
            if status.stdout.strip():
                raise cli_errors.UserError(
                    "working tree is dirty; commit or stash before tagging a release "
                    "(or pass --force)",
                    kind="InvalidInput",
                )
        listing = subprocess.run(
            ["git", "tag", "--list", tag],
            capture_output=True,
            text=True,
            check=True,
        )
        tag_exists = bool(listing.stdout.strip())
        plan = {
            "tag": tag,
            "version": resolved,
            "push": push,
            "remote": remote,
            "tag_exists": tag_exists,
            "dry_run": dry_run,
        }
        if dry_run:
            suffix = f" and push to {remote} (triggers pipeline)" if push else ""
            emit_json_or_text(plan, f"dry-run: would create tag {tag}{suffix}", flags=flags)
            return
        if tag_exists and not force:
            raise cli_errors.UserError(
                f"tag {tag!r} already exists; pass --force to overwrite", kind="InvalidInput"
            )
        tag_cmd = ["git", "tag", "-a", tag, "-m", f"Release {tag}"]
        if force:
            tag_cmd.insert(2, "-f")
        subprocess.run(tag_cmd, check=True)
        pushed = False
        if push:
            push_cmd = ["git", "push"]
            if force:
                push_cmd.append("--force")
            push_cmd.extend([remote, tag])
            subprocess.run(push_cmd, check=True)
            pushed = True
        plan["pushed"] = pushed
        text = (
            f"tagged {tag}; pushed to {remote} (pipeline triggered)"
            if pushed
            else f"tagged {tag}; run `git push {remote} {tag}` to trigger the pipeline"
        )
        emit_json_or_text(plan, text, flags=flags)
    except subprocess.CalledProcessError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"git command failed: {exc}", kind="InvalidInput"), flags=flags
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return


@release_app.command("changelog")
def release_changelog(ctx: typer.Context) -> None:
    """Mine the current ``CHANGELOG.md`` unreleased section."""
    from eawf.surfaces.render.release_notes import mine_unreleased_changelog

    flags: GlobalFlags = ctx.obj
    path = Path("CHANGELOG.md")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot read CHANGELOG.md: {exc}", kind="NotFound"), flags=flags
        )
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
    from eawf.surfaces.render.release_notes import (
        ReleaseNotesValidationError,
        build_release_notes,
        release_slug,
    )

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
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    except OSError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(f"cannot write release notes: {exc}", kind="InvalidInput"),
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
