"""The reflection report: build, render, store, export and prune, all on this machine.

Everything the reflection verbs write lands in one local collection, ``.ea/local/reflect``,
which is gitignored and retention-bounded, or at an output path the operator names. An
output path inside the tree's canonical store is refused before anything is read, so no
verb of the group can be turned into a canonical write by its arguments.

A report is a pure function of the projection it was built from and the titles the fill
resolved: it carries no clock reading and no fill counter, so two renders of one
projection are byte-identical. What the fill sent and answered goes to the manifest beside
the report instead.

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
from pydantic import BaseModel, ConfigDict

from eawf.kernel.store.paths import LOCAL_DIRNAME
from eawf.observability.reflect.runs import RunReading
from eawf.observability.reflect.titles import (
    FillAccount,
    ResolvedTitle,
    TitleCache,
    TitleSource,
    session_digest,
)
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

_DATED: Final = re.compile(r"^(\d{4}-\d{2}-\d{2})-reflect-")


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


class ReflectReport(BaseModel):
    """The machine form of one reflection report.

    Attributes:
        schema_version: The report shape's version.
        projects: The projects the listed sessions belong to.
        projection_revision: The canonical sequence the sessions were read at.
        title_fill: ``disabled`` under ``--local-only``, ``enabled`` otherwise.
        sessions: The listed sessions, in Run key order.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    projects: tuple[str, ...]
    projection_revision: int
    title_fill: Literal["enabled", "disabled"]
    sessions: tuple[SessionLine, ...]


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


def build_report(
    readings: Sequence[RunReading], titles: Sequence[ResolvedTitle], *, local_only: bool
) -> ReflectReport:
    """Return the report of the listed sessions and their resolved titles.

    Args:
        readings: The listed sessions, in the order their titles were resolved.
        titles: One title per reading, in the same order.
        local_only: Whether the fill was disabled.

    Returns:
        The report. Every row is this project's own, so every row is quotable.
    """
    sessions = []
    for reading, title in zip(readings, titles, strict=True):
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
                quotable=title.source is not TitleSource.SCRUBBED_EXTRACT,
            )
        )
    return ReflectReport(
        projects=tuple(sorted({reading.run.urn.project_key for reading in readings})),
        projection_revision=max((reading.canonical_sequence for reading in readings), default=0),
        title_fill="disabled" if local_only else "enabled",
        sessions=tuple(sessions),
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


def _session_text(line: SessionLine) -> str:
    """Return one session's report line."""
    wall = "unended" if line.wall_hours is None else f"{line.wall_hours}h"
    title = "∅ no title" if line.title is None else f'"{line.title}"'
    mark = QUOTABLE_MARK if line.quotable else NON_QUOTABLE_MARK
    return (
        f"session {line.run_key} {line.status} {line.runtime} {line.model_family}"
        f" wall {wall} steps {line.model_steps} tools {line.tool_work}"
        f" subagents {line.subagents} · title {title} [{line.title_source.value}] · {mark}"
    )


def render_text(report: ReflectReport) -> str:
    """Return the plain-text report, one fact per line, scrubbed line by line."""
    fill = (
        "title fill: disabled (--local-only); every title is from the fallback chain"
        if report.title_fill == "disabled"
        else "title fill: enabled; each title states its source"
    )
    lines = [
        f"reflect report · projects {', '.join(report.projects) or 'none'}"
        f" · projection revision {report.projection_revision}",
        fill,
        "quotability: every row is this project's own; no cross-project row is listed",
        f"sessions: {len(report.sessions)}",
        *(_session_text(line) for line in report.sessions),
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
        manifest: The fill manifest in the local collection.
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
            collection's own dated name. The machine form lands beside it.

    Returns:
        The three paths written.
    """
    stem = f"{on.isoformat()}{REPORT_STEM_SUFFIX}"
    text = out if out is not None else root / f"{stem}.txt"
    document = text.with_suffix(".json")
    manifest_path = root / f"{on.isoformat()}{MANIFEST_STEM_SUFFIX}.json"
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
    """Return the report document at ``path``.

    Raises:
        pydantic.ValidationError: The file is not a report document.
    """
    return ReflectReport.model_validate_json(path.read_bytes())


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
    "CanonicalWriteRefusedError",
    "FillManifest",
    "PruneResult",
    "ReflectReport",
    "SessionLine",
    "StoredReport",
    "build_report",
    "export_report",
    "fill_manifest",
    "latest_report",
    "load_report",
    "prune_collection",
    "reflect_root",
    "render_text",
    "resolve_output_path",
    "store_report",
]
