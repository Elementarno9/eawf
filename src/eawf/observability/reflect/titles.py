"""Fill each listed session's title from a scrubbed structural digest, and nothing else.

The title fill is the one call the reflection surface makes off this machine, so its
input is fixed by construction: :class:`SessionDigest` has a field for every fact it
may send -- project label, runtime, model family, terminal observation, model-step,
subagent and tool-work counts and wall hours -- and no field a prompt, a tool payload,
a tool output, reasoning or a transcript line could ride in on.

A digest is keyed by the SHA-256 of its canonical text. A key the local cache holds is
answered from disk, so a filled title never calls out again and two renders of one
projection come out byte-identical. A provider answer of ``UNKNOWN`` is left uncached:
storing it would pin a placeholder the next run could have filled.

Every title resolves through one chain, per title: the provider's answer
(``model_written``), then ``structural`` -- date, project label, runtime and scale,
carrying no operator wording -- and ``unavailable`` only when the record supports
neither. ``scrubbed_extract`` is a declared source that nothing here produces: a Run
record holds no operator request text to extract a clause from.
"""

from __future__ import annotations

import hashlib
import logging
import os
import secrets
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Final, Literal, Protocol

from pydantic import AfterValidator, BaseModel, ConfigDict, Field

from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.state.epoch2.measurement import (
    UNKNOWN_ATTRIBUTION,
    CounterSnapshot,
    MeasuredRuntime,
)
from eawf.kernel.state.types import UtcDatetime
from eawf.observability.reflect.runs import RunReading
from eawf.platform.scrub.scan import redact_text

logger = logging.getLogger(__name__)

#: The marker every helper session the fill spawns opens with, so the session-history
#: sweep records it under its own drop reason and no cohort counts it.
REFLECTION_MARKER: Final = "[eawf-reflect-title]"

#: The answer a provider gives when a digest supports no title.
UNKNOWN_ANSWER: Final = "UNKNOWN"

#: The longest a persisted title may be.
TITLE_MAX_CHARS: Final = 72

#: How long one provider call may take before its title falls back.
PROVIDER_TIMEOUT_SECONDS: Final = 60.0

#: What the provider is asked. It names no model and carries no session content: the
#: digest that follows it is the only session-derived text sent.
TITLE_INSTRUCTION: Final = (
    "Write one imperative noun phrase of at most 72 characters, with no trailing period,"
    " naming the work the session below most likely did. Answer UNKNOWN if the facts"
    " support no title. The facts are counts and labels only."
)

_MODEL_STEP_KINDS: Final = frozenset(
    {RunEventKind.MESSAGE_SUMMARIZED, RunEventKind.REASONING_SUMMARIZED}
)
_TOOL_WORK_KINDS: Final = frozenset({RunEventKind.TOOL_REQUESTED, RunEventKind.COMMAND_STARTED})
_SECONDS_PER_HOUR: Final = Decimal(3600)
_TENTH: Final = Decimal("0.1")


class TitleSource(StrEnum):
    """Where a title came from, in fallback order."""

    MODEL_WRITTEN = "model_written"
    SCRUBBED_EXTRACT = "scrubbed_extract"
    STRUCTURAL = "structural"
    UNAVAILABLE = "unavailable"


def _title_shape(value: str) -> str:
    """Refuse a string that is not a persistable title."""
    if not value or len(value) > TITLE_MAX_CHARS:
        raise ValueError(f"a title is 1 to {TITLE_MAX_CHARS} characters")
    if value.endswith("."):
        raise ValueError("a title carries no trailing period")
    if value == UNKNOWN_ANSWER:
        raise ValueError("an UNKNOWN answer is never stored as a title")
    return value


#: A title as the cache persists it.
TitleStr = Annotated[str, AfterValidator(_title_shape)]

#: A cache key: the SHA-256 of one digest's canonical text.
DigestKey = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class SessionDigest(BaseModel):
    """The scrubbed structural facts one session's title is asked for.

    Attributes:
        project_label: The project the session belongs to.
        runtime: The harness the session ran under, or ``unknown``.
        model_family: The model the counters were attributed to, or ``unknown``.
        terminal_observation: The Run's status when it was read.
        model_steps: Summarized messages and reasoning turns.
        subagents: Child Runs the session started.
        tool_work: Tool requests and commands the session started.
        wall_hours: The Run's wall-clock length in hours, to a tenth; ``None``
            while it has not ended.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    project_label: str
    runtime: str
    model_family: str
    terminal_observation: str
    model_steps: int = Field(ge=0)
    subagents: int = Field(ge=0)
    tool_work: int = Field(ge=0)
    wall_hours: Decimal | None

    def text(self) -> str:
        """Return the canonical text sent to the provider and hashed for the key."""
        return self.model_dump_json()

    def key(self) -> str:
        """Return the digest's cache key."""
        return hashlib.sha256(self.text().encode("utf-8")).hexdigest()


class CachedTitle(BaseModel):
    """One provider answer, stored under the digest it answered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    title: TitleStr
    stored_at: UtcDatetime


class TitleCache(BaseModel):
    """The local title cache: provider answers keyed by digest hash, never digest text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    entries: dict[DigestKey, CachedTitle] = Field(default_factory=dict)


class TitleProvider(Protocol):
    """Something that answers a digest with a title."""

    @property
    def name(self) -> str:
        """The provider's name, as the fill account states it."""
        ...

    def title_for(self, digest_text: str) -> str | None:
        """Return the provider's raw answer, or ``None`` when it gave none."""
        ...


@dataclass(frozen=True, slots=True)
class ClaudeTitleProvider:
    """The claude runtime in print mode, spawned once per uncached digest.

    Attributes:
        binary: The resolved ``claude`` executable.
    """

    binary: str

    @property
    def name(self) -> str:
        """The provider's name."""
        return "claude-code"

    @classmethod
    def discover(cls) -> ClaudeTitleProvider | None:
        """Return the provider when the ``claude`` executable is on ``PATH``."""
        binary = shutil.which("claude")
        return None if binary is None else cls(binary=binary)

    def argv(self, digest_text: str) -> list[str]:
        """Return the helper session's argv, whose prompt opens with the marker."""
        prompt = f"{REFLECTION_MARKER} {TITLE_INSTRUCTION}\n{digest_text}"
        return [self.binary, "-p", prompt, "--output-format", "text"]

    def title_for(self, digest_text: str) -> str | None:
        """Return the helper session's answer, or ``None`` when it failed or timed out."""
        try:
            done = subprocess.run(
                self.argv(digest_text),
                capture_output=True,
                text=True,
                timeout=PROVIDER_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning(f"title_for provider={self.name} err={exc.__class__.__name__}")
            return None
        return done.stdout if done.returncode == 0 else None


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedTitle:
    """One listed session's title and the source it carries.

    Attributes:
        run_key: The Run the session is.
        title: The title, or ``None`` when its source is ``unavailable``.
        source: Where the title came from.
    """

    run_key: str
    title: str | None
    source: TitleSource


@dataclass(slots=True, kw_only=True)
class FillAccount:
    """What the fill sent and answered, for the run's verification manifest.

    Attributes:
        provider: The provider called, or ``None`` when none was.
        local_only: Whether ``--local-only`` was in force; every egress count is
            then zero.
        digests_sent: Digests sent to the provider.
        cache_hits: Titles answered from the local cache.
        unknown_uncached: ``UNKNOWN`` answers left out of the cache.
        by_source: Titles resolved per source.
    """

    provider: str | None
    local_only: bool
    digests_sent: int = 0
    cache_hits: int = 0
    unknown_uncached: int = 0
    by_source: dict[str, int] = field(default_factory=lambda: {s.value: 0 for s in TitleSource})


def _model_family(reading: RunReading) -> str:
    """Return the model the Run's counters were attributed to, or ``unknown``."""
    for source in (reading.run.captured_runtime, reading.run.counter_baseline):
        if isinstance(source, (MeasuredRuntime, CounterSnapshot)):
            return source.model
    return UNKNOWN_ATTRIBUTION


def session_digest(reading: RunReading) -> SessionDigest:
    """Return the structural digest of one Run, counted off its record and events.

    Args:
        reading: The Run and its event lines.

    Returns:
        The digest; it carries no event text of any kind.
    """
    run = reading.run
    kinds = [event.event_kind for event in reading.events if event.quarantine is None]
    hours = None
    if run.started_at is not None and run.ended_at is not None:
        seconds = Decimal((run.ended_at - run.started_at).total_seconds())
        hours = (seconds / _SECONDS_PER_HOUR).quantize(_TENTH)
    return SessionDigest(
        project_label=run.urn.project_key,
        runtime=run.vendor_session.harness if run.vendor_session else UNKNOWN_ATTRIBUTION,
        model_family=_model_family(reading),
        terminal_observation=run.status.value,
        model_steps=sum(1 for kind in kinds if kind in _MODEL_STEP_KINDS),
        subagents=sum(1 for kind in kinds if kind is RunEventKind.CHILD_RUN_STARTED),
        tool_work=sum(1 for kind in kinds if kind in _TOOL_WORK_KINDS),
        wall_hours=hours,
    )


def model_title(answer: str | None) -> str | None:
    """Return the persistable title a provider answer yields, or ``None``.

    The first line is taken, trailing periods dropped, the result scrubbed, and a
    phrase over the limit cut at the last word boundary that fits; ``UNKNOWN`` and an
    empty answer yield nothing.
    """
    if answer is None:
        return None
    lines = answer.strip().splitlines()
    first = redact_text(lines[0].strip()).rstrip(". ") if lines else ""
    if not first or first == UNKNOWN_ANSWER:
        return None
    if len(first) > TITLE_MAX_CHARS:
        first = first[: TITLE_MAX_CHARS + 1].rsplit(" ", 1)[0].rstrip(".,;: ")
    return first[:TITLE_MAX_CHARS] or None


def structural_title(reading: RunReading, digest: SessionDigest) -> str | None:
    """Return the title the record alone supports, or ``None`` for a Run never started."""
    started = reading.run.started_at
    if started is None:
        return None
    title = (
        f"Review the {digest.project_label} {digest.runtime} session of"
        f" {started.date().isoformat()}, {digest.model_steps} steps"
    )
    return title[:TITLE_MAX_CHARS].rstrip(". ")


def fill_titles(
    readings: Sequence[RunReading],
    *,
    cache: TitleCache,
    provider: TitleProvider | None,
    local_only: bool,
    now: datetime,
) -> tuple[tuple[ResolvedTitle, ...], FillAccount, TitleCache]:
    """Return every listed session's title, the fill's account and the cache after it.

    Args:
        readings: The listed sessions.
        cache: The local title cache as read.
        provider: The provider to ask for an uncached digest; ``None`` when none is
            available or ``--local-only`` is in force.
        local_only: Whether ``--local-only`` is in force. The caller passes no provider
            then, so the fill has nothing it could call out through.
        now: When an answer is stored.

    Returns:
        One title per reading in order, the account, and the cache with every new
        answer added under its key.
    """
    account = FillAccount(
        provider=None if provider is None else provider.name, local_only=local_only
    )
    entries: dict[str, CachedTitle] = dict(cache.entries)
    titles: list[ResolvedTitle] = []
    for reading in readings:
        digest = session_digest(reading)
        key = digest.key()
        title: str | None = None
        if key in entries:
            account.cache_hits += 1
            title = entries[key].title
        elif provider is not None:
            answer = provider.title_for(digest.text())
            account.digests_sent += 1
            title = model_title(answer)
            if title is not None:
                entries[key] = CachedTitle(title=title, stored_at=now)
            elif answer is not None and answer.strip() == UNKNOWN_ANSWER:
                account.unknown_uncached += 1
        source = TitleSource.MODEL_WRITTEN
        if title is None:
            title = structural_title(reading, digest)
            source = TitleSource.UNAVAILABLE if title is None else TitleSource.STRUCTURAL
        account.by_source[source.value] += 1
        titles.append(ResolvedTitle(run_key=reading.run.key, title=title, source=source))
    logger.debug(
        f"fill_titles sessions={len(titles)} sent={account.digests_sent} "
        f"hits={account.cache_hits} local_only={local_only}"
    )
    return tuple(titles), account, TitleCache(entries=entries)


def load_title_cache(path: Path) -> TitleCache:
    """Return the cache at ``path``; a missing file is an empty cache.

    Raises:
        pydantic.ValidationError: The file is not a valid cache, which includes a
            key that is not a digest hash and a stored ``UNKNOWN``.
    """
    if not path.exists():
        return TitleCache()
    return TitleCache.model_validate_json(path.read_bytes())


def save_title_cache(path: Path, cache: TitleCache) -> None:
    """Replace the cache at ``path`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(cache.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


__all__ = [
    "PROVIDER_TIMEOUT_SECONDS",
    "REFLECTION_MARKER",
    "TITLE_INSTRUCTION",
    "TITLE_MAX_CHARS",
    "UNKNOWN_ANSWER",
    "CachedTitle",
    "ClaudeTitleProvider",
    "FillAccount",
    "ResolvedTitle",
    "SessionDigest",
    "TitleCache",
    "TitleProvider",
    "TitleSource",
    "fill_titles",
    "load_title_cache",
    "model_title",
    "save_title_cache",
    "session_digest",
    "structural_title",
]
