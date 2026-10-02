"""git.pr: the generations a Batch has taken, as the console sees them.

The route only reads. ``m`` opens the conflict card, and every mutation -- merging,
pushing, opening or closing a pull request -- stays in the operator's git tool. Nothing
this module reaches writes a file, and the frame says so where an operator will look for
the button that is not there.

Under a held read model the rows are the Batch's generations at one committed cursor,
newest last, with the head marked from its position rather than from a stored flag. The
branch, review and checks rows are the tree's checkout and the pull request open for it,
as the daemon read them from ``git`` and ``gh``; a half it could not read names the tool
and why. With
no read model held the route draws its epoch-1 frame from the prototype registers, which
is the mode the tracked golden contract replays.
"""

from __future__ import annotations

from eawf.kernel.projection.integration import GitPrReadModel
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.vcs.repository_read import BranchDrift, RepositoryAnswer
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import clock_minute, day, group
from eawf.surfaces.tui.console.frame import Grid, View, g_frame, thin
from eawf.surfaces.tui.console.keybar import route_pairs
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
    wrapped,
)

_COMMITS: tuple[list[str], ...] = (
    ["8f14c2", "13:58", "Bound the replay window to the acked cursor"],
    ["a1d0e9", "13:31", "Add an ordering test for the normalizer"],
    ["77c410", "13:12", "Probe: reproduce the ordering assumption"],
)
_KEYS = route_pairs("git.pr")

#: The sentence the route prints where an operator looks for the verb it does not have.
#: Every key this route binds reads; nothing it reaches writes a file or a remote.
READ_ONLY = "Merging, closing and pushing happen in your git tool."

#: What the generation section says for a Batch that has taken no delivery.
NO_GENERATION = "∅ this Batch has taken no generation · nothing has been integrated into it"

#: What ``Enter`` answers. The console opens no commit surface, so the row an operator
#: is on is the whole record it holds of that delivery.
COMMIT_IS_THE_ROW = "this row is the whole record the console holds of that commit"

#: What the branch, review and checks rows say before the daemon's first read arrives.
NOT_READ = "… not read yet · the daemon reads git and gh for this tree"

_ROWS = Grid([10, 8, 22, 0], 2)


def _drift(drift: BranchDrift) -> str:
    """Return one drift as ``origin/main ahead 4 · behind 1``, or that its ref is gone."""
    if drift.gone:
        return f"{drift.ref} is gone"
    return f"{drift.ref} ahead {group(drift.ahead)} · behind {group(drift.behind)}"


def _branch_rows(repository: RepositoryAnswer | None, w: int) -> list[str]:
    """Return the branch row and its last commit, or why the checkout went unread."""
    if repository is None:
        return wrapped("BRANCH", NOT_READ, w)
    read = repository.branch
    if read is None:
        return wrapped("BRANCH", f"{UNAVAILABLE} · {repository.branch_unread}", w)
    parts = [
        read.branch or f"detached at {read.head.sha[:7]}",
        _drift(read.upstream) if read.upstream else "no upstream",
        *([_drift(read.default)] if read.default else []),
    ]
    at = read.head.committed_at
    last = f"last commit {read.head.sha[:7]} · {day(at)} {clock_minute(at)} · {read.head.subject}"
    return [*wrapped("BRANCH", " · ".join(parts), w), more(last)]


def _review_rows(repository: RepositoryAnswer | None, w: int) -> list[str]:
    """Return the review and checks rows of the pull request open for the branch."""
    if repository is None:
        return [label("REVIEW", NOT_READ), label("CHECKS", NOT_READ)]
    pr = repository.pull_request
    if pr is None:
        reason = repository.pull_request_unread
        if reason is not None:
            return [
                *wrapped("REVIEW", f"{UNAVAILABLE} · {reason}", w),
                label("CHECKS", f"{UNAVAILABLE} · read with the review"),
            ]
        branch = repository.branch.branch if repository.branch else None
        return [
            label("REVIEW", f"no pull request is open for {branch}"),
            label("CHECKS", "none · no pull request to report on"),
        ]
    decision = (pr.review_decision or "no review decision").replace("_", " ").lower()
    review = (
        f"PR #{pr.number} {pr.state.lower()} · {decision} · "
        f"{dv.plural(pr.approvals, 'approval')} · "
        f"{dv.plural(pr.changes_requested, 'change')} requested"
    )
    if not pr.checks:
        return [
            label("REVIEW", review),
            label("CHECKS", f"none · nothing reported on #{pr.number}"),
        ]
    counts = {
        outcome: sum(1 for check in pr.checks if check.outcome == outcome)
        for outcome in ("pass", "fail", "pending", "skipped")
    }
    checks = " · ".join(f"{group(n)} {outcome}" for outcome, n in counts.items() if n)
    failing = [check.name for check in pr.checks if check.outcome == "fail"]
    return [
        label("REVIEW", review),
        label("CHECKS", checks),
        *([more(f"failing: {', '.join(failing)}")] if failing else []),
    ]


def _commit_rows(view: View, model: GitPrReadModel) -> list[str]:
    """Return the commits section: one row per generation the Batch took, head marked."""
    session, w = view.session, view.w
    cursor = dv.sel_in(session, len(model.generations))
    rows = [_ROWS.head(["COMMIT", "WHEN", "GENERATION", "TOUCHED"])]
    rows.extend(
        _ROWS.row(
            [
                row.head_sha[:6],
                clock_minute(row.created_at),
                row.key + (" · head" if row.selected else ""),
                f"generation {group(row.ordinal)} · {dv.plural(len(row.changed_paths), 'path')}",
            ],
            index == cursor,
            w,
        )
        for index, row in enumerate(model.generations)
    )
    if not model.generations:
        rows.append(f"   {NO_GENERATION}")
    return rows


def _batch_of(view: View, model: GitPrReadModel) -> str | None:
    """Return the Batch the frame is about: its subject, or the Batch a Run subject is in.

    The light verb opens this route from a Run, and a Run's delivery is its Batch's, so a
    Run subject is read through the Batch its record is filed under; one whose Batch is
    not stated names none rather than borrowing another Batch.
    """
    subject = view.session.subj_id
    if subject is None:
        return model.rows[0].key if model.rows else None
    run = next(
        (row for row in view.rows if row.key == subject and row.collection is Epoch2Collection.RUN),
        None,
    )
    return run.facts.get("batch") if run is not None else subject


def native_frame(view: View, model: GitPrReadModel) -> list[str]:
    """Return the Git frame drawn from the read model the daemon served.

    A repository fact the daemon could not read renders unavailable with the reason it
    gave, never clean, and a check still running renders pending, never passed.

    Args:
        view: The render being built.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    w = view.w
    batch = _batch_of(view, model)
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(view, model, *([batch] if batch else []), "Git"),
        summary=(f"{batch or 'no Batch'} · read only · the console never touches a remote"),
    )
    selected = model.selected_generation()
    tip = (
        f"head {selected.key} · {selected.head_sha[:6]} · generation {group(selected.ordinal)}"
        if selected
        else f"{UNAVAILABLE} · no generation is this Batch's head"
    )
    body = [
        *_branch_rows(model.repository, w),
        label("HEAD", tip),
        thin(w),
        *_commit_rows(view, model),
        thin(w),
        *_review_rows(model.repository, w),
        thin(w),
        label("ACTION", READ_ONLY),
        more("the merge-conflict card also only displays"),
    ]
    return finish(view, top, body, _KEYS)


def _proto_frame(view: View) -> list[str]:
    """Return the epoch-1 Git frame the prototype registers drive."""
    s, w = view.session, view.w
    grid = Grid([9, 9, 0])
    dv.sel_in(s, len(_COMMITS))
    body = [
        " BRANCH       eawf/bound-replay-window · ahead 4 · behind 1",
        " HEAD         8f14c2 · authored by the agent at 13:58",
        thin(w),
        grid.head(["COMMIT", "WHEN", "SUBJECT"]),
    ]
    body.extend(grid.row(x, i == s.sel, w) for i, x in enumerate(_COMMITS))
    body.extend(
        [
            thin(w),
            " REVIEW       PR #418 open · 1 approval · 1 change requested",
            " CHECKS       tests pass · lint pass · conformance ? unknown",
            thin(w),
            f" ACTION       {READ_ONLY}",
            "              the merge-conflict overlay also only displays",
        ]
    )
    return g_frame(
        view,
        crumb="Eä ▸ … ▸ BAT-0001 ▸ Git",
        ctx="read only · the console never touches a remote",
        body=body,
        keys=_KEYS,
    )


def render(view: View) -> list[str]:
    """Return the Git frame, native when a read model is held and epoch-1 otherwise."""
    model = native(view)
    if isinstance(model, GitPrReadModel):
        return native_frame(view, model)
    return _proto_frame(view)


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the conflict card on ``m``, and answer ``Enter`` with what the row holds.

    ``Enter`` is answered rather than left to the dispatcher because the footer
    advertises it and the console has no commit surface to open: the row is the whole
    record it holds, and saying so is honest where a silent key is a broken promise.
    """
    if busy(ctx.s):
        return False
    if key == "m":
        go(ctx, "merge.conflict", "the conflict in this PR")
        return True
    if key == "Enter":
        ctx.log("Enter", COMMIT_IS_THE_ROW)
        return True
    return False
