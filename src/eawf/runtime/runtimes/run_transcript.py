"""Read the whole transcript of a Run, or of every Run of a Task, from the runtime's files.

The machine-local transcript index ties each Run to the file its Claude Code or Codex
session wrote, and each subagent's file to the session that spawned it; a host session
bound to a Task is gathered with that Task's Runs. This module
resolves a Run or Task key to those files and reads each one as a stream of typed
entries: what the user and the assistant said, and each tool call with its whole input
and output. Nothing is cut, because the reader is the operator on their own machine;
hidden reasoning is still left out, as it is everywhere else a transcript is read.

A file is read as an append-only log. A reader remembers how far it has read, so a
followed transcript prints only what was appended since the last pass, and a line the
runtime has not finished writing is left for the next pass instead of being read as
corrupt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

from eawf.observability.reflect.runs import read_run_briefs
from eawf.runtime.runtimes.host_transcript import HostHarness
from eawf.runtime.session.task_binding import session_task_keys
from eawf.runtime.session.transcript_index import TranscriptIndexRow, read_transcript_index

logger = logging.getLogger(__name__)

#: What one transcript entry is: a message, a tool call or result, or a reader's notice.
EntryKind = Literal["user", "assistant", "tool_call", "tool_result", "notice"]

#: The roles whose messages a transcript prints.
_ROLES: Final = frozenset({"user", "assistant"})

#: The Codex rollout content blocks that carry words.
_CODEX_TEXT_BLOCKS: Final = frozenset({"input_text", "output_text"})

#: What a Codex local shell call is named, since its item carries no tool name.
_CODEX_SHELL_TOOL: Final = "local_shell"


@dataclass(frozen=True, slots=True)
class TranscriptEntry:
    """One entry of a transcript, in full.

    Attributes:
        kind: What the entry is.
        text: The words, the tool's input or the tool's output, uncut.
        tool_name: The tool a call or result belongs to; ``None`` for a message or
            for a result whose call the file never stated.
        is_error: Whether a tool result reports a failure.
    """

    kind: EntryKind
    text: str
    tool_name: str | None = None
    is_error: bool = False


@dataclass(frozen=True, slots=True)
class TranscriptSource:
    """One transcript file a key resolved to.

    Attributes:
        runtime: The harness that wrote the file.
        run_key: The Run the session served; ``None`` for a subagent no Run adopted.
        path: The file on this machine.
        subagent: Whether a session spawned this one.
    """

    runtime: HostHarness
    run_key: str | None
    path: Path
    subagent: bool


@dataclass(frozen=True, slots=True)
class TranscriptSelection:
    """The Runs a key names and the transcript files indexed for them.

    Attributes:
        run_keys: The Runs the key resolved to, in key order.
        sources: Their transcript files, subagents after the session that spawned
            them, in index order.
    """

    run_keys: tuple[str, ...]
    sources: tuple[TranscriptSource, ...]


def _key_of(ref: str) -> str:
    """Return the key a URN or bare key ends in."""
    return ref.rsplit("/", 1)[-1]


def _linked_rows(
    rows: tuple[TranscriptIndexRow, ...], run_keys: frozenset[str], hosts: frozenset[str]
) -> list[TranscriptIndexRow]:
    """Return the rows of *run_keys* and of the host sessions *hosts*, then their subagents."""
    linked = [
        row
        for row in rows
        if (row.run_ref is not None and _key_of(row.run_ref) in run_keys)
        or (row.parent_session_digest is None and row.vendor_session_digest in hosts)
    ]
    sessions = {row.vendor_session_digest for row in linked}
    while spawned := [
        row
        for row in rows
        if row.parent_session_digest in sessions and row.vendor_session_digest not in sessions
    ]:
        linked.extend(spawned)
        sessions.update(row.vendor_session_digest for row in spawned)
    return linked


def select_transcripts(tree_root: Path, key: str) -> TranscriptSelection:
    """Return the transcript files a Run key, or a Task key's Runs and sessions, are indexed with.

    Args:
        tree_root: The tree's ``.ea`` directory.
        key: A Run key or URN, or a Task key whose Runs and bound host sessions
            are gathered.

    Returns:
        The Runs and their files; a file indexed twice is named once.

    Raises:
        LookupError: The tree holds no Run under *key* and neither a Run of nor
            a host session bound to a Task keyed *key*, or no transcript is
            indexed for what it names.
        pydantic.ValidationError: A Run row, an index line or a binding line
            does not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    wanted = _key_of(key)
    runs = read_run_briefs(tree_root)
    chosen = [run for run in runs if run.key == wanted] or [
        run for run in runs if run.task_key == wanted
    ]
    hosts = frozenset(
        digest for digest, task_key in session_task_keys(tree_root).items() if task_key == wanted
    )
    if not chosen and not hosts:
        raise LookupError(
            f"the tree holds no Run keyed {wanted!r} and no Run of, or session bound to, "
            "a Task so keyed"
        )
    run_keys = frozenset(run.key for run in chosen)
    seen: set[tuple[str, str]] = set()
    sources: list[TranscriptSource] = []
    for row in _linked_rows(read_transcript_index(tree_root), run_keys, hosts):
        if (row.runtime, row.transcript_path) in seen:
            continue
        seen.add((row.runtime, row.transcript_path))
        sources.append(
            TranscriptSource(
                runtime=row.runtime,
                run_key=None if row.run_ref is None else _key_of(row.run_ref),
                path=Path(row.transcript_path),
                subagent=row.parent_session_digest is not None,
            )
        )
    if not sources:
        raise LookupError(f"no transcript is indexed for {wanted}")
    logger.info(f"select_transcripts key={wanted} runs={len(run_keys)} files={len(sources)}")
    return TranscriptSelection(run_keys=tuple(sorted(run_keys)), sources=tuple(sources))


def runs_ended(tree_root: Path, run_keys: Iterable[str]) -> bool:
    """Return whether every Run of *run_keys* has stopped.

    Args:
        tree_root: The tree's ``.ea`` directory.
        run_keys: The Runs a followed transcript belongs to.

    Returns:
        ``True`` when each of them carries its end stamp.

    Raises:
        pydantic.ValidationError: A Run row does not validate.
        LedgerTornTailError: The run ledger ends mid-line.
    """
    keys = frozenset(run_keys)
    return all(run.ended_at is not None for run in read_run_briefs(tree_root) if run.key in keys)


def _dumped(value: object) -> str:
    """Return *value* as the text an entry prints: a string as-is, anything else as JSON."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def _claude_result_text(content: object) -> str:
    """Return a Claude tool result's output: its text blocks joined, or its JSON."""
    if isinstance(content, list) and all(
        isinstance(block, dict) and block.get("type") == "text" for block in content
    ):
        return "\n".join(str(block.get("text", "")) for block in content)
    return _dumped(content)


class _Parser:
    """Read one runtime's records as entries, remembering each call's tool name."""

    def __init__(self, runtime: HostHarness) -> None:
        self._runtime = runtime
        self._calls: dict[str, str] = {}

    def entries(self, record: Mapping[str, Any]) -> Iterator[TranscriptEntry]:
        if self._runtime == "claude-code":
            return self._claude(record)
        return self._codex(record)

    def _claude(self, record: Mapping[str, Any]) -> Iterator[TranscriptEntry]:
        role = record.get("type")
        message = record.get("message")
        if role not in _ROLES or not isinstance(message, dict):
            return
        content = message.get("content")
        if isinstance(content, str):
            yield TranscriptEntry(kind=role, text=content)
            return
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict):
                yield from self._claude_block(role, block)

    def _claude_block(self, role: EntryKind, block: Mapping[str, Any]) -> Iterator[TranscriptEntry]:
        kind = block.get("type")
        if kind == "text":
            yield TranscriptEntry(kind=role, text=str(block.get("text", "")))
        elif kind == "tool_use":
            name = str(block.get("name", ""))
            self._calls[str(block.get("id"))] = name
            yield TranscriptEntry(
                kind="tool_call", text=_dumped(block.get("input")), tool_name=name
            )
        elif kind == "tool_result":
            yield TranscriptEntry(
                kind="tool_result",
                text=_claude_result_text(block.get("content")),
                tool_name=self._calls.get(str(block.get("tool_use_id"))),
                is_error=block.get("is_error") is True,
            )

    def _codex(self, record: Mapping[str, Any]) -> Iterator[TranscriptEntry]:
        payload = record.get("payload")
        if record.get("type") != "response_item" or not isinstance(payload, dict):
            return
        kind = payload.get("type")
        call_id = str(payload.get("call_id"))
        if kind == "message" and payload.get("role") in _ROLES:
            blocks = payload.get("content")
            texts = [
                str(block.get("text", ""))
                for block in (blocks if isinstance(blocks, list) else ())
                if isinstance(block, dict) and block.get("type") in _CODEX_TEXT_BLOCKS
            ]
            yield TranscriptEntry(kind=payload["role"], text="\n".join(texts))
        elif kind in {"function_call", "custom_tool_call", "local_shell_call"}:
            name = str(payload.get("name", _CODEX_SHELL_TOOL))
            self._calls[call_id] = name
            given = payload.get("arguments", payload.get("input", payload.get("action")))
            yield TranscriptEntry(kind="tool_call", text=_dumped(given), tool_name=name)
        elif kind in {"function_call_output", "custom_tool_call_output"}:
            output = payload.get("output")
            if isinstance(output, dict) and isinstance(output.get("content"), str):
                output = output["content"]
            yield TranscriptEntry(
                kind="tool_result", text=_dumped(output), tool_name=self._calls.get(call_id)
            )


class TranscriptReader:
    """Read one transcript file as it grows, from where the last read stopped."""

    def __init__(self, source: TranscriptSource) -> None:
        """Start before the first byte of *source*'s file."""
        self.source = source
        self._parser = _Parser(source.runtime)
        self._offset = 0
        self._line = 0
        # Set once the file was read or its absence reported, so a file is
        # reported missing once and only if it never appeared.
        self._seen = False

    def read(self, *, final: bool) -> tuple[TranscriptEntry, ...]:
        """Return the entries appended since the last read.

        Args:
            final: Whether no more will be appended. A last line without its
                newline is read only then, and a file still missing is reported.

        Returns:
            The new entries, with a notice for each line that is not a JSON
            object and, on the final read, for a file that never appeared.
        """
        try:
            with self.source.path.open("rb") as handle:
                handle.seek(self._offset)
                chunk = handle.read()
        except FileNotFoundError:
            if final and not self._seen:
                self._seen = True
                return (TranscriptEntry(kind="notice", text="the transcript file is missing"),)
            return ()
        self._seen = True
        lines = chunk.split(b"\n")
        tail = lines.pop()
        if final and tail.strip():
            lines.append(tail)
            tail = b""
        self._offset += len(chunk) - len(tail)
        return tuple(entry for raw in lines for entry in self._entries(raw))

    def _entries(self, raw: bytes) -> Iterator[TranscriptEntry]:
        self._line += 1
        if not raw.strip():
            return
        try:
            record = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            record = None
        if not isinstance(record, dict):
            yield TranscriptEntry(
                kind="notice", text=f"line {self._line} is not a JSON object and is skipped"
            )
            return
        yield from self._parser.entries(record)


def follow_transcripts(
    readers: Iterable[TranscriptReader], keep_following: Callable[[], bool]
) -> Iterator[tuple[TranscriptSource, TranscriptEntry]]:
    """Yield every entry of *readers*, then what is appended, until told to stop.

    Each pass reads every file from where it stopped. Between passes
    *keep_following* is asked whether to go on; it is where the caller waits. Once
    it answers ``False`` one final pass reads what was written meanwhile.

    Args:
        readers: One reader per transcript file.
        keep_following: Waits, then answers whether another pass is wanted.

    Yields:
        Each entry with the file it was read from, in file then append order.
    """
    held = tuple(readers)
    final = False
    while True:
        for reader in held:
            for entry in reader.read(final=final):
                yield reader.source, entry
        if final:
            return
        final = not keep_following()


__all__ = [
    "EntryKind",
    "TranscriptEntry",
    "TranscriptReader",
    "TranscriptSelection",
    "TranscriptSource",
    "follow_transcripts",
    "runs_ended",
    "select_transcripts",
]
