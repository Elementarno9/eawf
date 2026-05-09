"""Unified emission helper that respects ``--json`` / ``--plain``.

Every CLI handler routes its output through :func:`emit_json_or_text` so the
JSON envelope shape stays consistent across commands. The text branch uses
:func:`typer.echo` (which honours stdout TTY); the JSON branch uses
:mod:`orjson` with stable formatting (sorted keys, two-space indent) to keep
golden-test diffs deterministic.
"""

from __future__ import annotations

from typing import Any

import orjson
import typer

from eawf.cli.flags import GlobalFlags


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
        raw = orjson.dumps(payload, option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS)
        typer.echo(raw.decode("utf-8"))
    else:
        typer.echo(text)
