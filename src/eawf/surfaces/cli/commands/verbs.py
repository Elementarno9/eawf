"""``eawf verbs``: emit the verb catalog."""

from __future__ import annotations

import typer

from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text


def verbs_cmd(ctx: typer.Context) -> None:
    """List every verb with its entity, parameters, typed errors and effect class."""
    from eawf.surfaces.cli.verb_catalog import verb_catalog, verb_catalog_text

    flags: GlobalFlags = ctx.obj
    catalog = verb_catalog()
    emit_json_or_text(catalog.model_dump(mode="json"), verb_catalog_text(catalog), flags=flags)
