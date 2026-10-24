"""``eawf release canary-export``: write a release's native-canary evidence export.

The export is a committed artifact the ``provider`` and ``membership``
rows read, not a record the daemon owns, so the verb runs in this process
like ``preflight``: it reads this machine's stored certifications and
writes the file into the checkout for the operator to commit. The logic
lives in :mod:`eawf.workflow.evidence.canary_export`.
"""

from __future__ import annotations

from typing import Annotated

import typer
from pydantic import TypeAdapter

from eawf.kernel.spec.release import release_key
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.release import release_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.workflow.evidence.canary_export import write_canary_export
from eawf.workflow.evidence.provider_certification import (
    OsClass,
    canary_evidence_path,
    provider_evidence_refs,
)


@release_app.command("canary-export")
def release_canary_export(
    ctx: typer.Context,
    version: Annotated[str, typer.Argument(help="Checkpoint version, e.g. 0.7.0rc3.")],
    os_class: Annotated[
        list[str],
        typer.Option(
            "--os-class",
            help="Operating-system class every advertised runtime ships on; repeatable.",
        ),
    ],
) -> None:
    """Export the stored certifications of the installed runtimes for one checkpoint.

    Each runtime the previous export advertises is claimed at the version
    installed now, on every ``--os-class`` given, and each claim carries
    the driver certification ``eawf runtime certify --task`` stored for it,
    copied unchanged. A class no stored certification covers is refused.
    """
    flags: GlobalFlags = ctx.obj
    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    try:
        key = release_key(version)
        os_classes = TypeAdapter(tuple[OsClass, ...]).validate_python(os_class)
        evidence = write_canary_export(state_path, key, os_classes=os_classes)
    except ValueError as exc:
        cli_errors.emit_error(cli_errors.ValidationError(str(exc)), flags=flags)
        return
    path = canary_evidence_path(state_path.parent.parent, key)
    refs = provider_evidence_refs(evidence)
    emit_json_or_text(
        {
            "release_key": key,
            "path": path.relative_to(state_path.parent.parent).as_posix(),
            "certification_urns": list(refs),
        },
        f"{key}: wrote {path.name} with {', '.join(refs)}",
        flags=flags,
    )


__all__ = ["release_canary_export"]
