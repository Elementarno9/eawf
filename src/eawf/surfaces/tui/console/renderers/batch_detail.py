"""batch.detail: the batch's own tasks, found by their back-reference.

The frame therefore cannot show a different set from the milestone that summarises it.

The native frame is the packet's labelled facts about one Batch: its Tasks first, the
caret walking them, then the Milestone it is cut under, how many Tasks it holds, the head
it is bound at and its checks. A Batch whose merge outcome is unknown draws the two-pane
frame instead, and a frame opened on no Batch lists the register to pick one from.
"""

from __future__ import annotations

from eawf.kernel.projection.spine import SpineView
from eawf.kernel.state.epoch2.task import TaskStatus
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.frame import Table, View, bar, build, header, route_keys_bar, thin
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.lifecycle import Layout
from eawf.surfaces.tui.console.renderers.children import (
    Record,
    child_cursor,
    child_rows,
    children,
    lrow,
    status,
)
from eawf.surfaces.tui.console.renderers.detail import at, state_of, subject_of, unknown_frame
from eawf.surfaces.tui.console.renderers.read_model import UNKNOWN_WORD, cell
from eawf.surfaces.tui.console.renderers.spine import (
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


def _head_text(facts: dict[str, str]) -> str:
    """Return the head the Batch is bound at, the branch it merges to, and when it moved."""
    head = facts.get("head")
    parts = [head[:7] if head else f"{UNKNOWN_WORD} · no head binding is stated"]
    if "target_branch" in facts:
        parts.append(f"merges to {facts['target_branch']}")
    if "updated_at" in facts:
        parts.append(f"last moved {at(facts['updated_at'])}")
    return " · ".join(parts)


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
        return native_frame(view, spine)
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
        lrow("HEAD", _head_text(facts)),
        lrow("CHECKS", cell(subject.field("checks"))),
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
