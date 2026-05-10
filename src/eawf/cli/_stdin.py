"""Shared stdin guard for Typer commands that consume a piped JSON envelope.

Three commands today (``eawf cc statusline``, ``eawf cc statusline prewarm``,
``eawf render-output``) read a JSON document from stdin via blocking
``sys.stdin.read()``. When invoked at a TTY with no piped data the read
blocks indefinitely — which looks like the CLI hung. This helper exits
with a clear hint instead.

The helper lives in :mod:`eawf.cli` rather than next to any one command
because it is shared across two packages (``eawf.cli.commands.cc`` and
``eawf.cli.commands.render_output``) and a runtime-side import would
introduce a cycle.
"""

from __future__ import annotations

import sys

import typer

_HINT = (
    "{name} expects a JSON envelope on stdin. "
    "Pipe a payload (e.g. `echo '{{}}' | {name}`) "
    "or invoke via the Claude Code hook."
)


def require_piped_stdin(name: str) -> None:
    """Exit ``2`` with a hint when stdin is a TTY (no piped input).

    *name* is the command label surfaced in the hint (e.g.
    ``"eawf cc statusline"``); it is repeated twice in the message via the
    template above. Callers invoke this immediately before any
    ``sys.stdin.read()`` so the operator sees the hint instead of a hang.
    """
    if sys.stdin.isatty():
        typer.echo(_HINT.format(name=name), err=True)
        raise typer.Exit(code=2)


__all__ = ["require_piped_stdin"]
