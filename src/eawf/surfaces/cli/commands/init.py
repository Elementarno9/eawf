"""``eawf init`` Typer command.

Surface contract:

- ``eawf init`` (no flags, TTY) launches the questionary wizard. CI must
  pass ``--no-input``; absent ``--no-input`` and an actual TTY, the
  launcher falls through to interactive mode anyway. Tests that need to
  drive the interactive surface inject a
  :class:`prompt_toolkit.input.PipeInput` via
  :func:`prompt_toolkit.application.create_app_session`.
- ``eawf init --no-input --project-code DEMO --profile core ...`` runs the
  pure pipeline in :func:`eawf.platform.install.wizard.run_wizard_no_input`.
- ``eawf init --no-input --profiles core,python ...`` is the comma-list
  equivalent of repeated ``--profile`` flags.
- ``eawf init --no-input --template research ...`` selects a bundled
  bootstrap template (three v0.3 templates: research, engineering,
  reverse-engineering). Mutually exclusive with
  ``--profiles`` / ``--profile``.
- ``eawf init --force`` allows the pipeline to overwrite an existing
  ``.ea/state.json`` or ``.ea/config.yaml`` (otherwise init refuses).
- ``eawf init --refresh-gitignore`` updates only the managed ignore block
  for ``--state-path`` and skips the wizard pipeline.
- ``eawf init --epoch2-canary provision --target <dir>`` lays down a
  fresh project at ``<dir>``, declares it a disposable canary born at
  authority epoch 2, allocates it a fresh daemon runtime directory and
  registers it; ``--epoch2-canary teardown --target <dir>`` removes the
  registry row, the runtime directory and the tree. Neither changes what
  a plain ``eawf init`` writes.

Exit codes (mapped via :class:`eawf.surfaces.cli.errors.CliError` subclasses):

- ``0`` — success.
- ``3`` (``INVALID_INPUT``) — validation failure on inputs (regex, profile
  membership, missing required field) or pre-existing ``.ea/`` without
  ``--force``.
- ``5`` (``LOCK_CONFLICT``) — sibling lock contention on the freshly-written
  state file (rare; concurrent ``eawf init`` against the same target).

The handler delegates almost everything to :mod:`eawf.platform.install.wizard` and
keeps itself confined to argument parsing, error mapping, and JSON-envelope
emission — see ``AGENTS.md`` rule 1 (CLI is dispatch; library implements).
"""

from __future__ import annotations

import logging
import shutil
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.runtime.lock import portalock
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from pydantic import ValidationError

    from eawf.platform.install.wizard import WizardAnswers, WizardResult

logger = logging.getLogger(__name__)


class CanaryAction(StrEnum):
    """What ``--epoch2-canary`` does with the tree at ``--target``."""

    PROVISION = "provision"
    TEARDOWN = "teardown"


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
    mutually exclusive: at most one may be passed. Passing none falls
    through to the wizard default (``["core"]``).

    When ``--template`` is the chosen surface, the template's
    ``profiles.enabled`` becomes the profiles list and the remaining
    keys become ``template_extras`` (deep-merged into ``.ea/config.yaml``
    by :func:`eawf.platform.install.wizard._build_config_yaml`).

    Args:
        profile: Repeatable ``--profile`` values (legacy v0.1 surface).
        profiles_csv: ``--profiles a,b,c`` comma list.
        template: ``--template <name>`` bundled template name.

    Returns:
        Pair ``(profiles_list, template_extras)``. ``profiles_list`` is
        ``None`` when no flag chose a profile set (wizard default
        applies). ``template_extras`` is ``None`` unless ``--template``
        was selected.

    Raises:
        UserError: More than one surface used, or unknown template
            (``kind="InvalidInput"``).
    """
    from eawf.platform.profiles.discovery import load_init_template

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
    auto_install_plugins: bool,
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

    ``template_extras`` is the parsed bootstrap-template payload;
    forwarded into the wizard so ``_build_config_yaml`` can deep-merge it
    into the canonical ``.ea/config.yaml``.
    """
    from eawf.platform.install.wizard import WizardAnswers

    return WizardAnswers(
        state_path=str(state_path),
        project_code=project_code or "",
        project_title=project_title or "",
        lifecycle_depth=lifecycle_depth,
        profiles=tuple(profiles or ("core",)),
        runtime=runtime,
        plugins=tuple(plugins or ()),
        mcp=tuple(mcp or ()),
        auto_install_plugins=auto_install_plugins,
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
        "gitignore_path": str(result.gitignore_path),
        "materialised_state_keys": list(result.materialised_state_keys),
        "gitignore_patterns": list(result.gitignore_patterns),
        "auto_installed_plugins": list(result.auto_installed_plugins),
        "subagent_spec_preview": result.subagent_spec_preview,
    }


def _provision_canary(
    target_dir: Path,
    *,
    project_code: str,
    runtime: str,
    lifecycle_depth: str,
    registry_path: Path | None,
) -> tuple[dict[str, object], str]:
    """Provision a disposable epoch-2 canary at *target_dir*.

    Every refusal the library can raise before a write -- a malformed or
    taken code, a directory that is not fresh -- runs first. A failure
    after the tree exists removes the tree again, so a refused provision
    never leaves a half-built canary behind.

    Returns:
        ``(payload, text)`` for :func:`emit_json_or_text`.

    Raises:
        CanaryProvisionError: A pre-write refusal.
        CliError: The wizard or the registry write failed.
    """
    from eawf.platform.install.canary import (
        canary_ref,
        discard_canary,
        provision_canary,
        register_canary,
        require_code_unregistered,
        require_fresh_root,
    )
    from eawf.platform.install.wizard import run_wizard_no_input
    from eawf.surfaces.cli.commands.repo import (
        _persist_registry,
        _read_registry_for_write,
        _resolve_registry_path,
    )

    ref = canary_ref(project_code)
    require_fresh_root(target_dir)
    registry_file = _resolve_registry_path(registry_path)
    require_code_unregistered(_read_registry_for_write(registry_file), ref)
    answers = _build_answers(
        state_path=Path(".ea/state.json"),
        project_code=project_code,
        project_title=f"epoch-2 canary {project_code}",
        profiles=None,
        runtime=runtime,
        lifecycle_depth=lifecycle_depth,
        plugins=None,
        mcp=None,
        auto_install_plugins=False,
        acceptance_tests=True,
        acceptance_lint=True,
        acceptance_typecheck=True,
    )
    try:
        result = run_wizard_no_input(answers, target_dir)
        provision = provision_canary(
            repo_root=target_dir, ref=ref, provisioned_at=datetime.now(UTC)
        )
    except Exception:
        shutil.rmtree(target_dir, ignore_errors=True)
        raise
    try:
        registry = _read_registry_for_write(registry_file)
        _persist_registry(register_canary(registry, provision), registry_file)
    except Exception:
        discard_canary(provision, removed_registry_codes=())
        raise
    payload: dict[str, object] = {
        "action": CanaryAction.PROVISION.value,
        "canary": provision.ref.model_dump(mode="json"),
        "epoch": 2,
        "generation_id": provision.generation_id,
        "registry_path": str(registry_file),
        "root": str(provision.root),
        "runtime_dir": str(provision.runtime_dir),
        "state_path": str(result.state_path),
    }
    text = (
        f"eawf init: epoch-2 canary {project_code} at {provision.root}; serve it with "
        f"EAWF_RUNTIME_DIR={provision.runtime_dir}"
    )
    return payload, text


def _teardown_canary(
    target_dir: Path, *, registry_path: Path | None
) -> tuple[dict[str, object], str]:
    """Tear down the canary at *target_dir*, registry row first.

    Returns:
        ``(payload, text)`` for :func:`emit_json_or_text`.

    Raises:
        CanaryProvisionError: The tree is not a canary provisioned by
            ``eawf init``, or its daemon may still be running.
        CliError: The registry write failed.
    """
    from eawf.platform.install.canary import (
        discard_canary,
        read_provision,
        require_no_live_daemon,
        unregister_canary,
    )
    from eawf.surfaces.cli.commands.repo import (
        _persist_registry,
        _read_registry_for_write,
        _resolve_registry_path,
    )

    provision = read_provision(target_dir)
    require_no_live_daemon(provision)
    registry_file = _resolve_registry_path(registry_path)
    updated, removed = unregister_canary(_read_registry_for_write(registry_file), provision)
    if removed:
        _persist_registry(updated, registry_file)
    teardown = discard_canary(provision, removed_registry_codes=removed)
    payload: dict[str, object] = {
        "action": CanaryAction.TEARDOWN.value,
        "canary": teardown.ref.model_dump(mode="json"),
        "registry_path": str(registry_file),
        "removed_registry_codes": list(teardown.removed_registry_codes),
        "root": str(provision.root),
        "runtime_dir_removed": teardown.runtime_dir_removed,
    }
    text = (
        f"eawf init: tore down epoch-2 canary {teardown.ref.project_code}; "
        f"registry rows removed={list(teardown.removed_registry_codes)}"
    )
    return payload, text


def _run_epoch2_canary(
    action: CanaryAction,
    *,
    target_dir: Path,
    project_code: str | None,
    runtime: str,
    lifecycle_depth: str,
    registry_path: Path | None,
    flags: GlobalFlags,
) -> None:
    """Dispatch ``--epoch2-canary`` and emit its envelope or its error."""
    from eawf.platform.install.canary import DEFAULT_CANARY_CODE, CanaryProvisionError

    try:
        if action is CanaryAction.PROVISION:
            payload, text = _provision_canary(
                target_dir,
                project_code=project_code or DEFAULT_CANARY_CODE,
                runtime=runtime,
                lifecycle_depth=lifecycle_depth,
                registry_path=registry_path,
            )
        else:
            payload, text = _teardown_canary(target_dir, registry_path=registry_path)
    except CanaryProvisionError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="InvalidInput"), flags=flags)
        return
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return
    except portalock.LockTimeout as exc:
        cli_errors.emit_error(cli_errors.StateConflict(str(exc), kind="LockConflict"), flags=flags)
        return
    emit_json_or_text(payload, text, flags=flags)


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
    quick: Annotated[
        bool,
        typer.Option(
            "--quick",
            help=(
                "Run non-interactive init with detected profiles, an inferred "
                "project code, managed .gitignore, and runtime-plugin install."
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
    auto_install_plugins: Annotated[
        bool,
        typer.Option(
            "--auto-install-plugins/--no-auto-install-plugins",
            help="Install the selected runtime plugin after init writes the workspace files.",
        ),
    ] = False,
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
    refresh_gitignore: Annotated[
        bool,
        typer.Option(
            "--refresh-gitignore",
            help=(
                "Refresh only the managed .gitignore block for the selected "
                "state path; do not run the init wizard."
            ),
        ),
    ] = False,
    force: Annotated[
        bool,
        typer.Option(
            "--force",
            help="Overwrite existing .ea/ canonical files.",
        ),
    ] = False,
    epoch2_canary: Annotated[
        CanaryAction | None,
        typer.Option(
            "--epoch2-canary",
            help=(
                "Provision a disposable epoch-2 canary repository at --target "
                "(--project-code names it, default CANARY), or tear one down."
            ),
        ),
    ] = None,
    registry_path: Annotated[
        Path | None,
        typer.Option(
            "--registry-path",
            help="Override ``~/.eawf/registry.json`` for --epoch2-canary (tests).",
        ),
    ] = None,
) -> None:
    """Initialise a new Eä Workflow workspace at *target*."""
    flags: GlobalFlags = ctx.obj
    target_dir = (target or Path.cwd()).resolve()

    if epoch2_canary is not None:
        _run_epoch2_canary(
            epoch2_canary,
            target_dir=target_dir,
            project_code=project_code,
            runtime=runtime,
            lifecycle_depth=lifecycle_depth,
            registry_path=registry_path,
            flags=flags,
        )
        return

    if refresh_gitignore:
        from eawf.platform.install.gitignore_writer import write_gitignore

        resolved_state_path = (
            state_path if state_path.is_absolute() else target_dir / state_path
        ).resolve()
        try:
            gitignore_result = write_gitignore(
                target_dir,
                state_path=resolved_state_path,
            )
        except ValueError as exc:
            cli_errors.emit_error(
                cli_errors.UserError(str(exc), kind="InvalidInput"),
                flags=flags,
            )
            return
        refresh_payload: dict[str, object] = {
            "gitignore_path": str(gitignore_result.path),
            "gitignore_patterns": list(gitignore_result.patterns),
        }
        emit_json_or_text(
            refresh_payload,
            f"eawf init: refreshed gitignore={gitignore_result.path}",
            flags=flags,
        )
        return

    from pydantic import ValidationError

    from eawf.platform.install.wizard import run_wizard_interactive, run_wizard_no_input
    from eawf.platform.profiles.discovery import list_init_templates

    if list_templates:
        names = list_init_templates()
        list_payload: dict[str, object] = {"templates": list(names)}
        text = "\n".join(names) if names else "(no bundled templates)"
        emit_json_or_text(list_payload, text, flags=flags)
        return

    from eawf.platform.install.wizard import detect_profiles_for_target

    try:
        resolved_profiles, template_extras = _resolve_profiles_and_template(
            profile=profile,
            profiles_csv=profiles,
            template=template,
        )
    except cli_errors.CliError as exc:
        cli_errors.emit_error(exc, flags=flags)
        return

    if quick and resolved_profiles is None:
        resolved_profiles = list(detect_profiles_for_target(target_dir))

    if flags.no_input or quick:
        if not project_code and not quick:
            cli_errors.emit_error(
                cli_errors.UserError(
                    "--no-input requires --project-code; pass --project-code DEMO",
                    kind="InvalidInput",
                ),
                flags=flags,
            )
            return  # never reached — emit_error raises typer.Exit
        if quick and not project_code:
            from eawf.platform.install.wizard import quick_project_code_for_target

            project_code = quick_project_code_for_target(target_dir)
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
                auto_install_plugins=quick or auto_install_plugins,
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
