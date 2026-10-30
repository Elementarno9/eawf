"""``eawf run transcript``: print every transcript of a Run, or of each Run of a Task.

The verb reads the files the machine-local transcript index names and prints them in
full, tool inputs and outputs included. It writes nothing. ``--follow`` keeps printing
what the runtime appends until the Runs end or the operator interrupts, and
``--scrub`` rewrites machine paths and token-shaped strings before anything prints.

Importing this module attaches the verb to the ``run`` group.
"""

from __future__ import annotations

import time
from collections.abc import Iterable, Iterator
from typing import TYPE_CHECKING, Annotated, Any, Final

import typer

from eawf.surfaces.cli import errors as cli_errors
from eawf.surfaces.cli.commands.lifecycle import run_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text
from eawf.surfaces.cli.scope import resolve_state_path

if TYPE_CHECKING:
    from eawf.runtime.runtimes.run_transcript import TranscriptEntry, TranscriptSource

#: How long a followed transcript waits between reads, in seconds.
FOLLOW_POLL_SECONDS: Final = 1.0

#: How each entry kind is labelled in the text rendering.
_LABELS: Final = {
    "user": "user",
    "assistant": "assistant",
    "tool_call": "tool call",
    "tool_result": "tool result",
    "notice": "notice",
}


def wait_between_reads() -> None:
    """Wait before a followed transcript is read again."""
    time.sleep(FOLLOW_POLL_SECONDS)


def _heading(source: TranscriptSource) -> str:
    run = source.run_key or "no Run"
    kind = " subagent" if source.subagent else ""
    return f"== {source.runtime}{kind} transcript of {run}: {source.path}"


def _line(entry: TranscriptEntry) -> str:
    label = _LABELS[entry.kind]
    if entry.is_error:
        label = f"{label} error"
    if entry.tool_name is not None:
        label = f"{label} {entry.tool_name}"
    return f"{label}: {entry.text}"


def _text_lines(stream: Iterable[tuple[TranscriptSource, TranscriptEntry]]) -> Iterator[str]:
    """Yield a heading whenever the file changes, then each entry's line."""
    current: TranscriptSource | None = None
    for source, entry in stream:
        if source is not current:
            current = source
            yield _heading(source)
        yield _line(entry)


@run_app.command("transcript")
def run_transcript_cmd(
    ctx: typer.Context,
    key: Annotated[str, typer.Argument(help="A Run key or URN, or a Task key for all its Runs.")],
    follow: Annotated[
        bool,
        typer.Option("--follow", help="Keep printing appended entries until the Runs end."),
    ] = False,
    scrub: Annotated[
        bool,
        typer.Option("--scrub", help="Rewrite machine paths and token-shaped strings."),
    ] = False,
) -> None:
    """Print a Run's or a Task's transcripts in full; no record moves."""
    from eawf.observability.logging.scrub import SensitiveScrubber
    from eawf.runtime.runtimes.run_transcript import (
        TranscriptReader,
        follow_transcripts,
        runs_ended,
        select_transcripts,
    )

    flags: GlobalFlags = ctx.obj
    if follow and flags.json_output:
        cli_errors.emit_error(
            cli_errors.UserError("--follow prints text; drop --json", kind="InvalidInput"),
            flags=flags,
        )
    try:
        tree_root = resolve_state_path(flags.workspace).parent
        selection = select_transcripts(tree_root, key)
    except (FileNotFoundError, LookupError) as exc:
        cli_errors.emit_error(cli_errors.UserError(str(exc), kind="NotFound"), flags=flags)
    readers = [TranscriptReader(source) for source in selection.sources]

    def keep_following() -> bool:
        if not follow:
            return False
        wait_between_reads()
        return not runs_ended(tree_root, selection.run_keys)

    clean = SensitiveScrubber().scrub if scrub else str
    stream = follow_transcripts(readers, keep_following)
    if flags.json_output:
        entries: list[dict[str, Any]] = [
            {
                "runtime": source.runtime,
                "run_key": source.run_key,
                "path": clean(str(source.path)),
                "subagent": source.subagent,
                "kind": entry.kind,
                "tool_name": entry.tool_name,
                "is_error": entry.is_error,
                "text": clean(entry.text),
            }
            for source, entry in stream
        ]
        emit_json_or_text(
            {"run_keys": list(selection.run_keys), "entries": entries}, "", flags=flags
        )
        return
    try:
        for line in _text_lines(stream):
            typer.echo(clean(line))
    except KeyboardInterrupt:
        return


__all__ = ["FOLLOW_POLL_SECONDS", "run_transcript_cmd", "wait_between_reads"]
