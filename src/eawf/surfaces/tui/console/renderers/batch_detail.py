"""batch.detail: the batch's own tasks, found by their back-reference.

The frame therefore cannot show a different set from the milestone that summarises it.

The native frame is the packet's labelled facts about one Batch: its Tasks first, the
caret walking them, then the Milestone it is cut under, how many Tasks it holds, the head
it is bound at, the branch it integrates into, the Runs of its Tasks, and the review and
checks of the pull request open for that branch, as the daemon read them from ``gh``. A
Batch whose merge outcome is unknown draws the two-pane frame instead, and a frame opened
on no Batch lists the register to pick one from.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.state.epoch2.run import RunStatus
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.vcs.repository_read import RepositoryAnswer
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import Table, View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.lifecycle import Layout
from eawf.surfaces.tui.console.live_reads import (
    BRANCH_READS_MAX,
    BRANCH_REVIEWS_READ,
    REPOSITORY_READ,
    HeldBranchReviews,
)
from eawf.surfaces.tui.console.renderers.children import (
    Record,
    child_cursor,
    child_rows,
    children,
    lrow,
    status,
)
from eawf.surfaces.tui.console.renderers.detail import at, state_of, subject_of, unknown_frame
from eawf.surfaces.tui.console.renderers.git_pr import review_texts
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD
from eawf.surfaces.tui.console.renderers.spine import (
    ExtraColumns,
    detail_head,
    detail_keys,
    held,
    native_frame,
)

OWN = "BAT-0001"


def _task_rows(view: View, bid: str) -> list[tuple[str, str, str]]:
    fx = view.fixture
    rows = []
    for x in dv.children_of(fx, bid, "EAWF-", "BATCH"):
        runs = len(dv.children_of(fx, x, "RUN-", "SCOPE")) or (
            1 if dv.field_of(fx, x, "RUNS") else 0
        )
        name = dv.field_of(fx, x, "NAME") or ""
        rows.append((f"{x} {name}", dv.ref_state(fx, x) or "∅", str(runs)))
    return rows


def _task_line(task: Record, *, focused: bool) -> str:
    """Return one Task row: its key and state, the title only on the row under the caret.

    The state leads the title so a long title can never clip it off the row.
    """
    line = f"{task.key} · {status(task)}"
    return f"{line}  {task.title}" if focused and task.title else line


#: What the base, review and checks rows say of a Batch that names no branch yet.
NO_BRANCH = "∅ the Batch names no target branch yet · no pull request to look up"


def _head_text(facts: dict[str, str]) -> str:
    """Return the head the Batch is bound at and when it moved."""
    head = facts.get("head")
    parts = [head[:7] if head else f"{UNKNOWN_WORD} · no head binding is stated"]
    if "updated_at" in facts:
        parts.append(f"last moved {at(facts['updated_at'])}")
    return " · ".join(parts)


def _runs_text(spine: SpineView, tasks: Sequence[Record]) -> str:
    """Return how many Runs ran the Batch's Tasks, how many still run, and the newest."""
    keys = {task.key for task in tasks}
    runs = [
        row
        for row in spine.rows
        if row.collection is Epoch2Collection.RUN and row.parent_key in keys
    ]
    if not runs:
        return "∅ no Run of its Tasks is held"
    running = sum(1 for run in runs if status(run) == RunStatus.RUNNING.value)
    newest = max(runs, key=lambda run: run.facts.get("created_at", ""))
    return f"{dv.plural(len(runs), 'run')} · {running} running · newest {newest.key}"


def _pull_request_rows(view: View, facts: dict[str, str]) -> list[str]:
    """Return the review and checks of the pull request open for the Batch's branch."""
    if "target_branch" not in facts:
        return [lrow("REVIEW", NO_BRANCH), lrow("CHECKS", NO_BRANCH)]
    answer = view.live.get(REPOSITORY_READ)
    repository = answer if isinstance(answer, RepositoryAnswer) else None
    review, checks, failing = review_texts(repository)
    return [
        lrow("REVIEW", review),
        lrow("CHECKS", checks + (f" · failing: {', '.join(failing)}" if failing else "")),
    ]


#: The register's columns: the shared three, then each Batch's review and checks.
_REGISTER_TABLE = Table([16, 8, 20, 30, 0], 2)

#: What a register row's review and checks say before the read arrives.
REGISTER_UNREAD = "… not read yet"

#: What a register row past the branch cap says in place of its review.
PAST_CAP = f"not read · only {BRANCH_READS_MAX} branches are looked up"


def _compact_review(answer: RepositoryAnswer) -> tuple[str, str]:
    """Return one register row's review and checks: the frame's words, shortened."""
    pr = answer.pull_request
    if pr is None and answer.pull_request_unread is not None:
        return "unavailable · Enter says why", "unavailable"
    if pr is None:
        return "no pull request", "none"
    decision = (pr.review_decision or "no review decision").replace("_", " ").lower()
    outcomes = ("pass", "fail", "pending", "skipped")
    counted = ((o, sum(1 for check in pr.checks if check.outcome == o)) for o in outcomes)
    checks = " · ".join(f"{group(n)} {outcome}" for outcome, n in counted if n)
    return f"#{pr.number} {pr.state.lower()} · {decision}", checks or "none reported"


def _register_cells(held: HeldBranchReviews | None) -> Callable[[SpineRow], tuple[str, ...]]:
    """Return what each register row draws under REVIEW and CHECKS from ``held``."""

    def cells(row: SpineRow) -> tuple[str, ...]:
        if row.collection is not Epoch2Collection.BATCH:
            return ("", "")
        branch = row.facts.get("target_branch")
        if branch is None:
            return ("∅ no branch named", "∅ no branch named")
        if held is None:
            return (REGISTER_UNREAD, REGISTER_UNREAD)
        answer = held.answers.get(branch)
        return (PAST_CAP, PAST_CAP) if answer is None else _compact_review(answer)

    return cells


def _register_columns(view: View) -> ExtraColumns:
    """Return the Batch register's review and checks columns, read once per branch."""
    answer = view.live.get(BRANCH_REVIEWS_READ)
    held_reviews = answer if isinstance(answer, HeldBranchReviews) else None
    return ExtraColumns(
        table=_REGISTER_TABLE, names=("REVIEW", "CHECKS"), cells=_register_cells(held_reviews)
    )


def batch_frame(view: View, spine: SpineView) -> list[str]:
    """Return one Batch's frame: its Tasks, then the facts the read model states about it.

    Args:
        view: The render being built; its session names the Batch.
        spine: The Batch register with the Tasks filed under each Batch.

    Returns:
        The full frame, keybar last; the register frame when the session names no Batch.
    """
    session = view.session
    subject = subject_of(view, spine)
    if subject is None:
        return native_frame(view, spine, extra=_register_columns(view))
    keys = detail_keys(view, spine)
    state = state_of(subject)
    if state is not None and state.layout is Layout.UNKNOWN:
        return unknown_frame(view, spine, subject, keys)
    tasks = children(spine.rows, subject.key, Epoch2Collection.TASK)
    done = sum(1 for task in tasks if status(task) == TaskStatus.COMPLETED.value)
    facts = dict(subject.facts)
    below = [
        lrow("MILESTONE", subject.parent_key or "∅ cut under no Milestone"),
        lrow("COUNT", f"{dv.plural(len(tasks), 'task')} · {done} completed"),
        lrow("BASE", facts.get("target_branch", "∅ no target branch is chosen yet")),
        lrow("HEAD", _head_text(facts)),
        lrow("RUNS", _runs_text(spine, tasks)),
        *_pull_request_rows(view, facts),
        *([lrow("FAILED", facts["failure"])] if "failure" in facts else []),
    ]
    top = detail_head(view, spine, subject)
    cursor = child_cursor(session, [task.key for task in tasks], subject=subject.key)
    listed = child_rows(
        view,
        "TASKS",
        [_task_line(task, focused=index == cursor) for index, task in enumerate(tasks)],
        cursor,
        chrome=len(top) + len(below),
        empty="∅ no Task is filed under this Batch",
    )
    return build(view, [*top, *listed, *below], route_keys_bar(view, keys))


def render(view: View) -> list[str]:
    """Return the Batch frame, native when a read model is held."""
    spine = held(view)
    if spine is not None:
        return batch_frame(view, spine)
    s, fx, w = view.session, view.fixture, view.w
    bid = dv.subj_of(s, OWN)
    tasks = _task_rows(view, bid)
    if dv.own_body(s, OWN):
        dv.sel_in(s, len(tasks))
    milestone = (dv.field_of(fx, bid, "MILESTONE") or "").split(" ")[0]
    parent = f"{milestone} ▸ " if milestone else ""
    facts = [("NAME", "field"), ("STATE", "field"), ("CHECKS", "worst")]
    subject = dv.subj_facts(fx, bid, facts) or "∅ name and state unavailable"
    table = Table([34, 19, 0], 2)
    rows = [
        header(view, f" Eä ▸ … ▸ {parent}{bid}"),
        f" Batch {bid} {subject}",
        bar(w),
        " HEAD            Integration base a1f4c9e matches the shared branch.",
        "                 authorized at      a1f4c9e   still exact",
        thin(w),
        table.head(["TASK FRONTIER", "STATE", "RUNS"]),
    ]
    dv.publish_nav(s, [t[0].split(" ")[0] for t in tasks])
    rows.extend(table.row(list(t), i == s.sel) for i, t in enumerate(tasks))
    rows.append(thin(w))
    rows.append(" VERIFICATION CYCLE   checking → audit review → changes → repair")
    rows.append("   checking          3 of 4 checks passed          typical ~6m")
    if not dv.own_body(s, OWN):
        rows = dv.absent(s, fx, rows, entity_id=bid, what="tasks or integration detail", w=w)
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS["batch.detail"]))
