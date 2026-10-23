"""The ``list`` and ``show`` read verbs of the native entity groups.

``eawf task|milestone|plan|decision|action list`` lists the records of the
selected generation, and ``show <KEY>`` prints one record as the generation
stores it. They read the generation through
:mod:`eawf.observability.reflect.entities` with no daemon and no lock, and
never open the epoch-1 ``state.json``.

This module also owns the ``decision`` group: a native decision has no
lifecycle verb on the command line yet, so the group carries only the reads.
The verbs of the other groups attach onto the apps those groups already mount.

JSON answers (``--json``), inside the verb envelope for a contract group:

- ``task list``: ``{"tasks": [{"key", "urn", "status", "priority", "title",
  "batch_ref", "due_scope"}, ...]}``
- ``milestone|plan|decision|action list``: ``{"milestones"|"plan_revisions"|
  "decisions"|"actions": [{"key", "urn", "status", "title"}, ...]}``
- ``<group> show <KEY>``: ``{"task"|"milestone"|"plan_revision"|"decision"|
  "action": <the stored row>}``

Exit codes:

- ``0`` on success, including a list that matches nothing.
- ``1`` (``USER_ERROR``) with ``data.kind`` ``NotFound`` when no tree, no
  native generation or no record of the key is found, and ``InvalidInput``
  when the generation document or a ledger cannot be read.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, Final

import typer

from eawf.kernel.state.epoch2.task_status import TaskStatus
from eawf.kernel.state.resolve import resolve_with_reason
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.cli import errors
from eawf.surfaces.cli.commands.action import action_app
from eawf.surfaces.cli.commands.lifecycle import milestone_app, task_app
from eawf.surfaces.cli.commands.plan import plan_app
from eawf.surfaces.cli.flags import GlobalFlags
from eawf.surfaces.cli.output import emit_json_or_text

logger = logging.getLogger(__name__)

decision_app = typer.Typer(
    name="decision",
    help="Native decisions of the selected generation (list, show).",
    no_args_is_help=True,
)


@dataclass(frozen=True, slots=True)
class _Entity:
    """One entity group's read verbs: where they mount, what they read, how they answer.

    Attributes:
        app: The group's Typer the verbs attach onto.
        collection: The generation collection the records are read from.
        many: The key a list answers under.
        one: The key a show answers under.
        label: The plural a text list names when it lists nothing.
        noun: The record's name in help and refusals.
    """

    app: typer.Typer
    collection: Epoch2Collection
    many: str
    one: str
    label: str
    noun: str


_TASK: Final = _Entity(task_app, Epoch2Collection.TASK, "tasks", "task", "tasks", "Task")
_ENTITIES: Final = (
    _TASK,
    _Entity(
        milestone_app,
        Epoch2Collection.MILESTONE,
        "milestones",
        "milestone",
        "milestones",
        "Milestone",
    ),
    _Entity(
        plan_app,
        Epoch2Collection.PLAN_REVISION,
        "plan_revisions",
        "plan_revision",
        "plan revisions",
        "plan revision",
    ),
    _Entity(
        decision_app, Epoch2Collection.DECISION, "decisions", "decision", "decisions", "decision"
    ),
    _Entity(
        action_app,
        Epoch2Collection.PENDING_ACTION,
        "actions",
        "action",
        "actions",
        "pending action",
    ),
)


def _read[T](flags: GlobalFlags, read: Callable[[Path], T]) -> T:
    """Return what *read* answers for the tree *flags* resolve, or print the refusal.

    Args:
        flags: Resolved global flags; the workspace anchors the tree walk.
        read: The library read, given the tree's ``.ea`` directory.

    Returns:
        The read's answer.

    Raises:
        typer.Exit: After printing the error envelope, when there is no tree,
            the tree holds no native generation, or the generation cannot be read.
    """
    from eawf.kernel.store.ledger import LedgerError
    from eawf.observability.reflect.entities import GenerationUnavailableError

    state_path, _reason = resolve_with_reason(workspace=flags.workspace)
    tree_root = state_path.parent
    if not tree_root.is_dir():
        errors.emit_error(
            errors.UserError(f"no eawf tree at {tree_root}", kind="NotFound"), flags=flags
        )
    try:
        return read(tree_root)
    except (GenerationUnavailableError, FileNotFoundError) as exc:
        errors.emit_error(errors.UserError(str(exc), kind="NotFound"), flags=flags)
    except (LedgerError, OSError, ValueError) as exc:
        errors.emit_error(
            errors.UserError(f"generation document unreadable: {exc}", kind="InvalidInput"),
            flags=flags,
        )


def _list_text(rows: list[dict[str, Any]], label: str) -> str:
    """Render list rows one per line as ``KEY  STATUS  title``, blanks left out."""
    if not rows:
        return f"no {label}"
    return "\n".join(
        "  ".join(part for part in (row["key"], row["status"], row["title"]) if part)
        for row in rows
    )


def _register_show(entity: _Entity) -> None:
    """Attach ``show <KEY>`` onto *entity*'s group."""

    def show(
        ctx: typer.Context,
        key: Annotated[str, typer.Argument(help="The record's key.")],
    ) -> None:
        from eawf.observability.reflect.entities import read_standing_rows

        flags: GlobalFlags = ctx.obj
        row = _read(flags, lambda root: read_standing_rows(root, entity.collection)).get(key)
        if row is None:
            errors.emit_error(
                errors.UserError(f"no {entity.noun} {key} in the generation", kind="NotFound"),
                flags=flags,
            )
        emit_json_or_text({entity.one: row}, json.dumps(row, indent=2), flags=flags)

    entity.app.command("show", help=f"Print one {entity.noun} as the generation stores it.")(show)


def _register_list(entity: _Entity) -> None:
    """Attach ``list`` onto *entity*'s group."""

    def list_(ctx: typer.Context) -> None:
        from eawf.observability.reflect.entities import read_standing_rows, summarize

        flags: GlobalFlags = ctx.obj
        rows = [
            summary.model_dump(mode="json")
            for summary in summarize(
                entity.collection,
                _read(flags, lambda root: read_standing_rows(root, entity.collection)),
            )
        ]
        emit_json_or_text({entity.many: rows}, _list_text(rows, entity.label), flags=flags)

    entity.app.command("list", help=f"List every {entity.noun} of the generation by key.")(list_)


@task_app.command("list")
def task_list_cmd(
    ctx: typer.Context,
    status: Annotated[
        TaskStatus | None,
        typer.Option("--status", case_sensitive=False, help="Keep only Tasks in this status."),
    ] = None,
    milestone: Annotated[
        str | None,
        typer.Option(
            "--milestone",
            help="Keep only Tasks due in, or filed in a Batch of, this Milestone key.",
        ),
    ] = None,
) -> None:
    """List every Task of the generation by key, with its priority, Batch and due scope."""
    from eawf.observability.reflect.entities import list_tasks

    flags: GlobalFlags = ctx.obj
    rows = [
        task.model_dump(mode="json")
        for task in _read(flags, lambda root: list_tasks(root, status=status, milestone=milestone))
    ]
    emit_json_or_text({_TASK.many: rows}, _list_text(rows, _TASK.label), flags=flags)


for _entity in _ENTITIES:
    if _entity is not _TASK:
        _register_list(_entity)
    _register_show(_entity)


__all__ = ["decision_app", "task_list_cmd"]
