"""git.pr: the generations a Batch has taken, as the console sees them.

The route only reads. ``m`` opens the conflict card, and every mutation -- merging,
pushing, opening or closing a pull request -- stays in the operator's git tool. Nothing
this module reaches writes a file, and the frame says so where an operator will look for
the button that is not there.

Under a held read model the rows are the Batch's generations at one committed cursor,
newest last, with the head marked from its position rather than from a stored flag. With
no read model held the route draws its epoch-1 frame from the prototype registers, which
is the mode the tracked golden contract replays.
"""

from __future__ import annotations

from eawf.kernel.projection.integration import GenerationRow, GitPrReadModel
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import (
    Grid,
    View,
    bar,
    build,
    g_frame,
    route_keys_bar,
    thin,
)
from eawf.surfaces.tui.console.header import header_row
from eawf.surfaces.tui.console.keybar import ROUTE_KEYS
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import counts, crumb, native, unstated_rows

_COMMITS: tuple[list[str], ...] = (
    ["8f14c2", "13:58", "Bound the replay window to the acked cursor"],
    ["a1d0e9", "13:31", "Add an ordering test for the normalizer"],
    ["77c410", "13:12", "Probe: reproduce the ordering assumption"],
)
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "row"),
    ("Enter", "commit"),
    ("m", "conflict"),
    ("y", "copy"),
    ("Esc", "back"),
)

#: The sentence the route prints where an operator looks for the verb it does not have.
#: Every key this route binds reads; nothing it reaches writes a file or a remote.
READ_ONLY = "Merging, closing and pushing happen in your git tool."

#: What the generation section says for a Batch that has taken no delivery.
NO_GENERATION = "∅ this Batch has taken no generation · nothing has been integrated into it"

#: What ``Enter`` answers. The console opens no commit surface, so the row an operator
#: is on is the whole record it holds of that delivery.
COMMIT_IS_THE_ROW = "this row is the whole record the console holds of that commit"

_ROWS = Grid([20, 14, 10, 0], 2)


def _generation_cells(row: GenerationRow) -> list[str]:
    """Return one generation's cells: its key, ordinal, commit and what it touched."""
    paths = len(row.changed_paths)
    return [
        row.key + (" ◂ head" if row.selected else ""),
        f"generation {group(row.ordinal)}",
        row.head_sha[:6],
        f"{group(paths)} path" + ("" if paths == 1 else "s"),
    ]


def _generation_rows(view: View, model: GitPrReadModel) -> list[str]:
    """Return the generation section: one line per generation, or the honest absence."""
    session, w = view.session, view.w
    cursor = dv.sel_in(session, len(model.generations))
    taken = len(model.generations)
    head = f" HISTORY   {group(taken)} generation" + ("" if taken == 1 else "s")
    selected = model.selected_generation()
    rows = [f"{head} · head {selected.key}" if selected else f" HISTORY   {NO_GENERATION}"]
    if not model.generations:
        return rows
    rows.append(_ROWS.head(["GENERATION", "ORDINAL", "COMMIT", "TOUCHED"]))
    rows.extend(
        _ROWS.row(_generation_cells(row), index == cursor, w)
        for index, row in enumerate(model.generations)
    )
    return rows


def native_frame(view: View, model: GitPrReadModel) -> list[str]:
    """Return the Git frame drawn from the read model the daemon served.

    Args:
        view: The render being built.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    session, w = view.session, view.w
    rows: list[str] = [
        header_row(session, crumb=crumb(view, model), scope=model.scope_id, needs=0, w=w),
        " " + counts(model),
        bar(w),
    ]
    rows.extend(_generation_rows(view, model))
    rows.append(thin(w))
    rows.append(f" ACTION    {READ_ONLY}")
    rows.append("           the merge-conflict card also only displays")
    rows.append(thin(w))
    rows.extend(unstated_rows(model))
    return build(view, rows, route_keys_bar(view, ROUTE_KEYS[session.route]))


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
