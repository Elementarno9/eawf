"""``eawf schema`` Typer sub-app — JSON Schema dump for canonical models.

CLI dispatch only (AGENTS rule 1): the single ``dump`` verb resolves the
repo root, drives :func:`eawf.docs.autogen.generate_all`, and routes the
written-path list through :func:`eawf.surfaces.cli.output.emit_json_or_text`.

``eawf schema dump`` emits the deterministic JSON Schema of the canonical
state + envelope Pydantic models *and* the introspection-driven reference
markdown pages under ``docs/reference/autogen/`` so the committed docs can
never silently drift from the source tree. Pass ``--schema-only`` to write
just the ``.schema.json`` dumps.

Exit codes:

- ``0`` — schema + reference pages written.
- ``1`` (``USER_ERROR``) — no repo root resolvable.
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


schema_app = typer.Typer(
    name="schema",
    help="Dump JSON Schema + reference pages for the canonical models.",
    no_args_is_help=True,
    add_completion=False,
)


def _resolve_repo_root(workspace: Path | None) -> Path:
    """Resolve the repo root that owns ``docs/reference/autogen``.

    Args:
        workspace: Optional ``-w/--workspace`` override; ``None`` falls
            back to the current working directory.

    Returns:
        The repo root directory.

    Raises:
        UserError: When *workspace* is given but is not a directory.
    """
    if workspace is None:
        return Path.cwd()
    if not workspace.is_dir():
        raise cli_errors.UserError(f"workspace is not a directory: {workspace}")
    return workspace


@schema_app.command("dump")
def schema_dump(
    ctx: typer.Context,
    schema_only: Annotated[
        bool,
        typer.Option(
            "--schema-only",
            help="Write only the .schema.json dumps, not the reference markdown.",
        ),
    ] = False,
) -> None:
    """Dump JSON Schema (and reference pages) for the canonical models.

    Writes the deterministic JSON Schema of the state + envelope models to
    ``docs/reference/autogen/<model>.schema.json``. Without ``--schema-only``
    the full set of introspection-driven reference pages (cli / skills /
    schema / enum / error-code / exit-code) is regenerated too.
    """
    flags: GlobalFlags = ctx.obj
    try:
        repo_root = _resolve_repo_root(flags.workspace)
    except cli_errors.CliError as err:
        cli_errors.emit_error(err, flags=flags)
        return

    # Imported lazily so building the CLI tree (eawf.surfaces.cli.app import) does not
    # pull the heavy autogen dependency graph (state.models / store.kinds /
    # yaml) — see tests/perf/cli/test_import_budget.py.
    from eawf.docs.autogen import dump_schemas, generate_all

    written = dump_schemas(repo_root) if schema_only else generate_all(repo_root)

    rel = sorted(str(p.relative_to(repo_root)) for p in written)
    payload: dict[str, object] = {
        "schema_only": schema_only,
        "written": rel,
        "count": len(rel),
    }
    text = "schema dump: wrote {count} file(s)\n{paths}".format(
        count=len(rel),
        paths="\n".join(f"  {p}" for p in rel),
    )
    emit_json_or_text(payload, text, flags=flags)


__all__ = ["schema_app"]
