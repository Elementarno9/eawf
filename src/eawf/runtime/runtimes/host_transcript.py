"""The transcript bridge: a host subagent's own transcript, read as Run event payloads.

A subagent the host harness spawned writes its conversation to a transcript file the
harness owns. Eawf never sees that conversation stream live, so when the subagent
stops, this module reads the file the stop hook points at and turns what the subagent
said into the typed payloads a Run's stream carries: one message payload per user or
assistant message, and one child-run payload per delegation the subagent itself made,
drawn as work elsewhere rather than inlined.

What is left out is left out on purpose. Hidden reasoning is never bridged, a tool
call's input and output are not the Run's words, and a harness-injected developer
prompt is instructions rather than conversation. A message too long for one block is
cut and says so, and a message carrying a path, an address or a token shape is
withheld whole: the run ledger refuses those shapes, and a transcript line must not be
the way one reaches it.

Reading is fail-open. A missing file, an unreadable one or a malformed line yields
fewer payloads, never an exception, because a stop hook that raises breaks the
operator's session over a record that is only an observation.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any, Final, Literal

from eawf.kernel.runtime.events import ChildRunPayload, MessageSummaryPayload
from eawf.observability.logging.state_leak import default_allowed_emails, scan_state_leaks

logger = logging.getLogger(__name__)

#: The host harnesses whose subagent transcripts this bridge reads.
HostHarness = Literal["claude-code", "codex"]

#: The longest message a block holds; the transcript's bounded text.
SUMMARY_LIMIT: Final = 500

#: What a withheld message says in place of its text.
WITHHELD_TEXT: Final = "this message carries a path, an address or a token shape and is withheld"

#: The host tools that spawn a subagent of their own.
_DELEGATION_TOOLS: Final = frozenset({"Task", "Agent"})

#: How many hex characters of a digest name a delegation request.
_DELEGATION_DIGEST_CHARS: Final = 32

BridgedPayload = MessageSummaryPayload | ChildRunPayload


def _records(path: Path) -> Iterator[Mapping[str, Any]]:
    """Yield each well-formed JSON object line of *path*, skipping everything else."""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    yield record
    except OSError as exc:
        logger.info(f"host transcript unreadable name={path.name} error={exc!r}")


def _message(
    role: Literal["user", "assistant"], text: str, *, allowed: frozenset[str]
) -> MessageSummaryPayload | None:
    """Return the message payload for *text*, cut or withheld as it must be."""
    stripped = text.strip()
    if not stripped:
        return None
    if scan_state_leaks(stripped, allowed_emails=allowed):
        return MessageSummaryPayload(message_role=role, summary=WITHHELD_TEXT, truncated=True)
    cut = stripped[:SUMMARY_LIMIT].strip()
    return MessageSummaryPayload(
        message_role=role, summary=cut, truncated=len(stripped) > SUMMARY_LIMIT
    )


def scrubbed_words(text: str, *, limit: int = SUMMARY_LIMIT) -> str | None:
    """Return *text* as a record may hold it: cut to *limit*, or withheld whole.

    Args:
        text: What the host reported, in its own words.
        limit: The most characters the record holds.

    Returns:
        The stripped words cut to *limit*; :data:`WITHHELD_TEXT` when they carry a
        path, an address or a token shape; ``None`` when there are none.
    """
    stripped = text.strip()
    if not stripped:
        return None
    if scan_state_leaks(stripped, allowed_emails=default_allowed_emails()):
        return WITHHELD_TEXT[:limit]
    return stripped[:limit].strip()


def _delegation(harness: HostHarness, call_id: str) -> ChildRunPayload:
    """Return the requested-phase payload for one spawn call the subagent made."""
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:_DELEGATION_DIGEST_CHARS]
    return ChildRunPayload(
        child_run_ref=None,
        delegation_request_ref=f"delegation://{harness}/{digest}",
        phase="requested",
    )


def _claude_payloads(
    record: Mapping[str, Any], *, allowed: frozenset[str]
) -> Iterator[BridgedPayload]:
    """Yield the payloads one Claude Code transcript line carries."""
    role = record.get("type")
    message = record.get("message")
    if role not in {"user", "assistant"} or not isinstance(message, dict):
        return
    content = message.get("content")
    if isinstance(content, str):
        bridged = _message(role, content, allowed=allowed)
        if bridged is not None:
            yield bridged
        return
    if not isinstance(content, list):
        return
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    bridged = _message(role, "\n".join(texts), allowed=allowed)
    if bridged is not None:
        yield bridged
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") in _DELEGATION_TOOLS
            and isinstance(block.get("id"), str)
            and block["id"]
        ):
            yield _delegation("claude-code", block["id"])


def _codex_payloads(
    record: Mapping[str, Any], *, allowed: frozenset[str]
) -> Iterator[BridgedPayload]:
    """Yield the payloads one Codex rollout line carries."""
    payload = record.get("payload")
    if record.get("type") != "response_item" or not isinstance(payload, dict):
        return
    role = payload.get("role")
    content = payload.get("content")
    if payload.get("type") != "message" or role not in {"user", "assistant"}:
        return
    if not isinstance(content, list):
        return
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") in {"input_text", "output_text"}
        and isinstance(block.get("text"), str)
    ]
    bridged = _message(role, "\n".join(texts), allowed=allowed)
    if bridged is not None:
        yield bridged


def claude_record_payloads(record: Mapping[str, Any]) -> tuple[BridgedPayload, ...]:
    """Return the payloads one Claude Code conversation line carries.

    A stream-json line of a headless ``claude`` turn has the shape of a transcript line,
    so the same reading serves a Run Eawf started itself.

    Args:
        record: One decoded JSON line.

    Returns:
        Its message and delegation payloads, cut or withheld as a transcript line's are.
    """
    return tuple(_claude_payloads(record, allowed=default_allowed_emails()))


def read_host_transcript(path: Path, *, harness: HostHarness) -> tuple[BridgedPayload, ...]:
    """Return what a host subagent said and delegated, in transcript order.

    Args:
        path: The subagent's own transcript, as the stop hook named it.
        harness: The harness that wrote it, which fixes how a line is read.

    Returns:
        One payload per bridged message or delegation; empty when the file is
        missing, unreadable or holds nothing the Run said.
    """
    reader = _claude_payloads if harness == "claude-code" else _codex_payloads
    allowed = default_allowed_emails()
    bridged = tuple(
        payload for record in _records(path) for payload in reader(record, allowed=allowed)
    )
    logger.info(f"read_host_transcript harness={harness} payloads={len(bridged)}")
    return bridged


__all__ = [
    "SUMMARY_LIMIT",
    "WITHHELD_TEXT",
    "BridgedPayload",
    "HostHarness",
    "claude_record_payloads",
    "read_host_transcript",
    "scrubbed_words",
]
