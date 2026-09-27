"""evidence: a claim as a sentence somebody has to act on, with its ladder of rungs.

One ladder feeds both this route and the rung card, so the row and the card can never
disagree.

The native frame draws one Claim over the four rungs ``resolve``, ``anchor``, ``screen`` and
``entail``. A rung whose outcome no producer has recorded is unknown, with ``open`` as of,
never failed; no rung's outcome is inferred from another's, and the standing derives from
the rungs alone, so a claim with no entailing outcome stands uncertified. The claim's own
lifecycle status is drawn apart from its standing, because ``REFUTED`` is a lifecycle fact
and ``refuted`` a reading of the ladder.
"""

from __future__ import annotations

from eawf.kernel.projection.route_view import RouteReadModel, RouteRecord
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.frame import Grid, View, chip, g_frame, lab, prose, thin
from eawf.surfaces.tui.console.navigation import Ctx, busy, go
from eawf.surfaces.tui.console.renderers.read_model import (
    UNAVAILABLE,
    UNKNOWN_WORD,
    counts,
    finish,
    label,
    more,
    native,
    native_head,
    route_crumb,
)

#: The one claim the prototype ladder records.
OWN = "CLM-0004"
_KEYS: tuple[tuple[str, str], ...] = (
    ("↑↓", "rung"),
    ("Enter", "what it found"),
    ("y", "copy"),
    ("Esc", "back"),
)


def _prose(wide: bool, x: bool, room: int) -> list[str]:
    rows = prose(
        "IN WORDS",
        [
            "Replay any recorded log and every event arrives in the order it was recorded"
            " — no inversions, and no gaps in the sequence."
        ],
        room,
    )
    proves = (
        "A replayed run reads exactly like the live one, which is what lets Activity, the"
        " Timeline and History be rebuilt from the log at all."
        if wide
        else "A replay can be read exactly like the live run."
    )
    rows += prose("IT PROVES", [proves], room)
    rows += prose(
        "BREAKS IF",
        [
            "One replay puts two events out of order, or a gap is closed by inventing an event"
            " instead of reconciling it."
        ],
        room,
    )
    if x:
        rows += prose(
            "WHO CARES",
            [
                "Every operator reading a rebuilt Timeline, and every verdict a Run signs off"
                " from replayed evidence."
            ],
            room,
        )
    return rows


def _ladder(wide: bool, room: int) -> list[str]:
    first = "Each rung is a harder test than the one below it, and only the top rung certifies."
    ladder = (
        [first, "A claim with three passes is still an uncertified claim."] if wide else [first]
    )
    standing = (
        [
            "This claim stands uncertified.",
            "Rung 3 has no outcome — unknown, not failed — so nothing here has been retracted.",
        ]
        if wide
        else ["This claim stands uncertified.", "Rung 3 has no outcome — unknown, not failed."]
    )
    return prose("LADDER", ladder, room) + prose("STANDING", standing, room)


def _absent(view: View, claim: str) -> list[str]:
    """Return the frame of a claim this route holds no ladder for, said rather than swapped."""
    s, fx = view.session, view.fixture
    head = [f"Eä ▸ {fx.scope} ▸ Evidence ▸ {claim}", f"Claim {claim}", ""]
    # the header, context and rule rows are g_frame's own, so only the body is kept
    _, _, _, *body = dv.absent(s, fx, head, entity_id=claim, what="rungs or supports", w=view.w)
    return g_frame(view, crumb=head[0], ctx=head[1], body=body, keys=_KEYS)


#: The four rungs of the evidence ladder, lowest first, with what each one checks. The
#: names are the plan contract's and never the conformance runner's stage names.
RUNGS: tuple[tuple[str, str], ...] = (
    ("resolve", "the reference resolves to a digest"),
    ("anchor", "the claim is anchored to the evidence it cites"),
    ("screen", "an advisory screen; a negative routes onward"),
    ("entail", "the evidence entails the claim; it alone certifies"),
)

#: What a claim with no rung outcome stands as: nothing certified it and nothing refuted it.
UNCERTIFIED = "uncertified"


def _claim(view: View, model: RouteReadModel) -> RouteRecord | None:
    """Return the Claim the frame is about: the session's subject, else the first claim held."""
    claims = [row for row in model.rows if row.collection is Epoch2Collection.CLAIM]
    subject = view.session.subj_id
    return next((row for row in claims if row.key == subject), claims[0] if claims else None)


def _rung_rows(view: View) -> list[str]:
    """Return the ladder: one row per rung, wider frames adding when and what it ran over."""
    s, w = view.session, view.w
    wide, x = w >= 120, w >= 160
    heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED"]
    cols = [13, 12]
    if x:
        heads.append("IT RAN OVER")
    if wide:
        heads.append("AS OF")
    checked_w = w - 3 - sum(cols) - (24 if x else 0) - (14 if wide else 0)
    grid = Grid([*cols, checked_w, *([24] if x else []), *([14] if wide else []), 0][: len(heads)])
    rows = [grid.head(heads)]
    dv.sel_in(s, len(RUNGS))
    for i, (name, checked) in enumerate(RUNGS):
        # no producer records a rung outcome yet, and a rung with none is unknown -- never
        # failed, and never inferred from the claim or from the rung above or below it
        cells = [f"{i + 1} {name}", UNKNOWN_WORD, checked]
        if x:
            cells.append(UNAVAILABLE)
        if wide:
            cells.append("open")
        rows.append(grid.row(cells, i == s.sel, w))
    return rows


def native_frame(view: View, model: RouteReadModel) -> list[str]:
    """Return the Evidence frame drawn from the read model the daemon served.

    Args:
        view: The render being built; its session's subject names the Claim.
        model: The route's read model at the committed cursor.

    Returns:
        The full frame, keybar last.
    """
    w = view.w
    claim = _claim(view, model)
    key = claim.key if claim is not None else "no claim"
    subject = f"Claim {key}" if claim is not None else "No Claim held"
    top = native_head(
        view,
        model,
        crumb_text=route_crumb(model, "Evidence", *([claim.key] if claim else [])),
        summary=f"{subject} · {len(RUNGS)} rungs · {UNCERTIFIED} · {counts(model)}",
    )
    if claim is None:
        body = [label("CLAIM", f"{UNAVAILABLE} · this scope holds no Claim the ladder is about")]
    else:
        title = claim.title or f"{UNAVAILABLE} · the claim states no title"
        body = [label("CLAIM", f"{claim.key} · {title}")]
    body += [thin(w), *_rung_rows(view), thin(w)]
    body += [
        label("LADDER", "Each rung is a harder test than the one below it."),
        more("Only the entailing rung certifies; a screen negative is advisory."),
        label("STANDING", f"{UNCERTIFIED} · no rung has an outcome, so nothing certifies it"),
    ]
    if claim is not None:
        body.append(more(f"lifecycle {value_cell(claim.field('status')).full}"))
    body.append(label("SUPPORTS", f"{UNAVAILABLE} · no finding cites this claim yet"))
    if w >= 120:
        evidence = [row.key for row in model.rows if row.collection is Epoch2Collection.EVIDENCE]
        held = " · ".join(evidence) if evidence else "no evidence record is held"
        body += [thin(w), label("GRAPH", f"{key} ← {held} · typed edges {UNKNOWN_WORD}")]
    return finish(view, top, body, _KEYS)


def render(view: View) -> list[str]:
    """Return the Evidence frame, native when a read model is held.

    A drill onto a claim other than the one the prototype ladder records states that
    claim's absence rather than drawing the recorded claim under its id.
    """
    model = native(view)
    if model is not None:
        return native_frame(view, model)
    s, w = view.session, view.w
    if s.subj_id and s.subj_id != OWN:
        return _absent(view, s.subj_id)
    rungs = view.fixture.registers.ev_rungs
    wide = w >= 120
    x = w >= 160
    room = w - 15
    if x:
        grid = Grid([13, 12, 38, 24, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED", "IT RAN OVER", "AS OF"]
    elif wide:
        grid = Grid([13, 12, 38, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED", "AS OF"]
    else:
        grid = Grid([13, 12, 0])
        heads = ["RUNG", "OUTCOME", "WHAT IT CHECKED"]
    dv.sel_in(s, len(rungs))
    body = [lab("CLAIM", "CLM-0004 · the normalizer preserves event ordering under replay")]
    body += _prose(wide, x, room)
    body.append(lab("SUPPORTS", "MLS-0007 · 3 claims in graph · 1 uncertified"))
    body.append(thin(w))
    body.append(grid.head(heads))
    for i, r in enumerate(rungs):
        cells = [r["n"], chip(r["sev"], r["out"]), r["checked"]]
        if x:
            cells.append(r["over"])
        if wide:
            cells.append(r["as"])
        body.append(grid.row(cells, i == s.sel, w))
    body.append(thin(w))
    body += _ladder(wide, room)
    if wide:
        body.append(thin(w))
        body.append(lab("GRAPH", "CLM-0004 ← CLM-0002 probe evidence ← EVT-0119"))
    return g_frame(
        view,
        crumb=f"Eä ▸ {view.fixture.scope} ▸ Evidence ▸ CLM-0004",
        ctx=f"Claim CLM-0004 · {len(rungs)} rungs · uncertified · as of 14:02",
        body=body,
        keys=_KEYS,
    )


def seam(ctx: Ctx, key: str, shift: bool) -> bool:
    """Open the rung card on Enter; the rung is captured when the card opens."""
    s = ctx.s
    if s.route != "evidence" or key != "Enter" or busy(s):
        return False
    s.rung = s.sel
    go(ctx, "evidence.digest", f"rung {s.rung + 1} · its input digest and what it found")
    return True
