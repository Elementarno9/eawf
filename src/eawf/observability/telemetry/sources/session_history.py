"""Session-history sweep reader: local runtime transcripts to observed rows.

The sweep is the one producer of the observed collection. It reads a
runtime's own local session history (Claude Code project transcripts), folds
each transcript into an :class:`~eawf.observability.telemetry.models.ObservedSession`
carrying the five token classes plus a price source, and measures per-kind
block durations (tool calls, shell commands by family, delegated subagents)
into :class:`~eawf.observability.telemetry.models.ObservedDuration` rows.

Only statistics and closed-vocabulary labels leave a transcript: the session
id is hashed, no path, command string or message text is kept, and every
label outside :data:`~eawf.observability.telemetry.models.DURATION_LABELS`
becomes ``other``.

The projector drives this reader and owns the coverage funnel; this module
only parses and classifies.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eawf.observability.telemetry.models import (
    DURATION_LABELS,
    OTHER_LABEL,
    DurationKind,
    ObservedDuration,
    ObservedSession,
    PriceSourceKind,
)
from eawf.observability.telemetry.pricing import resolve_price_source
from eawf.runtime.runtimes.metering import price_token_counts
from eawf.runtime.session.vendor_id import hash_vendor_session_id

logger = logging.getLogger(__name__)

__all__ = [
    "ParsedTranscript",
    "classify_label",
    "claude_history_root",
    "discover_history",
    "observed_rows",
    "parse_transcript",
]

_PROJECTS_DIR_ENV = "EAWF_CLAUDE_PROJECTS_DIR"
_SHELL_TOOL = "Bash"
_DELEGATION_TOOLS = frozenset({"Task", "Agent"})
_MCP_PREFIX = "mcp__"


def claude_history_root(project_root: Path) -> Path:
    """Return the Claude Code transcript directory for *project_root*.

    Claude Code names each project directory after its working directory with
    every non-alphanumeric character replaced by ``-``. The projects root
    honours ``EAWF_CLAUDE_PROJECTS_DIR`` (tests redirect it away from the real
    home directory), then ``CLAUDE_CONFIG_DIR``, then ``~/.claude``.

    Args:
        project_root: The repository root the sessions ran in.

    Returns:
        The history directory; it need not exist.
    """
    override = os.environ.get(_PROJECTS_DIR_ENV)
    if override:
        projects = Path(override)
    else:
        config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
        projects = (Path(config_dir) if config_dir else Path.home() / ".claude") / "projects"
    return projects / re.sub(r"[^A-Za-z0-9]", "-", str(project_root))


def discover_history(root: Path) -> list[Path]:
    """Return the top-level session transcripts under *root*, sorted.

    Only root sessions are swept; no proxy session is synthesized from a
    descendant, so every counted session was observed directly.

    Args:
        root: The runtime history directory.

    Returns:
        Sorted ``*.jsonl`` files; empty when *root* is missing.
    """
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.jsonl") if p.is_file())


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


def _command_family(tool_input: object) -> str:
    """Return the closed-vocabulary family of a shell tool call's command."""
    if not isinstance(tool_input, dict):
        return OTHER_LABEL
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return OTHER_LABEL
    head = command.split(maxsplit=1)[0]
    return classify_label(DurationKind.COMMAND, head)


def _subagent_purpose(tool_input: object) -> str:
    """Return the closed-vocabulary purpose of a delegation tool call."""
    if not isinstance(tool_input, dict):
        return OTHER_LABEL
    return classify_label(DurationKind.SUBAGENT, tool_input.get("subagent_type"))


def _non_negative_int(raw: object) -> int:
    """Return *raw* when it is a non-negative JSON integer, else ``0``."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _timestamp(record: dict[str, Any]) -> datetime | None:
    """Return the record's ISO-8601 timestamp, or ``None``."""
    raw = record.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


@dataclass
class _Block:
    """One open tool call awaiting its result."""

    labels: tuple[tuple[DurationKind, str], ...]
    started: datetime


@dataclass
class ParsedTranscript:
    """The statistics one transcript folds into.

    Attributes:
        session_id: Raw vendor session id (hashed before any row is built).
        model: First billed model id.
        started_at: Earliest record timestamp.
        ended_at: Latest record timestamp.
        turn_count: Distinct billed assistant messages.
        input_tokens: Non-cached input tokens.
        output_tokens: Output tokens.
        cache_read_tokens: Prompt-cache read tokens.
        cache_write_5m_tokens: Prompt-cache writes at the 5-minute rate.
        cache_write_1h_tokens: Prompt-cache writes at the 1-hour rate.
        durations: Per ``(kind, label)`` list of measured block durations.
        skipped_lines: Malformed lines skipped while parsing.
    """

    session_id: str | None = None
    model: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    turn_count: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_5m_tokens: int = 0
    cache_write_1h_tokens: int = 0
    durations: dict[tuple[DurationKind, str], list[int]] = field(default_factory=dict)
    skipped_lines: int = 0
    _billed: set[str] = field(default_factory=set)
    _open: dict[str, _Block] = field(default_factory=dict)

    @property
    def cache_write_tokens(self) -> int:
        """Return prompt-cache writes across both TTL tiers."""
        return self.cache_write_5m_tokens + self.cache_write_1h_tokens

    @property
    def has_operator_work(self) -> bool:
        """Return whether the session billed at least one assistant message."""
        return self.turn_count > 0

    def _widen(self, ts: datetime | None) -> None:
        if ts is None:
            return
        if self.started_at is None or ts < self.started_at:
            self.started_at = ts
        if self.ended_at is None or ts > self.ended_at:
            self.ended_at = ts

    def _bill(self, record: dict[str, Any], message: dict[str, Any]) -> None:
        # One billed message is written once per content block with the same
        # usage repeated, so usage is counted once per message id.
        usage = message.get("usage")
        if not isinstance(usage, dict):
            return
        key = message.get("id") or record.get("requestId") or record.get("uuid")
        if isinstance(key, str) and key:
            if key in self._billed:
                return
            self._billed.add(key)
        if self.model is None and isinstance(message.get("model"), str):
            self.model = message["model"]
        self.turn_count += 1
        self.input_tokens += _non_negative_int(usage.get("input_tokens"))
        self.output_tokens += _non_negative_int(usage.get("output_tokens"))
        self.cache_read_tokens += _non_negative_int(usage.get("cache_read_input_tokens"))
        write_total = _non_negative_int(usage.get("cache_creation_input_tokens"))
        split = usage.get("cache_creation")
        one_hour = (
            _non_negative_int(split.get("ephemeral_1h_input_tokens"))
            if isinstance(split, dict)
            else 0
        )
        one_hour = min(one_hour, write_total)
        self.cache_write_1h_tokens += one_hour
        self.cache_write_5m_tokens += write_total - one_hour

    def _open_blocks(self, content: list[Any], ts: datetime) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            block_id = block.get("id")
            if not isinstance(block_id, str) or not block_id:
                continue
            name = block.get("name")
            labels = [(DurationKind.TOOL_CALL, classify_label(DurationKind.TOOL_CALL, name))]
            if name == _SHELL_TOOL:
                labels.append((DurationKind.COMMAND, _command_family(block.get("input"))))
            elif name in _DELEGATION_TOOLS:
                labels.append((DurationKind.SUBAGENT, _subagent_purpose(block.get("input"))))
            self._open[block_id] = _Block(labels=tuple(labels), started=ts)

    def _close_blocks(self, content: list[Any], ts: datetime) -> None:
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            opened = self._open.pop(str(block.get("tool_use_id")), None)
            if opened is None or ts < opened.started:
                continue
            elapsed_ms = int((ts - opened.started).total_seconds() * 1000)
            for key in opened.labels:
                self.durations.setdefault(key, []).append(elapsed_ms)

    def fold(self, record: dict[str, Any], *, fallback_session_id: str) -> None:
        """Fold one transcript record into the running statistics.

        Args:
            record: One decoded transcript line.
            fallback_session_id: Session id to use when no record names one.
        """
        if self.session_id is None:
            raw_id = record.get("sessionId")
            self.session_id = raw_id if isinstance(raw_id, str) and raw_id else fallback_session_id
        ts = _timestamp(record)
        self._widen(ts)
        message = record.get("message")
        if not isinstance(message, dict):
            return
        if record.get("type") == "assistant":
            self._bill(record, message)
        content = message.get("content")
        if ts is None or not isinstance(content, list):
            return
        if record.get("type") == "assistant":
            self._open_blocks(content, ts)
        elif record.get("type") == "user":
            self._close_blocks(content, ts)


def parse_transcript(path: Path) -> ParsedTranscript | None:
    """Parse one transcript, or return ``None`` when it holds no readable record.

    A malformed line is skipped and counted; a file that cannot be decoded or
    holds no JSON object record at all is unparsable.

    Args:
        path: The transcript file.

    Returns:
        The folded statistics, or ``None`` for an unparsable file.
    """
    parsed = ParsedTranscript()
    records = 0
    try:
        with path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    parsed.skipped_lines += 1
                    continue
                if not isinstance(record, dict):
                    parsed.skipped_lines += 1
                    continue
                records += 1
                parsed.fold(record, fallback_session_id=path.stem)
    except (OSError, UnicodeDecodeError) as exc:
        logger.warning(f"parse_transcript file={path.name!r} error={type(exc).__name__}")
        return None
    return parsed if records else None


def observed_rows(
    parsed: ParsedTranscript, *, runtime: str, project_id: str
) -> tuple[ObservedSession, list[ObservedDuration]]:
    """Build the observed session row and its duration rows.

    Args:
        parsed: A transcript with operator work.
        runtime: Runtime the history belongs to.
        project_id: Scope label of the owning project.

    Returns:
        The session row and one duration row per measured ``(kind, label)``.

    Raises:
        ValueError: When *parsed* names no session id.
        pydantic.ValidationError: When a row fails its invariants.
    """
    if not parsed.session_id:
        raise ValueError("an observed session needs a vendor session id")
    ref = hash_vendor_session_id(parsed.session_id)
    cost, price_source, rate_version = _price(parsed)
    duration_ms: int | None = None
    if parsed.started_at is not None and parsed.ended_at is not None:
        duration_ms = int((parsed.ended_at - parsed.started_at).total_seconds() * 1000)
    total = (
        parsed.input_tokens
        + parsed.output_tokens
        + parsed.cache_read_tokens
        + parsed.cache_write_tokens
    )
    session = ObservedSession.model_validate(
        {
            "vendor_session_ref": ref,
            "runtime": runtime,
            "project_id": project_id,
            "model": parsed.model,
            "started_at": parsed.started_at,
            "ended_at": parsed.ended_at,
            "duration_ms": duration_ms,
            "turn_count": parsed.turn_count,
            "input_tokens": parsed.input_tokens,
            "output_tokens": parsed.output_tokens,
            "cache_read_tokens": parsed.cache_read_tokens,
            "cache_write_tokens": parsed.cache_write_tokens,
            "reasoning_tokens": None,
            "total_tokens": total,
            "cost_usd": cost,
            "price_source": price_source,
            "rate_table_version": rate_version,
        }
    )
    durations = [
        ObservedDuration.model_validate(
            {
                "vendor_session_ref": ref,
                "runtime": runtime,
                "kind": kind,
                "label": label,
                "count": len(values),
                "total_ms": sum(values),
                "max_ms": max(values),
            }
        )
        for (kind, label), values in sorted(parsed.durations.items())
    ]
    return session, durations


def _price(parsed: ParsedTranscript) -> tuple[Decimal | None, PriceSourceKind, str | None]:
    """Price the parsed token classes against the embedded rate table.

    An unpriced session records a null cost, never a zero.
    """
    if parsed.model is None:
        return None, PriceSourceKind.UNPRICED, None
    source, version = resolve_price_source(parsed.model)
    if source is PriceSourceKind.UNPRICED:
        return None, source, None
    cost = price_token_counts(
        parsed.model,
        input_tokens=parsed.input_tokens,
        output_tokens=parsed.output_tokens,
        cache_creation_5m_input_tokens=parsed.cache_write_5m_tokens,
        cache_creation_1h_input_tokens=parsed.cache_write_1h_tokens,
        cache_read_input_tokens=parsed.cache_read_tokens,
    )
    if cost is None:
        return None, PriceSourceKind.UNPRICED, None
    return cost, source, version
