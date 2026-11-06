"""Context generations, reasoning sizing, origin provenance and the opaque-blob guard.

Every transcript is a synthetic fixture under ``tmp_path``, read through the
session-history reader the way the analysis reads it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from eawf.observability.reflect.generations import (
    OPAQUE_BLOB,
    GenerationBlock,
    OpaqueBlobError,
    SessionContext,
    SizeBasis,
    refuse_opaque_blob,
    session_context,
)
from eawf.observability.reflect.preamble import InstructionOrigin, ReconstructionBasis
from eawf.observability.telemetry.sources.session_history import (
    HistoryFile,
    SessionDetail,
    read_session_detail,
)

_SIGNATURE = "s" * 1_600
_ENCRYPTED = "Ab3d" * 100


def _ts(second: int) -> str:
    return f"2026-09-01T10:{second // 60:02d}:{second % 60:02d}Z"


def _user(second: int, content: object) -> dict[str, Any]:
    return {
        "type": "user",
        "sessionId": "synthetic-claude",
        "version": "2.1.235",
        "cwd": "/synthetic/project",
        "timestamp": _ts(second),
        "message": {"content": content},
    }


def _assistant(
    second: int, message_id: str, output: int, content: list[dict[str, Any]]
) -> dict[str, Any]:
    return {
        "type": "assistant",
        "sessionId": "synthetic-claude",
        "version": "2.1.235",
        "timestamp": _ts(second),
        "message": {
            "id": message_id,
            "model": "claude-opus-4-7",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "output_tokens": output,
                "cache_read_input_tokens": 30,
                "cache_creation_input_tokens": 40,
            },
        },
    }


def _text() -> dict[str, Any]:
    return {"type": "text", "text": "x" * 40}


def _thinking(signature: str = _SIGNATURE) -> dict[str, Any]:
    return {"type": "thinking", "thinking": "", "signature": signature}


def _claude_rows(*, plain_messages: int = 3) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = [_user(0, "Do some work")]
    for index in range(plain_messages):
        rows.append(_assistant(1 + index, f"plain-{index}", 30, [_text()]))
    base = 1 + plain_messages
    # One message is written as one row per content block; only the last row
    # carries the completed output count.
    rows.append(_assistant(base, "short-signature", 5, [_thinking()]))
    rows.append(_assistant(base + 1, "short-signature", 130, [_text()]))
    rows.append(_assistant(base + 2, "long-signature", 5, [_thinking("s" * 16_000)]))
    rows.append(_assistant(base + 3, "long-signature", 130, [_text()]))
    rows.append(
        {
            "type": "system",
            "subtype": "compact_boundary",
            "sessionId": "synthetic-claude",
            "timestamp": _ts(base + 4),
            "compactMetadata": {"trigger": "auto", "preTokens": 1234},
        }
    )
    rows.append(_assistant(base + 5, "after", 30, [_text()]))
    return rows


def _codex_rows() -> list[dict[str, Any]]:
    usage = {
        "input_tokens": 100,
        "cached_input_tokens": 40,
        "output_tokens": 10,
        "reasoning_output_tokens": 4,
        "total_tokens": 110,
    }
    return [
        {
            "timestamp": _ts(0),
            "type": "session_meta",
            "payload": {
                "id": "synthetic-codex",
                "cwd": "/synthetic/project",
                "cli_version": "0.50.1",
                "base_instructions": "be terse",
            },
        },
        {"timestamp": _ts(1), "type": "turn_context", "payload": {"model": "gpt-6.1-sol"}},
        {
            "timestamp": _ts(2),
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "project rules"},
        },
        {
            "timestamp": _ts(3),
            "type": "event_msg",
            "payload": {"type": "user_message", "message": "go"},
        },
        {
            "timestamp": _ts(4),
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": _ENCRYPTED},
        },
        {
            "timestamp": _ts(5),
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {"total_token_usage": usage, "last_token_usage": usage},
            },
        },
        {
            "timestamp": _ts(6),
            "type": "response_item",
            "payload": {"type": "reasoning", "encrypted_content": _ENCRYPTED},
        },
    ]


def _detail(tmp_path: Path, runtime: str, rows: list[dict[str, Any]]) -> SessionDetail:
    path = tmp_path / runtime / "session.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"{json.dumps(row)}\n" for row in rows), encoding="utf-8")
    detail = read_session_detail(HistoryFile(runtime, path))  # type: ignore[arg-type]
    assert detail is not None
    return detail


def _blocks(context: SessionContext) -> list[GenerationBlock]:
    return [block for generation in context.generations for block in generation.blocks]


def _reasoning(context: SessionContext) -> list[GenerationBlock]:
    return [block for block in _blocks(context) if block.kind == "assistant_reasoning"]


# ---- generations ----------------------------------------------------------------------


def test_session_context_splits_generations_at_each_compaction(tmp_path: Path) -> None:
    context = session_context(_detail(tmp_path, "claude", _claude_rows()))

    first, second = context.generations
    assert context.compaction_count == 1
    assert (first.summary.index, first.summary.reason) == (1, "before compaction 1")
    assert (second.summary.index, second.summary.reason) == (2, "after compaction 1")
    assert (first.summary.requests, second.summary.requests) == (5, 1)
    assert first.summary.final_request_input == 80
    assert first.summary.reconciliation_delta == 80 - first.summary.appended_tokens
    assert first.summary.appended_tokens == sum(first.summary.tokens_by_kind.values())
    assert context.peak_request_input == 80
    ordinals = [block.ordinal for block in _blocks(context)]
    assert ordinals == list(range(1, len(ordinals) + 1))


def test_session_context_marks_memory_unknown_when_none_was_read(tmp_path: Path) -> None:
    detail = _detail(tmp_path, "claude", _claude_rows())

    unknown = session_context(detail, memory_files=None)
    known = session_context(detail, memory_files=(("AGENTS.md", 1_000),))

    assert "preamble_memory_files_unknown" in unknown.limitations
    assert "preamble_memory_files_unknown" not in known.limitations
    origins = {block.origin for block in _blocks(unknown)}
    assert InstructionOrigin.MEMORY_FILE not in origins
    assert InstructionOrigin.MEMORY_FILE in {block.origin for block in _blocks(known)}


def test_session_context_of_an_uncompacted_session_is_the_current_context(tmp_path: Path) -> None:
    rows = [row for row in _claude_rows() if row.get("type") != "system"]

    context = session_context(_detail(tmp_path, "claude", rows))

    (only,) = context.generations
    assert only.summary.reason == "current context"
    assert context.compaction_count == 0


def test_session_context_opens_a_claude_session_with_the_rebuilt_preamble(tmp_path: Path) -> None:
    context = session_context(
        _detail(tmp_path, "claude", _claude_rows()), memory_files=(("AGENTS.md", 1_000),)
    )

    head = context.generations[0].blocks[:3]
    assert [block.origin for block in head] == [
        InstructionOrigin.BUILTIN_SYSTEM_PROMPT,
        InstructionOrigin.BUILTIN_TOOL_SCHEMA,
        InstructionOrigin.MEMORY_FILE,
    ]
    assert all(block.size_basis is SizeBasis.RECONSTRUCTED for block in head)
    assert all(block.request_ordinal == 0 for block in head)
    check = context.generations[0].summary.preamble_reconciliation
    assert check is not None
    assert check.reconstructed_tokens == 4_300 + 24_500 + 1_000
    assert check.residual_tokens == 80 - 3
    assert context.generations[1].summary.preamble_reconciliation is None
    assert "preamble_tool_schema_not_itemised" in context.limitations


def test_session_context_discloses_an_unmeasured_version(tmp_path: Path) -> None:
    rows = _claude_rows()
    for row in rows:
        row.pop("version", None)

    context = session_context(_detail(tmp_path, "claude", rows))

    assert "preamble_version_unmeasured" in context.limitations
    assert context.generations[0].summary.preamble_reconciliation is None


def test_session_context_of_an_empty_session_has_no_generation() -> None:
    detail = SessionDetail(
        vendor_session_ref=f"vsid-{0:032x}",
        runtime="codex",
        cwd_digest=None,
        steps=(),
        tool_spans={},
        blocks=(),
        compactions=(),
    )

    context = session_context(detail)

    assert context.generations == ()
    assert context.peak_request_input is None
    assert context.compaction_count == 0


def test_session_context_rejects_a_negative_memory_estimate(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="token estimate"):
        session_context(_detail(tmp_path, "claude", _claude_rows()), memory_files=(("A.md", -1),))


# ---- the reasoning-signature guard ----------------------------------------------------


def test_claude_reasoning_ignores_signature_length(tmp_path: Path) -> None:
    context = session_context(_detail(tmp_path, "claude", _claude_rows()))

    short, long = _reasoning(context)
    # Each request output 130 less a 17-token text block, less the 13-token
    # residual its three plain requests measure as bias.
    assert context.reasoning_bias_offset == 13
    assert short.tokens == long.tokens == 100
    assert short.size_basis is SizeBasis.OUTPUT_RESIDUAL


def test_claude_reasoning_without_a_bias_sample_says_so(tmp_path: Path) -> None:
    context = session_context(_detail(tmp_path, "claude", _claude_rows(plain_messages=1)))

    assert context.reasoning_bias_offset is None
    assert "reasoning_bias_offset_unavailable" in context.limitations
    assert [block.tokens for block in _reasoning(context)] == [113, 113]


def test_claude_reasoning_spreads_one_request_over_its_blocks(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = [_user(0, "go")]
    rows += [_assistant(1 + i, f"plain-{i}", 17, [_text()]) for i in range(3)]
    rows += [_assistant(5, "split", 5, [_thinking()]) for _ in range(3)]
    rows.append(_assistant(6, "split", 24, [_text()]))

    context = session_context(_detail(tmp_path, "claude", rows))

    assert [block.tokens for block in _reasoning(context)] == [3, 2, 2]


def test_claude_reasoning_floors_at_zero(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = [_user(0, "go")]
    rows += [_assistant(1 + i, f"plain-{i}", 60, [_text()]) for i in range(3)]
    rows += [_assistant(5, "thin", 5, [_thinking()]), _assistant(6, "thin", 20, [_text()])]

    context = session_context(_detail(tmp_path, "claude", rows))

    assert [block.tokens for block in _reasoning(context)] == [0]


def test_codex_reasoning_is_the_provider_counter(tmp_path: Path) -> None:
    context = session_context(_detail(tmp_path, "codex", _codex_rows()))

    counted, unbilled = _reasoning(context)
    assert (counted.tokens, counted.size_basis) == (4, SizeBasis.PROVIDER_COUNTER)
    assert (unbilled.tokens, unbilled.size_basis) == (None, SizeBasis.UNAVAILABLE)
    assert context.reasoning_bias_offset is None
    assert all(block.basis is None for block in _blocks(context))


# ---- the singular-origin invariant ----------------------------------------------------


def test_no_origin_carries_both_recorded_and_reconstructed_blocks(tmp_path: Path) -> None:
    claude = session_context(
        _detail(tmp_path, "claude", _claude_rows()), memory_files=(("AGENTS.md", 1_000),)
    )
    codex = session_context(_detail(tmp_path, "codex", _codex_rows()))

    bases: dict[InstructionOrigin, set[bool]] = {}
    for context in (claude, codex):
        for block in _blocks(context):
            if block.origin is not None:
                bases.setdefault(block.origin, set()).add(block.basis is not None)

    assert {origin: kinds for origin, kinds in bases.items() if len(kinds) > 1} == {}
    # Both sides must be present, or the check passes on an empty set.
    assert bases[InstructionOrigin.SESSION_BASE] == {False}
    assert bases[InstructionOrigin.DEVELOPER_PROJECT] == {False}
    assert bases[InstructionOrigin.BUILTIN_SYSTEM_PROMPT] == {True}
    assert bases[InstructionOrigin.MEMORY_FILE] == {True}


@pytest.mark.parametrize(
    ("origin", "basis"),
    [
        (InstructionOrigin.MEMORY_FILE, None),
        (InstructionOrigin.SESSION_BASE, ReconstructionBasis.VERSION_TABLE),
        (None, ReconstructionBasis.READ_FROM_DISK_NOW),
    ],
)
def test_generation_block_refuses_a_conflated_origin(
    origin: InstructionOrigin | None, basis: ReconstructionBasis | None
) -> None:
    with pytest.raises(ValueError, match="does not match basis"):
        GenerationBlock(
            ordinal=1,
            kind="system",
            lane="main",
            request_ordinal=0,
            tokens=1,
            size_basis=SizeBasis.CHARS,
            origin=origin,
            basis=basis,
        )


# ---- the opaque-blob guard ------------------------------------------------------------


def test_no_serialized_row_holds_an_opaque_blob(tmp_path: Path) -> None:
    for runtime, rows in (("claude", _claude_rows()), ("codex", _codex_rows())):
        text = session_context(_detail(tmp_path, runtime, rows)).model_dump_json()

        assert refuse_opaque_blob(text) == text
        assert OPAQUE_BLOB.search(text) is None
        assert _SIGNATURE[:200] not in text
        assert _ENCRYPTED not in text


def test_refuse_opaque_blob_at_the_floor() -> None:
    assert refuse_opaque_blob("Ab3d" * 49 + "Abc") == "Ab3d" * 49 + "Abc"
    with pytest.raises(OpaqueBlobError, match="200 chars"):
        refuse_opaque_blob('{"ref": "' + "Ab3d" * 50 + '"}')


def test_refuse_opaque_blob_never_quotes_the_blob() -> None:
    blob = "Ab3d" * 60

    with pytest.raises(OpaqueBlobError) as raised:
        refuse_opaque_blob(f'{{"signature": "{blob}"}}')

    assert blob not in str(raised.value)


def test_refuse_opaque_blob_admits_the_ids_a_row_carries() -> None:
    text = json.dumps({"session_ref": f"vsid-{'a' * 32}", "key": "0" * 64, "text": ""})

    assert refuse_opaque_blob(text) == text
