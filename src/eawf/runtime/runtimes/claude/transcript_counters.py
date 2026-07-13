"""Aggregate a Claude Code session transcript into runtime counters.

Claude Code's ``Stop`` / ``SessionEnd`` hook payload carries no cost or usage
block -- it ships ``transcript_path`` and expects the consumer to read the
session JSONL itself. The transcript is where the runtime facts live: each
assistant row carries ``message.usage`` (the per-class token tallies) and
``message.model`` (the billed model id), and each completed turn emits a
``durationMs`` row.

This module turns that file into the same
:class:`~eawf.runtime.runtimes.claude.runtime_counters.RuntimeCounters` shape the
statusline parser emits, so the runtime-capture path has one counter contract
regardless of which surface fed it. Cost is *derived* rather than read: the
transcript ships no cost figure, so the aggregated token classes are priced
through :func:`~eawf.runtime.runtimes.metering.price_token_counts` -- the same
Decimal pricing table a headless spawn is billed against, keeping interactive
and headless cost attribution consistent.

Two transcript quirks the aggregator must handle:

- **Duplicate usage rows.** A single assistant message is appended once per
  content block, each copy repeating the *same* ``message.usage``. Summing rows
  blindly multiplies the token tally, so rows are deduplicated by message id.
- **Duration lives off the message, and lands late.** Per-turn time is emitted on
  ``type: "system"`` / ``subtype: "turn_duration"`` rows as a top-level
  ``durationMs``, not inside the assistant message -- and that row is written
  *after* the Stop hook has already run, so a hook-time read of a live session
  sees no duration for the turn in flight. The row timestamps carry the rest, but
  the first-to-last SPAN over them is the operator's wall clock, not the agent's
  work: it counts every minute spent reading, and a wave left claimed overnight
  would bank tens of EU for nothing. So the aggregator sums each TURN's span
  instead (:func:`_turn_spans_ms`) -- a turn runs from an operator prompt to the
  row before the next one -- and counts nothing between turns. Everything inside
  a turn is agent runtime, including the minutes it sits blocked on a tool it
  called; nothing outside one is. Both measures grow monotonically with the
  transcript, so their maximum is monotonic too and a later capture never reports
  a smaller duration than an earlier one -- which keeps the close-time delta
  against the claim-time baseline non-negative without a re-origin.

Every read fails open: a missing, unreadable, or usage-free transcript yields
``None`` so the Stop hook stays non-blocking.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)

#: Stable harness id stamped on counters aggregated from a Claude transcript.
_HARNESS_ID = "claude-code"

#: Which definition of "duration" this module currently computes. Bump it whenever
#: that definition changes, because a counter is only comparable against a baseline
#: taken under the same definition -- the daemon re-origins a wave whose baseline
#: carries a different version rather than differencing two incompatible measures.
#:
#: 1 = whole-session wall-clock span (the operator's clock, idle included).
#: 2 = summed gaps between rows, dropping gaps over a 15-minute ceiling.
#: 3 = summed per-TURN spans: everything inside a turn, nothing between turns.
#:     (Still the wall clock: a turn contains the stall at a tool-permission
#:     prompt, where the agent waits on a human -- 12.9 hours of it, once.)
#: 4 = Claude's own `turn_duration` per completed turn, which excludes that stall.
MEASURE_VERSION: int = 4

#: Optional override for the Claude projects root, used by tests to redirect
#: transcript lookups away from the real ``~/.claude/`` tree.
_PROJECTS_DIR_ENV = "EAWF_CLAUDE_PROJECTS_DIR"


@dataclass
class _TokenTally:
    """Running per-class token totals aggregated across transcript rows."""

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_5m_input_tokens: int = 0
    cache_creation_1h_input_tokens: int = 0

    @property
    def total(self) -> int:
        """Return the summed token count across every class."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


def projects_root() -> Path:
    """Return the Claude Code projects root, honouring the test override."""
    override = os.environ.get(_PROJECTS_DIR_ENV)
    if override:
        return Path(override)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    root = Path(config_dir) if config_dir else Path.home() / ".claude"
    return root / "projects"


def _project_slug(cwd: Path) -> str:
    """Return the Claude projects directory name for *cwd*.

    Claude Code names each project directory after the working directory with
    every non-alphanumeric character replaced by ``-`` (so ``/workspace/repo``
    becomes ``-workspace-repo``).
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def transcript_path_for_session(session_id: str, *, cwd: Path | None = None) -> Path | None:
    """Return the session-transcript path for *session_id*, or ``None``.

    Resolves the per-project transcript directory from *cwd* first (the common
    case: the claiming session runs inside the repo), then falls back to a glob
    across every project directory so a session started from a different working
    directory -- a worktree, say -- still resolves.

    Args:
        session_id: The Claude Code session id (a UUID) whose transcript is
            wanted.
        cwd: Working directory the session ran in. ``None`` skips the
            direct-path probe and goes straight to the glob.

    Returns:
        The transcript path when a readable file exists, else ``None``.
    """
    if not session_id:
        return None
    root = projects_root()
    if cwd is not None:
        direct = root / _project_slug(cwd) / f"{session_id}.jsonl"
        if direct.is_file():
            return direct
    try:
        matches = sorted(root.glob(f"*/{session_id}.jsonl"))
    except OSError as exc:
        logger.debug(f"transcript_path_for_session session={session_id!r} err={exc!r}")
        return None
    return matches[0] if matches else None


def _read_rows(transcript_path: Path) -> list[dict[str, Any]]:
    """Return decoded transcript rows, skipping malformed lines."""
    rows: list[dict[str, Any]] = []
    with transcript_path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
    return rows


def _usage_key(row: dict[str, Any], message: dict[str, Any]) -> str | None:
    """Return the dedupe key for a usage-carrying row.

    A single assistant message is appended once per content block with the same
    usage repeated, so the message id (falling back to the request id, then the
    row uuid) identifies the *billed call* rather than the row.
    """
    for candidate in (message.get("id"), row.get("requestId"), row.get("uuid")):
        if isinstance(candidate, str) and candidate:
            return candidate
    return None


def _non_negative_int(raw: Any) -> int:
    """Return *raw* when it is a non-negative JSON integer, else ``0``."""
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        return 0
    return raw


def _add_usage(tally: _TokenTally, usage: dict[str, Any]) -> None:
    """Fold one message's usage block into *tally*."""
    tally.input_tokens += _non_negative_int(usage.get("input_tokens"))
    tally.output_tokens += _non_negative_int(usage.get("output_tokens"))
    tally.cache_creation_input_tokens += _non_negative_int(usage.get("cache_creation_input_tokens"))
    tally.cache_read_input_tokens += _non_negative_int(usage.get("cache_read_input_tokens"))
    cache_creation = usage.get("cache_creation")
    if isinstance(cache_creation, dict):
        tally.cache_creation_5m_input_tokens += _non_negative_int(
            cache_creation.get("ephemeral_5m_input_tokens")
        )
        tally.cache_creation_1h_input_tokens += _non_negative_int(
            cache_creation.get("ephemeral_1h_input_tokens")
        )


def _row_timestamp(row: dict[str, Any]) -> datetime | None:
    """Return the row's ISO-8601 ``timestamp`` as a datetime, else ``None``."""
    raw = row.get("timestamp")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def _is_operator_prompt(row: dict[str, Any]) -> bool:
    """Return whether *row* is a fresh operator prompt -- the start of a turn.

    Claude Code writes ``type: "user"`` rows for two different things: the
    operator typing (text content), and a tool handing its result back mid-turn
    (``tool_result`` content). Only the former starts a turn.
    """
    if row.get("type") != "user":
        return False
    content = (row.get("message") or {}).get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        return any(isinstance(block, dict) and block.get("type") == "text" for block in content)
    return False


def _price(model: str | None, tally: _TokenTally) -> Decimal | None:
    """Return the token-derived cost for *tally* under *model*, or ``None``.

    An unknown or unpriced model yields ``None`` so the counters still capture
    duration and tokens with a null cost rather than reporting a fabricated
    ``$0``.
    """
    if model is None:
        return None
    from eawf.runtime.runtimes.metering import price_token_counts

    # The TTL split only appears on newer transcript rows; when it is absent the
    # whole cache-creation tally prices at the 5-minute rate rather than
    # vanishing from the bill.
    split_total = tally.cache_creation_5m_input_tokens + tally.cache_creation_1h_input_tokens
    unsplit = max(0, tally.cache_creation_input_tokens - split_total)
    return price_token_counts(
        model,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
        cache_creation_5m_input_tokens=tally.cache_creation_5m_input_tokens + unsplit,
        cache_creation_1h_input_tokens=tally.cache_creation_1h_input_tokens,
        cache_read_input_tokens=tally.cache_read_input_tokens,
    )


@dataclass
class _TranscriptScan:
    """What one pass over the transcript rows yields.

    Attributes:
        tally: Per-class token totals, deduplicated by billed message.
        duration_ms: Agent working time so far -- the larger of the turn-duration
            sum and the idle-filtered row-gap sum (both monotonic, see the module
            docstring).
        turn_duration_ms: The turn-duration sum alone, kept for the debug log so
            a zero (the Stop-hook race) is visible.
        model: The last billed model id seen, or ``None``.
        messages: How many distinct billed messages the tally covers.
    """

    tally: _TokenTally
    duration_ms: int
    turn_duration_ms: int
    model: str | None
    messages: int


def _scan_rows(rows: list[dict[str, Any]]) -> _TranscriptScan:
    """Fold the transcript rows into token, duration, and attribution totals."""
    tally = _TokenTally()
    seen: set[str] = set()
    turn_duration_ms = 0
    model: str | None = None
    for row in rows:
        turn_duration_ms += _non_negative_int(row.get("durationMs"))
        message = row.get("message")
        if not isinstance(message, dict):
            continue
        model_id = message.get("model")
        if isinstance(model_id, str) and model_id:
            model = model_id
        usage = message.get("usage")
        if not isinstance(usage, dict):
            continue
        key = _usage_key(row, message)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        _add_usage(tally, usage)

    # Claude measures each completed turn itself and writes it as a `turn_duration`
    # row, and -- crucially -- its figure EXCLUDES the time the agent spent waiting
    # on the OPERATOR to approve a tool. Nothing derivable from the transcript can
    # make that distinction: an approval stall and a long-running tool have the
    # identical shape (assistant `tool_use` ... `tool_result`), and one such stall
    # in this repo's own history was 12.9 hours of sleep. So read Claude's number
    # instead of re-deriving one. Twice now, a derived measure has turned out to be
    # the wall clock wearing a disguise.
    #
    # There is deliberately NO span fallback. Spanning the in-flight turn would buy
    # one capture of extra coverage and cost both defects back: it charges that
    # turn's approval stalls, and it makes the duration DROP when Claude's smaller
    # figure lands, which reads as a counter reset and re-origins the wave. Nothing
    # is lost by waiting: the row arrives before the next capture, so the turn is
    # counted then. An unmeasured turn reports zero rather than a guess.
    return _TranscriptScan(
        tally=tally,
        duration_ms=turn_duration_ms,
        turn_duration_ms=turn_duration_ms,
        model=model,
        messages=len(seen),
    )


def aggregate_transcript_counters(transcript_path: Path | str | None) -> RuntimeCounters | None:
    """Aggregate a Claude session transcript into cumulative runtime counters.

    Args:
        transcript_path: Path to the session JSONL, typically read off the Stop
            hook payload's ``transcript_path``. ``None`` short-circuits.

    Returns:
        :class:`RuntimeCounters` stamped ``harness="claude-code"`` carrying the
        session's cumulative agent working time (see :func:`_turn_spans_ms` --
        the summed spans of its turns, NOT the session's wall-clock span),
        per-class token tallies, billed model id, and the token-derived
        ``cost_usd``. The transcript splits no
        model-API duration out of the total, so both ``api_duration_ms`` and
        ``total_duration_ms`` carry that one duration -- the same convention the
        headless spawn snapshot uses, which keeps the default API-duration EU
        basis working on the interactive path. ``None`` when the transcript is
        absent, unreadable, or carries no usable counter (neither a duration nor
        a token tally), so the caller degrades rather than capturing an empty
        snapshot.
    """
    if transcript_path is None:
        return None
    path = Path(transcript_path)
    try:
        rows = _read_rows(path)
    except OSError as exc:
        logger.debug(f"aggregate_transcript_counters path={path.name!r} err={exc!r}")
        return None

    scan = _scan_rows(rows)
    tally = scan.tally
    model = scan.model
    duration_ms = scan.duration_ms
    if duration_ms == 0 and tally.total == 0:
        return None

    cost_usd = _price(model, tally)
    counters = RuntimeCounters(
        measure_version=MEASURE_VERSION,
        api_duration_ms=duration_ms,
        total_duration_ms=duration_ms,
        cost_usd=cost_usd,
        input_tokens=tally.input_tokens,
        output_tokens=tally.output_tokens,
        cache_creation_input_tokens=tally.cache_creation_input_tokens,
        cache_read_input_tokens=tally.cache_read_input_tokens,
        harness=_HARNESS_ID,
        model=model,
    )
    logger.debug(
        f"aggregate_transcript_counters rows={len(rows)} messages={scan.messages} "
        f"duration_ms={duration_ms} turn_duration_ms={scan.turn_duration_ms} "
        f"tokens={tally.total} model={model!r}"
    )
    return counters


__all__ = [
    "MEASURE_VERSION",
    "aggregate_transcript_counters",
    "projects_root",
    "transcript_path_for_session",
]
