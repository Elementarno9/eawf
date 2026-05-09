"""``eawf init`` Typer command.

Surface contract:

- ``eawf init`` (no flags, TTY) launches the Textual wizard. CI must pass
  ``--no-input``; absent ``--no-input`` and an actual TTY, the launcher
  falls through to interactive mode anyway and the test harness will fail
  fast (Textual cannot bind to a fake TTY).
- ``eawf init --no-input --project-code DEMO --profile core ...`` runs the
  pure pipeline in :func:`eawf.install.wizard.run_wizard_no_input`.
- ``eawf init --force`` allows the pipeline to overwrite an existing
  ``.ea/state.json`` or ``.ea/config.yaml`` (otherwise init refuses).

Exit codes (mapped via :class:`eawf.cli.errors.CliError` subclasses):

- ``0`` — success.
- ``3`` (``INVALID_INPUT``) — validation failure on inputs (regex, profile
  membership, missing required field) or pre-existing ``.ea/`` without
  ``--force``.
- ``5`` (``LOCK_CONFLICT``) — sibling lock contention on the freshly-written
  state file (rare; concurrent ``eawf init`` against the same target).

The handler delegates almost everything to :mod:`eawf.install.wizard` and
keeps itself confined to argument parsing, error mapping, and JSON-envelope
emission — see ``AGENTS.md`` rule 1 (CLI is dispatch; library implements).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer
from pydantic import ValidationError

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.install.wizard import (
    WizardAnswers,
    WizardResult,
    run_wizard_interactive,
    run_wizard_no_input,
)
from eawf.lock import portalock

logger = logging.getLogger(__name__)


def _build_answers(
    *,
    state_path: Path,
    project_code: str | None,
    project_title: str | None,
    profiles: list[str] | None,
    runtime: str,
    lifecycle_depth: str,
    plugins: list[str] | None,
    mcp: list[str] | None,
    acceptance_tests: bool,
    acceptance_lint: bool,
    acceptance_typecheck: bool,
    write_confirm: bool,
) -> WizardAnswers:
    """Coerce CLI flag values into a validated :class:`WizardAnswers`.

    Translates Typer-side ``None`` defaults into the wizard's expected
    shapes — empty strings are normalised to project_code/title sentinels
    so :class:`WizardAnswers` can apply its own validation. Multichoice
    flags are coerced to tuples (Pydantic accepts lists, but tuples make
    :class:`WizardAnswers` ``frozen=True`` happy on the round-trip).
    """
    return WizardAnswers(
        state_path=str(state_path),
        project_code=project_code or "",
        project_title=project_title or "",
        lifecycle_depth=lifecycle_depth,
        profiles=tuple(profiles or ("core",)),
        runtime=runtime,
        plugins=tuple(plugins or ()),
        mcp=tuple(mcp or ()),
        acceptance_tests=acceptance_tests,
        acceptance_lint=acceptance_lint,
        acceptance_typecheck=acceptance_typecheck,
        write_confirm=write_confirm,
    )


def _result_to_payload(result: WizardResult) -> dict[str, object]:
    """Render a :class:`WizardResult` as a JSON-serialisable envelope payload.

    Paths are stringified deterministically; lists pass through. Used by
    both the JSON branch (``--json``) and the text branch (which only
    emits a one-line "wrote N files" summary built from this payload).
    """
    return {
        "project_code": result.project_code,
        "profiles_enabled": list(result.profiles_enabled),
        "state_path": str(result.state_path),
        "config_path": str(result.config_path),
        "agents_md_path": str(result.agents_md_path),
        "claude_md_path": str(result.claude_md_path),
        "manifest_path": str(result.manifest_path),
        "materialised_state_keys": list(result.materialised_state_keys),
    }


def init_cmd(
    ctx: typer.Context,
    target: Annotated[
        Path | None,
        typer.Option(
            "--target",
            help="Target directory (defaults to the current working directory).",
        ),
    ] = None,
    state_path: Annotated[
        Path,
        typer.Option(
            "--state-path",
            help="Path of the state file relative to the target dir (or absolute).",
        ),
    ] = Path(".ea/state.json"),
    project_code: Annotated[
        str | None,
        typer.Option(
            "--project-code",
            help="Project code (uppercase, alnum/dash, 2-16 chars).",
        ),
    ] = None,
    project_title: Annotated[
        str | None,
        typer.Option("--project-title", help="Free-form project title."),
    ] = None,
    profile: Annotated[
        list[str] | None,
        typer.Option(
            "--profile",
            help="Profiles to enable (repeatable; defaults to 'core').",
        ),
    ] = None,
    runtime: Annotated[
        str,
        typer.Option(
            "--runtime",
            help="Default runtime (claude-code|opencode|generic).",
        ),
    ] = "claude-code",
    lifecycle_depth: Annotated[
        str,
        typer.Option(
            "--lifecycle-depth",
            help="Default lifecycle depth (phase|iter|wave).",
        ),
    ] = "phase",
    plugin: Annotated[
        list[str] | None,
        typer.Option("--plugin", help="Optional plugins (repeatable)."),
    ] = None,
    mcp: Annotated[
        list[str] | None,
        typer.Option("--mcp", help="Optional MCP servers (repeatable)."),
    ] = None,
    acceptance_tests: Annotated[
        bool,
        typer.Option(
            "--acceptance-tests/--no-acceptance-tests",
            help="Require tests as an acceptance gate.",
        ),
    ] = True,
    acceptance_lint: Annotated[
        bool,
        typer.Option(
            "--acceptance-lint/--no-acceptance-lint",
            help="Require lint as an acceptance gate.",
        ),
    ] = True,
    acceptance_typecheck: Annotated[
        bool,
        typer.Option(
            "--acceptance-typecheck/--no-acceptance-typecheck",
            help="Require typecheck as an acceptance gate.",
        ),
    ] = True,
    write_confirm: Annotated[
        bool,
        typer.Option(
            "--write-confirm/--no-write-confirm",
            help="Confirm before writing files (interactive only).",
        ),
    ] = True,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing .ea/ canonical files.",
        ),
    ] = False,
) -> None:
    """Initialise a new Eä Workflow workspace at *target*."""
    flags: GlobalFlags = ctx.obj
    target_dir = (target or Path.cwd()).resolve()

    if flags.no_input:
        if not project_code:
            cli_errors.emit_error(
                cli_errors.InvalidInput(
                    "--no-input requires --project-code; pass --project-code DEMO"
                ),
                flags=flags,
            )
            return  # never reached — emit_error raises typer.Exit
        try:
            answers = _build_answers(
                state_path=state_path,
                project_code=project_code,
                project_title=project_title,
                profiles=profile,
                runtime=runtime,
                lifecycle_depth=lifecycle_depth,
                plugins=plugin,
                mcp=mcp,
                acceptance_tests=acceptance_tests,
                acceptance_lint=acceptance_lint,
                acceptance_typecheck=acceptance_typecheck,
                write_confirm=write_confirm,
            )
        except ValidationError as exc:
            cli_errors.emit_error(cli_errors.InvalidInput(str(exc)), flags=flags)
            return
        try:
            result = run_wizard_no_input(answers, target_dir, force=force)
        except cli_errors.CliError as exc:
            cli_errors.emit_error(exc, flags=flags)
            return
        except portalock.LockTimeout as exc:
            cli_errors.emit_error(cli_errors.LockConflict(str(exc)), flags=flags)
            return
        payload = _result_to_payload(result)
        text = (
            f"eawf init: project={result.project_code} "
            f"profiles={list(result.profiles_enabled)} "
            f"state={result.state_path} agents_md={result.agents_md_path}"
        )
        emit_json_or_text(payload, text, flags=flags)
        return

    # Interactive path. We only attempt the Textual launch if the operator
    # did NOT pass --no-input — the Textual app needs a live terminal and
    # will crash inside CliRunner. Tests never hit this branch.
    try:
        result = run_wizard_interactive(target_dir)
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.LockConflict(str(exc)), flags=flags)
        return
    payload = _result_to_payload(result)
    text = (
        f"eawf init: project={result.project_code} "
        f"profiles={list(result.profiles_enabled)} state={result.state_path}"
    )
    emit_json_or_text(payload, text, flags=flags)
