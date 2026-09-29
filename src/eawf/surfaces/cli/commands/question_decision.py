"""``eawf question open-decision``: file an operator decision the host shows as a typed question.

The verb is a dispatch over ``runtime.question.open_decision``: the document
it sends is the daemon's own closed request, validated there, and the answer
it prints is the daemon's typed answer, bound host question included.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

import typer

from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verb this command forwards to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
QUESTION_OPEN_DECISION: Final = "runtime.question.open_decision"


def question_open_decision(
    ctx: typer.Context,
    actor: Annotated[str, typer.Option("--actor", help="Principal key filing the decision.")],
    from_spec: Annotated[
        Path,
        typer.Option(
            "--from-spec",
            help=(
                "JSON file (or - for stdin) carrying urn, idempotency_key, requested_by, "
                "question, options and, optionally, the recommendation, terms, config_axis, "
                "override_window_minutes and assignee."
            ),
        ),
    ],
) -> None:
    """File a reversible operator decision the host shows as a typed question.

    The daemon resolves the repository's configuration, builds the waiting
    decision and presents it before writing it, so a question an operator
    could not answer as shown is refused with nothing filed. The answer
    carries the bound host question; the asker shows it and stops, and the
    operator's choice is sealed by option id, never by free text.
    """
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer
    from eawf.surfaces.cli.verb_contract import (
        answer_envelope,
        emit_envelope,
        read_spec_document,
        refusal_envelope,
    )

    flags: GlobalFlags = ctx.obj
    try:
        spec = read_spec_document(from_spec)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    urn = str(spec.get("urn", ""))
    try:
        answer = _native_answer(
            QUESTION_OPEN_DECISION,
            {**spec, "actor": actor},
            flags=flags,
            verb_text="question open-decision",
        )
    except DaemonRpcError as exc:
        if exc.code != errors.RPC_VALIDATION_FAILED:
            errors.emit_error(errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
            return
        refusal = refusal_envelope(exc.message, operation=QUESTION_OPEN_DECISION, urn=urn)
        emit_envelope(refusal, urn=urn, flags=flags)
        return
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    envelope = answer_envelope(
        answer,
        operation=QUESTION_OPEN_DECISION,
        urn=urn,
        revision_before=None,
        revision_after=None,
    )
    emit_envelope(envelope, urn=urn, flags=flags)
