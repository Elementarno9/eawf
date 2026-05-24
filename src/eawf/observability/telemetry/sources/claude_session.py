"""claude_session source adapter — reader for Claude Code transcript logs.

A Claude Code session log is a JSONL transcript: one JSON object per line,
each a *record* of the session. Records carry a ``type`` (``user`` /
``assistant`` / ``summary`` / ``system``), a ``sessionId``, a ``cwd``, a
``gitBranch``, an ISO-8601 ``timestamp``, a ``uuid`` / ``parentUuid`` linkage,
and (for assistant records) a ``message`` with ``model`` + ``usage`` token
counts. This adapter folds one transcript file into a single
:class:`~eawf.observability.telemetry.models.TelemetrySession` row (C09 §5.9.4 — per-session
projection), summing token usage across assistant turns and deriving the
session metadata from the first record that carries it.

A line that fails JSON parsing is skipped with a logged ``WARNING`` (file +
1-based line number) and the fold continues (C09 §6 F3); a transcript with no
parseable records yields nothing (an empty session is not projected). The
adapter implements the :class:`~eawf.observability.telemetry.sources.base.SessionSource`
protocol over :class:`TelemetrySession` rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from eawf.observability.telemetry.models import TelemetrySession

logger = logging.getLogger(__name__)

_ASSISTANT = "assistant"


@dataclass
class _SessionAccumulator:
    """Running fold of one Claude Code transcript into a session row."""

    session_id: str | None = None
    cwd: str | None = None
    git_branch_first: str | None = None
    model_primary: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    turn_count: int = 0
    seen_uuids: set[str] = field(default_factory=set)
    orphan_count: int = 0
    child_count: int = 0


def _parse_ts(raw: object) -> datetime | None:
    """Return a parsed ISO-8601 timestamp, or ``None`` when absent / malformed."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _coerce_int(raw: object) -> int:
    """Return *raw* as an int, defaulting to ``0`` for absent / non-numeric values."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


class ClaudeSessionSource:
    """Reader for Claude Code transcript logs.

    Implements the :class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.observability.telemetry.models.TelemetrySession` rows.
    """

    source_name = "claude"

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield Claude transcript files (``*.jsonl``) under *root*.

        A missing root yields nothing (C09 §6 F2). Files are yielded in sorted
        order so projection is deterministic across runs.
        """
        if not root.is_dir():
            return
        yield from sorted(p for p in root.glob("*.jsonl") if p.is_file())

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        """Fold the transcript at *path* into a single :class:`TelemetrySession`.

        Yields exactly one row when the transcript holds at least one
        parseable record; yields nothing for a missing path or an
        all-unparseable transcript. Corrupt lines are skipped with a logged
        ``WARNING`` (C09 §6 F3).
        """
        if not path.is_file():
            return
        acc = _SessionAccumulator()
        record_seen = False
        with path.open(encoding="utf-8") as handle:
            for line_no, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    logger.warning(
                        f"iter_rows source={self.source_name} path={str(path)!r} "
                        f"line={line_no} skipped malformed json"
                    )
                    continue
                if not isinstance(record, dict):
                    logger.warning(
                        f"iter_rows source={self.source_name} path={str(path)!r} "
                        f"line={line_no} skipped non-object record"
                    )
                    continue
                record_seen = True
                _fold_record(acc, record, fallback_session_id=path.stem)

        if not record_seen:
            return
        yield _session_from_accumulator(acc, jsonl_path=path)


def _coerce_str(raw: object) -> str | None:
    """Return *raw* as a non-empty string, or ``None`` when absent / blank / non-string."""
    return raw if isinstance(raw, str) and raw else None


def _fold_identity(acc: _SessionAccumulator, record: dict[str, Any]) -> None:
    """Fill the first-seen session-identity fields (session id, cwd, branch)."""
    if acc.session_id is None:
        acc.session_id = _coerce_str(record.get("sessionId"))
    if acc.cwd is None:
        acc.cwd = _coerce_str(record.get("cwd"))
    if acc.git_branch_first is None:
        acc.git_branch_first = _coerce_str(record.get("gitBranch"))


def _fold_timestamp(acc: _SessionAccumulator, record: dict[str, Any]) -> None:
    """Widen the session's started/ended window with this record's timestamp."""
    ts = _parse_ts(record.get("timestamp"))
    if ts is None:
        return
    if acc.started_at is None or ts < acc.started_at:
        acc.started_at = ts
    if acc.ended_at is None or ts > acc.ended_at:
        acc.ended_at = ts


def _fold_lineage(acc: _SessionAccumulator, record: dict[str, Any]) -> None:
    """Track uuid/parentUuid linkage to derive the orphan rate."""
    uuid = record.get("uuid")
    if isinstance(uuid, str) and uuid:
        acc.seen_uuids.add(uuid)
    parent = record.get("parentUuid")
    if isinstance(parent, str) and parent:
        acc.child_count += 1
        if parent not in acc.seen_uuids:
            acc.orphan_count += 1


def _fold_assistant_usage(acc: _SessionAccumulator, message: dict[str, Any]) -> None:
    """Add one assistant turn's model + token usage into the accumulator."""
    if acc.model_primary is None:
        acc.model_primary = _coerce_str(message.get("model"))
    usage = message.get("usage")
    if isinstance(usage, dict):
        acc.total_input_tokens += _coerce_int(usage.get("input_tokens"))
        acc.total_output_tokens += _coerce_int(usage.get("output_tokens"))
        acc.total_cache_read += _coerce_int(usage.get("cache_read_input_tokens"))
        acc.total_cache_write += _coerce_int(usage.get("cache_creation_input_tokens"))


def _fold_record(
    acc: _SessionAccumulator, record: dict[str, Any], *, fallback_session_id: str
) -> None:
    """Fold one transcript record into the running accumulator."""
    _fold_identity(acc, record)
    _fold_timestamp(acc, record)
    _fold_lineage(acc, record)

    if record.get("type") == _ASSISTANT:
        acc.turn_count += 1
        message = record.get("message")
        if isinstance(message, dict):
            _fold_assistant_usage(acc, message)

    if acc.session_id is None and fallback_session_id:
        acc.session_id = fallback_session_id


def _session_from_accumulator(acc: _SessionAccumulator, *, jsonl_path: Path) -> TelemetrySession:
    """Build a :class:`TelemetrySession` from a completed accumulator fold."""
    duration_ms: int | None = None
    if acc.started_at is not None and acc.ended_at is not None:
        duration_ms = int((acc.ended_at - acc.started_at).total_seconds() * 1000)

    orphan_rate = acc.orphan_count / acc.child_count if acc.child_count else 0.0

    return TelemetrySession(
        session_id=acc.session_id or jsonl_path.stem,
        project_id="",
        runtime="claude",
        wave_id=None,
        attempt_id=None,
        session_log_path=str(jsonl_path),
        started_at=acc.started_at,
        ended_at=acc.ended_at,
        duration_ms=duration_ms,
        model_primary=acc.model_primary,
        total_input_tokens=acc.total_input_tokens,
        total_output_tokens=acc.total_output_tokens,
        total_cache_read=acc.total_cache_read,
        total_cache_write=acc.total_cache_write,
        turn_count=acc.turn_count,
        end_marker="other",
        parent_uuid_orphan_rate=orphan_rate,
        git_branch_first=acc.git_branch_first,
    )


__all__ = ["ClaudeSessionSource"]
