"""Dispatch scorecard: how efficiently one dispatched Run did its work, as checkable numbers.

A Run leaves two deterministic traces. Each tool call has a receipt on the
receipt ledger, and the provider reports usage readings on the run ledger.
Together they show whether a worker read the repository in windows that were
too small, re-read text it already had, took long to make its first change,
or spent more tokens than its Task needed. This module folds those traces
into a :class:`DispatchScorecard` and checks it against a tier of fixed limits.

The fold reads a :class:`DispatchExtract`, never the ledgers directly. An
extract keeps only what the scorecard reads: the Run's status and times,
its usage readings, and per call the tool, the outcome, the completion time
and, for a repository read, the window's size, whether it was cut, and an
ordinal key that stands for its content digest. No file text, path or digest
is carried. A committed extract is therefore a scrubbed copy of a real Run,
and a test can score it without replaying a transcript. Scoring from a tree
builds the extract first, so the ledger and a fixture take the same path.

There are two tiers. ``fixture`` is the hard gate for a smoke Task, which is
small enough to finish fast. ``real`` holds the looser limits for a real
Task. A limit is inclusive: a value equal to it passes. A value that was not
measured (no succeeded patch, no reading, no end time) breaches nothing,
because a gate that fails a Run for a number nobody took would hide the
breaches that are real. As in :mod:`eawf.observability.bench.turn_cost`, the
rates and durations are ``Decimal``, so a value at the limit compares exactly.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self

import orjson
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator

from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
from eawf.kernel.runtime.semantic import RepoReadOutput, SemanticCallStatus, SemanticToolId
from eawf.kernel.runtime.usage import UsagePayload, aggregate_usage
from eawf.kernel.state.epoch2.authority import resolve_authority
from eawf.kernel.state.epoch2.base import RunKey
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.types import UtcDatetime
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.observability.reflect.runs import read_tree_run
from eawf.runtime.daemon.semantic_gateway import RECEIPT_PAYLOAD_KIND, SemanticCallReceipt

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "EXTRACT_SCHEMA_VERSION",
    "TIER_THRESHOLDS",
    "DispatchCheck",
    "DispatchExtract",
    "DispatchMetric",
    "DispatchMetricCheck",
    "DispatchScorecard",
    "DispatchTier",
    "ExtractedCall",
    "TierThresholds",
    "check_scorecard",
    "load_extract",
    "read_dispatch_extract",
    "score_dispatch",
    "write_extract",
]

EXTRACT_SCHEMA_VERSION: Final = "dispatch-extract/v1"

_NonNegative = Annotated[int, Field(ge=0)]
_Rate = Annotated[Decimal, Field(ge=Decimal(0), le=Decimal(1))]
_Seconds = Annotated[Decimal, Field(ge=Decimal(0))]
_MICROSECONDS: Final = Decimal(1_000_000)
_ONE_MICROSECOND: Final = timedelta(microseconds=1)


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ExtractedCall(_Frozen):
    """One tool call of a Run, without the content it returned.

    Attributes:
        tool_id: The tool the call named.
        status: How the gateway answered it.
        completed_at: When the answer was sealed.
        truncated: For a repository read that returned its window inline,
            whether the window was cut; ``None`` for every other call.
        content_key: For such a read, an ordinal key (``c0001``, ...) that
            two reads share exactly when their content digests are equal.
        byte_count: For such a read, the bytes of the file the window covers.
    """

    tool_id: SemanticToolId
    status: SemanticCallStatus
    completed_at: UtcDatetime
    truncated: bool | None = None
    content_key: Annotated[str, Field(pattern=r"^c[0-9]{4,}$")] | None = None
    byte_count: _NonNegative | None = None

    @model_validator(mode="after")
    def _a_window_is_stated_whole(self) -> Self:
        """Refuse a read window with only some of its three facts.

        Raises:
            ValueError: ``truncated``, ``content_key`` and ``byte_count`` are
                not all set or all unset.
        """
        stated = {self.truncated is None, self.content_key is None, self.byte_count is None}
        if len(stated) != 1:
            raise ValueError("a read window states truncated, content_key and byte_count together")
        return self

    @property
    def is_read_window(self) -> bool:
        """Whether the call is a repository read that returned its window inline."""
        return self.tool_id is SemanticToolId.REPO_READ and self.truncated is not None


class DispatchExtract(_Frozen):
    """What the scorecard reads from one Run, scrubbed of content.

    Attributes:
        schema_version: Version of this shape.
        run_key: The Run's key.
        status: The Run's recorded status.
        started_at: When the Run started; ``None`` while queued.
        ended_at: When the Run ended; ``None`` while it has not.
        usage: The Run's usage readings that were not quarantined, in ledger order.
        calls: The Run's tool calls, in ledger order.
    """

    schema_version: Literal["dispatch-extract/v1"] = EXTRACT_SCHEMA_VERSION
    run_key: RunKey
    status: RunStatus
    started_at: UtcDatetime | None
    ended_at: UtcDatetime | None
    usage: tuple[UsagePayload, ...]
    calls: tuple[ExtractedCall, ...]


class DispatchScorecard(_Frozen):
    """One Run's efficiency, folded from its extract.

    Attributes:
        run_key: The Run scored.
        status: The Run's recorded status.
        turns: How many usage readings the Run reported, one per model turn.
        read_truncation_rate: The share of inline repository reads whose
            window was cut; ``0`` for a Run that read nothing.
        identical_reread_rate: The share of those reads that repeat an
            earlier read's content digest and byte count.
        billable_tokens: Input, output and cache-read tokens of the folded
            readings, or ``None`` when the Run reported no token count.
        cache_read_tokens: The cache-read slice of ``billable_tokens``.
        first_patch_seconds: Seconds from the Run's start to its first
            succeeded ``workspace_apply_patch``; ``None`` without one.
        wall_seconds: Seconds from the Run's start to its end; ``None``
            while it has not ended.
    """

    run_key: RunKey
    status: RunStatus
    turns: _NonNegative
    read_truncation_rate: _Rate
    identical_reread_rate: _Rate
    billable_tokens: _NonNegative | None
    cache_read_tokens: _NonNegative | None
    first_patch_seconds: _Seconds | None
    wall_seconds: _Seconds | None


class DispatchTier(StrEnum):
    """Which set of limits a scorecard is checked against.

    Values:
        FIXTURE: The hard gate for a smoke Task.
        REAL: The limits for a real Task.
    """

    FIXTURE = "fixture"
    REAL = "real"


class DispatchMetric(StrEnum):
    """A scorecard value a tier puts a limit on, in report order."""

    STATUS = "status"
    TURNS = "turns"
    READ_TRUNCATION_RATE = "read_truncation_rate"
    IDENTICAL_REREAD_RATE = "identical_reread_rate"
    BILLABLE_TOKENS = "billable_tokens"
    FIRST_PATCH_SECONDS = "first_patch_seconds"
    WALL_SECONDS = "wall_seconds"


class TierThresholds(_Frozen):
    """The inclusive limits of one tier; a Run must also have COMPLETED.

    Attributes:
        max_turns: Most usage readings.
        max_read_truncation_rate: Largest share of cut read windows.
        max_identical_reread_rate: Largest share of identical re-reads.
        max_billable_tokens: Most billable tokens.
        max_first_patch_seconds: Longest time to the first succeeded patch.
        max_wall_seconds: Longest wall clock, or ``None`` for no limit.
    """

    max_turns: _NonNegative
    max_read_truncation_rate: _Rate
    max_identical_reread_rate: _Rate
    max_billable_tokens: _NonNegative
    max_first_patch_seconds: _Seconds
    max_wall_seconds: _Seconds | None


TIER_THRESHOLDS: Final[Mapping[DispatchTier, TierThresholds]] = MappingProxyType(
    {
        DispatchTier.FIXTURE: TierThresholds(
            max_turns=25,
            max_read_truncation_rate=Decimal("0.20"),
            max_identical_reread_rate=Decimal("0.05"),
            max_billable_tokens=500_000,
            max_first_patch_seconds=Decimal(300),
            max_wall_seconds=Decimal(600),
        ),
        DispatchTier.REAL: TierThresholds(
            max_turns=60,
            max_read_truncation_rate=Decimal("0.30"),
            max_identical_reread_rate=Decimal("0.10"),
            max_billable_tokens=2_000_000,
            max_first_patch_seconds=Decimal(900),
            max_wall_seconds=None,
        ),
    }
)
"""The limits of each tier, from the dispatch release-validation brief."""

_REQUIRED_STATUS: Final = RunStatus.COMPLETED

_Observed = Decimal | int | str | None


class DispatchMetricCheck(_Frozen):
    """One scorecard value beside its limit.

    Attributes:
        metric: The value checked.
        observed: The scorecard's value; ``None`` when it was not measured.
        limit: The tier's inclusive limit, the required status for
            ``status``, or ``None`` when the tier sets no limit.
        breached: Whether the value is past the limit.
    """

    metric: DispatchMetric
    observed: _Observed
    limit: _Observed
    breached: bool


class DispatchCheck(_Frozen):
    """A scorecard checked against one tier.

    Attributes:
        tier: The tier checked against.
        scorecard: The scorecard checked.
        checks: One row per :class:`DispatchMetric`, in its order.
    """

    tier: DispatchTier
    scorecard: DispatchScorecard
    checks: tuple[DispatchMetricCheck, ...]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def breaches(self) -> tuple[DispatchMetric, ...]:
        """The metrics past their limit, in report order."""
        return tuple(row.metric for row in self.checks if row.breached)

    @property
    def passed(self) -> bool:
        """Whether no metric is past its limit."""
        return not self.breaches


def score_dispatch(extract: DispatchExtract) -> DispatchScorecard:
    """Fold *extract* into its scorecard.

    Args:
        extract: The Run's extract.

    Returns:
        The :class:`DispatchScorecard`.
    """
    reads = [call for call in extract.calls if call.is_read_window]
    seen: set[tuple[str | None, int | None]] = set()
    rereads = 0
    for call in reads:
        window = (call.content_key, call.byte_count)
        rereads += window in seen
        seen.add(window)
    totals = aggregate_usage(extract.usage)
    patches = [
        call.completed_at
        for call in extract.calls
        if call.tool_id is SemanticToolId.WORKSPACE_APPLY_PATCH and call.status == "succeeded"
    ]
    card = DispatchScorecard(
        run_key=extract.run_key,
        status=extract.status,
        turns=len(extract.usage),
        read_truncation_rate=_rate(sum(bool(call.truncated) for call in reads), len(reads)),
        identical_reread_rate=_rate(rereads, len(reads)),
        billable_tokens=totals.tokens,
        cache_read_tokens=totals.cache_tokens,
        first_patch_seconds=_seconds(extract.started_at, min(patches, default=None)),
        wall_seconds=_seconds(extract.started_at, extract.ended_at),
    )
    logger.debug(
        f"score_dispatch run={card.run_key} turns={card.turns} reads={len(reads)} "
        f"rereads={rereads} patches={len(patches)}"
    )
    return card


def check_scorecard(scorecard: DispatchScorecard, tier: DispatchTier) -> DispatchCheck:
    """Check *scorecard* against the limits of *tier*.

    Args:
        scorecard: The scorecard to check.
        tier: The tier whose limits apply.

    Returns:
        The :class:`DispatchCheck`, one row per metric.
    """
    limits = TIER_THRESHOLDS[tier]
    status = DispatchMetricCheck(
        metric=DispatchMetric.STATUS,
        observed=scorecard.status.value,
        limit=_REQUIRED_STATUS.value,
        breached=scorecard.status is not _REQUIRED_STATUS,
    )
    bounded: tuple[tuple[DispatchMetric, Decimal | int | None, Decimal | int | None], ...] = (
        (DispatchMetric.TURNS, scorecard.turns, limits.max_turns),
        (
            DispatchMetric.READ_TRUNCATION_RATE,
            scorecard.read_truncation_rate,
            limits.max_read_truncation_rate,
        ),
        (
            DispatchMetric.IDENTICAL_REREAD_RATE,
            scorecard.identical_reread_rate,
            limits.max_identical_reread_rate,
        ),
        (DispatchMetric.BILLABLE_TOKENS, scorecard.billable_tokens, limits.max_billable_tokens),
        (
            DispatchMetric.FIRST_PATCH_SECONDS,
            scorecard.first_patch_seconds,
            limits.max_first_patch_seconds,
        ),
        (DispatchMetric.WALL_SECONDS, scorecard.wall_seconds, limits.max_wall_seconds),
    )
    rows = [status] + [
        DispatchMetricCheck(
            metric=metric,
            observed=observed,
            limit=limit,
            breached=observed is not None and limit is not None and observed > limit,
        )
        for metric, observed, limit in bounded
    ]
    check = DispatchCheck(tier=tier, scorecard=scorecard, checks=tuple(rows))
    logger.debug(
        f"check_scorecard run={scorecard.run_key} tier={tier.value} "
        f"breaches={[metric.value for metric in check.breaches]}"
    )
    return check


def read_dispatch_extract(tree_root: Path, ref: str) -> DispatchExtract:
    """Return the extract of the Run *ref* names, read from the tree's ledgers.

    Reads without a daemon and without a lock, as
    :func:`~eawf.observability.reflect.runs.read_tree_run` does.

    Args:
        tree_root: The tree's ``.ea`` directory.
        ref: The Run's key or its qualified URN.

    Returns:
        The Run's :class:`DispatchExtract`.

    Raises:
        LookupError: The tree holds no Run under that key.
        pydantic.ValidationError: A receipt of the Run does not validate.
    """
    reading = read_tree_run(tree_root, ref)
    authority = resolve_authority(tree_root)
    assert authority.target is not None and authority.generation_id is not None
    document = authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
    urn = str(reading.run.urn)
    receipts = [
        SemanticCallReceipt.model_validate(item.payload)
        for item in read_ledger_records(ledger_path(document, Epoch2Collection.RECEIPT))
        if item.payload.get("payload_kind") == RECEIPT_PAYLOAD_KIND
        and item.payload.get("run_ref") == urn
    ]
    usage = tuple(
        event.payload
        for event in reading.events
        if event.quarantine is None and isinstance(event.payload, UsagePayload)
    )
    return DispatchExtract(
        run_key=reading.run.key,
        status=reading.run.status,
        started_at=reading.run.started_at,
        ended_at=reading.run.ended_at,
        usage=usage,
        calls=_extracted_calls(receipts),
    )


def load_extract(path: Path) -> DispatchExtract:
    """Read an extract from *path*.

    Args:
        path: A JSON file holding one extract.

    Returns:
        The parsed :class:`DispatchExtract`.

    Raises:
        FileNotFoundError: *path* does not exist.
        ValueError: The file is not JSON, or not a valid extract
            (``pydantic.ValidationError`` is a ``ValueError``).
    """
    if not path.exists():
        raise FileNotFoundError(f"dispatch extract not found: {path}")
    try:
        payload = orjson.loads(path.read_bytes())
    except orjson.JSONDecodeError as exc:
        raise ValueError(f"malformed dispatch extract JSON in {path}: {exc}") from exc
    return DispatchExtract.model_validate(payload)


def write_extract(extract: DispatchExtract, path: Path) -> None:
    """Write *extract* to *path* as sorted, indented JSON, creating parent directories.

    Args:
        extract: The extract to write.
        path: The destination file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = orjson.dumps(
        extract.model_dump(mode="json"), option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS
    )
    path.write_bytes(raw + b"\n")


# --- Internal helpers ------------------------------------------------------


def _extracted_calls(receipts: Sequence[SemanticCallReceipt]) -> tuple[ExtractedCall, ...]:
    """Return *receipts* without their content, each digest replaced by an ordinal key."""
    keys: dict[str, str] = {}
    calls: list[ExtractedCall] = []
    for receipt in receipts:
        output = receipt.result.bounded_output
        window: dict[str, object] = {}
        if isinstance(output, RepoReadOutput):
            key = keys.setdefault(output.content_digest, f"c{len(keys) + 1:04d}")
            window = {
                "truncated": output.truncated,
                "content_key": key,
                "byte_count": output.byte_count,
            }
        calls.append(
            ExtractedCall.model_validate(
                {
                    "tool_id": receipt.tool_id,
                    "status": receipt.result.status,
                    "completed_at": receipt.result.completed_at,
                    **window,
                }
            )
        )
    return tuple(calls)


def _rate(count: int, total: int) -> Decimal:
    """Return *count* over *total*, or ``0`` when there is nothing to divide."""
    return Decimal(count) / Decimal(total) if total else Decimal(0)


def _seconds(start: datetime | None, end: datetime | None) -> Decimal | None:
    """Return the exact seconds from *start* to *end*, or ``None`` when either is unknown."""
    if start is None or end is None:
        return None
    return Decimal((end - start) // _ONE_MICROSECOND) / _MICROSECONDS
