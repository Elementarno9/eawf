"""Report rendered projections whose stamp lags the installed rule graph.

Every stamped projection names the effective-graph digest it was rendered
from. The installed package compiles the graph afresh; a projection whose
stamp names a different digest, or that was never rendered, delivers policy
the installed package no longer holds. Nothing blocks a commit or a push on
that drift, and stale policy does its damage in repositories that are merely
read, so a session-start check compares the stamps and reports the
divergence once per session, naming the refresh command. A rule source or
render failure is reported the same way, as one typed line naming its code and
the refresh command, rather than escaping into the session as a traceback.

The import shim carries no stamp and no prose, so it has nothing to lag.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path, PurePosixPath
from typing import Final

from pydantic import ValidationError

from eawf.platform.rules.carriers import carrier_stamp_line
from eawf.platform.rules.compile import RuleCompileError
from eawf.platform.rules.host_facts import HostFactError
from eawf.platform.rules.loader import RuleSourceError
from eawf.platform.rules.modules import RuleModuleError
from eawf.platform.rules.records import RuleModel
from eawf.platform.rules.render import (
    ProjectionPlan,
    RuleProjectionError,
    plan_rule_projections,
    rule_source_present,
)

logger = logging.getLogger(__name__)

#: The command that re-renders every projection from the installed graph.
REFRESH_COMMAND: Final[str] = "eawf sync"

#: Machine-local record of the sessions already told, relative to the
#: repository root.
REPORTED_SESSIONS_PATH: Final[str] = ".ea/local/rule-staleness-sessions.json"

#: How many recent session ids the record keeps; enough for every session
#: open on one machine at a time, small enough that the file never grows.
REPORTED_SESSIONS_KEPT: Final[int] = 64

#: The refusals a projection plan raises; each carries a stable ``code``.
RuleFailure = (
    RuleSourceError | RuleModuleError | RuleCompileError | RuleProjectionError | HostFactError
)

_RULE_FAILURES: Final = (
    RuleSourceError,
    RuleModuleError,
    RuleCompileError,
    RuleProjectionError,
    HostFactError,
)

_GRAPH_STAMP: Final[re.Pattern[str]] = re.compile(
    r"^<!-- eawf:projection kind=\S+ graph=(?P<graph>sha256:[0-9a-f]{64}) "
)


class StaleProjection(RuleModel):
    """One projection whose stamp does not name the installed graph.

    Attributes:
        target: Repository-relative path of the projection.
        stamped_digest: The graph digest its stamp names; ``None`` when the
            file is absent or carries no stamp.
        current_digest: The digest the installed graph renders it from.
    """

    target: str
    stamped_digest: str | None
    current_digest: str


class _ReportedSessions(RuleModel):
    """The on-disk record of sessions already told about staleness.

    Attributes:
        session_ids: Most recent last.
    """

    session_ids: tuple[str, ...] = ()


def stamp_graph_digest(text: str) -> str | None:
    """Return the graph digest a projection's stamp names.

    Args:
        text: The complete file content of a projection, a view or a carrier.

    Returns:
        The stamped digest, or ``None`` when ``text`` carries no stamp.
    """
    stamp = carrier_stamp_line(text)
    if stamp is None:
        stamp = text.partition("\n")[0]
    match = _GRAPH_STAMP.match(stamp)
    return None if match is None else match.group("graph")


def projection_staleness(repo_root: Path, plan: ProjectionPlan) -> tuple[StaleProjection, ...]:
    """Compare each stamped output of ``plan`` with the file on disk.

    Args:
        repo_root: The repository root.
        plan: What the installed package renders now.

    Returns:
        Every stamped target whose on-disk stamp names another graph or is
        missing, in render order.
    """
    stale: list[StaleProjection] = []
    for target, text in plan.outputs:
        current = stamp_graph_digest(text)
        if current is None:
            continue
        path = repo_root.joinpath(*PurePosixPath(target).parts)
        stamped = stamp_graph_digest(path.read_text(encoding="utf-8")) if path.is_file() else None
        if stamped != current:
            stale.append(
                StaleProjection(target=target, stamped_digest=stamped, current_digest=current)
            )
    return tuple(stale)


def staleness_report(stale: tuple[StaleProjection, ...]) -> str | None:
    """Render the one-line staleness report.

    Args:
        stale: The stale projections.

    Returns:
        One line naming the stale targets and :data:`REFRESH_COMMAND`, or
        ``None`` when nothing is stale.
    """
    if not stale:
        return None
    targets = ", ".join(item.target for item in stale)
    return (
        f"eawf rules: {len(stale)} rendered projection(s) do not match the installed rule "
        f"graph ({targets}); run `{REFRESH_COMMAND}` to refresh them"
    )


def rule_failure_report(error: RuleFailure) -> str:
    """Render a rule source or render failure as the one-line session report.

    Args:
        error: The refusal the projection plan raised.

    Returns:
        One line naming the failure code, the reason and
        :data:`REFRESH_COMMAND`.
    """
    reason = " ".join(str(error).split()) or type(error).__name__
    return (
        f"eawf rules: the rule projections could not be checked ({error.code}: {reason}); "
        f"fix the rule source, then run `{REFRESH_COMMAND}`"
    )


def report_projection_staleness_once(
    repo_root: Path, *, session_id: str | None, home: Path | None = None
) -> str | None:
    """Return the staleness report the first time a session asks for it.

    Args:
        repo_root: The repository root.
        session_id: The runtime session id; ``None`` when the runtime sent
            none, in which case every call reports, since there is no
            session to remember.
        home: The directory holding the ``.eawf`` home; ``None`` for the
            user's home directory.

    Returns:
        The one-line report, or ``None`` when the repository has no rule
        source, every stamp matches, or this session was already told. A
        rule source that fails to load, compile or render yields the
        :func:`rule_failure_report` line instead of raising.
    """
    if not rule_source_present(repo_root):
        return None
    reported = _read_reported(repo_root)
    if session_id is not None and session_id in reported.session_ids:
        return None
    try:
        plan = plan_rule_projections(repo_root, home=home)
    except _RULE_FAILURES as exc:
        logger.warning(f"rule projections unplannable code={exc.code} error={exc}")
        report: str | None = rule_failure_report(exc)
    else:
        report = staleness_report(projection_staleness(repo_root, plan))
    if report is None:
        return None
    if session_id is not None:
        kept = (*reported.session_ids, session_id)[-REPORTED_SESSIONS_KEPT:]
        _write_reported(repo_root, _ReportedSessions(session_ids=kept))
    logger.info(f"rule projections stale session_id={session_id} report={report!r}")
    return report


def _read_reported(repo_root: Path) -> _ReportedSessions:
    """Read the sessions already told.

    Args:
        repo_root: The repository root.

    Returns:
        The record; empty when absent or unreadable, since the worst case is
        one repeated report.
    """
    path = repo_root.joinpath(*PurePosixPath(REPORTED_SESSIONS_PATH).parts)
    if not path.is_file():
        return _ReportedSessions()
    try:
        return _ReportedSessions.model_validate_json(path.read_bytes())
    except ValidationError as exc:
        logger.warning(f"rule staleness session record unreadable path={path} error={exc}")
        return _ReportedSessions()


def _write_reported(repo_root: Path, record: _ReportedSessions) -> None:
    """Replace the record of sessions already told.

    Args:
        repo_root: The repository root.
        record: The record to write.
    """
    path = repo_root.joinpath(*PurePosixPath(REPORTED_SESSIONS_PATH).parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    tmp.write_text(json.dumps(record.model_dump(mode="json")) + "\n", encoding="utf-8")
    tmp.replace(path)


__all__ = [
    "REFRESH_COMMAND",
    "REPORTED_SESSIONS_KEPT",
    "REPORTED_SESSIONS_PATH",
    "RuleFailure",
    "StaleProjection",
    "projection_staleness",
    "report_projection_staleness_once",
    "rule_failure_report",
    "staleness_report",
    "stamp_graph_digest",
]
