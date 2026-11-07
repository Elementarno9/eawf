"""Session-history reader: local Claude and Codex transcripts to observed rows.

# noqa: EAWF010 the one history reader, by contract; split seam: the session detail view

This module is the one reader of the runtimes' own local session history:
Claude Code project transcripts and Codex rollouts. Every other consumer,
the reflect pages included, reads history through it.

A transcript folds line by line into a :class:`TranscriptFold`. The fold is a
model, so the sweep stores it in its per-file index and resumes it when the
file grows: only the appended bytes are decoded. Each billed request becomes
an :class:`~eawf.observability.telemetry.models.ObservedRequest`; the session
row, an :class:`~eawf.observability.telemetry.models.ObservedSession`, sums
its requests, and per-kind block durations become
:class:`~eawf.observability.telemetry.models.ObservedDuration` rows. Codex
lines fold through :func:`~eawf.observability.telemetry.sources.codex_session.fold_codex_record`.

Only statistics and closed-vocabulary labels leave a transcript: session and
request ids are hashed, the working directory is kept as a digest, no path,
command string or message text is kept, and every label outside
:data:`~eawf.observability.telemetry.models.DURATION_LABELS` becomes
``other``. :func:`read_session_detail` returns one session's steps, tool
spans, content blocks and compaction boundaries for the session pages, as
sizes and kinds, never text.

The projector drives this reader and owns the index and the coverage funnel;
this module only locates, parses and classifies.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import IO, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.observability.telemetry.models import (
    DURATION_LABELS,
    OTHER_LABEL,
    DurationKind,
    ObservedDuration,
    ObservedRequest,
    ObservedSession,
    PriceSourceKind,
)
from eawf.observability.telemetry.pricing import resolve_price_source
from eawf.observability.telemetry.sources.codex_session import CodexFold, fold_codex_record
from eawf.runtime.runtimes.metering import price_token_counts
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

__all__ = [
    "MAIN_LANE",
    "REFLECTION_MARKER",
    "BlockKind",
    "CompactionBoundary",
    "ContentBlock",
    "DurationTally",
    "HistoryFile",
    "HistoryRuntime",
    "ParsedTranscript",
    "RequestLedger",
    "SessionDetail",
    "ToolSpan",
    "TranscriptFold",
    "classify_label",
    "claude_history_root",
    "claude_projects_root",
    "codex_sessions_root",
    "discover_claude_project",
    "discover_history",
    "discover_history_files",
    "fold_history_lines",
    "history_lines",
    "history_path_digest",
    "observed_rows",
    "project_history_lines",
    "read_session_detail",
    "working_dir_digest",
]

HistoryRuntime = Literal["claude", "codex"]
"""The runtimes whose local session history this module reads."""

_PROJECTS_DIR_ENV = "EAWF_CLAUDE_PROJECTS_DIR"
REFLECTION_MARKER: Final = "[eawf-reflect-title]"
"""The marker a reflect title helper session's prompt opens with.

The title fill spawns a print-mode session per uncached digest; when the
runtime keeps its transcript, the sweep finds it here and drops it, so a
helper never counts as work or gets a title of its own.
"""

_SHELL_TOOL = "Bash"
_DELEGATION_TOOLS = frozenset({"Task", "Agent"})
_MCP_PREFIX = "mcp__"
_CODEX_SHELL_TOOLS = frozenset({"shell", "exec_command", "local_shell", "container.exec"})
_CODEX_DELEGATION_TOOLS = frozenset({"spawn_agent"})
_CODEX_CALLS = frozenset({"function_call", "custom_tool_call", "local_shell_call"})
_CODEX_OUTPUTS = frozenset({"function_call_output", "custom_tool_call_output"})
_DIGEST_HEX: Final = 32
# A forked Codex thread opens with its parent's usage rows copied in and
# re-stamped at the instant of the fork, so the copy is a leading run of
# readings closer together than any thread completes turns. Genuine turns are
# seconds apart and an unforked rollout opens with at most a handful of close
# readings, so twenty readings under half a second apart is a copy.
_REPLAY_MAX_GAP_SECONDS: Final = 0.5
_REPLAY_MIN_ROWS: Final = 20
_VERSION_PATTERN: Final = r"^\d{1,4}\.\d{1,4}\.\d{1,6}$"
_VERSION_RE: Final = re.compile(_VERSION_PATTERN)


# ---- where history lives -------------------------------------------------------------


def claude_projects_root() -> Path:
    """Return the directory Claude Code keeps one transcript directory per project in.

    Honours ``EAWF_CLAUDE_PROJECTS_DIR`` (tests redirect it away from the real
    home directory), then ``CLAUDE_CONFIG_DIR``, then ``~/.claude``.

    Returns:
        The projects directory; it need not exist.
    """
    override = os.environ.get(_PROJECTS_DIR_ENV)
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    return (Path(config_dir) if config_dir else Path.home() / ".claude") / "projects"


def claude_history_root(project_root: Path) -> Path:
    """Return the Claude Code transcript directory for *project_root*.

    Claude Code names each project directory after its working directory with
    every non-alphanumeric character replaced by ``-``.

    Args:
        project_root: The repository root the sessions ran in.

    Returns:
        The history directory; it need not exist.
    """
    return claude_projects_root() / re.sub(r"[^A-Za-z0-9]", "-", str(project_root))


def codex_sessions_root() -> Path:
    """Return the directory Codex keeps its date-sharded rollouts in.

    Returns:
        ``$CODEX_HOME/sessions``, or ``~/.codex/sessions``; it need not exist.
    """
    configured = os.environ.get("CODEX_HOME")
    return (Path(configured) if configured else Path.home() / ".codex") / "sessions"


def discover_history(root: Path) -> list[Path]:
    """Return the top-level session transcripts under one Claude project directory.

    Args:
        root: One project's history directory.

    Returns:
        Sorted ``*.jsonl`` files; empty when *root* is missing.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.jsonl") if p.is_file())


MAIN_LANE: Final = "main"
"""The lane of a session's own top-level transcript."""


@dataclass(frozen=True, slots=True)
class HistoryFile:
    """One history file and the runtime whose format it is in.

    Attributes:
        runtime: The runtime that wrote the file.
        path: The file. It stays in memory; no row stores it.
        subagent: Whether the file is a Claude subagent transcript, kept
            under ``<session>/subagents/`` beside its parent session's file.
    """

    runtime: HistoryRuntime
    path: Path
    subagent: bool = False

    @property
    def fallback_session_id(self) -> str:
        """Return the session id to use when no record names one.

        A subagent transcript belongs to the session its directory is named
        after, so its requests bill that session, never one of their own.
        """
        return self.path.parent.parent.name if self.subagent else self.path.stem

    @property
    def lane(self) -> str:
        """Return the lane the file's requests are attributed to.

        ``main`` for a session's own transcript; a subagent's lane is a
        digest of its file name, so no raw agent id is stored.
        """
        return _digest("agent", self.path.stem) if self.subagent else MAIN_LANE


def discover_claude_project(root: Path) -> list[HistoryFile]:
    """Return one Claude project directory's session transcripts, then its subagent ones.

    Each subagent transcript lies under ``<session>/subagents/agent-*.jsonl``
    and bills its parent session, so the parent's own files come first.

    Args:
        root: One project's history directory.

    Returns:
        The files in a stable order; empty when *root* is missing.
    """
    if not root.is_dir():
        return []
    subagents = sorted(p for p in root.glob("*/subagents/agent-*.jsonl") if p.is_file())
    return [HistoryFile("claude", path) for path in discover_history(root)] + [
        HistoryFile("claude", path, subagent=True) for path in subagents
    ]


def discover_history_files(*, claude_projects: Path, codex_sessions: Path) -> list[HistoryFile]:
    """Return every Claude transcript of every project, then every Codex rollout.

    Args:
        claude_projects: The Claude projects directory, see
            :func:`claude_projects_root`.
        codex_sessions: The Codex sessions directory, see
            :func:`codex_sessions_root`.

    Returns:
        The files in a stable order; a missing directory contributes none.
    """
    files: list[HistoryFile] = []
    if claude_projects.is_dir():
        for project in sorted(p for p in claude_projects.iterdir() if p.is_dir()):
            files.extend(discover_claude_project(project))
    if codex_sessions.is_dir():
        rollouts = sorted(p for p in codex_sessions.rglob("rollout-*.jsonl") if p.is_file())
        files.extend(HistoryFile("codex", path) for path in rollouts)
    return files


# ---- digests ---------------------------------------------------------------------------


def _digest(prefix: str, value: str) -> str:
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:_DIGEST_HEX]}"


def working_dir_digest(cwd: str | Path) -> str:
    """Return the digest a session row carries for its working directory.

    A scope computes the digest of its own project root and compares, so a
    row never needs the path itself.

    Args:
        cwd: An absolute working directory.

    Returns:
        ``cwd-`` and 32 hex characters of the SHA-256 of the normalised path.

    Raises:
        ValueError: When *cwd* is empty.
    """
    text = str(cwd)
    if not text:
        raise ValueError("an empty working directory names no project")
    return _digest("cwd", os.path.normpath(text))


def history_path_digest(path: Path) -> str:
    """Return the digest the sweep index keys a history file by.

    Args:
        path: The history file.

    Returns:
        ``src-`` and 32 hex characters of the SHA-256 of the path.
    """
    return _digest("src", str(path))


def _request_ref(key: str) -> str:
    return _digest("req", key)


# ---- reading lines ---------------------------------------------------------------------


def _complete_lines(handle: IO[bytes]) -> Iterator[bytes]:
    """Yield each newline-terminated line from the handle's position.

    A trailing line with no newline is still being written, so it is left
    unread for a later sweep to see complete.
    """
    for raw in handle:
        if not raw.endswith(b"\n"):
            return
        yield raw


def _decode(raw: bytes) -> dict[str, Any] | None:
    """Return the JSON object one line holds, or ``None`` for any other line."""
    try:
        record = json.loads(raw)
    except json.JSONDecodeError, UnicodeDecodeError:
        return None
    return record if isinstance(record, dict) else None


def history_lines(path: Path) -> Iterator[str]:
    """Yield the complete text lines of one history file.

    Reading is fail-open: an unreadable file yields nothing more and a line
    that is not UTF-8 is skipped, because the history is the runtime's file
    and an observation, not a record.

    Args:
        path: One session history file.

    Yields:
        Each complete line, newline stripped.
    """
    try:
        with path.open("rb") as handle:
            for raw in _complete_lines(handle):
                try:
                    yield raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    continue
    except OSError as exc:
        logger.warning(f"history_lines file={path.name!r} err={type(exc).__name__}")


def project_history_lines(project_root: Path, *, modified_since: datetime | None) -> Iterator[str]:
    """Yield the lines of every top-level Claude transcript of one project.

    Args:
        project_root: The repository root; its path names the history
            directory.
        modified_since: Skip a file last modified before this, or ``None``
            to read every file.

    Yields:
        Each complete line of each file, file by file.
    """
    floor = None if modified_since is None else modified_since.timestamp()
    for path in discover_history(claude_history_root(project_root)):
        try:
            if floor is not None and path.stat().st_mtime < floor:
                continue
        except OSError:
            continue
        yield from history_lines(path)


# ---- folding ---------------------------------------------------------------------------


def classify_label(kind: DurationKind, name: object) -> str:
    """Map a raw block name onto *kind*'s closed label vocabulary.

    Args:
        kind: The block kind being labelled.
        name: The raw tool name, command head token or subagent type.

    Returns:
        *name* when it is in the vocabulary (MCP tools collapse to ``mcp``),
        else ``other``.
    """
    if not isinstance(name, str):
        return OTHER_LABEL
    if kind is DurationKind.TOOL_CALL and name.startswith(_MCP_PREFIX):
        return "mcp"
    return name if name in DURATION_LABELS[kind] else OTHER_LABEL


def _command_family(command: object) -> str:
    """Return the closed-vocabulary family of a shell command string or argv."""
    if isinstance(command, list) and command and isinstance(command[0], str):
        head = command[0]
        if head in {"bash", "sh", "zsh"} and len(command) >= 3 and isinstance(command[2], str):
            return _command_family(command[2])
        return classify_label(DurationKind.COMMAND, Path(head).name)
    if not isinstance(command, str) or not command.strip():
        return OTHER_LABEL
    return classify_label(DurationKind.COMMAND, command.split(maxsplit=1)[0])


def _claude_labels(name: object, tool_input: object) -> tuple[tuple[DurationKind, str], ...]:
    """Return the duration labels one Claude tool call is measured under."""
    labels = [(DurationKind.TOOL_CALL, classify_label(DurationKind.TOOL_CALL, name))]
    if isinstance(tool_input, dict):
        if name == _SHELL_TOOL:
            labels.append((DurationKind.COMMAND, _command_family(tool_input.get("command"))))
        elif name in _DELEGATION_TOOLS:
            purpose = classify_label(DurationKind.SUBAGENT, tool_input.get("subagent_type"))
            labels.append((DurationKind.SUBAGENT, purpose))
    elif name == _SHELL_TOOL:
        labels.append((DurationKind.COMMAND, OTHER_LABEL))
    elif name in _DELEGATION_TOOLS:
        labels.append((DurationKind.SUBAGENT, OTHER_LABEL))
    return tuple(labels)


def _non_negative_int(raw: object) -> int:
    """Return *raw* when it is a non-negative JSON integer, else ``0``."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _timestamp(record: Mapping[str, Any]) -> datetime | None:
    """Return the record's ISO-8601 timestamp, or ``None``.

    A timestamp without an offset is read as UTC, so every instant returned
    compares with every other one.
    """
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _nonempty(raw: object) -> str | None:
    return raw if isinstance(raw, str) and raw else None


class DurationTally(BaseModel):
    """The measured durations of one block kind and label, summed as they arrive.

    Attributes:
        kind: The block kind.
        label: Closed-vocabulary label within *kind*.
        count: Completed blocks measured.
        total_ms: Summed duration.
        max_ms: Longest single block.
    """

    model_config = ConfigDict(extra="forbid")

    kind: DurationKind
    label: str
    count: int = 0
    total_ms: int = 0
    max_ms: int = 0


class OpenTool(BaseModel):
    """One Claude tool call still awaiting its result."""

    model_config = ConfigDict(extra="forbid")

    labels: tuple[tuple[DurationKind, str], ...]
    started: datetime


@dataclass(slots=True)
class RequestLedger:
    """The request rows of one session and which of them changed since loading.

    Attributes:
        rows: Request rows by ``request_ref``.
        changed: Refs of the rows added or replaced since the ledger loaded.
        foreign: Refs another file of the same session already billed; a
            request among them is skipped, never billed twice.
    """

    rows: dict[str, ObservedRequest] = field(default_factory=dict)
    changed: set[str] = field(default_factory=set)
    foreign: frozenset[str] = frozenset()

    def put(self, row: ObservedRequest) -> None:
        """Add or replace *row* and mark it changed."""
        self.rows[row.request_ref] = row
        self.changed.add(row.request_ref)


def _priced(
    model: str | None,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_5m_tokens: int,
    cache_write_1h_tokens: int,
) -> tuple[Decimal | None, PriceSourceKind, str | None]:
    """Price one request's token classes against the embedded rate table.

    An unpriced request records a null cost, never a zero.
    """
    if model is None:
        return None, PriceSourceKind.UNPRICED, None
    source, version = resolve_price_source(model)
    if source is PriceSourceKind.UNPRICED:
        return None, source, None
    cost = price_token_counts(
        model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_creation_5m_input_tokens=cache_write_5m_tokens,
        cache_creation_1h_input_tokens=cache_write_1h_tokens,
        cache_read_input_tokens=cache_read_tokens,
    )
    if cost is None:
        return None, PriceSourceKind.UNPRICED, None
    return cost, source, version


class PendingCodexRequest(BaseModel):
    """One Codex reading held while the rollout's leading run is undecided."""

    model_config = ConfigDict(extra="forbid")

    ts: datetime | None
    model: str | None
    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int


class TranscriptFold(BaseModel):
    """The resumable fold of one history file.

    Everything a later append needs is here, except the request rows, which
    the sweep keeps in its store and hands back as a :class:`RequestLedger`.
    The index stores it, so it holds digests where a record holds an id or a
    path: see :meth:`index_json`.

    Attributes:
        runtime: The runtime whose format the file is in.
        lane: The lane the file's requests are attributed to, see
            :attr:`HistoryFile.lane`.
        vendor_session_ref: Hashed vendor session id, or ``None`` before a
            record named one.
        cwd_digest: Digest of the first working directory a record named.
        model: First model a request billed against.
        started_at: Earliest record timestamp.
        ended_at: Latest record timestamp.
        lines: Non-blank lines read.
        records: Lines that decoded to a JSON object.
        skipped_lines: Lines that did not.
        open_tools: Claude tool calls awaiting their result, by digest of
            the call id.
        tallies: Measured durations by ``kind/label``.
        codex: The Codex rollout fold, on a Codex file.
        replay_open: Whether a Codex rollout's leading run of readings is
            still undecided.
        replay_pending: The readings of that run, held until it is decided.
        replayed_requests: Readings dropped as a parent's copied history.
        prompted: Whether the opening prompt has been read.
        reflection_helper: Whether that prompt opens with
            :data:`REFLECTION_MARKER`; such a file bills nothing.
        shared_refs: Requests skipped because another file of the session
            billed them first.
    """

    model_config = ConfigDict(extra="forbid")

    runtime: HistoryRuntime
    lane: str = MAIN_LANE
    vendor_session_ref: str | None = None
    cwd_digest: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    lines: int = 0
    records: int = 0
    skipped_lines: int = 0
    open_tools: dict[str, OpenTool] = Field(default_factory=dict)
    tallies: dict[str, DurationTally] = Field(default_factory=dict)
    codex: CodexFold | None = None
    replay_open: bool = True
    replay_pending: list[PendingCodexRequest] = Field(default_factory=list)
    replayed_requests: int = 0
    shared_refs: set[str] = Field(default_factory=set)
    prompted: bool = False
    reflection_helper: bool = False

    def index_json(self) -> str:
        """Return the fold as the JSON the index stores and an append resumes from.

        The Codex rollout fold keeps the raw session id, working directory and
        branch for the whole-file adapter; resuming needs none of them, since
        this fold already holds their digests, so they are left out.
        """
        return self.model_dump_json(exclude={"codex": {"session_id", "cwd", "git_branch_first"}})

    def _name_session(self, session_id: str) -> None:
        if self.vendor_session_ref is None:
            self.vendor_session_ref = hash_vendor_session_id(session_id)

    def fold_line(
        self, raw: bytes, ledger: RequestLedger, *, fallback_session_id: str
    ) -> dict[str, Any] | None:
        """Fold one complete line.

        Args:
            raw: The line's bytes.
            ledger: The session's request rows, extended in place.
            fallback_session_id: Session id to use when no record names one.

        Returns:
            The decoded record, or ``None`` for a blank or malformed line.
        """
        if not raw.strip():
            return None
        self.lines += 1
        record = _decode(raw)
        if record is None:
            self.skipped_lines += 1
            return None
        self.records += 1
        if self.runtime == "claude":
            self._fold_claude(record, ledger, fallback_session_id)
        else:
            self._fold_codex(record, ledger, fallback_session_id)
        return record

    def _widen(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.started_at is None or ts < self.started_at:
            self.started_at = ts
        if self.ended_at is None or ts > self.ended_at:
            self.ended_at = ts

    def _fold_claude(
        self, record: dict[str, Any], ledger: RequestLedger, fallback_session_id: str
    ) -> None:
        self._name_session(_nonempty(record.get("sessionId")) or fallback_session_id)
        if self.cwd_digest is None and (cwd := _nonempty(record.get("cwd"))):
            self.cwd_digest = working_dir_digest(cwd)
        ts = _timestamp(record)
        self._widen(ts)
        message = record.get("message")
        if not isinstance(message, dict):
            return
        if not self.prompted and record.get("type") == "user" and not record.get("isMeta"):
            self._read_prompt(message.get("content"))
        if record.get("type") == "assistant" and not self.reflection_helper:
            self._bill_claude(record, message, ts, ledger)
        content = message.get("content")
        if ts is None or not isinstance(content, list):
            return
        if record.get("type") == "assistant":
            self._open_tools(content, ts)
        elif record.get("type") == "user":
            self._close_tools(content, ts)

    def _read_prompt(self, content: object) -> None:
        """Read the opening prompt, a string or text blocks; a tool result is none."""
        if isinstance(content, list):
            texts = [
                b.get("text") for b in content if isinstance(b, dict) and b.get("type") == "text"
            ]
            content = texts[0] if texts else None
        if isinstance(content, str):
            self.prompted = True
            self.reflection_helper = content.startswith(REFLECTION_MARKER)

    def _bill_claude(
        self,
        record: dict[str, Any],
        message: dict[str, Any],
        ts: datetime | None,
        ledger: RequestLedger,
    ) -> None:
        # One message is written once per content block, each row repeating
        # the input and cache counters and the output as it stood then; the
        # row with the largest output is the completed reading.
        usage = message.get("usage")
        ref_text = self.vendor_session_ref
        if not isinstance(usage, dict) or ref_text is None:
            return
        key = (
            _nonempty(message.get("id"))
            or _nonempty(record.get("requestId"))
            or _nonempty(record.get("uuid"))
            or f"row:{self.lines}"
        )
        request_ref = _request_ref(key)
        if request_ref in ledger.foreign:
            self.shared_refs.add(request_ref)
            return
        output = _non_negative_int(usage.get("output_tokens"))
        held = ledger.rows.get(request_ref)
        if held is not None and output <= held.output_tokens:
            return
        model = _nonempty(message.get("model"))
        if self.model is None and model is not None:
            self.model = model
        write_total = _non_negative_int(usage.get("cache_creation_input_tokens"))
        split = usage.get("cache_creation")
        one_hour = (
            _non_negative_int(split.get("ephemeral_1h_input_tokens"))
            if isinstance(split, dict)
            else 0
        )
        one_hour = min(one_hour, write_total)
        input_tokens = _non_negative_int(usage.get("input_tokens"))
        cache_read = _non_negative_int(usage.get("cache_read_input_tokens"))
        cost, source, version = _priced(
            model,
            input_tokens=input_tokens,
            output_tokens=output,
            cache_read_tokens=cache_read,
            cache_write_5m_tokens=write_total - one_hour,
            cache_write_1h_tokens=one_hour,
        )
        ledger.put(
            ObservedRequest(
                vendor_session_ref=ref_text,
                runtime="claude",
                lane=self.lane,
                request_ref=request_ref,
                ordinal=held.ordinal if held is not None else len(ledger.rows) + 1,
                ts=held.ts if held is not None else ts,
                model=model,
                input_tokens=input_tokens,
                output_tokens=output,
                cache_read_tokens=cache_read,
                cache_write_tokens=write_total,
                cache_write_1h_tokens=one_hour,
                reasoning_tokens=None,
                total_tokens=input_tokens + output + cache_read + write_total,
                cost_usd=cost,
                price_source=source,
                rate_table_version=version,
            )
        )

    def _open_tools(self, content: list[Any], ts: datetime) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            block_id = _nonempty(block.get("id"))
            if block_id is None:
                continue
            labels = _claude_labels(block.get("name"), block.get("input"))
            self.open_tools[_digest("tool", block_id)] = OpenTool(labels=labels, started=ts)

    def _close_tools(self, content: list[Any], ts: datetime) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            opened = self.open_tools.pop(_digest("tool", str(block.get("tool_use_id"))), None)
            if opened is None or ts < opened.started:
                continue
            elapsed_ms = int((ts - opened.started).total_seconds() * 1000)
            for kind, label in opened.labels:
                tally = self.tallies.setdefault(
                    f"{kind.value}/{label}", DurationTally(kind=kind, label=label)
                )
                tally.count += 1
                tally.total_ms += elapsed_ms
                tally.max_ms = max(tally.max_ms, elapsed_ms)

    def _fold_codex(
        self, record: dict[str, Any], ledger: RequestLedger, fallback_session_id: str
    ) -> None:
        if self.codex is None:
            self.codex = CodexFold()
        usage = fold_codex_record(self.codex, record)
        self._name_session(self.codex.session_id or fallback_session_id)
        if self.cwd_digest is None and self.codex.cwd:
            self.cwd_digest = working_dir_digest(self.codex.cwd)
        ts = _timestamp(record)
        self._widen(ts)
        if usage is None or self.vendor_session_ref is None:
            return
        reading = PendingCodexRequest(
            ts=ts,
            model=usage.model,
            input_tokens=usage.input_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            output_tokens=usage.output_tokens,
            reasoning_tokens=usage.reasoning_tokens,
        )
        if self.replay_open and ledger.rows and not self.replay_pending:
            # A fold indexed before the guard existed has already billed its
            # opening run; there is nothing left to decide.
            self.replay_open = False
        if self.replay_open and ts is not None:
            last = self.replay_pending[-1].ts if self.replay_pending else None
            if not self.replay_pending or (
                last is not None and (ts - last).total_seconds() < _REPLAY_MAX_GAP_SECONDS
            ):
                self.replay_pending.append(reading)
                return
        self.settle(ledger)
        self._bill_codex(reading, ledger)

    def settle(self, ledger: RequestLedger) -> None:
        """Decide a Codex rollout's leading run: drop it as copied history or bill it.

        Called when the run breaks and at the end of every read, so a short
        rollout is billed even when it never breaks its opening run.

        Args:
            ledger: The session's request rows, extended in place.
        """
        if not self.replay_open:
            return
        self.replay_open = False
        pending, self.replay_pending = self.replay_pending, []
        if len(pending) >= _REPLAY_MIN_ROWS:
            self.replayed_requests += len(pending)
            return
        for reading in pending:
            self._bill_codex(reading, ledger)

    def _bill_codex(self, reading: PendingCodexRequest, ledger: RequestLedger) -> None:
        ref_text = self.vendor_session_ref
        if ref_text is None:
            return
        if self.model is None:
            self.model = reading.model
        ordinal = len(ledger.rows) + 1
        ledger.put(
            ObservedRequest(
                vendor_session_ref=ref_text,
                runtime="codex",
                lane=self.lane,
                request_ref=_request_ref(f"codex:{ordinal}"),
                ordinal=ordinal,
                ts=reading.ts,
                model=reading.model,
                input_tokens=reading.input_tokens,
                output_tokens=reading.output_tokens,
                cache_read_tokens=reading.cache_read_tokens,
                cache_write_tokens=0,
                reasoning_tokens=reading.reasoning_tokens,
                total_tokens=reading.input_tokens
                + reading.output_tokens
                + reading.cache_read_tokens,
                # Codex rows in the rate table are placeholders, not OpenAI's
                # published rates, so a request records its tokens and no cost.
                cost_usd=None,
                price_source=PriceSourceKind.UNPRICED,
            )
        )


def fold_history_lines(
    handle: IO[bytes],
    fold: TranscriptFold,
    ledger: RequestLedger,
    *,
    fallback_session_id: str,
    observe: Callable[[dict[str, Any], int], None] | None = None,
) -> tuple[int, int]:
    """Fold every complete line from the handle's position.

    Args:
        handle: A binary handle positioned where the fold left off.
        fold: The file's fold, advanced in place.
        ledger: The session's request rows, extended in place.
        fallback_session_id: Session id to use when no record names one.
        observe: Called with each decoded record and the request count after
            it, for a reader that needs more than the rows.

    Returns:
        ``(bytes consumed, lines decoded)``; the bytes stop before a trailing
        line with no newline.
    """
    consumed = 0
    decoded = 0
    for raw in _complete_lines(handle):
        consumed += len(raw)
        if raw.strip():
            decoded += 1
        record = fold.fold_line(raw, ledger, fallback_session_id=fallback_session_id)
        if record is not None and observe is not None:
            # A held opening reading is a request already made, so the blocks
            # after it count it even before the run is decided.
            observe(record, len(ledger.rows) + len(fold.replay_pending))
    fold.settle(ledger)
    return consumed, decoded


# ---- the session view ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ParsedTranscript:
    """The statistics one session's transcripts fold into.

    Attributes:
        runtime: The runtime whose format the files are in.
        vendor_session_ref: Hashed vendor session id, or ``None``.
        cwd_digest: Digest of the recorded working directory, or ``None``.
        model: First billed model id.
        started_at: Earliest record timestamp.
        ended_at: Latest record timestamp.
        requests: The billed requests, the session's own lane first, each
            lane in order.
        durations: Measured block durations per ``(kind, label)``.
        skipped_lines: Malformed lines skipped while parsing.
    """

    runtime: HistoryRuntime
    vendor_session_ref: str | None
    cwd_digest: str | None
    model: str | None
    started_at: datetime | None
    ended_at: datetime | None
    requests: tuple[ObservedRequest, ...]
    durations: dict[tuple[DurationKind, str], DurationTally]
    skipped_lines: int

    @classmethod
    def merged(
        cls, folds: Sequence[TranscriptFold], requests: Iterable[ObservedRequest]
    ) -> ParsedTranscript:
        """Return the view of one session's files and its request rows.

        A Claude session is its own transcript plus one per subagent; their
        durations add up, the span covers them all, and the working directory
        and model come from the session's own transcript when it has them.

        Args:
            folds: The folds of the session's files; at least one.
            requests: Every request row of the session, all lanes.

        Returns:
            The session view.

        Raises:
            ValueError: When *folds* is empty.
        """
        if not folds:
            raise ValueError("a session view needs at least one transcript fold")
        ordered = sorted(folds, key=lambda fold: fold.lane != MAIN_LANE)
        durations: dict[tuple[DurationKind, str], DurationTally] = {}
        for fold in ordered:
            for tally in fold.tallies.values():
                held = durations.get((tally.kind, tally.label))
                if held is None:
                    durations[(tally.kind, tally.label)] = tally.model_copy()
                    continue
                held.count += tally.count
                held.total_ms += tally.total_ms
                held.max_ms = max(held.max_ms, tally.max_ms)
        starts = [fold.started_at for fold in folds if fold.started_at is not None]
        ends = [fold.ended_at for fold in folds if fold.ended_at is not None]
        return cls(
            runtime=ordered[0].runtime,
            vendor_session_ref=next(
                (fold.vendor_session_ref for fold in ordered if fold.vendor_session_ref), None
            ),
            cwd_digest=next((fold.cwd_digest for fold in ordered if fold.cwd_digest), None),
            model=next((fold.model for fold in ordered if fold.model), None),
            started_at=min(starts, default=None),
            ended_at=max(ends, default=None),
            requests=tuple(
                sorted(requests, key=lambda row: (row.lane != MAIN_LANE, row.lane, row.ordinal))
            ),
            durations=durations,
            skipped_lines=sum(fold.skipped_lines for fold in folds),
        )

    @property
    def turn_count(self) -> int:
        """Return the number of billed requests."""
        return len(self.requests)

    @property
    def input_tokens(self) -> int:
        """Return non-cached input tokens across requests."""
        return sum(row.input_tokens for row in self.requests)

    @property
    def output_tokens(self) -> int:
        """Return output tokens across requests."""
        return sum(row.output_tokens for row in self.requests)

    @property
    def cache_read_tokens(self) -> int:
        """Return prompt-cache reads across requests."""
        return sum(row.cache_read_tokens for row in self.requests)

    @property
    def cache_write_tokens(self) -> int:
        """Return prompt-cache writes across both TTL tiers."""
        return sum(row.cache_write_tokens for row in self.requests)

    @property
    def cache_write_1h_tokens(self) -> int:
        """Return prompt-cache writes at the 1-hour rate."""
        return sum(row.cache_write_1h_tokens for row in self.requests)

    @property
    def cache_write_5m_tokens(self) -> int:
        """Return prompt-cache writes at the 5-minute rate."""
        return self.cache_write_tokens - self.cache_write_1h_tokens

    @property
    def reasoning_tokens(self) -> int | None:
        """Return reasoning tokens, or ``None`` when no request reported a counter."""
        counted = [
            row.reasoning_tokens for row in self.requests if row.reasoning_tokens is not None
        ]
        return sum(counted) if counted else None

    @property
    def has_operator_work(self) -> bool:
        """Return whether the session billed at least one request."""
        return bool(self.requests)


def observed_rows(
    parsed: ParsedTranscript, *, project_id: str
) -> tuple[ObservedSession, list[ObservedDuration]]:
    """Build the observed session row and its duration rows.

    The session's token classes are the sums of its requests. Its cost is
    their summed cost when every request that counted a token was priced;
    otherwise the session is unpriced, so a partial sum never reads as the
    whole.

    Args:
        parsed: A transcript with operator work.
        project_id: Scope label of the owning project.

    Returns:
        The session row and one duration row per measured ``(kind, label)``.

    Raises:
        ValueError: When *parsed* names no session.
        pydantic.ValidationError: When a row fails its invariants.
    """
    ref = parsed.vendor_session_ref
    if not ref:
        raise ValueError("an observed session needs a vendor session ref")
    cost, price_source, rate_version = _session_price(parsed.requests)
    duration_ms: int | None = None
    if parsed.started_at is not None and parsed.ended_at is not None:
        duration_ms = int((parsed.ended_at - parsed.started_at).total_seconds() * 1000)
    session = ObservedSession(
        vendor_session_ref=ref,
        runtime=parsed.runtime,
        project_id=project_id,
        cwd_digest=parsed.cwd_digest,
        model=parsed.model,
        started_at=parsed.started_at,
        ended_at=parsed.ended_at,
        duration_ms=duration_ms,
        turn_count=parsed.turn_count,
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
        cache_read_tokens=parsed.cache_read_tokens,
        cache_write_tokens=parsed.cache_write_tokens,
        reasoning_tokens=parsed.reasoning_tokens,
        total_tokens=sum(row.total_tokens for row in parsed.requests),
        cost_usd=cost,
        price_source=price_source,
        rate_table_version=rate_version,
    )
    durations = [
        ObservedDuration(
            vendor_session_ref=ref,
            runtime=parsed.runtime,
            kind=kind,
            label=label,
            count=tally.count,
            total_ms=tally.total_ms,
            max_ms=tally.max_ms,
        )
        for (kind, label), tally in sorted(parsed.durations.items())
    ]
    return session, durations


def _session_price(
    requests: tuple[ObservedRequest, ...],
) -> tuple[Decimal | None, PriceSourceKind, str | None]:
    """Sum the request costs, or return unpriced when any counted request is."""
    priced = [row for row in requests if row.price_source is not PriceSourceKind.UNPRICED]
    if not priced or any(
        row.total_tokens and row.price_source is PriceSourceKind.UNPRICED for row in requests
    ):
        return None, PriceSourceKind.UNPRICED, None
    cost = sum((row.cost_usd or Decimal(0) for row in priced), Decimal(0))
    return cost, priced[0].price_source, priced[0].rate_table_version


# ---- one session's detail --------------------------------------------------------------

BlockKind = Literal[
    "user",
    "assistant",
    "assistant_reasoning",
    "tool_call",
    "tool_result",
    "system",
    "runtime_injection",
    "compaction_summary",
    "project_instruction",
    "tool_definition",
    "unknown",
]
"""Closed kinds of one transcript content block."""

_CLAUDE_BLOCK_KINDS: Final[dict[str, BlockKind]] = {
    "thinking": "assistant_reasoning",
    "redacted_thinking": "assistant_reasoning",
    "tool_use": "tool_call",
    "server_tool_use": "tool_call",
    "tool_result": "tool_result",
    "web_search_tool_result": "tool_result",
}
_CODEX_ROLE_KINDS: Final[dict[str, BlockKind]] = {
    "user": "user",
    "assistant": "assistant",
    "developer": "project_instruction",
    "system": "system",
}


class ContentBlock(BaseModel):
    """One content block of a session, as a kind and a size, never its text.

    Attributes:
        ordinal: 1-based position among the session's blocks.
        kind: The block's closed kind.
        lane: ``main``, or ``sidechain`` for a Claude sidechain record.
        request_ordinal: The requests billed when the block was written.
        ts: The record's timestamp, or ``None``.
        size_chars: Characters of the block's serialised content.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1)
    kind: BlockKind
    lane: Literal["main", "sidechain"]
    request_ordinal: int = Field(ge=0)
    ts: datetime | None
    size_chars: int = Field(ge=0)


class ToolSpan(BaseModel):
    """One tool call from its request to its result.

    Attributes:
        label: Closed-vocabulary label within the span's lane.
        started_at: When the call was made.
        ended_at: When its result arrived, or ``None`` when none did.
        duration_ms: The span, or ``None`` when it never closed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    started_at: datetime | None
    ended_at: datetime | None
    duration_ms: int | None = Field(default=None, ge=0)


class CompactionBoundary(BaseModel):
    """One point where the runtime compacted the session's context.

    Attributes:
        block_ordinal: Blocks written before the boundary.
        request_ordinal: Requests billed before the boundary.
        ts: When the runtime compacted, or ``None``.
        trigger: ``auto`` or ``manual`` when recorded.
        pre_tokens: Context tokens before compaction, when recorded.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    block_ordinal: int = Field(ge=0)
    request_ordinal: int = Field(ge=0)
    ts: datetime | None
    trigger: Literal["auto", "manual"] | None
    pre_tokens: int | None = Field(default=None, ge=0)


class SessionDetail(BaseModel):
    """One session's model steps, tool spans per lane, blocks and compactions.

    Attributes:
        vendor_session_ref: Hashed vendor session id.
        runtime: The runtime whose history it is.
        cwd_digest: Digest of the recorded working directory, or ``None``.
        steps: The session's requests, in order.
        tool_spans: Tool spans by lane: plain tool calls, shell commands and
            delegated agents.
        blocks: Content blocks, in order.
        compactions: Compaction boundaries, in order.
        runtime_version: The first ``major.minor.patch`` runtime version a
            record named, or ``None``; the preamble rebuild is keyed by it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor_session_ref: str = Field(pattern=r"^vsid-[0-9a-f]{32}$")
    runtime: HistoryRuntime
    cwd_digest: str | None
    steps: tuple[ObservedRequest, ...]
    tool_spans: dict[DurationKind, tuple[ToolSpan, ...]]
    blocks: tuple[ContentBlock, ...]
    compactions: tuple[CompactionBoundary, ...]
    runtime_version: str | None = Field(default=None, pattern=_VERSION_PATTERN)


def _runtime_version(record: dict[str, Any]) -> str | None:
    """Return the runtime version a record names, when it is a plain version number.

    Claude rows carry ``version``; a Codex ``session_meta`` payload carries
    ``cli_version``. Anything not shaped like a version is dropped, so no free
    text rides along.
    """
    payload = record.get("payload")
    raw = record.get("version")
    if raw is None and isinstance(payload, dict):
        raw = payload.get("cli_version")
    return raw if isinstance(raw, str) and _VERSION_RE.fullmatch(raw) else None


def _size(value: object) -> int:
    if isinstance(value, str):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False, default=str))


def _trigger(raw: object) -> Literal["auto", "manual"] | None:
    if raw == "auto":
        return "auto"
    if raw == "manual":
        return "manual"
    return None


@dataclass(slots=True)
class _DetailBuilder:
    """Collect blocks, spans and compactions from the records a fold decodes."""

    runtime: HistoryRuntime
    blocks: list[ContentBlock] = field(default_factory=list)
    compactions: list[CompactionBoundary] = field(default_factory=list)
    open_spans: dict[str, tuple[DurationKind, str, datetime | None]] = field(default_factory=dict)
    spans: dict[DurationKind, list[ToolSpan]] = field(default_factory=dict)
    runtime_version: str | None = None

    def observe(self, record: dict[str, Any], requests: int) -> None:
        if self.runtime_version is None:
            self.runtime_version = _runtime_version(record)
        ts = _timestamp(record)
        lane: Literal["main", "sidechain"] = "sidechain" if record.get("isSidechain") else "main"
        pieces = (
            self._claude_pieces(record, ts, requests)
            if self.runtime == "claude"
            else self._codex_pieces(record, ts, requests)
        )
        for kind, value in pieces:
            self.blocks.append(
                ContentBlock(
                    ordinal=len(self.blocks) + 1,
                    kind=kind,
                    lane=lane,
                    request_ordinal=requests,
                    ts=ts,
                    size_chars=_size(value),
                )
            )

    def _compaction(
        self,
        ts: datetime | None,
        requests: int,
        trigger: object = None,
        pre_tokens: object = None,
    ) -> None:
        pre = (
            pre_tokens if isinstance(pre_tokens, int) and not isinstance(pre_tokens, bool) else None
        )
        self.compactions.append(
            CompactionBoundary(
                block_ordinal=len(self.blocks),
                request_ordinal=requests,
                ts=ts,
                trigger=_trigger(trigger),
                pre_tokens=pre if pre is None or pre >= 0 else None,
            )
        )

    def _open(self, call_id: object, lane: DurationKind, label: str, ts: datetime | None) -> None:
        if isinstance(call_id, str) and call_id:
            self.open_spans[call_id] = (lane, label, ts)

    def _close(self, call_id: object, ts: datetime | None) -> None:
        opened = self.open_spans.pop(str(call_id), None)
        if opened is None:
            return
        lane, label, started = opened
        duration = None
        if started is not None and ts is not None and ts >= started:
            duration = int((ts - started).total_seconds() * 1000)
        span = ToolSpan(label=label, started_at=started, ended_at=ts, duration_ms=duration)
        self.spans.setdefault(lane, []).append(span)

    def _claude_pieces(
        self, record: dict[str, Any], ts: datetime | None, requests: int
    ) -> list[tuple[BlockKind, object]]:
        row_type = record.get("type")
        if row_type == "summary":
            return [("compaction_summary", record.get("summary") or "")]
        if row_type == "system":
            if record.get("subtype") == "compact_boundary":
                meta = record.get("compactMetadata")
                meta = meta if isinstance(meta, dict) else {}
                self._compaction(ts, requests, meta.get("trigger"), meta.get("preTokens"))
                return []
            return [("runtime_injection", record.get("content") or "")]
        if row_type not in {"user", "assistant"}:
            return []
        message = record.get("message")
        content = message.get("content") if isinstance(message, dict) else None
        role: BlockKind = "user" if row_type == "user" else "assistant"
        if isinstance(content, str):
            return [(role, content)]
        if not isinstance(content, list):
            return []
        return self._claude_content(content, role, ts)

    def _claude_content(
        self, content: list[Any], role: BlockKind, ts: datetime | None
    ) -> list[tuple[BlockKind, object]]:
        pieces: list[tuple[BlockKind, object]] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            kind = (
                role
                if block_type == "text"
                else _CLAUDE_BLOCK_KINDS.get(str(block_type), "unknown")
            )
            pieces.append((kind, block))
            if block_type == "tool_use":
                labels = _claude_labels(block.get("name"), block.get("input"))
                lane, label = labels[-1]
                self._open(block.get("id"), lane, label, ts)
            elif block_type == "tool_result":
                self._close(block.get("tool_use_id"), ts)
        return pieces

    def _codex_pieces(
        self, record: dict[str, Any], ts: datetime | None, requests: int
    ) -> list[tuple[BlockKind, object]]:
        row_type = record.get("type")
        payload = record.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        payload_type = payload.get("type")
        if row_type == "session_meta":
            pieces: list[tuple[BlockKind, object]] = []
            if payload.get("base_instructions"):
                pieces.append(("system", payload["base_instructions"]))
            if payload.get("dynamic_tools"):
                pieces.append(("tool_definition", payload["dynamic_tools"]))
            return pieces
        if row_type == "compacted":
            self._compaction(ts, requests)
            summary = payload.get("message") or payload.get("replacement_history")
            return [("compaction_summary", summary)] if summary else []
        if row_type == "event_msg" and payload_type == "user_message":
            return [("user", payload.get("message") or "")]
        if row_type != "response_item":
            return []
        return self._codex_response_item(payload, ts)

    def _codex_response_item(
        self, payload: dict[str, Any], ts: datetime | None
    ) -> list[tuple[BlockKind, object]]:
        payload_type = payload.get("type")
        if payload_type == "message":
            return [
                (
                    _CODEX_ROLE_KINDS.get(str(payload.get("role")), "unknown"),
                    payload.get("content") or "",
                )
            ]
        if payload_type == "reasoning":
            return [("assistant_reasoning", payload)]
        if payload_type in _CODEX_CALLS:
            lane, label = _codex_span_label(payload)
            self._open(payload.get("call_id"), lane, label, ts)
            return [("tool_call", payload)]
        if payload_type in _CODEX_OUTPUTS:
            self._close(payload.get("call_id"), ts)
            return [("tool_result", payload)]
        return []


def _codex_span_label(payload: dict[str, Any]) -> tuple[DurationKind, str]:
    """Return the lane and closed label of one Codex tool call."""
    name = payload.get("name")
    if payload.get("type") == "local_shell_call" or name in _CODEX_SHELL_TOOLS:
        arguments = payload.get("arguments")
        command: object = None
        if isinstance(arguments, str):
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                command = parsed.get("command") or parsed.get("cmd")
        action = payload.get("action")
        if command is None and isinstance(action, dict):
            command = action.get("command")
        return DurationKind.COMMAND, _command_family(command)
    if name in _CODEX_DELEGATION_TOOLS:
        return DurationKind.SUBAGENT, OTHER_LABEL
    return DurationKind.TOOL_CALL, classify_label(DurationKind.TOOL_CALL, name)


def read_session_detail(file: HistoryFile) -> SessionDetail | None:
    """Read one session's detail for the session pages.

    The file is folded whole through the same fold the sweep resumes, so the
    steps equal the session's request rows.

    Args:
        file: The session's history file.

    Returns:
        The detail, or ``None`` when the file cannot be read or names no
        session.
    """
    fold = TranscriptFold(runtime=file.runtime, lane=file.lane)
    ledger = RequestLedger()
    builder = _DetailBuilder(runtime=file.runtime)
    try:
        with file.path.open("rb") as handle:
            fold_history_lines(
                handle,
                fold,
                ledger,
                fallback_session_id=file.fallback_session_id,
                observe=builder.observe,
            )
    except OSError as exc:
        logger.warning(f"read_session_detail file={file.path.name!r} error={type(exc).__name__}")
        return None
    ref = fold.vendor_session_ref
    if ref is None:
        return None
    return SessionDetail(
        vendor_session_ref=ref,
        runtime=file.runtime,
        cwd_digest=fold.cwd_digest,
        steps=tuple(sorted(ledger.rows.values(), key=lambda row: row.ordinal)),
        tool_spans={lane: tuple(spans) for lane, spans in builder.spans.items()},
        blocks=tuple(builder.blocks),
        compactions=tuple(builder.compactions),
        runtime_version=builder.runtime_version,
    )
