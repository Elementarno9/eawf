"""``eawf rules`` — read what the rule render generated, migrate, roll back.

``eawf rules view <module>`` prints the detailed view of one selected rule
module, the command every module-index line in the project card names. The
view is read from disk as the render transaction wrote it; the library judges
it against the current rule graph, so a lagging view prints with a stale
notice and a missing one names the command that renders it.

``eawf rules migrate`` checks that every legacy render block and profile field
has exactly one typed disposition, and exits non-zero naming each gap.

``eawf rules rollback [GENERATION]`` re-selects a complete stored generation
of the projections: the named one, or the one selected before the current.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

rules_app = typer.Typer(
    name="rules",
    help="Read, migrate and roll back the projections rendered from .ea/rules.yaml.",
    no_args_is_help=True,
    add_completion=False,
)


@rules_app.command("view")
def rules_view(
    ctx: typer.Context,
    reference: Annotated[
        str,
        typer.Argument(
            help="Module reference from the card's module index (e.g. eawf.craft.test)."
        ),
    ],
) -> None:
    """Print the detailed view of one selected rule module."""
    from eawf.platform.rules import RuleCompileError, RuleModuleError, RuleSourceError
    from eawf.platform.rules.render import read_rule_view
    from eawf.platform.rules.views import RuleViewError

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()
    try:
        read = read_rule_view(repo_root, reference)
    except RuleViewError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    except (RuleSourceError, RuleCompileError, RuleModuleError) as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"rule source refused: {exc}"), flags=flags
        )
        return
    if read.text is None:
        cli_errors.emit_error(
            cli_errors.UserError(read.message or read.target, kind="NotFound"), flags=flags
        )
        return
    text = read.text if read.message is None else f"{read.message}\n\n{read.text}"
    emit_json_or_text(read.model_dump(mode="json"), text, flags=flags)


@rules_app.command("migrate")
def rules_migrate(ctx: typer.Context) -> None:
    """Check every legacy block and profile field has exactly one disposition."""
    from eawf.platform.rules.migration import plan_legacy_migration

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()
    try:
        migration = plan_legacy_migration(repo_root)
    except ValueError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"legacy migration refused: {exc}"), flags=flags
        )
        return
    payload = {
        **migration.model_dump(mode="json"),
        "undisposed": list(migration.undisposed),
        "complete": migration.complete,
    }
    gaps = [
        *(f"undisposed: {key}" for key in migration.undisposed),
        *(f"disposes no inventory item: {key}" for key in migration.unknown),
        *(f"disposed more than once: {key}" for key in migration.repeated),
        *(f"obligation owned by no rule: {line}" for line in migration.unresolved),
    ]
    if gaps:
        cli_errors.emit_error(
            cli_errors.ValidationError(
                f"legacy migration incomplete, {len(gaps)} gaps:\n" + "\n".join(gaps)
            ),
            flags=flags,
            data=payload,
        )
        return
    emit_json_or_text(
        payload, f"legacy migration complete: {len(migration.entries)} items disposed", flags=flags
    )


@rules_app.command("rollback")
def rules_rollback(
    ctx: typer.Context,
    generation: Annotated[
        str | None,
        typer.Argument(
            help="Generation digest to select; default the one selected before the current."
        ),
    ] = None,
) -> None:
    """Re-select a complete stored generation of the rule projections."""
    from eawf.platform.rules.generations import RuleGenerationError, rollback_rule_projections
    from eawf.platform.rules.render import RuleProjectionError

    flags: GlobalFlags = ctx.obj
    repo_root = (flags.workspace or Path.cwd()).resolve()
    try:
        written = rollback_rule_projections(repo_root, generation)
    except RuleGenerationError as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
        return
    except RuleProjectionError as exc:
        cli_errors.emit_error(
            cli_errors.ValidationError(f"rule rollback refused: {exc}"), flags=flags
        )
        return
    text = (
        f"selected generation {written.manifest.generation}; "
        f"changed {len(written.changed)}, removed {len(written.removed)}"
    )
    emit_json_or_text(written.model_dump(mode="json"), text, flags=flags)


__all__ = ["rules_app"]
