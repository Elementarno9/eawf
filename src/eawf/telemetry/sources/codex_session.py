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
:class:`~eawf.telemetry.models.TelemetrySession` row (C09 §5.9.4 — per-session
projection): turn count from ``turn_context`` records, token totals from the
last ``token_count`` event, session metadata from the first record that carries
it. A line that fails JSON parsing is skipped with a logged ``WARNING`` (file +
1-based line number) and the fold continues (C09 §6 F3); a rollout with no
parseable records yields nothing. Unknown record types are skipped silently for
forward compatibility. The adapter implements the
:class:`~eawf.telemetry.sources.base.SessionSource` protocol over
:class:`TelemetrySession` rows.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from eawf.telemetry.models import TelemetrySession

logger = logging.getLogger(__name__)

_SESSION_META = "session_meta"
_TURN_CONTEXT = "turn_context"
_EVENT_MSG = "event_msg"
_TOKEN_COUNT = "token_count"


@dataclass
class _CodexAccumulator:
    """Running fold of one Codex rollout transcript into a session row."""

    session_id: str | None = None
    cwd: str | None = None
    git_branch_first: str | None = None
    model_primary: str | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_cache_read: int = 0
    turn_count: int = 0


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


class CodexSessionSource:
    """Reader for Codex CLI rollout transcripts.

    Implements the :class:`~eawf.telemetry.sources.base.SessionSource` protocol
    over :class:`~eawf.telemetry.models.TelemetrySession` rows.
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
                _fold_record(acc, record)

        if not record_seen:
            return
        yield _session_from_accumulator(acc, jsonl_path=path)


def _fold_record(acc: _CodexAccumulator, record: dict[str, Any]) -> None:
    """Fold one rollout record into the running accumulator."""
    ts = _parse_ts(record.get("timestamp"))
    if ts is not None:
        if acc.started_at is None or ts < acc.started_at:
            acc.started_at = ts
        if acc.ended_at is None or ts > acc.ended_at:
            acc.ended_at = ts

    record_type = record.get("type")
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return

    if record_type == _SESSION_META:
        _fold_session_meta(acc, payload)
    elif record_type == _TURN_CONTEXT:
        _fold_turn_context(acc, payload)
    elif record_type == _EVENT_MSG and payload.get("type") == _TOKEN_COUNT:
        _fold_token_count(acc, payload)


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
    if acc.model_primary is None:
        model = payload.get("model")
        acc.model_primary = model if isinstance(model, str) and model else None
    if acc.cwd is None:
        cwd = payload.get("cwd")
        acc.cwd = cwd if isinstance(cwd, str) and cwd else None


def _fold_token_count(acc: _CodexAccumulator, payload: dict[str, Any]) -> None:
    """Adopt cumulative token totals from a ``token_count`` event payload.

    Codex reports running cumulative totals on every ``token_count`` event under
    ``info.total_token_usage``; the last event therefore carries the session
    totals. We overwrite (not sum) so the fold is monotonic regardless of how
    many events the rollout emits.
    """
    info = payload.get("info")
    if not isinstance(info, dict):
        return
    totals = info.get("total_token_usage")
    if not isinstance(totals, dict):
        return
    input_tokens = _coerce_int(totals.get("input_tokens"))
    cached_input = _coerce_int(totals.get("cached_input_tokens"))
    output_tokens = _coerce_int(totals.get("output_tokens"))
    reasoning_tokens = _coerce_int(totals.get("reasoning_output_tokens"))
    acc.total_input_tokens = max(input_tokens - cached_input, 0)
    acc.total_cache_read = cached_input
    acc.total_output_tokens = output_tokens + reasoning_tokens


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
        total_cache_write=0,
        turn_count=acc.turn_count,
        end_marker="other",
        git_branch_first=acc.git_branch_first,
    )


__all__ = ["CodexSessionSource"]
