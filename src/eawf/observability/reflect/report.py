"""The reflection report: build, render, store, export and prune, all on this machine.

Everything the reflection verbs write lands in one local collection, ``.ea/local/reflect``,
which is gitignored and retention-bounded, or at an output path the operator names. An
output path inside the tree's canonical store is refused before anything is read, so no
verb of the group can be turned into a canonical write by its arguments.

A report is a pure function of the projection it was built from and the titles the fill
resolved: it carries no clock reading and no fill counter, so two renders of one
projection are byte-identical. What the fill sent and answered goes to the manifest beside
the report instead.

A report lists only the sessions of the selected scope and window, and keeps prior
reflection sessions and the helper sessions they spawned out of that baseline: they
are listed in their own excluded section with the reason, since they are still
observed usage. Every session, totals and issue row says whether it is quotable: a row
of the tree's own project may be quoted into a committed artifact, any other may not.

A static export ships its data as a generated script chunk beside the page rather than as
a sibling file the page fetches, because a page opened from the filesystem cannot fetch on
every browser.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Final, Literal

import orjson
from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.identity import EntityKind, IdentityError, validate_entity_key
from eawf.kernel.runtime.events import MessageSummaryPayload, RunEventKind
from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.reflect.issues import HighlightedIssue, highlight_issues
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.titles import (
    REFLECTION_MARKER,
    FillAccount,
    ResolvedTitle,
    TitleCache,
    TitleSource,
    session_digest,
)
from eawf.observability.reflect.totals import RunSpend, TotalsRow, run_spend, scope_totals
from eawf.platform.scrub.scan import redact_text

logger = logging.getLogger(__name__)

#: The local collection the reflection verbs write under, below ``.ea/local``.
REFLECT_DIRNAME: Final = "reflect"

#: The title cache's file name inside the collection.
TITLE_CACHE_FILENAME: Final = "title-cache.json"

#: The directory static exports are written under, inside the collection.
EXPORT_DIRNAME: Final = "export"

#: The stem suffix of a report; the stem opens with the report's date.
REPORT_STEM_SUFFIX: Final = "-reflect-report"

#: The stem suffix of the fill manifest written beside a report.
MANIFEST_STEM_SUFFIX: Final = "-reflect-manifest"

#: The retention class of reports, manifests and exports.
REPORT_RETENTION: Final = timedelta(days=30)

#: The retention class of cached titles.
TITLE_CACHE_RETENTION: Final = timedelta(days=90)

#: The script chunk a static export ships its data in.
EXPORT_DATA_FILENAME: Final = "report-data.js"

#: The page a static export opens on.
EXPORT_PAGE_FILENAME: Final = "index.html"

#: The quotability mark closing a report row a committed artifact may quote.
QUOTABLE_MARK: Final = "quotable"

#: The quotability mark closing a report row no committed artifact may quote.
NON_QUOTABLE_MARK: Final = "non-quotable"

#: How a session reads where no reading priced its cost; never ``$0.00``.
UNPRICED_MARK: Final = "unpriced"

#: How a report opens the request of a prior reflection session.
_REFLECT_INVOCATIONS: Final = ("/reflect", "/eawf:reflect")

_DATED: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})-reflect-")
_WINDOW: Final = re.compile(r"^([1-9]\d{0,3})d$")


class CanonicalWriteRefusedError(ValueError):
    """An output path would land in the tree's canonical store."""


class SessionLine(BaseModel):
    """One listed session of the report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_key: str
    status: str
    runtime: str
    model_family: str
    wall_hours: Decimal | None
    model_steps: int
    tool_work: int
    subagents: int
    title: str | None
    title_source: TitleSource
    quotable: bool
    cost_microusd: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    cache_tokens: int | None = None


#: Why a Run is kept out of the baseline.
ExclusionReason = Literal["reflect_session", "helper_session"]


class ExcludedLine(BaseModel):
    """One session kept out of the baseline, listed with its reason and its cost.

    Attributes:
        run_key: The Run.
        reason: ``reflect_session`` for a prior reflection, ``helper_session`` for a
            session a reflection spawned.
        cost_microusd: Its cost, or ``None`` when no reading priced it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_key: str
    reason: ExclusionReason
    cost_microusd: int | None


class ReportScope(BaseModel):
    """The scope a report lists the sessions of.

    Attributes:
        kind: ``project`` for every Run the tree holds, ``milestone`` or ``track``
            for the Runs whose scope sits under that entity.
        key: The Milestone or Track key, ``None`` for ``project``.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["project", "milestone", "track"] = "project"
    key: str | None = None

    def text(self) -> str:
        """Return the scope as ``--scope`` spells it."""
        return self.kind if self.key is None else f"{self.kind}:{self.key}"

    def admits(self, reading: RunReading) -> bool:
        """Return whether *reading*'s Run is inside this scope."""
        if self.kind == "milestone":
            return reading.milestone_key == self.key
        if self.kind == "track":
            return self.key in reading.track_keys
        return True


class ReflectReport(BaseModel):
    """The machine form of one reflection report.

    Attributes:
        schema_version: The report shape's version.
        projects: The projects the listed sessions belong to.
        projection_revision: The canonical sequence the sessions were read at.
        title_fill: ``disabled`` under ``--local-only``, ``enabled`` otherwise.
        scope: The scope the sessions were selected from.
        window_days: The days back from the run a session must have started within,
            or ``None`` for no window.
        sessions: The listed sessions, in Run key order.
        totals: The money and tokens of the listed sessions, one row per project.
        issues: The issues the listed sessions show.
        excluded: The sessions of the scope and window kept out of the baseline.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = 2
    projects: tuple[str, ...]
    projection_revision: int
    title_fill: Literal["enabled", "disabled"]
    scope: ReportScope = Field(default_factory=ReportScope)
    window_days: int | None = None
    sessions: tuple[SessionLine, ...]
    totals: tuple[TotalsRow, ...] = ()
    issues: tuple[HighlightedIssue, ...] = ()
    excluded: tuple[ExcludedLine, ...] = ()


class FillManifest(BaseModel):
    """What the title fill of one run sent and answered."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    provider: str | None
    local_only: bool
    sessions_listed: int
    digests_sent: int
    cache_hits: int
    unknown_uncached: int
    titles_by_source: dict[str, int]


def reflect_root(tree_root: Path) -> Path:
    """Return the local collection the reflection verbs write under."""
    return tree_root / LOCAL_DIRNAME / REFLECT_DIRNAME


def resolve_output_path(tree_root: Path, out: Path) -> Path:
    """Return ``out`` resolved, refusing a path inside the canonical store.

    Args:
        tree_root: The tree's ``.ea`` directory.
        out: The output path the operator named.

    Returns:
        The absolute output path.

    Raises:
        CanonicalWriteRefusedError: ``out`` resolves inside ``.ea`` but outside its
            local tree, where every file is canonical state or a committed record.
    """
    resolved = out.resolve()
    store = tree_root.resolve()
    if resolved.is_relative_to(store) and not resolved.is_relative_to(store / LOCAL_DIRNAME):
        raise CanonicalWriteRefusedError(
            f"{out} is inside the canonical store; a reflection output stays under"
            f" {store.name}/{LOCAL_DIRNAME} or outside {store.name}"
        )
    return resolved


def parse_scope(text: str) -> ReportScope:
    """Return the scope a ``--scope`` value names.

    Args:
        text: ``project``, ``milestone:<KEY>`` or ``track:<KEY>``.

    Returns:
        The scope.

    Raises:
        ValueError: The value names another scope, or a key that is not a Milestone
            or Track key.
    """
    if text == "project":
        return ReportScope()
    kind, _, key = text.partition(":")
    if kind not in ("milestone", "track") or not key:
        raise ValueError(f"scope {text!r} is not one of project, milestone:<KEY> or track:<KEY>")
    entity = EntityKind.MILESTONE if kind == "milestone" else EntityKind.TRACK
    try:
        validate_entity_key(entity, key)
    except IdentityError as exc:
        raise ValueError(
            f"scope {text!r} is not one of project, milestone:<KEY> or track:<KEY>: {exc}"
        ) from exc
    return ReportScope(kind="milestone" if kind == "milestone" else "track", key=key)


def parse_window(text: str) -> int:
    """Return the days a ``--window`` value spans.

    Args:
        text: ``<N>d``, with ``N`` from 1 to 9999.

    Returns:
        ``N``.

    Raises:
        ValueError: The value is not a whole number of days.
    """
    found = _WINDOW.match(text)
    if found is None:
        raise ValueError(f"window {text!r} is not <N>d, such as 7d")
    return int(found.group(1))


def _opening_request(reading: RunReading) -> str | None:
    """Return the first user message the Run's stream summarized, or ``None``."""
    for event in reading.events:
        payload = event.payload
        if (
            event.quarantine is None
            and event.event_kind is RunEventKind.MESSAGE_SUMMARIZED
            and isinstance(payload, MessageSummaryPayload)
            and payload.message_role == "user"
        ):
            return payload.summary
    return None


def _exclusion(reading: RunReading, excluded: dict[str, ExclusionReason]) -> ExclusionReason | None:
    """Return why *reading* is kept out of the baseline, or ``None`` when it is not.

    A prior reflection opens with the skill's invocation; a helper session opens with
    the marker the title fill writes, or was delegated by a Run already excluded.
    """
    parent = reading.run.parent_run_ref
    if parent is not None and parent.entity_key in excluded:
        return "helper_session"
    request = _opening_request(reading)
    if request is None:
        return None
    if REFLECTION_MARKER in request:
        return "helper_session"
    first = request.strip().split(maxsplit=1)
    if first and first[0] in _REFLECT_INVOCATIONS:
        return "reflect_session"
    return None


@dataclass(frozen=True, slots=True, kw_only=True)
class Cohort:
    """The Runs of one scope and window, split into the baseline and the excluded.

    Attributes:
        sessions: The listed sessions, in Run key order.
        excluded: The Runs kept out of the baseline, each with its reason.
    """

    sessions: tuple[RunReading, ...]
    excluded: tuple[tuple[RunReading, ExclusionReason], ...]


def select_cohort(
    readings: Sequence[RunReading],
    *,
    scope: ReportScope,
    window_days: int | None,
    now: datetime,
) -> Cohort:
    """Return the Runs a report lists, and those it keeps out of the baseline.

    Args:
        readings: Every Run the tree holds, in Run key order, so a delegator is
            judged before the Runs it delegated.
        scope: The scope to keep.
        window_days: Keep only Runs started within this many days of ``now``; a Run
            that never started is outside every window. ``None`` keeps every Run.
        now: The report's clock.

    Returns:
        The cohort.
    """
    excluded: dict[str, ExclusionReason] = {}
    for reading in readings:
        reason = _exclusion(reading, excluded)
        if reason is not None:
            excluded[reading.run.key] = reason
    since = None if window_days is None else now - timedelta(days=window_days)
    selected = [
        reading
        for reading in readings
        if scope.admits(reading)
        and (
            since is None
            or (reading.run.started_at is not None and reading.run.started_at >= since)
        )
    ]
    logger.debug(
        f"select_cohort scope={scope.text()} window={window_days} selected={len(selected)}"
        f" excluded={sum(1 for item in selected if item.run.key in excluded)}"
    )
    return Cohort(
        sessions=tuple(item for item in selected if item.run.key not in excluded),
        excluded=tuple(
            (item, excluded[item.run.key]) for item in selected if item.run.key in excluded
        ),
    )


def build_report(
    cohort: Cohort,
    titles: Sequence[ResolvedTitle],
    *,
    scope: ReportScope,
    window_days: int | None,
    local_only: bool,
) -> ReflectReport:
    """Return the report of the listed sessions and their resolved titles.

    Args:
        cohort: The listed sessions and the excluded ones.
        titles: One title per listed session, in the same order.
        scope: The scope the cohort was selected from.
        window_days: The window the cohort was selected in, or ``None``.
        local_only: Whether the fill was disabled.

    Returns:
        The report. A row is quotable only when its Run belongs to the tree's own
        project and its title is not an excerpt.
    """
    readings = cohort.sessions
    spends: list[RunSpend] = [run_spend(reading) for reading in readings]
    sessions = []
    for reading, title, spend in zip(readings, titles, spends, strict=True):
        digest = session_digest(reading)
        sessions.append(
            SessionLine(
                run_key=reading.run.key,
                status=digest.terminal_observation,
                runtime=digest.runtime,
                model_family=digest.model_family,
                wall_hours=digest.wall_hours,
                model_steps=digest.model_steps,
                tool_work=digest.tool_work,
                subagents=digest.subagents,
                title=title.title,
                title_source=title.source,
                quotable=reading.own_project and title.source is not TitleSource.SCRUBBED_EXTRACT,
                cost_microusd=spend.cost_microusd,
                input_tokens=spend.input_tokens,
                output_tokens=spend.output_tokens,
                cache_tokens=spend.cache_tokens,
            )
        )
    return ReflectReport(
        projects=tuple(sorted({reading.run.urn.project_key for reading in readings})),
        projection_revision=max((reading.canonical_sequence for reading in readings), default=0),
        title_fill="disabled" if local_only else "enabled",
        scope=scope,
        window_days=window_days,
        sessions=tuple(sessions),
        totals=scope_totals(readings, spends),
        issues=highlight_issues(readings, spends),
        excluded=tuple(
            ExcludedLine(
                run_key=reading.run.key,
                reason=reason,
                cost_microusd=run_spend(reading).cost_microusd,
            )
            for reading, reason in cohort.excluded
        ),
    )


def fill_manifest(account: FillAccount, *, sessions: int) -> FillManifest:
    """Return the manifest of one fill."""
    return FillManifest(
        provider=account.provider,
        local_only=account.local_only,
        sessions_listed=sessions,
        digests_sent=account.digests_sent,
        cache_hits=account.cache_hits,
        unknown_uncached=account.unknown_uncached,
        titles_by_source=dict(account.by_source),
    )


def _mark(quotable: bool) -> str:
    """Return the quotability mark a row closes with."""
    return QUOTABLE_MARK if quotable else NON_QUOTABLE_MARK


def _cost(value: int | None) -> str:
    """Return a cost in USD, or the unpriced mark where no reading priced it."""
    return UNPRICED_MARK if value is None else f"${Decimal(value) / Decimal(1_000_000):.4f}"


def _count(value: int | None) -> str:
    """Return a token count, or ``unreported`` where no reading counted it."""
    return "unreported" if value is None else str(value)


def _session_text(line: SessionLine) -> str:
    """Return one session's report line."""
    wall = "unended" if line.wall_hours is None else f"{line.wall_hours}h"
    title = "∅ no title" if line.title is None else f'"{line.title}"'
    return (
        f"session {line.run_key} {line.status} {line.runtime} {line.model_family}"
        f" wall {wall} steps {line.model_steps} tools {line.tool_work}"
        f" subagents {line.subagents} cost {_cost(line.cost_microusd)}"
        f" · title {title} [{line.title_source.value}] · {_mark(line.quotable)}"
    )


def _totals_text(row: TotalsRow) -> str:
    """Return one totals row's report line."""
    return (
        f"totals {row.scope} cost {_cost(row.cost_microusd)}"
        f" · priced {row.priced_runs} · unpriced {row.unpriced_runs}"
        f" · tokens in {_count(row.input_tokens)} out {_count(row.output_tokens)}"
        f" cache {_count(row.cache_tokens)} · {_mark(row.quotable)}"
    )


def _issue_text(issue: HighlightedIssue) -> str:
    """Return one issue's report line."""
    return (
        f"issue {issue.kind.value} {issue.subject}"
        f" · runs {', '.join(issue.evidence_rows)} · {_mark(issue.quotable)}"
    )


def _excluded_text(line: ExcludedLine) -> str:
    """Return one excluded session's report line."""
    return f"excluded {line.run_key} {line.reason} cost {_cost(line.cost_microusd)}"


def render_text(report: ReflectReport) -> str:
    """Return the plain-text report, one fact per line, scrubbed line by line."""
    fill = (
        "title fill: disabled (--local-only); every title is from the fallback chain"
        if report.title_fill == "disabled"
        else "title fill: enabled; each title states its source"
    )
    window = "all time" if report.window_days is None else f"last {report.window_days}d"
    lines = [
        f"reflect report · projects {', '.join(report.projects) or 'none'}"
        f" · projection revision {report.projection_revision}",
        f"selection: scope {report.scope.text()} · window {window}",
        fill,
        "quotability: a row of this tree's own project is quotable; any other is non-quotable",
        f"sessions: {len(report.sessions)}",
        *(_session_text(line) for line in report.sessions),
        f"totals: {len(report.totals)}",
        *(_totals_text(row) for row in report.totals),
        f"issues: {len(report.issues)}",
        *(_issue_text(issue) for issue in report.issues),
        f"excluded from the baseline: {len(report.excluded)}",
        *(_excluded_text(line) for line in report.excluded),
    ]
    return "".join(f"{redact_text(line)}\n" for line in lines)


def _atomic_write(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{secrets.token_hex(4)}")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True, kw_only=True)
class StoredReport:
    """Where one run's files landed.

    Attributes:
        text: The plain-text report.
        document: The machine form beside it.
        manifest: The fill manifest beside them.
    """

    text: Path
    document: Path
    manifest: Path


def store_report(
    root: Path,
    report: ReflectReport,
    manifest: FillManifest,
    *,
    on: date,
    out: Path | None,
) -> StoredReport:
    """Write the report, its machine form and the fill manifest.

    Args:
        root: The local collection.
        report: The report.
        manifest: The fill's manifest.
        on: The report's date, which opens every file stem.
        out: The resolved text path the operator named, or ``None`` for the
            collection's own dated name. The machine form and the manifest land
            beside it, so a named output leaves the collection untouched; the
            machine form takes the text's name with a ``.json`` suffix, or with
            ``.report.json`` when the text is itself named ``.json``.

    Returns:
        The three paths written.
    """
    stem = f"{on.isoformat()}{REPORT_STEM_SUFFIX}"
    text = out if out is not None else root / f"{stem}.txt"
    document = text.with_suffix(".report.json" if text.suffix == ".json" else ".json")
    manifest_path = text.parent / f"{on.isoformat()}{MANIFEST_STEM_SUFFIX}.json"
    _atomic_write(text, render_text(report).encode("utf-8"))
    _atomic_write(document, report.model_dump_json(indent=2).encode("utf-8"))
    _atomic_write(manifest_path, manifest.model_dump_json(indent=2).encode("utf-8"))
    logger.debug(f"store_report sessions={len(report.sessions)} stem={stem}")
    return StoredReport(text=text, document=document, manifest=manifest_path)


def latest_report(root: Path) -> Path | None:
    """Return the newest report document in the collection, or ``None``."""
    found = sorted(root.glob(f"*{REPORT_STEM_SUFFIX}.json"))
    return found[-1] if found else None


def load_report(path: Path) -> ReflectReport:
    """Return the report document at ``path``, upgrading one stored at schema 1.

    Schema 2 only added fields with defaults -- the scope, the window, the totals,
    the issues, the exclusions and each session's spend -- so a schema-1 document
    reads as schema 2 with those empty, which is what its run measured.

    Raises:
        orjson.JSONDecodeError: The file is not JSON.
        pydantic.ValidationError: The file is not a report document of either schema.
    """
    payload = orjson.loads(path.read_bytes())
    if isinstance(payload, dict) and payload.get("schema_version") == 1:
        payload["schema_version"] = 2
    return ReflectReport.model_validate(payload)


_PAGE: Final = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>Reflect report</title></head>
<body>
<h1>Reflect report</h1>
<p id="head"></p>
<table id="sessions"><thead><tr>
<th>Run</th><th>Status</th><th>Runtime</th><th>Model</th><th>Wall h</th>
<th>Steps</th><th>Tools</th><th>Subagents</th><th>Title</th><th>Source</th><th>Quotable</th>
</tr></thead><tbody></tbody></table>
<script src="report-data.js"></script>
<script>
const report = window.EAWF_REFLECT_REPORT;
document.getElementById("head").textContent =
  "projects " + (report.projects.join(", ") || "none") +
  " · projection revision " + report.projection_revision +
  " · title fill " + report.title_fill;
const body = document.querySelector("#sessions tbody");
for (const s of report.sessions) {
  const row = body.insertRow();
  for (const v of [s.run_key, s.status, s.runtime, s.model_family, s.wall_hours ?? "unended",
                   s.model_steps, s.tool_work, s.subagents, s.title ?? "no title",
                   s.title_source, s.quotable ? "yes" : "no"]) {
    row.insertCell().textContent = String(v);
  }
}
</script>
</body>
</html>
"""


def export_report(root: Path, document: Path, *, dest: Path | None) -> Path:
    """Write a static page for one report, its data as a generated script chunk.

    Args:
        root: The local collection.
        document: The report document to export.
        dest: The resolved directory the operator named, or ``None`` for one named
            after the report under the collection's export directory.

    Returns:
        The page's path.

    Raises:
        pydantic.ValidationError: ``document`` is not a report document.
    """
    report = load_report(document)
    target = dest if dest is not None else root / EXPORT_DIRNAME / document.stem
    payload = orjson.dumps(report.model_dump(mode="json")).decode("utf-8").replace("</", "<\\/")
    _atomic_write(
        target / EXPORT_DATA_FILENAME, f"window.EAWF_REFLECT_REPORT = {payload};\n".encode()
    )
    _atomic_write(target / EXPORT_PAGE_FILENAME, _PAGE.encode("utf-8"))
    return target / EXPORT_PAGE_FILENAME


def _dated(name: str) -> date | None:
    """Return the date a collection entry's name opens with, or ``None``."""
    found = _DATED.match(name)
    return None if found is None else date.fromisoformat(found.group(1))


@dataclass(frozen=True, slots=True, kw_only=True)
class PruneResult:
    """What one prune removed.

    Attributes:
        removed: The collection entries removed, relative to the collection.
        titles_removed: The cached titles dropped.
        cache: The cache after the prune.
    """

    removed: tuple[str, ...]
    titles_removed: int
    cache: TitleCache


def prune_collection(root: Path, cache: TitleCache, *, now: datetime) -> PruneResult:
    """Remove every dated entry and cached title past its retention class.

    Only entries of the local collection are considered, so a committed collection is
    never in reach.

    Args:
        root: The local collection.
        cache: The title cache as read.
        now: The prune's clock.

    Returns:
        What was removed, and the cache that remains.
    """
    cutoff = (now - REPORT_RETENTION).date()
    stale = [
        path
        for directory in (root, root / EXPORT_DIRNAME)
        if directory.is_dir()
        for path in sorted(directory.iterdir())
        if (when := _dated(path.name)) is not None and when < cutoff
    ]
    for path in stale:
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink()
    kept = {
        key: entry
        for key, entry in cache.entries.items()
        if entry.stored_at >= now - TITLE_CACHE_RETENTION
    }
    logger.debug(f"prune_collection removed={len(stale)} titles={len(cache.entries) - len(kept)}")
    return PruneResult(
        removed=tuple(str(path.relative_to(root)) for path in stale),
        titles_removed=len(cache.entries) - len(kept),
        cache=TitleCache(entries=kept),
    )


__all__ = [
    "EXPORT_DATA_FILENAME",
    "EXPORT_DIRNAME",
    "EXPORT_PAGE_FILENAME",
    "MANIFEST_STEM_SUFFIX",
    "NON_QUOTABLE_MARK",
    "QUOTABLE_MARK",
    "REFLECT_DIRNAME",
    "REPORT_RETENTION",
    "REPORT_STEM_SUFFIX",
    "TITLE_CACHE_FILENAME",
    "TITLE_CACHE_RETENTION",
    "UNPRICED_MARK",
    "CanonicalWriteRefusedError",
    "Cohort",
    "ExcludedLine",
    "ExclusionReason",
    "FillManifest",
    "PruneResult",
    "ReflectReport",
    "ReportScope",
    "SessionLine",
    "StoredReport",
    "build_report",
    "export_report",
    "fill_manifest",
    "latest_report",
    "load_report",
    "parse_scope",
    "parse_window",
    "prune_collection",
    "reflect_root",
    "render_text",
    "resolve_output_path",
    "select_cohort",
    "store_report",
]
