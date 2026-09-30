"""The plain-text report of one Run, planned in full before a byte is written.

A report is a local file under ``.ea/local/``, one line per fact. It never implies more
than it holds: its head states who asked for it, the projection revision it was read at,
the redaction policy and every purged range before the first fact line; a purged range is
written as ``∅ purged`` in its own position rather than skipped; a part with no producer
says so instead of printing nothing; and secrets are never carried, whatever was asked,
and the report says that too.

The plan is the whole report. :class:`RunReportPlan` holds every line the file will carry,
so the consequence block printed before the write -- parts, sizes, redactions and
destination -- is counted off the exact bytes that land, not estimated.
"""

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Final

from eawf.kernel.projection.transcript import TranscriptBlock, build_transcript_blocks
from eawf.kernel.projection.truth import TruthState
from eawf.kernel.runtime.sandbox_decision import SandboxDecision
from eawf.kernel.state.epoch2.measurement import (
    CounterName,
    ExcludedRuntime,
    Observed,
    UncapturedRuntime,
    Unobserved,
)
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.measurement.fold import SubtreeFold
from eawf.observability.reflect.runs import RunReading
from eawf.platform.scrub.scan import redact_text

logger = logging.getLogger(__name__)


class ReportPartName(StrEnum):
    """The parts a Run report may carry, in render order."""

    TIMELINE = "timeline"
    USAGE_AND_COST = "usage_and_cost"
    TRANSCRIPT = "transcript"
    SECRETS = "secrets"  # pragma: allowlist secret
    SANDBOX_DECISIONS = "sandbox_decisions"


#: The parts a report carries when ``--parts`` names none.
DEFAULT_PARTS: Final[tuple[ReportPartName, ...]] = (
    ReportPartName.TIMELINE,
    ReportPartName.USAGE_AND_COST,
    ReportPartName.TRANSCRIPT,
)

#: What every report states about what it withholds and rewrites.
REDACTION_POLICY: Final = (
    "secrets are never included; local paths, hosts, local URLs and emails are"
    " scrubbed from every line"
)

#: The quotability mark every report carries.
QUOTABILITY: Final = (
    "quotable: this project's own Run, quotable into this project's committed artifacts"
    " and never another project's"
)

#: How a purged range reads in place of the facts it would have held.
PURGED_TOKEN: Final = "∅ purged"

#: What the secrets part says, whether or not it was asked for.
SECRETS_LINE: Final = "secrets never included: policy redacts secrets from every report"

#: What the sandbox part says of a Run the gateway decided nothing for.
NO_SANDBOX_DECISIONS: Final = "sandbox_decisions: no call of this Run was decided by the gateway"


def parse_parts(text: str | None) -> tuple[ReportPartName, ...]:
    """Return the parts a comma-separated ``--parts`` value names, in render order.

    Args:
        text: The option value, or ``None`` for the default parts.

    Returns:
        The named parts, each once, in the order :class:`ReportPartName` declares.

    Raises:
        ValueError: The value names no part, or names one that is not declared.
    """
    if text is None:
        return DEFAULT_PARTS
    names = {item.strip() for item in text.split(",") if item.strip()}
    if not names:
        raise ValueError("--parts names no part")
    known = {part.value for part in ReportPartName}
    unknown = sorted(names - known)
    if unknown:
        raise ValueError(f"unknown part {', '.join(unknown)}; the parts are {', '.join(known)}")
    return tuple(part for part in ReportPartName if part.value in names)


@dataclass(frozen=True, slots=True, kw_only=True)
class PlannedPart:
    """One part of the report and the lines it carries.

    Attributes:
        name: The part.
        included: Whether the report carries its facts; ``False`` only for secrets.
        lines: The part's lines, as written.
    """

    name: ReportPartName
    included: bool
    lines: tuple[str, ...]

    @property
    def size(self) -> str:
        """Return the part's size as the consequence block states it."""
        if not self.included:
            return "never · redacted by policy"
        count = len(self.lines)
        return f"{count} line" + ("" if count == 1 else "s")


@dataclass(frozen=True, slots=True, kw_only=True)
class RunReportPlan:
    """The whole report of one Run, and where it lands.

    Attributes:
        urn: The Run reported.
        destination: The file the report is written to.
        shown_destination: The destination relative to the repository root.
        head: The lines stated before the first fact line.
        parts: The planned parts, in render order.
        redactions: The lines the scrub rewrote.
    """

    urn: str
    destination: Path
    shown_destination: str
    head: tuple[str, ...]
    parts: tuple[PlannedPart, ...]
    redactions: int

    def text(self) -> str:
        """Return the file's content, one fact per line."""
        lines = [*self.head, *(line for part in self.parts for line in part.lines)]
        return "".join(f"{line}\n" for line in lines)

    def consequence_lines(self) -> list[str]:
        """Return the consequence block printed before the write."""
        return [
            f"consequence: run report {self.urn}",
            *(f"  part {part.name.value}: {part.size}" for part in self.parts),
            f"  redactions: {self.redactions} line"
            + ("" if self.redactions == 1 else "s")
            + " scrubbed; secrets never included",
            f"  destination: {self.shown_destination}",
            "  not: no canonical record moves; nothing leaves the machine",
        ]


def _timeline_line(block: TranscriptBlock) -> str:
    """Return one timeline fact."""
    if block.purged is not None:
        span = f"{block.purged.first}-{block.purged.last}"
        return f"timeline {span} {PURGED_TOKEN} ({block.purged.count} sequences)"
    return f"timeline {block.sequence} {block.at.isoformat()} {block.kind.value}"


def _transcript_line(block: TranscriptBlock) -> str:
    """Return one transcript fact."""
    if block.purged is not None:
        return f"transcript {block.purged.first}-{block.purged.last} {PURGED_TOKEN}"
    text = block.text
    said = text.value if text.state is TruthState.KNOWN else f"unknown: {text.missing_reason}"
    return f"transcript {block.sequence} {block.lane} {said}"


def _fold_lines(fold: SubtreeFold) -> tuple[str, ...]:
    """Return the facts of the Run's delegation subtree, folded into it."""
    lines = [
        f"usage_and_cost subtree {len(fold.descendant_keys)} descendants"
        f" · {fold.sources} sources · unmeasured {', '.join(fold.unmeasured_keys) or 'none'}"
    ]
    for name, counter in fold.counters.items():
        if isinstance(counter, Unobserved):
            lines.append(f"usage_and_cost subtree {name.value} unobserved: {counter.reason}")
            continue
        lines.append(
            f"usage_and_cost subtree {name.value} provider {counter.provider_total}"
            f" · inherited {counter.inherited_baseline} · steps {counter.accepted_step_total}"
            f" · root {counter.root_share} · descendants {counter.descendant_share}"
            f" · residual {counter.residual}"
        )
    if fold.unreconciled:
        names = ", ".join(name.value for name in fold.unreconciled)
        lines.append(f"usage_and_cost reconciliation failure: non-zero residual on {names}")
    return tuple(lines)


def _usage_lines(reading: RunReading) -> tuple[str, ...]:
    """Return the usage and cost facts the Run's captured runtime and its subtree state."""
    subtree = () if reading.fold is None else _fold_lines(reading.fold)
    return _own_usage_lines(reading) + subtree


def _own_usage_lines(reading: RunReading) -> tuple[str, ...]:
    """Return the usage and cost facts the Run's own captured runtime states."""
    captured = reading.run.captured_runtime
    if captured is None:
        return ("usage_and_cost unavailable: the Run holds no captured runtime",)
    if isinstance(captured, UncapturedRuntime):
        return (f"usage_and_cost unavailable: uncaptured ({captured.reason.value})",)
    if isinstance(captured, ExcludedRuntime):
        return (f"usage_and_cost excluded: {captured.reason.value} ({captured.detail})",)
    lines = [
        f"usage_and_cost quality {captured.measurement_quality.value}"
        f" · source {captured.source.value} · model {captured.model}"
    ]
    for name in CounterName:
        reading_ = captured.counters[name]
        lines.append(
            f"usage_and_cost {name.value} {reading_.value}"
            if isinstance(reading_, Observed)
            else f"usage_and_cost {name.value} unobserved: {reading_.reason}"
        )
    return tuple(lines)


def _part_lines(
    part: ReportPartName, reading: RunReading, blocks: tuple[TranscriptBlock, ...]
) -> tuple[str, ...]:
    """Return one requested part's unscrubbed lines."""
    if part is ReportPartName.TIMELINE:
        return tuple(_timeline_line(block) for block in blocks) or ("timeline: no events",)
    if part is ReportPartName.TRANSCRIPT:
        return tuple(_transcript_line(block) for block in blocks) or ("transcript: no events",)
    if part is ReportPartName.USAGE_AND_COST:
        return _usage_lines(reading)
    return tuple(_decision_line(item) for item in reading.sandbox_decisions) or (
        NO_SANDBOX_DECISIONS,
    )


def _decision_line(decision: SandboxDecision) -> str:
    """Return one sandbox decision fact: when, what, why, the rule and the policy revision."""
    return (
        f"sandbox_decisions {decision.decided_at.isoformat()} {decision.decision.value}"
        f" {decision.reason} · rule {decision.rule} · {decision.rule_value}"
        f" · sandbox policy · rev {decision.policy_revision}"
    )


def plan_run_report(
    reading: RunReading,
    *,
    parts: tuple[ReportPartName, ...],
    actor: str | None,
    tree_root: Path,
    on: date,
) -> RunReportPlan:
    """Return the full report of one Run and where it lands.

    Args:
        reading: The Run and its event lines.
        parts: The parts asked for.
        actor: The principal the report is authored by, or ``None`` when none was named.
        tree_root: The tree's ``.ea`` directory; the report lands under its ``local``.
        on: The report's date, which opens its file name.

    Returns:
        The plan: every line, scrubbed, with the count of lines the scrub rewrote.
    """
    run = reading.run
    blocks, purged = build_transcript_blocks(reading.events)
    ranges = ", ".join(f"{item.first}-{item.last}" for item in purged) or "none"
    asked = ", ".join(part.value for part in parts if part is not ReportPartName.SECRETS)
    head = (
        f"run report {run.urn}",
        f"author {actor if actor is not None else 'unattributed: no --actor was named'}",
        f"projection revision {reading.canonical_sequence} · run revision {run.revision}",
        f"redaction policy: {REDACTION_POLICY}",
        f"purged ranges: {ranges}",
        QUOTABILITY,
        f"parts: {asked or 'none'} · secrets never",
    )
    planned: list[PlannedPart] = []
    redactions = 0
    for part in ReportPartName:
        if part is ReportPartName.SECRETS:
            planned.append(PlannedPart(name=part, included=False, lines=(SECRETS_LINE,)))
            continue
        if part not in parts:
            continue
        raw = _part_lines(part, reading, blocks)
        scrubbed = tuple(redact_text(line) for line in raw)
        redactions += sum(1 for before, after in zip(raw, scrubbed, strict=True) if before != after)
        planned.append(PlannedPart(name=part, included=True, lines=scrubbed))
    name = f"{on.isoformat()}-run-report-{run.key}.txt"
    logger.debug(f"plan_run_report run={run.key} parts={asked} redactions={redactions}")
    return RunReportPlan(
        urn=str(run.urn),
        destination=tree_root / LOCAL_DIRNAME / name,
        shown_destination=f"{tree_root.name}/{LOCAL_DIRNAME}/{name}",
        head=head,
        parts=tuple(planned),
        redactions=redactions,
    )


def write_run_report(plan: RunReportPlan) -> Path:
    """Write the planned report atomically and return where it landed."""
    plan.destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.destination.with_name(f"{plan.destination.name}.tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_text(plan.text(), encoding="utf-8")
        os.replace(tmp, plan.destination)
    finally:
        tmp.unlink(missing_ok=True)
    return plan.destination


__all__ = [
    "DEFAULT_PARTS",
    "NO_SANDBOX_DECISIONS",
    "PURGED_TOKEN",
    "QUOTABILITY",
    "REDACTION_POLICY",
    "SECRETS_LINE",
    "PlannedPart",
    "ReportPartName",
    "RunReportPlan",
    "parse_parts",
    "plan_run_report",
    "write_run_report",
]
