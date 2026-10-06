"""What a native worker's turn says, read as the typed items a Run's stream carries.

Both launchers hand their Run's stream more than words: each tool the provider runs
itself is stated when it is asked for and when it ends, with the sort of work it does,
and the fact that the model began thinking is stated once per assistant message. What
is never read is as deliberate. A tool's input and output are the provider's, not the
Run's words, and a thinking block's text is hidden reasoning: only the block's presence
is looked at, so no thinking text can reach a line or a store.

A tool keeps the provider's own name, lower-cased so it reads as one grammar across
providers, and its class is a pinned table over those names. A name the table does not
know is ``other``, never refused, because a provider adds tools faster than the table
can name them.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any, Final

from claude_agent_sdk import (
    AssistantMessage,
    Message,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)
from openai_codex.generated.v2_all import (
    CollabAgentToolCallStatus,
    CollabAgentToolCallThreadItem,
    CommandExecutionStatus,
    CommandExecutionThreadItem,
    DynamicToolCallStatus,
    DynamicToolCallThreadItem,
    FileChangeThreadItem,
    ItemCompletedNotification,
    ItemStartedNotification,
    McpToolCallStatus,
    McpToolCallThreadItem,
    PatchApplyStatus,
    ReasoningThreadItem,
    WebSearchThreadItem,
)

from eawf.kernel.runtime.events import (
    MessageSummaryPayload,
    ProviderToolClass,
    ProviderToolPayload,
    ReasoningSummaryPayload,
    TranscriptItem,
)
from eawf.runtime.runtimes.host_transcript import claude_record_payloads

#: The class of each Claude tool the transcript lanes apart from ``other``.
CLAUDE_TOOL_CLASSES: Final[Mapping[str, ProviderToolClass]] = MappingProxyType(
    {
        "Edit": "edit",
        "MultiEdit": "edit",
        "Write": "edit",
        "NotebookEdit": "edit",
        "Bash": "command",
        "Read": "read",
        "Grep": "search",
        "Glob": "search",
        "WebSearch": "search",
        "WebFetch": "search",
        "Task": "delegate",
        "Agent": "delegate",
    }
)

#: The name a Codex command item is stated under.
CODEX_COMMAND_TOOL: Final = "command_execution"

#: The name a Codex file-change (patch) item is stated under.
CODEX_PATCH_TOOL: Final = "file_change"

#: The name a Codex web search item is stated under.
CODEX_WEB_SEARCH_TOOL: Final = "web_search"

#: The class of each Codex tool name the transcript lanes apart from ``other``.
CODEX_TOOL_CLASSES: Final[Mapping[str, ProviderToolClass]] = MappingProxyType(
    {CODEX_PATCH_TOOL: "edit", CODEX_COMMAND_TOOL: "command"}
)

#: The longest tool name the payload holds.
_TOOL_NAME_LIMIT: Final = 64

#: What a name that does not start with a letter is prefixed with.
_TOOL_NAME_PREFIX: Final = "tool_"

#: How many hex characters of a digest name a provider call.
_CALL_DIGEST_CHARS: Final = 16


def provider_tool_name(name: str) -> str:
    """Return *name* in the payload's tool grammar.

    The name is lower-cased and every character outside ``[a-z0-9_]`` becomes ``_``,
    so an ``mcp__`` prefix survives; a name that would not start with a letter is
    prefixed, and the result is cut to the grammar's length.

    Args:
        name: The tool name as the provider spelled it.

    Returns:
        A name matching ``^[a-z][a-z0-9_]{0,63}$``.
    """
    spelled = re.sub(r"[^a-z0-9_]", "_", name.lower())
    if not spelled[:1].isalpha():
        spelled = _TOOL_NAME_PREFIX + spelled
    return spelled[:_TOOL_NAME_LIMIT]


def provider_call_ref(call_id: str) -> str:
    """Return the call reference both phases of one provider call share.

    The provider's own id is digested rather than kept, so its spelling never has to
    fit the call grammar and a replay of the same call names the same reference.
    """
    digest = hashlib.sha256(call_id.encode("utf-8")).hexdigest()[:_CALL_DIGEST_CHARS]
    return f"call-{digest}"


def _requested(call_id: str, name: str, tool_class: ProviderToolClass) -> ProviderToolPayload:
    return ProviderToolPayload(
        call_ref=provider_call_ref(call_id),
        tool_name=provider_tool_name(name),
        tool_class=tool_class,
        phase="requested",
    )


def _result(
    call_id: str, name: str, tool_class: ProviderToolClass, *, ok: bool
) -> ProviderToolPayload:
    return ProviderToolPayload(
        call_ref=provider_call_ref(call_id),
        tool_name=provider_tool_name(name),
        tool_class=tool_class,
        phase="result",
        outcome="ok" if ok else "error",
    )


class ClaudeTranscriptItems:
    """Read one Claude turn's messages as transcript items, remembering what it asked.

    A tool result names only the call it answers, so the name and class of each call
    are held from its request; and the provider repeats one assistant message once per
    content block, so the thinking marker is stated once per message id.
    """

    def __init__(self) -> None:
        """Start a turn with no call asked and no message thought about."""
        self._calls: dict[str, tuple[str, ProviderToolClass]] = {}
        self._thought: set[str] = set()

    def items(self, message: Message) -> tuple[TranscriptItem, ...]:
        """Return the items *message* carries, in the order the turn said them.

        Args:
            message: One message of the turn.

        Returns:
            Its thinking marker, its words, its tool calls and results and the
            delegations they request; empty for a message carrying none.
        """
        if isinstance(message, AssistantMessage):
            return self._assistant(message)
        if isinstance(message, UserMessage):
            return self._user(message)
        return ()

    def _assistant(self, message: AssistantMessage) -> tuple[TranscriptItem, ...]:
        items: list[TranscriptItem] = []
        if any(isinstance(block, ThinkingBlock) for block in message.content):
            # An unidentified message cannot be matched to its repeats, so it is
            # stated each time rather than never.
            key = message.message_id or message.uuid
            if key is None:
                items.append(ReasoningSummaryPayload(phase="started"))
            elif key not in self._thought:
                self._thought.add(key)
                items.append(ReasoningSummaryPayload(phase="started"))
        texts = [
            {"type": "text", "text": block.text}
            for block in message.content
            if isinstance(block, TextBlock)
        ]
        items.extend(_claude_words("assistant", texts))
        for block in message.content:
            if not isinstance(block, ToolUseBlock):
                continue
            tool_class = CLAUDE_TOOL_CLASSES.get(block.name, "other")
            self._calls[block.id] = (block.name, tool_class)
            items.append(_requested(block.id, block.name, tool_class))
            if tool_class == "delegate":
                use = {"type": "tool_use", "id": block.id, "name": block.name}
                items.extend(claude_record_payloads(_record("assistant", [use])))
        return tuple(items)

    def _user(self, message: UserMessage) -> tuple[TranscriptItem, ...]:
        if isinstance(message.content, str):
            return _claude_words("user", message.content)
        items: list[TranscriptItem] = []
        for block in message.content:
            if not isinstance(block, ToolResultBlock):
                continue
            asked = self._calls.get(block.tool_use_id)
            if asked is not None:
                name, tool_class = asked
                items.append(_result(block.tool_use_id, name, tool_class, ok=not block.is_error))
        return tuple(items)


def _record(role: str, content: str | list[dict[str, Any]]) -> dict[str, Any]:
    """Return a transcript line of *role* holding *content*, as the bridge reads one."""
    return {"type": role, "message": {"content": content}}


def _claude_words(
    role: str, content: str | list[dict[str, Any]]
) -> tuple[MessageSummaryPayload, ...]:
    """Return the message *content* says, cut or withheld as a transcript line's is."""
    return tuple(
        payload
        for payload in claude_record_payloads(_record(role, content))
        if isinstance(payload, MessageSummaryPayload)
    )


#: The Codex item statuses that end a call well.
_CODEX_OK: Final = frozenset(
    {
        CommandExecutionStatus.completed,
        PatchApplyStatus.completed,
        McpToolCallStatus.completed,
        DynamicToolCallStatus.completed,
        CollabAgentToolCallStatus.completed,
    }
)

#: A Codex item that is a tool call.
_CodexToolItem = (
    CommandExecutionThreadItem
    | FileChangeThreadItem
    | McpToolCallThreadItem
    | DynamicToolCallThreadItem
    | CollabAgentToolCallThreadItem
    | WebSearchThreadItem
)


def _codex_tool_name(item: _CodexToolItem) -> str:
    """Return the name a Codex tool item is stated under, before the grammar is applied."""
    if isinstance(item, CommandExecutionThreadItem):
        return CODEX_COMMAND_TOOL
    if isinstance(item, FileChangeThreadItem):
        return CODEX_PATCH_TOOL
    if isinstance(item, McpToolCallThreadItem):
        return f"mcp__{item.server}__{item.tool}"
    if isinstance(item, DynamicToolCallThreadItem):
        return f"{item.namespace}__{item.tool}" if item.namespace else item.tool
    if isinstance(item, CollabAgentToolCallThreadItem):
        return item.tool.name
    return CODEX_WEB_SEARCH_TOOL


def _codex_ok(item: _CodexToolItem) -> bool:
    """Return whether a completed Codex tool item ended well."""
    if isinstance(item, WebSearchThreadItem):
        return True
    if item.status not in _CODEX_OK:
        return False
    if isinstance(item, CommandExecutionThreadItem):
        return item.exit_code in (None, 0)
    if isinstance(item, McpToolCallThreadItem):
        return item.error is None
    if isinstance(item, DynamicToolCallThreadItem):
        return item.success is not False
    return True


class CodexTranscriptItems:
    """Read one Codex turn's item notifications as transcript items.

    A tool item is stated as requested when it starts and with its outcome when it
    completes; a reasoning item is stated as a thinking marker once, whichever of its
    notifications arrives first.
    """

    def __init__(self) -> None:
        """Start a turn with no reasoning item seen."""
        self._thought: set[str] = set()

    def items(
        self, note: ItemStartedNotification | ItemCompletedNotification
    ) -> tuple[TranscriptItem, ...]:
        """Return the items one item notification carries.

        Args:
            note: An ``item/started`` or ``item/completed`` notification.

        Returns:
            The thinking marker or the tool phase it states; empty for an item that is
            neither, an agent message included, which the launcher relays itself.
        """
        item = note.item.root
        if isinstance(item, ReasoningThreadItem):
            if item.id in self._thought:
                return ()
            self._thought.add(item.id)
            return (ReasoningSummaryPayload(phase="started"),)
        if not isinstance(item, _CodexToolItem):
            return ()
        name = _codex_tool_name(item)
        tool_class = CODEX_TOOL_CLASSES.get(name, "other")
        if isinstance(note, ItemStartedNotification):
            return (_requested(item.id, name, tool_class),)
        return (_result(item.id, name, tool_class, ok=_codex_ok(item)),)


__all__ = [
    "CLAUDE_TOOL_CLASSES",
    "CODEX_COMMAND_TOOL",
    "CODEX_PATCH_TOOL",
    "CODEX_TOOL_CLASSES",
    "CODEX_WEB_SEARCH_TOOL",
    "ClaudeTranscriptItems",
    "CodexTranscriptItems",
    "provider_call_ref",
    "provider_tool_name",
]
