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
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import Field

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

#: The host tool that puts a question to the person at the session.
_QUESTION_TOOL: Final = "AskUserQuestion"

#: How a transcript bears out a reported question and answer.
TranscriptCheck = Literal["matched", "tool_use_only", "absent", "mismatch"]


class SpokenMessage(MessageSummaryPayload):
    """A message payload that also carries the whole of what was said.

    The whole text never reaches a stream line: it is excluded from every dump, so the
    payload serialises exactly as the summary it extends. Only the dispatch sink reads
    it, to keep the full message in the machine-local transcript store.

    Attributes:
        text: The message, stripped but neither cut nor withheld.
    """

    text: str = Field(exclude=True, repr=False)


BridgedPayload = MessageSummaryPayload | ChildRunPayload


def _records(path: Path, *, containing: str | None = None) -> Iterator[Mapping[str, Any]]:
    """Yield each well-formed JSON object line of *path*, skipping everything else.

    Args:
        path: The JSONL file to read.
        containing: When set, only lines holding this text are decoded at all; a
            transcript runs to tens of megabytes, and decoding every line to find the
            few that name one call is most of the cost of reading it.
    """
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if containing is not None and containing not in line:
                    continue
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
        return SpokenMessage(
            message_role=role, summary=WITHHELD_TEXT, truncated=True, text=stripped
        )
    cut = stripped[:SUMMARY_LIMIT].strip()
    return SpokenMessage(
        message_role=role, summary=cut, truncated=len(stripped) > SUMMARY_LIMIT, text=stripped
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


def _canonical(value: object) -> str:
    """Return *value* as sorted-key JSON, so tuples and lists of equal items compare equal."""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _asked(record: Mapping[str, Any], tool_use_id: str) -> Mapping[str, Any] | None:
    """Return the ``tool_use`` block with *tool_use_id* an assistant line carries, if any."""
    message = record.get("message")
    if record.get("type") != "assistant" or not isinstance(message, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("id") == tool_use_id
        ):
            return block
    return None


def _answered(record: Mapping[str, Any], tool_use_id: str) -> Mapping[str, Any] | None:
    """Return the ``toolUseResult`` of the user line answering *tool_use_id*, if any."""
    message = record.get("message")
    result = record.get("toolUseResult")
    if record.get("type") != "user" or not isinstance(message, dict):
        return None
    if not isinstance(result, dict):
        return None
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in content:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_result"
            and block.get("tool_use_id") == tool_use_id
        ):
            return result
    return None


def question_exchange(
    transcript_path: Path,
    tool_use_id: str,
    questions: Sequence[Mapping[str, Any]],
    answers: Mapping[str, str],
) -> TranscriptCheck:
    """Return how a Claude Code transcript bears out one reported AskUserQuestion answer.

    The harness's hook reports what was asked and answered; the transcript is the
    harness's own record of the same call. Agreement does not prove a person answered,
    since the same user can write both, but disagreement proves the report is not the
    call the transcript holds. A question asked with its answers already in the tool
    input was never put to anyone, so it disagrees whatever the answers say.

    Args:
        transcript_path: The session transcript the hook named.
        tool_use_id: The harness's id for the question call.
        questions: The questions the hook reported, as the tool input carried them.
        answers: The answers the hook reported, keyed by question text.

    Returns:
        ``matched`` when the transcript holds an AskUserQuestion call with this id and
        these questions, and a result with these answers; ``tool_use_only`` when the
        call is there and its result is not yet; ``absent`` when the file cannot be
        read or holds no call with this id; ``mismatch`` when the call is another tool,
        carries pre-supplied answers, or its questions or answers differ.
    """
    asked: Mapping[str, Any] | None = None
    answered: Mapping[str, Any] | None = None
    for record in _records(transcript_path, containing=tool_use_id):
        if asked is None:
            asked = _asked(record, tool_use_id)
        if answered is None:
            answered = _answered(record, tool_use_id)
    check = _exchange_check(asked, answered, questions=questions, answers=answers)
    logger.info(f"question_exchange name={transcript_path.name} check={check}")
    return check


def _exchange_check(
    asked: Mapping[str, Any] | None,
    answered: Mapping[str, Any] | None,
    *,
    questions: Sequence[Mapping[str, Any]],
    answers: Mapping[str, str],
) -> TranscriptCheck:
    """Return the verdict on the call and result found against what the hook reported."""
    if asked is None:
        return "absent"
    tool_input = asked.get("input")
    if (
        asked.get("name") != _QUESTION_TOOL
        or not isinstance(tool_input, dict)
        or "answers" in tool_input
        or _canonical(tool_input.get("questions")) != _canonical(list(questions))
    ):
        return "mismatch"
    if answered is None:
        return "tool_use_only"
    if _canonical(answered.get("answers")) != _canonical(dict(answers)):
        return "mismatch"
    return "matched"


__all__ = [
    "SUMMARY_LIMIT",
    "WITHHELD_TEXT",
    "BridgedPayload",
    "HostHarness",
    "SpokenMessage",
    "TranscriptCheck",
    "claude_record_payloads",
    "question_exchange",
    "read_host_transcript",
    "scrubbed_words",
]
