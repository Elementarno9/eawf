"""Unified emission helper that respects ``--json`` / ``--plain``.

Every CLI handler routes its output through :func:`emit_json_or_text` so the
JSON envelope shape stays consistent across commands. The text branch uses
:func:`typer.echo` (which honours stdout TTY); the JSON branch uses
:mod:`orjson` with stable formatting (sorted keys, two-space indent) to keep
golden-test diffs deterministic.
"""

from __future__ import annotations

from typing import Any

import click
import orjson
import typer

from eawf.surfaces.cli.flags import GlobalFlags


def emit_json_or_text(
    payload: dict[str, Any],
    text: str,
    *,
    flags: GlobalFlags,
) -> None:
    """Print *payload* as JSON or *text* depending on ``flags.json_output``.

    Args:
        payload: Mapping serialised when JSON mode is active. Must be
            JSON-serialisable; orjson raises :class:`TypeError` on bad inputs.
        text: Human-readable body printed when JSON mode is inactive. Already
            formatted by the caller — the helper does not interpret markup.
        flags: Resolved global flags. Only ``json_output`` is consulted today;
            ``plain_output`` is reserved for downstream Rich-bypass logic.
    """
    if flags.json_output:
        raw = orjson.dumps(
            machine_payload(payload), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
        )
        typer.echo(raw.decode("utf-8"))
    else:
        typer.echo(text)


def _running_verb() -> tuple[str, ...]:
    """Return the command path of the running verb after ``eawf``, empty outside one."""
    ctx = click.get_current_context(silent=True)
    names: list[str] = []
    while ctx is not None and ctx.parent is not None:
        names.append(ctx.info_name or "")
        ctx = ctx.parent
    return tuple(reversed(names))


def machine_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Return what the machine mode prints for *payload*.

    A verb of a contract group answers with the one machine envelope: an
    answer that is not already one becomes the ``result`` of an ``ok``
    envelope whose operation is the verb's command path. A refusal is
    printed by :func:`eawf.surfaces.cli.errors.emit_error` as the typed
    error envelope, and a verb outside the contract groups prints its
    payload as it is.

    Args:
        payload: The verb's answer.

    Returns:
        The envelope, or *payload* unchanged when it is already an envelope,
        a refusal, or the answer of a verb outside the contract groups.
    """
    from eawf.runtime.daemon.methods.domain_envelope import (
        ENVELOPE_SCHEMA_VERSION,
        DomainEnvelope,
        DomainStatus,
    )
    from eawf.surfaces.cli.errors import ErrorEnvelope
    from eawf.surfaces.cli.verb_contract import CONTRACT_GROUPS

    verb = _running_verb()
    if not verb or verb[0] not in CONTRACT_GROUPS:
        return payload
    if set(payload) <= set(DomainEnvelope.model_fields) and "operation" in payload:
        return payload
    if set(payload) == set(ErrorEnvelope.model_fields):
        return payload
    envelope = DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.OK,
        operation=" ".join(verb),
        result=payload,
    )
    return envelope.model_dump(mode="json")
