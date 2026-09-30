"""``eawf question open-decision`` and ``eawf question answer``: ask the operator, and answer.

``open-decision`` is a dispatch over ``runtime.question.open_decision``: the
document it sends is the daemon's own closed request, validated there, and
the answer it prints is the daemon's typed answer, bound host question and
numbered prompt included. ``answer`` relays a reply to that numbered prompt
over ``runtime.question.answer_numbered``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags

#: The daemon verbs these commands forward to, spelled here so the Typer tree
#: builds without the daemon method registry on the path.
QUESTION_OPEN_DECISION: Final = "runtime.question.open_decision"
QUESTION_ANSWER_NUMBERED: Final = "runtime.question.answer_numbered"


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
    from eawf.surfaces.cli.verb_contract import read_spec_document

    flags: GlobalFlags = ctx.obj
    try:
        spec = read_spec_document(from_spec)
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    _forward(
        QUESTION_OPEN_DECISION,
        {**spec, "actor": actor},
        urn=str(spec.get("urn", "")),
        verb_text="question open-decision",
        flags=flags,
    )


def question_answer(
    ctx: typer.Context,
    urn: Annotated[str, typer.Argument(help="The pending action the numbered prompt names.")],
    revision: Annotated[
        int, typer.Option("--revision", help="The revision the numbered prompt names.")
    ],
    reply: Annotated[str, typer.Option("--reply", help="The option number the operator typed.")],
    actor: Annotated[str, typer.Option("--actor", help="Principal key of the person answering.")],
    receipt_ref: Annotated[
        str, typer.Option("--receipt-ref", help="Evidence row the answer is recorded under.")
    ],
    idempotency_key: Annotated[
        str, typer.Option("--idempotency-key", help="The name this answer is filed under.")
    ],
) -> None:
    """Seal the option a reply to a numbered question prompt chooses.

    A host without a multiple-choice picker prints the numbered prompt an
    open decision answers with; the operator's reply is relayed here and
    read back to the persisted option daemon-side. Only a bare option number
    binds. The console answers the same record, and the first answer wins.
    """
    _forward(
        QUESTION_ANSWER_NUMBERED,
        {
            "urn": urn,
            "expected_revision": revision,
            "idempotency_key": idempotency_key,
            "actor": actor,
            "resolver": {"principal_kind": "human", "principal_id": actor},
            "reply": reply,
            "receipt_ref": receipt_ref,
        },
        urn=urn,
        verb_text="question answer",
        flags=ctx.obj,
    )


def _forward(
    method: str, params: dict[str, Any], *, urn: str, verb_text: str, flags: GlobalFlags
) -> None:
    """Send one question verb to the daemon and print its typed answer or refusal."""
    from eawf.surfaces.cli._daemon_client import DaemonRpcError
    from eawf.surfaces.cli.commands.domain import _native_answer
    from eawf.surfaces.cli.verb_contract import answer_envelope, emit_envelope, refusal_envelope

    try:
        answer = _native_answer(method, params, flags=flags, verb_text=verb_text)
    except DaemonRpcError as exc:
        if exc.code != errors.RPC_VALIDATION_FAILED:
            errors.emit_error(errors.cli_error_for_rpc(exc.code, exc.message), flags=flags)
            return
        refusal = refusal_envelope(exc.message, operation=method, urn=urn)
        emit_envelope(refusal, urn=urn, flags=flags)
        return
    except errors.CliError as exc:
        errors.emit_error(exc, flags=flags)
        return
    envelope = answer_envelope(
        answer, operation=method, urn=urn, revision_before=None, revision_after=None
    )
    emit_envelope(envelope, urn=urn, flags=flags)
