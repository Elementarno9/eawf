"""codex_session source adapter — reader for Codex CLI rollout transcripts.

A Codex CLI session log is a date-sharded JSONL transcript at
``<codex-home>/sessions/<YYYY>/<MM>/<DD>/rollout-<ISO-UTC>-<uuid-v7>.jsonl``.
Each line is a *record* of the form ``{"timestamp", "type", "payload"}`` where
``type`` is one of ``session_meta`` / ``turn_context`` / ``response_item`` /
``event_msg``:

- ``session_meta`` (line 1) carries the session ``id``, ``cwd``, and ``git``
  metadata.
- ``turn_context`` carries the per-turn ``model`` + ``cwd``; the count of these
  records is the session's turn count.
- ``event_msg`` with ``payload.type == "token_count"`` carries cumulative token
  usage under ``payload.info.total_token_usage`` (the last such record holds the
  session totals).

This adapter folds one rollout file into a single
:class:`~eawf.observability.telemetry.models.TelemetrySession` row (C09 §5.9.4 — per-session
projection): turn count from ``turn_context`` records, token totals from the
last ``token_count`` event, session metadata from the first record that carries
it. The same fold yields one :class:`CodexRequestUsage` per new cumulative
reading, which the session-history sweep turns into per-request rows. A line
that fails JSON parsing is skipped with a logged ``WARNING`` (file + 1-based
line number) and the fold continues (C09 §6 F3); a rollout with no
parseable records yields nothing. Unknown record types are skipped silently for
forward compatibility. The adapter implements the
:class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol over
:class:`TelemetrySession` rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel, ConfigDict

from eawf.observability.telemetry.models import TelemetrySession
from eawf.observability.telemetry.pricing import active_price_book

logger = logging.getLogger(__name__)

_SESSION_META = "session_meta"
_TURN_CONTEXT = "turn_context"
_EVENT_MSG = "event_msg"
_TOKEN_COUNT = "token_count"

#: No single request reads more than the largest context window plus its
#: output; a reading above this is a counter a forked thread inherited from
#: its parent, not work the thread itself did.
_MAX_SINGLE_REQUEST_TOKENS: Final = 2_000_000

#: The write class Codex cache writes are priced under. A rollout reports
#: ``cache_write_input_tokens`` without saying whether they are 30-minute or
#: 12-hour writes, so they carry a class no price file states and stay
#: unpriced rather than billed at a guessed rate.
CODEX_WRITE_CLASS: Final = "unnamed"


class _CodexAccumulator(BaseModel):
    """Running fold of one Codex rollout transcript into a session row.

    A model, not a plain record, so the session-history index can store the
    fold and resume it when the rollout grows.

    Attributes:
        model_current: Model of the latest ``turn_context``, which the next
            request bills against.
        last_total: The latest cumulative reading as ``(input, cached,
            output, reasoning)``; a repeated reading is no new request.
        total_cache_write: The latest cumulative cache-write count.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None
    cwd: str | None = None
    git_branch_first: str | None = None
    model_primary: str | None = None
    model_current: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_reasoning_output_tokens: int = 0
    total_cache_read: int = 0
    total_cache_write: int = 0
    turn_count: int = 0
    last_total: tuple[int, int, int, int] | None = None


CodexFold = _CodexAccumulator
"""The public name of the rollout fold the session-history sweep resumes."""


@dataclass(frozen=True, slots=True)
class CodexRequestUsage:
    """The usage of one Codex request, read off a new cumulative reading.

    Attributes:
        model: Model the request billed against, or ``None``.
        input_tokens: Input tokens neither read from nor written to the cache.
        cache_read_tokens: Cached input tokens.
        cache_write_tokens: Input tokens written to the cache.
        output_tokens: Output tokens, reasoning included.
        reasoning_tokens: Reasoning slice of the output.
    """

    model: str | None
    input_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    output_tokens: int
    reasoning_tokens: int


def _parse_ts(raw: object) -> datetime | None:
    """Return a parsed ISO-8601 timestamp, or ``None`` when absent / malformed.

    A timestamp without an offset is read as UTC, so every instant returned
    compares with every other one.
    """
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _coerce_int(raw: object) -> int:
    """Return *raw* as an int, defaulting to ``0`` for absent / non-numeric values."""
    return raw if isinstance(raw, int) and not isinstance(raw, bool) else 0


class CodexSessionSource:
    """Reader for Codex CLI rollout transcripts.

    Implements the :class:`~eawf.observability.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.observability.telemetry.models.TelemetrySession` rows.
    """

    source_name = "codex"

    def discover(self, root: Path) -> Iterator[Path]:
        """Yield Codex rollout files (``rollout-*.jsonl``) under *root*.

        Codex stores rollouts in a date-sharded ``<YYYY>/<MM>/<DD>/`` tree, so
        discovery recurses. A missing root yields nothing (C09 §6 F2). Files are
        yielded in sorted order so projection is deterministic across runs.
        """
        if not root.is_dir():
            return
        yield from sorted(p for p in root.rglob("rollout-*.jsonl") if p.is_file())

    def iter_rows(self, path: Path) -> Iterator[TelemetrySession]:
        """Fold the rollout at *path* into a single :class:`TelemetrySession`.

        Yields exactly one row when the rollout holds at least one parseable
        record; yields nothing for a missing path or an all-unparseable rollout.
        Corrupt lines are skipped with a logged ``WARNING`` (C09 §6 F3).
        """
        if not path.is_file():
            return
        acc = _CodexAccumulator()
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
                fold_codex_record(acc, record)

        if not record_seen:
            return
        # The session-history fold imports this module for the rollout fold,
        # so its pricing entry point is imported here rather than at the top.
        from eawf.observability.telemetry.sources.session_history import (
            HistoryFile,
            history_file_cost,
        )

        session = _session_from_accumulator(acc, jsonl_path=path)
        cost = history_file_cost(HistoryFile(runtime="codex", path=path), active_price_book())
        yield session.model_copy(update={"total_cost_usd": cost})


def fold_codex_record(acc: _CodexAccumulator, record: dict[str, Any]) -> CodexRequestUsage | None:
    """Fold one rollout record into *acc*.

    Args:
        acc: The running fold of the rollout.
        record: One decoded rollout line.

    Returns:
        The request a ``token_count`` record completed, or ``None`` for any
        other record and for a repeated reading.
    """
    ts = _parse_ts(record.get("timestamp"))
    if ts is not None:
        if acc.started_at is None or ts < acc.started_at:
            acc.started_at = ts
        if acc.ended_at is None or ts > acc.ended_at:
            acc.ended_at = ts

    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None

    if record_type == _SESSION_META:
        _fold_session_meta(acc, payload)
    elif record_type == _TURN_CONTEXT:
        _fold_turn_context(acc, payload)
    elif record_type == _EVENT_MSG and payload.get("type") == _TOKEN_COUNT:
        return _fold_token_count(acc, payload)
    return None


def _fold_session_meta(acc: _CodexAccumulator, payload: dict[str, Any]) -> None:
    """Capture session id, cwd, and git branch from a ``session_meta`` payload."""
    if acc.session_id is None:
        session_id = payload.get("id")
        acc.session_id = session_id if isinstance(session_id, str) and session_id else None
    if acc.cwd is None:
        cwd = payload.get("cwd")
        acc.cwd = cwd if isinstance(cwd, str) and cwd else None
    if acc.git_branch_first is None:
        git = payload.get("git")
        if isinstance(git, dict):
            branch = git.get("branch")
            acc.git_branch_first = branch if isinstance(branch, str) and branch else None


def _fold_turn_context(acc: _CodexAccumulator, payload: dict[str, Any]) -> None:
    """Count one turn and capture the primary model from a ``turn_context``."""
    acc.turn_count += 1
    model = payload.get("model")
    if isinstance(model, str) and model:
        acc.model_current = model
        if acc.model_primary is None:
            acc.model_primary = model
    if acc.cwd is None:
        cwd = payload.get("cwd")
        acc.cwd = cwd if isinstance(cwd, str) and cwd else None


def _fold_token_count(acc: _CodexAccumulator, payload: dict[str, Any]) -> CodexRequestUsage | None:
    """Adopt cumulative token totals from a ``token_count`` event payload.

    Codex reports running cumulative totals on every ``token_count`` event under
    ``info.total_token_usage``; the last event therefore carries the session
    totals. We overwrite (not sum) so the fold is monotonic regardless of how
    many events the rollout emits.

    ``reasoning_output_tokens`` is a *subset* of ``output_tokens``, not a
    sibling of it: the vendor's own ``total_tokens`` equals ``input_tokens``
    plus ``output_tokens`` on every observed rollout. It is therefore kept on
    its own accumulator field and never added to the output total, which would
    double-count the reasoning slice.

    Codex repeats the latest reading when it only refreshes its rate limits,
    so a reading equal to the previous one completes no request. A new one
    takes its request's usage from ``last_token_usage``, or from the movement
    since the previous reading when that is absent or over the single-request
    ceiling.

    Returns:
        The request a new reading completed, or ``None``.
    """
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    totals = info.get("total_token_usage")
    if not isinstance(totals, dict):
        return None
    current = _usage_tuple(totals)
    input_tokens, cached_input, output_tokens, reasoning_tokens = current
    # Cache writes, like cache reads, are a slice of the gross input count:
    # the vendor's total_tokens is input plus output alone.
    writes_total = _cache_writes(totals)
    previous_writes = acc.total_cache_write
    uncached = max(input_tokens - cached_input, 0)
    acc.total_cache_write = min(writes_total, uncached)
    acc.total_input_tokens = uncached - acc.total_cache_write
    acc.total_cache_read = cached_input
    acc.total_output_tokens = output_tokens
    acc.total_reasoning_output_tokens = reasoning_tokens
    previous = acc.last_total
    if current == previous:
        return None
    acc.last_total = current
    last = info.get("last_token_usage")
    moved_writes = max(writes_total - previous_writes, 0)
    if isinstance(last, dict):
        usage, writes = _usage_tuple(last), _cache_writes(last)
    else:
        usage, writes = _movement(previous, current), moved_writes
    if usage[0] + usage[2] > _MAX_SINGLE_REQUEST_TOKENS:
        if previous is None:
            return None
        usage, writes = _movement(previous, current), moved_writes
    gross, cached, output, reasoning = usage
    cache_read = min(cached, gross)
    cache_write = min(writes, gross - cache_read)
    return CodexRequestUsage(
        model=acc.model_current,
        input_tokens=gross - cache_read - cache_write,
        cache_read_tokens=cache_read,
        cache_write_tokens=cache_write,
        output_tokens=output,
        reasoning_tokens=min(reasoning, output),
    )


def _cache_writes(raw: dict[str, Any]) -> int:
    """Return the ``cache_write_input_tokens`` of one Codex usage mapping."""
    return max(_coerce_int(raw.get("cache_write_input_tokens")), 0)


def _usage_tuple(raw: dict[str, Any]) -> tuple[int, int, int, int]:
    """Return ``(input, cached, output, reasoning)`` of one Codex usage mapping."""
    return (
        max(_coerce_int(raw.get("input_tokens")), 0),
        max(_coerce_int(raw.get("cached_input_tokens")), 0),
        max(_coerce_int(raw.get("output_tokens")), 0),
        max(_coerce_int(raw.get("reasoning_output_tokens")), 0),
    )


def _movement(
    previous: tuple[int, int, int, int] | None, current: tuple[int, int, int, int]
) -> tuple[int, int, int, int]:
    """Return how far each counter moved since *previous*, never below zero."""
    if previous is None:
        return current
    a, b, c, d = (max(now - before, 0) for now, before in zip(current, previous, strict=True))
    return (a, b, c, d)


def _session_from_accumulator(acc: _CodexAccumulator, *, jsonl_path: Path) -> TelemetrySession:
    """Build a :class:`TelemetrySession` from a completed accumulator fold."""
    duration_ms: int | None = None
    if acc.started_at is not None and acc.ended_at is not None:
        duration_ms = int((acc.ended_at - acc.started_at).total_seconds() * 1000)

    return TelemetrySession(
        session_id=acc.session_id or jsonl_path.stem,
        project_id="",
        runtime="codex",
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
        git_branch_first=acc.git_branch_first,
    )


__all__ = [
    "CODEX_WRITE_CLASS",
    "CodexFold",
    "CodexRequestUsage",
    "CodexSessionSource",
    "fold_codex_record",
]
