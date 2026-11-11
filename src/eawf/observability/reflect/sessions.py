"""One reflect run's session step: index local history, analyse it, title and page it.

The step holds the history index for the whole of its work, so a second run on the
same index waits instead of failing on the database lock. The index lands in the
tree's local collection, or beside an ``--out`` path that git would not commit; the
analysis and the pages over the scope's sessions land in the output area. Because the
index holds every project's sessions, a committed output area refuses them before
anything is written into it.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from eawf.observability.reflect import analysis as an
from eawf.observability.reflect.html import PageSources, write_pages
from eawf.observability.reflect.html.pages import reflect_headline
from eawf.observability.reflect.preamble import read_memory_files
from eawf.observability.reflect.report import ReportScope, reflect_root
from eawf.observability.reflect.runs import ProgressSink, tree_project_code
from eawf.observability.reflect.scopes import OutputArea, resolve_session_scope
from eawf.observability.reflect.session_titles import fill_session_titles
from eawf.observability.reflect.titles import CallBudget, FillAccount, TitleCache, TitleProvider
from eawf.observability.telemetry.projector import LocalHistory, history_index_lock
from eawf.observability.telemetry.store import SqliteMetricsStore

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True, kw_only=True)
class SessionRun:
    """What one run's session step wrote and answered.

    Attributes:
        analysis: The analysis document.
        overview: The overview page.
        cache: The title cache after the fill.
        account: What the session title fill sent and answered.
    """

    analysis: Path
    overview: Path
    cache: TitleCache
    account: FillAccount


def reflect_sessions(
    tree_root: Path,
    *,
    report_scope: ReportScope,
    area: OutputArea,
    history: LocalHistory,
    harness_store: Path,
    cache: TitleCache,
    provider: TitleProvider | None,
    budget: CallBudget,
    local_only: bool,
    now: datetime,
    on_wait: Callable[[], None],
    progress: ProgressSink | None = None,
) -> SessionRun:
    """Index local history, analyse it, title the scope's sessions and write the result.

    This project's sessions are labelled with the tree's project code, or with the
    repository's directory name when the tree holds no project row.

    Args:
        tree_root: The tree's ``.ea`` directory.
        report_scope: The parsed ``--scope``.
        area: Where the analysis and the pages go.
        history: This machine's session history.
        harness_store: The harness's history directory for this project, whose
            memory index the preamble rebuild reads.
        cache: The title cache as read.
        provider: The title provider, or ``None`` when none may be called.
        budget: The provider calls the run may still make.
        local_only: Whether ``--local-only`` is in force.
        now: The run's instant.
        on_wait: Called once when another run holds the history index.
        progress: Called with each stage's progress.

    Returns:
        What the step wrote and answered.

    Raises:
        ForeignRowsRefusedError: *area* is committed and the index holds sessions of
            other projects; nothing has been written into *area*.
        IndexBusyError: Another run held the index for longer than a run waits.
    """
    project_root = tree_root.parent
    label = tree_project_code(tree_root) or project_root.name
    scope = resolve_session_scope(report_scope, project_root=project_root, label=label)
    # a committed area may still refuse, so the index waits in the collection
    index_root = reflect_root(tree_root) if area.committed else area.directory
    index_root.mkdir(parents=True, exist_ok=True)
    index_path = index_root / an.HISTORY_INDEX_FILENAME
    with history_index_lock(index_path, on_wait=on_wait):
        store = SqliteMetricsStore(index_path)
        try:
            store.init_schema()
            history.index(store, project_id=scope.label, progress=progress)
            indexed = an.load_indexed_history(store)
            area.admit(scope.foreign_rows(row.cwd_digest for row in indexed.sessions))
            covered = frozenset(
                row.vendor_session_ref for row in indexed.sessions if scope.covers(row.cwd_digest)
            )
            details = {
                row.vendor_session_ref: detail
                for row in indexed.sessions
                if row.vendor_session_ref in covered
                and (detail := history.detail(store, row.vendor_session_ref)) is not None
            }
        finally:
            store.close()
        memory, _limits = read_memory_files(project_root, harness_store=harness_store)
        sessions = an.history_sessions(
            indexed,
            details=details,
            # only this project's memory files are read; other projects' stay unknown
            memory_files=dict.fromkeys(scope.own, memory),
            project_labels=scope.labels,
        )
        titles, account, cache = fill_session_titles(
            [session for session in sessions if session.session_ref in covered],
            cache=cache,
            provider=provider,
            local_only=local_only,
            now=now,
            budget=budget,
        )
        analysis = an.analyze_sessions(
            sessions, requests=indexed.requests, now=now, titles=titles, progress=progress
        )
        sources = PageSources(
            analysis=analysis,
            session_refs=covered,
            requests=indexed.requests,
            details=details,
            contexts={s.session_ref: s.context for s in sessions if s.context is not None},
            history_files=history.file_count,
            scope_label=scope.label,
        )
        analysis_path = an.write_analysis(area.directory, analysis, on=now.date())
        overview = write_pages(
            area.directory,
            sources,
            headline=reflect_headline(project_root),
            progress=progress,
        ).overview
    logger.debug(
        f"reflect_sessions scope={scope.label} sessions={len(sessions)} covered={len(covered)}"
    )
    return SessionRun(analysis=analysis_path, overview=overview, cache=cache, account=account)


__all__ = ["SessionRun", "reflect_sessions"]
