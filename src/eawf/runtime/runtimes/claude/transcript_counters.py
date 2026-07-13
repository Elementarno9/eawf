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
- **Duration lives off the message.** Wall-clock time is emitted on
  ``type: "system"`` / ``subtype: "turn_duration"`` rows as a top-level
  ``durationMs``, not inside the assistant message.

Every read fails open: a missing, unreadable, or usage-free transcript yields
``None`` so the Stop hook stays non-blocking.
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from eawf.runtime.runtimes.claude.runtime_counters import RuntimeCounters

logger = logging.getLogger(__name__)

#: Stable harness id stamped on counters aggregated from a Claude transcript.
_HARNESS_ID = "claude-code"

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


def aggregate_transcript_counters(transcript_path: Path | str | None) -> RuntimeCounters | None:
    """Aggregate a Claude session transcript into cumulative runtime counters.

    Args:
        transcript_path: Path to the session JSONL, typically read off the Stop
            hook payload's ``transcript_path``. ``None`` short-circuits.

    Returns:
        :class:`RuntimeCounters` stamped ``harness="claude-code"`` carrying the
        session's cumulative duration, per-class token tallies, billed model id,
        and the token-derived ``cost_usd``. The transcript reports turn
        wall-clock only (no model-API split), so both ``api_duration_ms`` and
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

    tally = _TokenTally()
    seen: set[str] = set()
    duration_ms = 0
    model: str | None = None
    for row in rows:
        duration_ms += _non_negative_int(row.get("durationMs"))
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

    if duration_ms == 0 and tally.total == 0:
        return None

    cost_usd = _price(model, tally)
    counters = RuntimeCounters(
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
        f"aggregate_transcript_counters rows={len(rows)} messages={len(seen)} "
        f"duration_ms={duration_ms} tokens={tally.total} model={model!r}"
    )
    return counters


__all__ = [
    "aggregate_transcript_counters",
    "projects_root",
    "transcript_path_for_session",
]
