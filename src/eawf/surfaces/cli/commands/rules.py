"""``eawf rules`` — read what the rule render generated.

``eawf rules view <module>`` prints the detailed view of one selected rule
module, the command every module-index line in the project card names. The
view is read from disk as the render transaction wrote it; the library judges
it against the current rule graph, so a lagging view prints with a stale
notice and a missing one names the command that renders it.
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
    help="Read the rule views rendered from .ea/rules.yaml.",
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


__all__ = ["rules_app"]
