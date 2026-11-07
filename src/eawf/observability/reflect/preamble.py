"""Rebuild the Claude harness preamble its transcript does not record.

Claude Code assembles the system prompt, the built-in tool schemas and the
memory files at request time and writes none of them to the transcript. Codex
writes its equivalents, so two sessions shown side by side would disagree about
what a context is made of for a reason that has nothing to do with either
session. What can be rebuilt is rebuilt here, and each part carries how it was
obtained, because the parts are not equally knowable.

Two rules hold. Each category is one block: one block per memory file or per
tool would put independent parts into one origin, where a later part reads as
replacing the earlier ones. And every part that could not be sourced is named
as a limitation, per category, rather than folded into one short total.

The skill, agent and MCP listings Claude Code also injects are not rebuilt:
the session detail records no attachment rows, which
``preamble_listings_not_rebuilt`` discloses on every Claude rebuild.
"""

from __future__ import annotations

import logging
import math
import re
from collections.abc import Mapping, Sequence
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: The key a version row stores its whole tool-schema size under while the
#: reading it came from gives one figure rather than a per-tool split.
TOOL_SCHEMA_TOTAL: Final = "_TOTAL"

#: The approximation every block estimate uses. Sharing it matters more than its
#: accuracy: the reconciliation compares a rebuild against a sum of block
#: estimates, and two different approximations would bias that comparison.
CHARS_PER_TOKEN: Final = 4

# The memory files Claude Code loads from the directory a session ran in, in
# sorted order so two reads of one directory produce the same list.
_MEMORY_FILE_NAMES: Final = ("AGENTS.md", "CLAUDE.md", "MEMORY.md")
_USER_MEMORY_ROOT: Final = "CLAUDE.md"
_PROJECT_MEMORY_INDEX: Final = Path("memory") / "MEMORY.md"
# An import is a bare filename at the head of the user-level root, so following
# one can only ever reach a sibling of the file that named it.
_MEMORY_IMPORT: Final = re.compile(r"^@([A-Za-z0-9._-]+)\s*$", re.MULTILINE)
_MEMORY_IMPORT_SCAN_CHARS: Final = 4096

# Agreement is within a tenth of the residual, floored so a small residual is
# not held to a tolerance no rebuild could meet.
_TOLERANCE_DIVISOR: Final = 10
_TOLERANCE_FLOOR: Final = 500


class InstructionOrigin(StrEnum):
    """One source of one kind of instruction a context opens with.

    An origin names a source, never a rendering label: every origin is either
    always recorded in the transcript or always rebuilt at report time, never
    both, so one word never sits on two figures a reader must weigh differently.

    Values:
        SESSION_BASE: The base instructions a Codex rollout records.
        DEVELOPER_PROJECT: A Codex developer-role message, recorded.
        TOOL_SCHEMA: The tool definitions a Codex rollout records.
        RUNTIME: A runtime injection a Claude transcript records.
        BUILTIN_SYSTEM_PROMPT: The Claude system prompt, rebuilt from the
            version table.
        BUILTIN_TOOL_SCHEMA: The Claude built-in tool schemas, rebuilt from
            the version table.
        MEMORY_FILE: The memory files, read from disk at report time.
    """

    SESSION_BASE = "session_base"
    DEVELOPER_PROJECT = "developer_project"
    TOOL_SCHEMA = "tool_schema"
    RUNTIME = "runtime"
    BUILTIN_SYSTEM_PROMPT = "builtin_system_prompt"
    BUILTIN_TOOL_SCHEMA = "builtin_tool_schema"
    MEMORY_FILE = "memory_file"


class ReconstructionBasis(StrEnum):
    """How a block that was not read from the transcript was obtained.

    Values:
        VERSION_TABLE: From the measured preamble of the session's version.
        READ_FROM_DISK_NOW: Read from disk at report time, which may differ
            from what the session saw.
    """

    VERSION_TABLE = "version_table"
    READ_FROM_DISK_NOW = "read_from_disk_now"


#: The origins only a rebuild produces; every other origin is only ever recorded.
RECONSTRUCTED_ORIGINS: Final = frozenset(
    {
        InstructionOrigin.BUILTIN_SYSTEM_PROMPT,
        InstructionOrigin.BUILTIN_TOOL_SCHEMA,
        InstructionOrigin.MEMORY_FILE,
    }
)


class PreambleVersion(BaseModel):
    """One measured Claude Code preamble, keyed by the version transcripts record.

    Attributes:
        version: The Claude Code version, as ``major.minor.patch``.
        system_prompt_tokens: The system prompt's size.
        tool_schema_tokens: Built-in tool schema sizes per tool, or one
            :data:`TOOL_SCHEMA_TOTAL` entry for the whole set.
        measured_at: The day the reading was taken.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    system_prompt_tokens: int = Field(ge=0)
    tool_schema_tokens: Mapping[str, int]
    measured_at: str


class ReconstructedBlock(BaseModel):
    """One rebuilt preamble part and how its size was obtained.

    Attributes:
        kind: The block kind it stands in for.
        origin: Its instruction origin, always a rebuilt one.
        basis: How its size was obtained.
        token_estimate: Its size in tokens.
        label: What a reader sees it called.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["system", "tool_definition", "project_instruction"]
    origin: InstructionOrigin
    basis: ReconstructionBasis
    token_estimate: int = Field(ge=0)
    label: str


class VersionMatch(BaseModel):
    """Which measured row a session was attributed to, and how far from its own.

    Attributes:
        requested: The version the session recorded.
        matched: The measured version used, or ``None``.
        distance: Patch releases below the requested version; set exactly
            when ``matched`` is, so no attribution arrives without its weight.
        entry: The matched row.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    requested: str | None
    matched: str | None
    distance: int | None
    entry: PreambleVersion | None = None

    @property
    def exact(self) -> bool:
        """Whether the session's own version was the one measured."""
        return self.distance == 0


class PreambleReconciliation(BaseModel):
    """The rebuilt preamble against what the first request was charged for.

    Attributes:
        reconstructed_tokens: The rebuilt preamble's size.
        residual_tokens: First-request input less every block before it.
        delta_tokens: Rebuilt size less the residual.
        status: ``agrees`` within tolerance, ``drifted`` outside it, or
            ``residual_implausible`` when the residual is not positive.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reconstructed_tokens: int
    residual_tokens: int
    delta_tokens: int
    status: Literal["agrees", "drifted", "residual_implausible"]


# Seeded from an operator reading of one version's context breakdown. Only the
# version-specific parts belong here: memory files differ per session and are
# read per session.
PREAMBLE_TABLE: Final[Mapping[str, PreambleVersion]] = MappingProxyType(
    {
        "2.1.235": PreambleVersion(
            version="2.1.235",
            system_prompt_tokens=4300,
            tool_schema_tokens=MappingProxyType({TOOL_SCHEMA_TOTAL: 24500}),
            measured_at="2026-08-20",
        ),
    }
)


def _release(version: str | None) -> tuple[int, int, int] | None:
    """Return *version* as a comparable triple; a shorter string is refused, not zero-filled."""
    if not version:
        return None
    parts = version.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    major, minor, patch = (int(part) for part in parts)
    return (major, minor, patch)


def match_version(
    version: str | None, *, table: Mapping[str, PreambleVersion] = PREAMBLE_TABLE
) -> VersionMatch:
    """Return the nearest measured row at or below *version*, within its release series.

    The fallback only reaches downwards, because a later release may have grown
    its preamble, and stays inside one ``major.minor`` series, because a feature
    release is when a preamble is rewritten wholesale.

    Args:
        version: The Claude Code version the session recorded.
        table: The measured rows.

    Returns:
        The match; unmatched when the version is unusable or outside every
        measured series.
    """
    wanted = _release(version)
    if wanted is None:
        return VersionMatch(requested=version, matched=None, distance=None)
    candidates = [
        (release, key)
        for key, release in ((key, _release(key)) for key in table)
        if release is not None and release[:2] == wanted[:2] and release[2] <= wanted[2]
    ]
    if not candidates:
        return VersionMatch(requested=version, matched=None, distance=None)
    release, key = max(candidates)
    return VersionMatch(
        requested=version, matched=key, distance=wanted[2] - release[2], entry=table[key]
    )


def _size_in_tokens(candidate: Path) -> int:
    """Return a file's size as tokens, or zero when it is not a readable non-empty file."""
    if not candidate.is_file():
        return 0
    size = candidate.stat().st_size
    return math.ceil(size / CHARS_PER_TOKEN) if size > 0 else 0


def _user_memory(user_root: Path) -> list[tuple[str, int]]:
    """Return the user-level memory files, following one level of ``@`` import."""
    tokens = _size_in_tokens(user_root / _USER_MEMORY_ROOT)
    if tokens == 0:
        return []
    found = [(f"{_USER_MEMORY_ROOT} (user)", tokens)]
    head = (user_root / _USER_MEMORY_ROOT).read_text(encoding="utf-8", errors="replace")
    for name in _MEMORY_IMPORT.findall(head[:_MEMORY_IMPORT_SCAN_CHARS]):
        imported = user_root / name
        if imported.parent != user_root:
            continue
        imported_tokens = _size_in_tokens(imported)
        if imported_tokens > 0:
            found.append((f"{name} (user)", imported_tokens))
    return found


def read_memory_files(
    directory: Path | None, *, harness_store: Path | None = None
) -> tuple[list[tuple[str, int]], frozenset[str]]:
    """Return the memory files a session loaded, as label and token estimate, plus caveats.

    Three sources share one provenance: the session's working directory, the
    user-level set the harness loads for every session, and the per-project
    memory index kept beside the transcripts. Every path is used and dropped;
    only a basename, a scope word and an estimate are returned.

    Args:
        directory: The session's working directory, or ``None`` when unknown.
        harness_store: The harness's directory for the session's project; its
            ``memory`` index and the user-level set two levels above it are
            read. ``None`` confines the read to *directory*.

    Returns:
        The ``(label, tokens)`` pairs, and ``preamble_memory_files_read_at_report_time``
        when any was found plus ``preamble_memory_files_unreadable`` when the
        working directory could not be read.
    """
    found: list[tuple[str, int]] = []
    readable = directory is not None and directory.is_dir()
    try:
        if readable and directory is not None:
            found.extend(
                (name, tokens)
                for name in _MEMORY_FILE_NAMES
                if (tokens := _size_in_tokens(directory / name)) > 0
            )
        if harness_store is not None:
            found.extend(_user_memory(harness_store.parent.parent))
            index_tokens = _size_in_tokens(harness_store / _PROJECT_MEMORY_INDEX)
            if index_tokens > 0:
                found.append((f"{_PROJECT_MEMORY_INDEX.name} (project)", index_tokens))
    except OSError as exc:
        logger.warning(f"read_memory_files err={type(exc).__name__}")
        return [], frozenset({"preamble_memory_files_unreadable"})
    limitations = {"preamble_memory_files_read_at_report_time"} if found else set()
    if not readable:
        limitations.add("preamble_memory_files_unreadable")
    return found, frozenset(limitations)


def _tool_schema_tokens(entry: PreambleVersion, tool_names: Sequence[str]) -> tuple[int, set[str]]:
    """Return the built-in tool schema size for *entry*, and what it could not say."""
    total = entry.tool_schema_tokens.get(TOOL_SCHEMA_TOTAL)
    if total is not None:
        return total, {"preamble_tool_schema_not_itemised"}
    limitations: set[str] = set()
    measured = 0
    for name in tool_names:
        tokens = entry.tool_schema_tokens.get(name)
        if tokens is None:
            limitations.add("preamble_tool_schema_unmeasured")
            continue
        measured += tokens
    return measured, limitations


def reconstruct_claude_preamble(
    *,
    version: str | None,
    tool_names: Sequence[str],
    memory_files: Sequence[tuple[str, int]],
    table: Mapping[str, PreambleVersion] = PREAMBLE_TABLE,
) -> tuple[tuple[ReconstructedBlock, ...], frozenset[str]]:
    """Return the rebuildable preamble parts and the limitations the attempt raised.

    Args:
        version: The Claude Code version the session recorded.
        tool_names: The built-in tools the session called, for an itemised row.
        memory_files: ``(label, tokens)`` pairs from :func:`read_memory_files`.
        table: The measured version rows.

    Returns:
        The parts in the order the harness assembles them (system prompt, tool
        schemas, memory files), one block per category, and the limitations.

    Raises:
        ValueError: A memory file's token estimate is negative.
    """
    limitations: set[str] = {"preamble_listings_not_rebuilt"}
    blocks: list[ReconstructedBlock] = []
    match = match_version(version, table=table)
    if match.entry is None:
        limitations.add("preamble_version_unmeasured")
    else:
        if not match.exact:
            limitations.add("preamble_version_approximated")
        blocks.append(
            ReconstructedBlock(
                kind="system",
                origin=InstructionOrigin.BUILTIN_SYSTEM_PROMPT,
                basis=ReconstructionBasis.VERSION_TABLE,
                token_estimate=match.entry.system_prompt_tokens,
                label="System prompt",
            )
        )
        schema_tokens, schema_limitations = _tool_schema_tokens(match.entry, tool_names)
        limitations |= schema_limitations
        if schema_tokens > 0:
            blocks.append(
                ReconstructedBlock(
                    kind="tool_definition",
                    origin=InstructionOrigin.BUILTIN_TOOL_SCHEMA,
                    basis=ReconstructionBasis.VERSION_TABLE,
                    token_estimate=schema_tokens,
                    label="Built-in tool schemas",
                )
            )
    memory_tokens = 0
    for name, tokens in memory_files:
        if tokens < 0:
            raise ValueError(f"{name}: token estimate must not be negative")
        memory_tokens += tokens
    if memory_tokens > 0:
        blocks.append(
            ReconstructedBlock(
                kind="project_instruction",
                origin=InstructionOrigin.MEMORY_FILE,
                basis=ReconstructionBasis.READ_FROM_DISK_NOW,
                token_estimate=memory_tokens,
                label=", ".join(name for name, tokens in memory_files if tokens > 0),
            )
        )
    return tuple(blocks), frozenset(limitations)


def reconcile_preamble(
    *, reconstructed_tokens: int, first_request_input: int, blocks_before_first_request: int
) -> PreambleReconciliation:
    """Compare the rebuilt preamble against what the first request was charged for.

    The first request's input is provider-reported and every block before it
    is on disk, so their difference is the preamble alone: an independent check
    on the rebuild. The residual is a diagnostic and never becomes a block.

    Args:
        reconstructed_tokens: The rebuilt preamble's size.
        first_request_input: The first request's whole input, cached included.
        blocks_before_first_request: The blocks written before that request.

    Returns:
        The reconciliation; a residual at or below zero, as a resumed transcript
        replaying its history yields, is ``residual_implausible``.

    Raises:
        ValueError: Any input is negative.
    """
    for name, value in (
        ("reconstructed_tokens", reconstructed_tokens),
        ("first_request_input", first_request_input),
        ("blocks_before_first_request", blocks_before_first_request),
    ):
        if value < 0:
            raise ValueError(f"{name} must not be negative")
    residual = first_request_input - blocks_before_first_request
    delta = reconstructed_tokens - residual
    status: Literal["agrees", "drifted", "residual_implausible"]
    if residual <= 0:
        status = "residual_implausible"
    elif abs(delta) <= max(residual // _TOLERANCE_DIVISOR, _TOLERANCE_FLOOR):
        status = "agrees"
    else:
        status = "drifted"
    return PreambleReconciliation(
        reconstructed_tokens=reconstructed_tokens,
        residual_tokens=residual,
        delta_tokens=delta,
        status=status,
    )


__all__ = [
    "CHARS_PER_TOKEN",
    "PREAMBLE_TABLE",
    "RECONSTRUCTED_ORIGINS",
    "TOOL_SCHEMA_TOTAL",
    "InstructionOrigin",
    "PreambleReconciliation",
    "PreambleVersion",
    "ReconstructedBlock",
    "ReconstructionBasis",
    "VersionMatch",
    "match_version",
    "read_memory_files",
    "reconcile_preamble",
    "reconstruct_claude_preamble",
]
