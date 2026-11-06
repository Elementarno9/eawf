"""The context generations of one session: what the model carried between compactions.

A generation is the run of content blocks and requests between two compaction
boundaries. Each block is sized in tokens and each generation is set against
the input its last request was charged for, so a reader can see what a context
was made of and how far the estimate sits from the provider's count.

Two guards hold here. A reasoning block is never sized from what is on disk:
Claude keeps only an encrypted signature, and a Codex reasoning item carries
encrypted content, so their size measures the signature rather than the
reasoning. Claude reasoning is derived from the request's exact output count
less its visible blocks, less a bias measured on the session's own requests
that did not reason; Codex reasoning is the provider's own counter. And no
serialized row may hold an opaque blob: :func:`refuse_opaque_blob` rejects any
run of 200 or more base64-alphabet characters, the shape a signature takes.
"""

from __future__ import annotations

import logging
import math
import re
import statistics
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.observability.reflect.preamble import (
    CHARS_PER_TOKEN,
    RECONSTRUCTED_ORIGINS,
    InstructionOrigin,
    PreambleReconciliation,
    ReconstructedBlock,
    ReconstructionBasis,
    reconcile_preamble,
    reconstruct_claude_preamble,
)
from eawf.observability.telemetry.models import DurationKind, ObservedRequest
from eawf.observability.telemetry.sources.session_history import (
    BlockKind,
    ContentBlock,
    HistoryRuntime,
    SessionDetail,
)

logger = logging.getLogger(__name__)

#: The shape a reasoning signature takes. The floor sits far above every id a
#: row carries (the longest is a 64-character digest) and far below the
#: shortest signature, which runs to four figures.
OPAQUE_BLOB: Final = re.compile(r"[A-Za-z0-9+/=_-]{200,}")

#: The fewest non-reasoning requests a bias offset is measured from.
REASONING_BIAS_MIN_SAMPLE: Final = 3

_VISIBLE_KINDS: Final[frozenset[BlockKind]] = frozenset({"assistant", "tool_call"})

_RECORDED_ORIGINS: Final[dict[tuple[HistoryRuntime, BlockKind], InstructionOrigin]] = {
    ("codex", "system"): InstructionOrigin.SESSION_BASE,
    ("codex", "project_instruction"): InstructionOrigin.DEVELOPER_PROJECT,
    ("codex", "tool_definition"): InstructionOrigin.TOOL_SCHEMA,
    ("claude", "runtime_injection"): InstructionOrigin.RUNTIME,
}


class OpaqueBlobError(ValueError):
    """A serialized row holds a run long enough to be a reasoning signature."""


class SizeBasis(StrEnum):
    """How a block's token figure was obtained.

    Values:
        CHARS: Its recorded characters over :data:`CHARS_PER_TOKEN`.
        OUTPUT_RESIDUAL: Its request's output count less the visible blocks
            and the session's bias offset.
        PROVIDER_COUNTER: Its request's reasoning counter.
        RECONSTRUCTED: The preamble rebuild; see the block's ``basis``.
        UNAVAILABLE: Nothing on record sizes it.
    """

    CHARS = "chars"
    OUTPUT_RESIDUAL = "output_residual"
    PROVIDER_COUNTER = "provider_counter"
    RECONSTRUCTED = "reconstructed"
    UNAVAILABLE = "unavailable"


class GenerationBlock(BaseModel):
    """One block of a generation, sized in tokens.

    Attributes:
        ordinal: 1-based position in the session, rebuilt preamble first.
        kind: The block's closed kind.
        lane: ``main``, or ``sidechain`` for a Claude sidechain record.
        request_ordinal: Requests billed when the block was written; ``0`` for
            a block ahead of the first request.
        tokens: The block's size, or ``None`` when nothing sizes it.
        size_basis: How ``tokens`` was obtained.
        origin: The instruction origin, or ``None`` for body content.
        basis: How a rebuilt block was obtained; ``None`` on every recorded one.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    ordinal: int = Field(ge=1)
    kind: BlockKind
    lane: str
    request_ordinal: int = Field(ge=0)
    tokens: int | None = Field(ge=0)
    size_basis: SizeBasis
    origin: InstructionOrigin | None = None
    basis: ReconstructionBasis | None = None

    @model_validator(mode="after")
    def _origin_is_singular(self) -> Self:
        """Hold every origin to one provenance: recorded or rebuilt, never both.

        Raises:
            ValueError: A rebuilt origin without a basis, a basis on a recorded
                origin, or a basis with no origin.
        """
        rebuilt = self.origin in RECONSTRUCTED_ORIGINS
        if rebuilt != (self.basis is not None):
            raise ValueError(f"origin {self.origin} does not match basis {self.basis}")
        return self


class GenerationSummary(BaseModel):
    """One generation's totals, without its blocks.

    Attributes:
        index: 1-based position among the session's generations.
        reason: ``current context``, ``before compaction 1`` or
            ``after compaction <n>``.
        requests: Requests billed in the generation.
        appended_tokens: Every sized block summed.
        tokens_by_kind: ``appended_tokens`` split by block kind.
        final_request_input: The last request's whole input, cached included,
            or ``None`` when the generation billed none.
        reconciliation_delta: ``final_request_input`` less ``appended_tokens``.
        preamble_reconciliation: The rebuilt preamble against the first
            request; on the first generation of a rebuilt session only.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    index: int = Field(ge=1)
    reason: str
    requests: int = Field(ge=0)
    appended_tokens: int = Field(ge=0)
    tokens_by_kind: dict[str, int]
    final_request_input: int | None
    reconciliation_delta: int | None
    preamble_reconciliation: PreambleReconciliation | None = None


class ContextGeneration(BaseModel):
    """One generation's totals and its blocks.

    Attributes:
        summary: The totals.
        blocks: The blocks, in order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    summary: GenerationSummary
    blocks: tuple[GenerationBlock, ...]


class SessionContext(BaseModel):
    """What one session's context was made of across its generations.

    Attributes:
        vendor_session_ref: The session's hashed id.
        runtime: The runtime whose history it is.
        generations: The generations, in order.
        compaction_count: Compaction boundaries the runtime recorded.
        peak_request_input: The largest whole input any request was charged
            for, or ``None`` when none billed.
        reasoning_bias_offset: The Claude bias offset subtracted from every
            reasoning residual, or ``None`` when too few requests measured it.
        limitations: What the reading could not say, sorted.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    vendor_session_ref: str
    runtime: HistoryRuntime
    generations: tuple[ContextGeneration, ...]
    compaction_count: int = Field(ge=0)
    peak_request_input: int | None
    reasoning_bias_offset: int | None
    limitations: tuple[str, ...]


def refuse_opaque_blob(text: str) -> str:
    """Return *text* unchanged when it holds no run shaped like a reasoning signature.

    Args:
        text: Serialized rows about to be written.

    Returns:
        *text*.

    Raises:
        OpaqueBlobError: A run of 200 or more base64-alphabet characters is
            present; the message gives its offset, never the run.
    """
    found = OPAQUE_BLOB.search(text)
    if found is not None:
        raise OpaqueBlobError(f"opaque blob of {len(found.group())} chars at {found.start()}")
    return text


def _request_input(step: ObservedRequest) -> int:
    return step.input_tokens + step.cache_read_tokens + step.cache_write_tokens


def _chars_tokens(block: ContentBlock) -> int:
    return math.ceil(block.size_chars / CHARS_PER_TOKEN)


def _apportion(total: int, count: int) -> list[int]:
    """Split *total* evenly over *count* blocks, the remainder to the first ones."""
    share, remainder = divmod(total, count)
    return [share + (1 if position < remainder else 0) for position in range(count)]


def _claude_reasoning(
    blocks: Sequence[ContentBlock], steps: dict[int, ObservedRequest], limitations: set[str]
) -> tuple[dict[int, int | None], int | None]:
    """Return the reasoning tokens per reasoning block ordinal, and the bias offset.

    Each request's reasoning is its exact output count less its visible blocks
    at :data:`CHARS_PER_TOKEN`; that residual carries a bias the session's own
    non-reasoning requests measure, so their median is subtracted.
    """
    visible: dict[int, int] = defaultdict(int)
    reasoning: dict[int, list[int]] = defaultdict(list)
    for block in blocks:
        if block.request_ordinal < 1:
            continue
        if block.kind == "assistant_reasoning":
            reasoning[block.request_ordinal].append(block.ordinal)
        elif block.kind in _VISIBLE_KINDS:
            visible[block.request_ordinal] += block.size_chars
    residuals = {
        ordinal: step.output_tokens - math.ceil(visible[ordinal] / CHARS_PER_TOKEN)
        for ordinal, step in steps.items()
    }
    baseline = sorted(value for ordinal, value in residuals.items() if ordinal not in reasoning)
    offset: int | None = None
    if len(baseline) >= REASONING_BIAS_MIN_SAMPLE:
        offset = int(statistics.median(baseline))
    else:
        limitations.add("reasoning_bias_offset_unavailable")
    sized: dict[int, int | None] = {}
    for ordinal, block_ordinals in reasoning.items():
        if ordinal not in residuals:
            sized.update(dict.fromkeys(block_ordinals))
            continue
        total = max(residuals[ordinal] - (offset or 0), 0)
        sized.update(zip(block_ordinals, _apportion(total, len(block_ordinals)), strict=True))
    return sized, offset


def _codex_reasoning(
    blocks: Sequence[ContentBlock], steps: dict[int, ObservedRequest]
) -> dict[int, int | None]:
    """Return the reasoning tokens per reasoning block ordinal, from the provider counter.

    A Codex response is written before the token count that bills it, so a
    reasoning item written after ``n`` requests belongs to request ``n + 1``.
    """
    reasoning: dict[int, list[int]] = defaultdict(list)
    for block in blocks:
        if block.kind == "assistant_reasoning":
            reasoning[block.request_ordinal + 1].append(block.ordinal)
    sized: dict[int, int | None] = {}
    for ordinal, block_ordinals in reasoning.items():
        step = steps.get(ordinal)
        if step is None or step.reasoning_tokens is None:
            sized.update(dict.fromkeys(block_ordinals))
            continue
        sized.update(
            zip(block_ordinals, _apportion(step.reasoning_tokens, len(block_ordinals)), strict=True)
        )
    return sized


def _recorded_block(
    block: ContentBlock,
    runtime: HistoryRuntime,
    offset: int,
    reasoning: dict[int, int | None],
) -> GenerationBlock:
    if block.kind == "assistant_reasoning":
        tokens = reasoning.get(block.ordinal)
        size_basis = (
            SizeBasis.UNAVAILABLE
            if tokens is None
            else SizeBasis.OUTPUT_RESIDUAL
            if runtime == "claude"
            else SizeBasis.PROVIDER_COUNTER
        )
    else:
        tokens, size_basis = _chars_tokens(block), SizeBasis.CHARS
    return GenerationBlock(
        ordinal=block.ordinal + offset,
        kind=block.kind,
        lane=block.lane,
        request_ordinal=block.request_ordinal,
        tokens=tokens,
        size_basis=size_basis,
        origin=_RECORDED_ORIGINS.get((runtime, block.kind)),
    )


def _preamble_blocks(preamble: Sequence[ReconstructedBlock]) -> list[GenerationBlock]:
    return [
        GenerationBlock(
            ordinal=position,
            kind=part.kind,
            lane="main",
            request_ordinal=0,
            tokens=part.token_estimate,
            size_basis=SizeBasis.RECONSTRUCTED,
            origin=part.origin,
            basis=part.basis,
        )
        for position, part in enumerate(preamble, start=1)
    ]


def _summary(
    index: int,
    reason: str,
    blocks: Sequence[GenerationBlock],
    steps: Sequence[ObservedRequest],
) -> GenerationSummary:
    by_kind: dict[str, int] = defaultdict(int)
    for block in blocks:
        if block.tokens is not None:
            by_kind[block.kind] += block.tokens
    appended = sum(by_kind.values())
    final_input = _request_input(steps[-1]) if steps else None
    return GenerationSummary(
        index=index,
        reason=reason,
        requests=len(steps),
        appended_tokens=appended,
        tokens_by_kind=dict(sorted(by_kind.items())),
        final_request_input=final_input,
        reconciliation_delta=None if final_input is None else final_input - appended,
    )


def _preamble_for(
    detail: SessionDetail, memory_files: Sequence[tuple[str, int]], limitations: set[str]
) -> tuple[ReconstructedBlock, ...]:
    if detail.runtime != "claude":
        return ()
    tool_names = sorted({span.label for span in detail.tool_spans.get(DurationKind.TOOL_CALL, ())})
    preamble, found = reconstruct_claude_preamble(
        version=detail.runtime_version, tool_names=tool_names, memory_files=memory_files
    )
    limitations |= found
    return preamble


def session_context(
    detail: SessionDetail, *, memory_files: Sequence[tuple[str, int]] | None = ()
) -> SessionContext:
    """Return one session's context generations.

    A Claude session's first generation opens with the rebuilt preamble, set
    against the first request's input; a Codex session records its own.

    Args:
        detail: The session's detail from the session-history reader.
        memory_files: ``(label, tokens)`` memory files for the Claude rebuild,
            from :func:`~eawf.observability.reflect.preamble.read_memory_files`,
            or ``None`` when the session's project's files were not read; the
            rebuild then leaves memory out and says so.

    Returns:
        The generations, their totals and what the reading could not say.

    Raises:
        ValueError: A memory file's token estimate is negative.
    """
    limitations: set[str] = set()
    if memory_files is None and detail.runtime == "claude":
        limitations.add("preamble_memory_files_unknown")
    steps = {step.ordinal: step for step in detail.steps}
    offset: int | None = None
    if detail.runtime == "claude":
        reasoning, offset = _claude_reasoning(detail.blocks, steps, limitations)
    else:
        reasoning = _codex_reasoning(detail.blocks, steps)
    preamble = _preamble_blocks(_preamble_for(detail, memory_files or (), limitations))
    recorded = [
        _recorded_block(block, detail.runtime, len(preamble), reasoning) for block in detail.blocks
    ]
    cuts = sorted(detail.compactions, key=lambda cut: cut.block_ordinal)
    bounds = [(cut.block_ordinal + len(preamble), cut.request_ordinal) for cut in cuts]
    bounds.append((len(preamble) + len(recorded), max(steps, default=0)))
    ordered_steps = [steps[ordinal] for ordinal in sorted(steps)]
    blocks = [*preamble, *recorded]
    generations: list[ContextGeneration] = []
    block_floor, step_floor = 0, 0
    for position, (block_cut, step_cut) in enumerate(bounds):
        members = [block for block in blocks if block_floor < block.ordinal <= block_cut]
        billed = [step for step in ordered_steps if step_floor < step.ordinal <= step_cut]
        block_floor, step_floor = max(block_floor, block_cut), max(step_floor, step_cut)
        if not members and not billed:
            continue
        reason = (
            "current context"
            if not cuts
            else "before compaction 1"
            if position == 0
            else f"after compaction {position}"
        )
        summary = _summary(len(generations) + 1, reason, members, billed)
        generations.append(ContextGeneration(summary=summary, blocks=tuple(members)))
    if preamble and ordered_steps and generations:
        before = sum(block.tokens or 0 for block in recorded if block.request_ordinal == 0)
        check = reconcile_preamble(
            reconstructed_tokens=sum(block.tokens or 0 for block in preamble),
            first_request_input=_request_input(ordered_steps[0]),
            blocks_before_first_request=before,
        )
        first = generations[0]
        generations[0] = first.model_copy(
            update={"summary": first.summary.model_copy(update={"preamble_reconciliation": check})}
        )
    logger.debug(
        f"session_context runtime={detail.runtime} blocks={len(blocks)} "
        f"generations={len(generations)}"
    )
    return SessionContext(
        vendor_session_ref=detail.vendor_session_ref,
        runtime=detail.runtime,
        generations=tuple(generations),
        compaction_count=len(cuts),
        peak_request_input=max((_request_input(step) for step in ordered_steps), default=None),
        reasoning_bias_offset=offset,
        limitations=tuple(sorted(limitations)),
    )


__all__ = [
    "OPAQUE_BLOB",
    "REASONING_BIAS_MIN_SAMPLE",
    "ContextGeneration",
    "GenerationBlock",
    "GenerationSummary",
    "OpaqueBlobError",
    "SessionContext",
    "SizeBasis",
    "refuse_opaque_blob",
    "session_context",
]
