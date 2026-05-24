"""``eawf help [<topic>]`` — prose help-topic surface.

The smallest prose layer the operator needs without leaving the terminal.
Six hand-authored topics ship with v0.3, each ≤80 lines, sourced from
``docs/help/<topic>.md``:

* ``exit-codes`` — the 0..5 exit-code table + repair commands.
* ``daemon`` — daemon lifecycle, log path, troubleshooting.
* ``profiles`` — composable profile bundles + precedence.
* ``urns`` — URN grammar + kind catalog.
* ``migration`` — per-cluster migration steps.
* ``streaming`` — ``--stream`` flag, NDJSON shape, EOF semantics.

``eawf help`` (no topic) lists the registered topics. ``eawf help <topic>``
renders the topic markdown: paged through ``less -R`` when stdout is a TTY,
flat markdown otherwise (so CI / pipe consumers get deterministic output).
An unknown topic exits 1 USER_ERROR with ``data.kind="NotFound"`` and the
list of registered topics.

Topic files are located relative to the repo root (the ancestor of this
package that contains ``docs/help/``); AI-summarise-on-miss is YAGNI and
deferred to v0.5+.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer

from eawf.surfaces.cli.errors import UserError, emit_error
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

#: Registered help topics in display order. Each maps to a
#: ``docs/help/<topic>.md`` file. The order is the order ``eawf help``
#: lists them — grouped by what the operator reaches for first.
TOPICS: tuple[str, ...] = (
    "exit-codes",
    "daemon",
    "profiles",
    "urns",
    "migration",
    "streaming",
)


def _topics_dir() -> Path | None:
    """Return the ``docs/help`` directory, or ``None`` when not found.

    Walks up from this module's location looking for an ancestor that
    contains ``docs/help``. Works in-repo and in editable installs; a
    wheel install without the ``docs`` tree returns ``None`` so the caller
    surfaces a clean error instead of crashing.

    Returns:
        The resolved ``docs/help`` path, or ``None`` when no ancestor
        carries one.
    """
    for ancestor in Path(__file__).resolve().parents:
        candidate = ancestor / "docs" / "help"
        if candidate.is_dir():
            return candidate
    return None


def _topic_path(topic: str) -> Path | None:
    """Return the markdown path for *topic*, or ``None`` when unavailable.

    Args:
        topic: Registered topic name.

    Returns:
        The ``docs/help/<topic>.md`` path when both the topic is registered
        and the file exists; otherwise ``None``.
    """
    if topic not in TOPICS:
        return None
    base = _topics_dir()
    if base is None:
        return None
    path = base / f"{topic}.md"
    return path if path.is_file() else None


def _page(text: str) -> None:
    """Page *text* through ``less -R`` on a TTY; print flat otherwise.

    Args:
        text: The topic markdown body to display.
    """
    pager_ok = sys.stdout.isatty() and os.environ.get("EAWF_NO_PAGER") != "1"
    less = shutil.which("less")
    if pager_ok and less is not None:
        try:
            subprocess.run([less, "-R"], input=text, text=True, check=True)
            return
        except OSError, subprocess.SubprocessError:
            logger.debug("_page less-failed falling-back-to-flat")
    typer.echo(text)


help_app = typer.Typer(
    name="help",
    help="Show prose help topics (exit-codes, daemon, profiles, urns, migration, streaming).",
    no_args_is_help=False,
    add_completion=False,
    invoke_without_command=True,
)


@help_app.callback(invoke_without_command=True)
def help_cmd(
    ctx: typer.Context,
    topic: Annotated[
        str | None,
        typer.Argument(help="Help topic to display; omit to list topics."),
    ] = None,
) -> None:
    """Render *topic* prose, or list registered topics when *topic* is omitted.

    Raises:
        typer.Exit: Via :func:`emit_error` with exit 1 USER_ERROR
            (``data.kind="NotFound"``) when *topic* is unknown or its
            markdown file is missing.
    """
    flags: GlobalFlags = ctx.obj
    if topic is None:
        listing = "\n".join(f"  {name}" for name in TOPICS)
        emit_json_or_text(
            {"topics": list(TOPICS)},
            f"Available help topics (use `eawf help <topic>`):\n{listing}",
            flags=flags,
        )
        return

    if topic not in TOPICS:
        emit_error(
            UserError(f"unknown help topic: {topic!r}"),
            flags=flags,
            data={"kind": "NotFound", "topics": list(TOPICS)},
        )
        return

    path = _topic_path(topic)
    if path is None:
        emit_error(
            UserError(f"help topic source missing for {topic!r}"),
            flags=flags,
            data={"kind": "NotFound", "topics": list(TOPICS)},
        )
        return

    body = path.read_text(encoding="utf-8")
    if flags.json_output:
        emit_json_or_text({"topic": topic, "body": body}, body, flags=flags)
        return
    _page(body)


__all__ = ["TOPICS", "help_app", "help_cmd"]
