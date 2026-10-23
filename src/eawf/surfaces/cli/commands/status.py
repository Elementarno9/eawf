"""``eawf status`` — the native summary, recent decisions, blockers, git head.

Output payload (JSON envelope, all keys always present):

.. code-block:: json

    {
      "git": {"head": "<sha>" | null,
              "branch": "<name>" | null,
              "dirty": true | false | null},
      "authority": {"epoch": 1 | 2,
                    "gap": "undeclared"|"marker_absent"|"marker_unreadable" | null,
                    "generation_id": "<gen-id>" | null},
      "native": {"track_key": "<key>" | null,
                 "milestone_key": "<key>" | null,
                 "open_task_count": <int>,
                 "open_tasks": [{"key", "status", "title"}, ...]} | null,
      "recent_decisions": [{"id", "title", "status"}, ...],
      "blockers": ["<short-text>", ...]
    }

Status reads only the selected generation of the tree it resolves; the v1
``state.json`` is never opened, so a tree born by ``eawf init`` without one
reports like any other. The tree is found by the resolver's walk, which
stops at the epoch marker as well as at the v1 document. ``native`` is
``null`` on a tree that is not at epoch 2, whose ``blockers`` then name the
gap, and on an epoch-2 tree whose generation document does not exist.
Status reads git once for ``git``.

Exit codes:

- ``0`` on success, including an epoch-2 tree whose decision ledger is
  unreadable: its decisions are left out and ``blockers`` names the cause.
- ``1`` (``USER_ERROR``) when the resolved ``.ea`` directory does not exist
  (``data.kind`` ``NotFound``), or when the generation document fails to
  load (``data.kind`` ``InvalidInput``; callers should rerun
  ``eawf validate`` to inspect).

The text branch prints a compact human summary; the JSON branch is the
canonical machine surface and the only one the acceptance gate inspects.
"""

from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any

import typer

from eawf.kernel.state.resolve import resolve_with_reason
from eawf.platform.subprocess_detach import no_window_kwargs
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

if TYPE_CHECKING:
    from eawf.kernel.state.epoch2.decision import Decision

logger = logging.getLogger(__name__)

_GIT_TIMEOUT_SECONDS: float = 5.0

#: The console route whose read model holds every Track and Milestone.
_POINTER_ROUTE = "track"


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Return ``git`` stdout (stripped) or ``None`` on any failure.

    Wraps :func:`subprocess.run` with a hard timeout so a hung git invocation
    cannot stall ``eawf status``. Any non-zero exit, missing binary, or
    timeout collapses to ``None`` — callers must treat git fields as
    optional. Successful runs return the stripped stdout (which may be the
    empty string, distinct from ``None``).
    """
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_SECONDS,
            **no_window_kwargs(),
        )
    except subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired:
        return None
    return proc.stdout.strip()


def _find_git_root(start: Path) -> Path:
    """Walk up from *start* to the first ancestor containing ``.git``.

    Falls back to *start* (resolved) when no ``.git`` is found in the
    ancestor chain — git invocations from there will return ``None``
    via :func:`_run_git`'s safe-degrade path. Used so a custom
    ``--state-path`` outside ``<repo>/.ea/`` still locates the
    enclosing repo for the head/branch/dirty queries.
    """
    p = start.resolve()
    for ancestor in (p, *p.parents):
        if (ancestor / ".git").exists():
            return ancestor
    return p


def _git_info(cwd: Path) -> dict[str, str | bool | None]:
    """Return ``{head, branch, dirty}`` for *cwd*.

    Each field is ``None`` independently when its query fails. A clean tree
    yields ``dirty=False`` (porcelain returned empty stdout); a dirty tree
    yields ``dirty=True``; a failed porcelain query collapses ``dirty`` to
    ``None``. ``head`` is the empty-string-coerced-None when stdout is empty
    so callers can render it as ``"<unknown>"``.
    """
    head_raw = _run_git(["rev-parse", "HEAD"], cwd)
    branch_raw = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd)
    porcelain = _run_git(["status", "--porcelain"], cwd)
    head: str | None = head_raw if head_raw else None
    branch: str | None = branch_raw if branch_raw else None
    dirty: bool | None = bool(porcelain) if porcelain is not None else None
    return {"head": head, "branch": branch, "dirty": dirty}


def _recent_decisions(native: tuple[Decision, ...], limit: int = 5) -> list[dict[str, str]]:
    """Return up to *limit* most-recent native decisions, newest first by ``created_at``.

    The status line carries only the id, the bounded title and the
    lifecycle status, lower-cased so a superseded decision reads as
    visibly historical.

    Args:
        native: The selected generation's native decisions.
        limit: How many decisions to list.

    Returns:
        One ``{"id", "title", "status"}`` row each, sorted by ``created_at``
        descending with the key as a stable tiebreaker so two decisions
        stamped in the same instant order deterministically across renders.
    """
    ranked = sorted(native, key=lambda d: (d.created_at, d.key), reverse=True)
    return [
        {"id": d.key, "title": d.title, "status": d.status.value.lower()} for d in ranked[:limit]
    ]


def status(
    ctx: typer.Context,
    workspace: Annotated[
        Path | None,
        typer.Option(
            "-w",
            "--workspace",
            help="Workspace root whose .ea tree status reads (overrides pwd-upward).",
        ),
    ] = None,
    json_output: Annotated[
        bool,
        typer.Option(
            "--json",
            help="Emit a machine-readable JSON envelope (overrides root --json).",
        ),
    ] = False,
) -> None:
    """Print the native summary, recent decisions, blockers and git head of the tree."""
    from eawf.kernel.migration.epoch2.generation import GENERATION_DOCUMENT
    from eawf.kernel.state.epoch2.authority import resolve_authority
    from eawf.kernel.store.ledger import LedgerError

    flags: GlobalFlags = ctx.obj
    effective_flags = GlobalFlags(
        json_output=flags.json_output or json_output,
        plain_output=flags.plain_output,
        no_input=flags.no_input,
        workspace=workspace if workspace is not None else flags.workspace,
    )

    state_path, _reason = resolve_with_reason(workspace=effective_flags.workspace)
    tree_root = state_path.parent
    if not tree_root.is_dir():
        errors.emit_error(
            errors.UserError(f"no eawf tree at {tree_root}", kind="NotFound"),
            flags=effective_flags,
        )
        return

    authority = resolve_authority(tree_root)
    blockers: list[str] = []
    native: dict[str, Any] | None = None
    native_decisions: tuple[Decision, ...] = ()
    if authority.target is None or authority.generation_id is None:
        gap = authority.gap.value if authority.gap is not None else None
        blockers.append(f"tree is at authority epoch 1 ({gap}); status reads only epoch 2")
    else:
        document_path = (
            authority.target.generation_path(authority.generation_id) / GENERATION_DOCUMENT
        )
        try:
            native = _native_summary(
                tree_root, document_path, generation_id=authority.generation_id
            )
        except FileNotFoundError as exc:
            # A marked tree whose generation has not written its document yet
            # still has status to report; only a document that exists and is
            # broken is worth refusing over.
            logger.info(f"status native=unavailable cause={exc!s}")
        except (OSError, ValueError) as exc:
            errors.emit_error(
                errors.UserError(f"generation document unreadable: {exc}", kind="InvalidInput"),
                flags=effective_flags,
            )
            return
        try:
            native_decisions = _native_decisions(tree_root)
        except (LedgerError, ValueError) as exc:
            # The decisions are one line of the report; a torn ledger costs that
            # line, not the tasks and git head the operator came for.
            logger.warning(f"status decisions=unavailable cause={exc!s}")
            blockers.append(f"decision ledger unreadable: {exc}")

    payload: dict[str, Any] = {
        "git": _git_info(cwd=_find_git_root(tree_root)),
        "authority": {
            "epoch": authority.epoch,
            "gap": authority.gap.value if authority.gap is not None else None,
            "generation_id": authority.generation_id,
        },
        "native": native,
        "recent_decisions": _recent_decisions(native_decisions),
        "blockers": blockers,
    }

    text = _format_text(payload)
    emit_json_or_text(payload, text, flags=effective_flags)


def _native_summary(
    tree_root: Path, document_path: Path, *, generation_id: str, limit: int = 10
) -> dict[str, Any]:
    """Return the pointers and open Tasks of the generation at *document_path*.

    The primary Track is the Track the active Milestone is filed under; with
    no Milestone active it is the first ACTIVE Track by key. When several
    Milestones are active the first by key is named.

    Args:
        tree_root: The tree's ``.ea`` directory, which the open Tasks are read from.
        document_path: The selected generation's document.
        generation_id: The selected generation, the projection's scope.
        limit: How many open Tasks to list; the count covers all of them.

    Returns:
        ``{"track_key", "milestone_key", "open_task_count", "open_tasks"}``,
        the keys ``None`` when the generation holds no such record and each
        listed Task as ``{"key", "status", "title"}``; ``title`` is the
        Task's intent, or ``None`` when its record states none.

    Raises:
        ValueError: The generation document is not a JSON object, or holds a
            Task, Track or Milestone row the projection cannot render.
        OSError: The generation document could not be read.
    """
    from eawf.kernel.projection.compute import build_route_projection
    from eawf.kernel.state.epoch2.milestone import MilestoneStatus
    from eawf.kernel.state.epoch2.track import TrackStatus
    from eawf.kernel.store.compaction import read_document
    from eawf.kernel.store.tiers import Epoch2Collection
    from eawf.observability.reflect.runs import CANONICAL_SEQUENCE_KEY
    from eawf.observability.reflect.tasks import read_open_tasks

    document = read_document(document_path)
    projection = build_route_projection(
        route=_POINTER_ROUTE,
        document=document,
        cursor=int(document.get(CANONICAL_SEQUENCE_KEY, 0)),
        scope_id=generation_id,
        generated_at=datetime.now(UTC),
    )
    rows = sorted(projection.rows, key=lambda row: row.key)
    milestone = next(
        (
            row
            for row in rows
            if row.collection is Epoch2Collection.MILESTONE
            and row.status.value == MilestoneStatus.ACTIVE.value
        ),
        None,
    )
    track_key = (
        milestone.parent_key
        if milestone is not None
        else next(
            (
                row.key
                for row in rows
                if row.collection is Epoch2Collection.TRACK
                and row.status.value == TrackStatus.ACTIVE.value
            ),
            None,
        )
    )
    tasks = read_open_tasks(tree_root)
    return {
        "track_key": track_key,
        "milestone_key": None if milestone is None else milestone.key,
        "open_task_count": len(tasks),
        "open_tasks": [
            {"key": task.key, "status": task.status.value, "title": task.title}
            for task in tasks[:limit]
        ],
    }


def _native_decisions(tree_root: Path) -> tuple[Decision, ...]:
    """Return the native decisions the epoch-2 tree at *tree_root* files.

    Raises:
        LedgerTornTailError: The decision ledger ends mid-line.
        ValueError: A decision ledger line does not satisfy its model.
    """
    from eawf.observability.reflect.decisions import read_native_decisions

    return read_native_decisions(tree_root)


def _format_text(payload: dict[str, Any]) -> str:
    """Render *payload* as a compact multi-line summary for human consumption."""
    git = payload["git"]
    branch = git["branch"] or "<unknown>"
    git_line = f"git: head={(git['head'] or '<unknown>')[:12]} branch={branch}"
    authority = payload["authority"]
    if authority["epoch"] == 2:
        authority_line = f"authority: epoch 2 (generation {authority['generation_id']})"
    else:
        authority_line = f"authority: epoch 1 ({authority['gap']})"
    decisions = payload["recent_decisions"]
    decisions_line = (
        f"recent decisions: {', '.join(d['id'] for d in decisions)}"
        if decisions
        else "recent decisions: none"
    )
    blockers = payload["blockers"]
    blockers_line = f"blockers: {', '.join(blockers) if blockers else 'none'}"
    native = payload["native"]
    native_lines: list[str] = []
    if native is not None:
        listed = ", ".join(f"{t['key']} {t['status']}" for t in native["open_tasks"])
        count = native["open_task_count"]
        native_lines = [
            f"track: {native['track_key'] or '<none>'} "
            f"milestone: {native['milestone_key'] or '<none>'}",
            f"open tasks: {count} ({listed})" if listed else "open tasks: none",
        ]
    lines = [*native_lines, git_line, authority_line, decisions_line, blockers_line]
    return "\n".join(lines)
