"""``eawf init`` Typer command.

Surface contract:

- ``eawf init`` (no flags, TTY) launches the questionary wizard. CI must
  pass ``--no-input``; absent ``--no-input`` and an actual TTY, the
  launcher falls through to interactive mode anyway. Tests that need to
  drive the interactive surface inject a
  :class:`prompt_toolkit.input.PipeInput` via
  :func:`prompt_toolkit.application.create_app_session`.
- ``eawf init --no-input --project-code DEMO --profile core ...`` runs the
  pure pipeline in :func:`eawf.install.wizard.run_wizard_no_input`.
- ``eawf init --no-input --profiles core,python ...`` is the comma-list
  equivalent of repeated ``--profile`` flags.
- ``eawf init --no-input --template research ...`` selects a bundled
  bootstrap template (three v0.3 templates: research, engineering,
  reverse-engineering). Mutually exclusive with
  ``--profiles`` / ``--profile``.
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
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.cli import errors as cli_errors
from eawf.cli.flags import GlobalFlags
from eawf.cli.output import emit_json_or_text
from eawf.lock import portalock

if TYPE_CHECKING:
    from pydantic import ValidationError

    from eawf.install.wizard import WizardAnswers, WizardResult

logger = logging.getLogger(__name__)


def _friendly_validation_message(exc: ValidationError) -> str:
    """Return the first error message from *exc*, stripped of Pydantic prefix.

    Pydantic v2 prepends ``"Value error, "`` to messages raised from
    :class:`ValueError` inside field validators. The wizard validators
    already produce operator-facing copy, so the prefix is noise; we
    strip it before handing the text to :class:`UserError`
    (``kind="InvalidInput"``).
    """
    errors = exc.errors()
    if not errors:
        return str(exc)
    msg = str(errors[0].get("msg", "")).strip()
    prefix = "Value error, "
    if msg.startswith(prefix):
        msg = msg[len(prefix) :]
    return msg or str(exc)


def _parse_profiles_csv(csv: str | None) -> list[str] | None:
    """Split a ``--profiles a,b,c`` comma list into a deduplicated ordered list.

    Whitespace around commas is tolerated. Empty entries are rejected so
    ``--profiles ,core`` fails fast instead of silently selecting only
    ``core``. The order of first appearance is preserved — the wizard's
    ``profiles`` field is order-significant (composition runs in
    caller order).
    """
    if csv is None:
        return None
    parts = [p.strip() for p in csv.split(",")]
    if any(not p for p in parts):
        raise cli_errors.UserError(
            f"--profiles got an empty entry: {csv!r}; comma-separate "
            "without trailing/leading commas",
            kind="InvalidInput",
        )
    seen: list[str] = []
    for p in parts:
        if p not in seen:
            seen.append(p)
    return seen


def _resolve_profiles_and_template(
    *,
    profile: list[str] | None,
    profiles_csv: str | None,
    template: str | None,
) -> tuple[list[str] | None, dict[str, Any] | None]:
    """Resolve the three init-surface flags into ``(profiles, template_extras)``.

    The three surfaces (`--profile`, `--profiles`, `--template`) are
    mutually exclusive: at most one may be passed.
    Passing none falls through to the wizard default (``["core"]``).
    Passing more than one raises :class:`UserError` (``kind="InvalidInput"``)
    so the operator picks the form they want.

    When ``--template`` is the chosen surface, the template's
    ``profiles.enabled`` becomes the profiles list and the remaining
    keys become ``template_extras`` (deep-merged into ``.ea/config.yaml``
    by :func:`eawf.install.wizard._build_config_yaml`).

    Args:
        profile: Repeatable ``--profile`` values (legacy v0.1 surface).
        profiles_csv: ``--profiles a,b,c`` comma list (P25-W16).
        template: ``--template <name>`` bundled template name (P25-W16).

    Returns:
        Pair ``(profiles_list, template_extras)``. ``profiles_list`` is
        ``None`` when no flag chose a profile set (wizard default
        applies). ``template_extras`` is ``None`` unless ``--template``
        was selected.

    Raises:
        UserError: More than one surface used, or unknown template
            (``kind="InvalidInput"``).
    """
    from eawf.profiles.discovery import load_init_template

    chosen = [
        flag
        for flag, present in (
            ("--profile", bool(profile)),
            ("--profiles", profiles_csv is not None),
            ("--template", template is not None),
        )
        if present
    ]
    if len(chosen) > 1:
        raise cli_errors.UserError(
            f"profile-selection flags are mutually exclusive: pass at most one of {chosen}",
            kind="InvalidInput",
        )

    if template is not None:
        try:
            payload = load_init_template(template)
        except cli_errors.CliError:
            raise
        template_profiles_section = payload.get("profiles", {})
        if not isinstance(template_profiles_section, dict):
            raise cli_errors.UserError(
                f"init template {template!r}: 'profiles' section must be a mapping",
                kind="InvalidInput",
            )
        enabled = template_profiles_section.get("enabled", [])
        if not isinstance(enabled, list) or not enabled:
            raise cli_errors.UserError(
                f"init template {template!r}: 'profiles.enabled' must be a "
                f"non-empty list of profile names",
                kind="InvalidInput",
            )
        return list(enabled), payload

    if profiles_csv is not None:
        return _parse_profiles_csv(profiles_csv), None

    return (profile if profile else None), None


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
    template_extras: dict[str, Any] | None = None,
) -> WizardAnswers:
    """Coerce CLI flag values into a validated :class:`WizardAnswers`.

    Translates Typer-side ``None`` defaults into the wizard's expected
    shapes — empty strings are normalised to project_code/title sentinels
    so :class:`WizardAnswers` can apply its own validation. Multichoice
    flags are coerced to tuples (Pydantic accepts lists; tuples are the
    canonical wizard-side form).

    ``write_confirm`` is intentionally NOT exposed at the CLI surface — it
    is reserved for the interactive questionary wizard (see
    :class:`WizardAnswers` field doc) and has no effect on the
    ``--no-input`` pipeline. The model default (``True``) carries through.

    ``template_extras`` is the parsed bootstrap-template payload (P25-W16);
    forwarded into the wizard so ``_build_config_yaml`` can deep-merge it
    into the canonical ``.ea/config.yaml``.
    """
    from eawf.install.wizard import WizardAnswers

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
        template_extras=template_extras,
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
            help=(
                "Profiles to enable (repeatable; defaults to 'core'). "
                "Mutually exclusive with --profiles and --template."
            ),
        ),
    ] = None,
    profiles: Annotated[
        str | None,
        typer.Option(
            "--profiles",
            help=(
                "Comma-separated profiles (e.g. 'core,python'). Equivalent "
                "to repeated --profile flags. Mutually exclusive with "
                "--profile and --template."
            ),
        ),
    ] = None,
    template: Annotated[
        str | None,
        typer.Option(
            "--template",
            help=(
                "Bundled bootstrap template (research|engineering|"
                "reverse-engineering). Mutually exclusive with --profile "
                "and --profiles. Lists via `eawf init --list-templates`."
            ),
        ),
    ] = None,
    list_templates: Annotated[
        bool,
        typer.Option(
            "--list-templates",
            help=(
                "Print the bundled init templates (one per line) and exit. "
                "Skips the wizard pipeline entirely."
            ),
        ),
    ] = False,
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
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing .ea/ canonical files.",
        ),
    ] = False,
) -> None:
    """Initialise a new Eä Workflow workspace at *target*."""
    from pydantic import ValidationError

    from eawf.install.wizard import run_wizard_interactive, run_wizard_no_input
    from eawf.profiles.discovery import list_init_templates

    flags: GlobalFlags = ctx.obj
    target_dir = (target or Path.cwd()).resolve()

    if list_templates:
        names = list_init_templates()
        list_payload: dict[str, object] = {"templates": list(names)}
        text = "\n".join(names) if names else "(no bundled templates)"
        emit_json_or_text(list_payload, text, flags=flags)
        return

    try:
        resolved_profiles, template_extras = _resolve_profiles_and_template(
            profile=profile,
            profiles_csv=profiles,
            template=template,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    if flags.no_input:
        if not project_code:
            cli_errors.emit_error(
                cli_errors.UserError(
                    "--no-input requires --project-code; pass --project-code DEMO",
                    kind="InvalidInput",
                ),
                flags=flags,
            )
            return  # never reached — emit_error raises typer.Exit
        try:
            answers = _build_answers(
                state_path=state_path,
                project_code=project_code,
                project_title=project_title,
                profiles=resolved_profiles,
                runtime=runtime,
                lifecycle_depth=lifecycle_depth,
                plugins=plugin,
                mcp=mcp,
                acceptance_tests=acceptance_tests,
                acceptance_lint=acceptance_lint,
                acceptance_typecheck=acceptance_typecheck,
                template_extras=template_extras,
            )
        except ValidationError as exc:
            cli_errors.emit_error(
                cli_errors.UserError(_friendly_validation_message(exc), kind="InvalidInput"),
                flags=flags,
            )
            return
        try:
            result = run_wizard_no_input(answers, target_dir, force=force)
        except cli_errors.CliError as exc:
            cli_errors.emit_error(exc, flags=flags)
            return
        except portalock.LockTimeout as exc:
            cli_errors.emit_error(
                cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags
            )
            return
        payload = _result_to_payload(result)
        text = (
            f"eawf init: project={result.project_code} "
            f"profiles={list(result.profiles_enabled)} "
            f"state={result.state_path} agents_md={result.agents_md_path}"
        )
        emit_json_or_text(payload, text, flags=flags)
        return

    # Interactive path. We only attempt the questionary launch if the
    # operator did NOT pass --no-input — questionary needs a live terminal
    # by default. Tests inject a ``prompt_toolkit.input.PipeInput`` via
    # ``create_app_session`` to drive this branch deterministically.
    try:
        result = run_wizard_interactive(target_dir, force=force)
    except ValidationError as exc:
        cli_errors.emit_error(
            cli_errors.UserError(_friendly_validation_message(exc), kind="InvalidInput"),
            flags=flags,
        )
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return
    payload = _result_to_payload(result)
    text = (
        f"eawf init: project={result.project_code} "
        f"profiles={list(result.profiles_enabled)} state={result.state_path}"
    )
    emit_json_or_text(payload, text, flags=flags)
