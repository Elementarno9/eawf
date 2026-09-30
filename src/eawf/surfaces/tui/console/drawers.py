"""Drawers: go, actions, inspect and raw.

A drawer keeps the route frame above it and replaces the frame's tail rows and keybar with
its own rows; the app composes it. The go drawer is shown while the ``g`` prefix is armed,
not through the session's overlay.

A console holding no prototype rows still draws the inspect and raw drawers under the
route it keeps: nothing has been read for the focused field, so each says that in the
unknown token rather than handing the frame to the route's unknown frame.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType

from eawf.kernel.projection.compute import ProjectionRow
from eawf.kernel.projection.spine import SpineRow, SpineView
from eawf.kernel.projection.truth import TruthField
from eawf.kernel.state.epoch2.consequence import MUTATIONS_BY_METHOD
from eawf.surfaces.tui.console import derive as dv
from eawf.surfaces.tui.console import prototype as pt
from eawf.surfaces.tui.console.action_menu import Availability, MenuVerb, menu_rows
from eawf.surfaces.tui.console.attention import ATTENTION_ROUTE, verb_available
from eawf.surfaces.tui.console.attention_verbs import ONLY_PRINCIPAL
from eawf.surfaces.tui.console.cells import value_cell
from eawf.surfaces.tui.console.eligibility import principals_of
from eawf.surfaces.tui.console.fixture import Fixture
from eawf.surfaces.tui.console.format import group
from eawf.surfaces.tui.console.frame import View, entry_state
from eawf.surfaces.tui.console.mutation import (
    GateKind,
    chrome_kept,
    gate,
    lifecycle_verbs,
    menu_entity,
    verb_check,
)
from eawf.surfaces.tui.console.notices import NOTICE_VERBS, notice_of
from eawf.surfaces.tui.console.renderers import copy_target
from eawf.surfaces.tui.console.renderers.spine import offered_verbs
from eawf.surfaces.tui.console.session import Session
from eawf.surfaces.tui.console.tokens import TRUTH
from eawf.surfaces.tui.console.width import pad
from eawf.workflow.projection.acceptance import AcceptanceBundleView

_UNKNOWN = TRUTH["unknown"].unicode

#: The route whose inspect drawer is about the tree rather than a field of one record.
HOME = "scope.home"

# The most stored fields the raw drawer quotes before it says how many it left out.
_RAW_LINES = 8

GO_ROWS: tuple[str, ...] = (
    " GO        g h  home · scope     g a  activity      g n  needs you",
    "           g b  backlog          g t  timeline      g r  release",
    "           g s  settings         g y  history       g d  health",
    "           g i  notifications    g l  sandbox log   g u  unattended",
)


def go_rows(view: View) -> list[str]:
    """Return the go drawer: the twelve destinations."""
    return list(GO_ROWS)


#: The attention verb whose refusal depends on how many principals the register names.
_ASSIGN = "assign"


def _assign_refused(view: View, verb: MenuVerb) -> Availability | None:
    """Return ``assign``'s refusal while the held register names nobody else to assign to.

    Once the register names a second principal the verb is judged like every other bound
    attention verb, so ``None`` is returned and the ordinary gate decides.
    """
    held = view.attention
    if verb.verb != _ASSIGN or held is None or held.withheld:
        return None
    if not principals_of(held.rows, view.principal) - {view.principal}:
        return Availability(False, ONLY_PRINCIPAL)
    return None


def _notice_takes(view: View, verb: MenuVerb) -> Availability | None:
    """Return that a notice verb acts while the cursor is on a held notice, else ``None``.

    A notice's verbs go to the notice ledger rather than to a pending action's mutators,
    so while a notice is selected the menu offers exactly what its key path does.
    """
    if view.session.route != ATTENTION_ROUTE or verb.key not in NOTICE_VERBS:
        return None
    return Availability(True) if notice_of(view.notices, view.session.sel_id) else None


#: The lifecycle verb whose refusal the acceptance journey's own checks can name.
_ACCEPT = MUTATIONS_BY_METHOD["domain.milestone.accept"].action


def _with_evidence(view: View, verb: MenuVerb, check: Availability) -> Availability:
    """Return ``check`` naming the acceptance checks beside a refused accept.

    A refusal names the evidence rather than a category, so while the held Milestone
    states the steps its acceptance journey ran, a refused accept says how many passed.
    """
    record = view.projection
    if check.ok or verb.verb != _ACCEPT or not isinstance(record, AcceptanceBundleView):
        return check
    if not record.criteria:
        return check
    state = "incomplete" if record.blocking() else "complete"
    counted = f"checks {state}, {record.proven()} of {len(record.criteria)} passed"
    return Availability(False, f"{counted} · {check.why}")


def action_rows(view: View) -> list[str]:
    """Return the action drawer: the route's verbs, refused ones with their live reasons.

    On a linked console the selection's lifecycle verbs follow the chrome's, each judged
    on the card it would open. When no request can leave the console they are not listed
    at all, and one row says so; a refusal the operator can act on keeps them listed with
    its reason.
    """
    s, fx, refusal = view.session, view.fixture, view.principal_refusal
    chrome = offered_verbs(s, fx, view.projection)
    decided = gate(s, fx, verb="lifecycle move", linked=view.linked, principal_refusal=refusal)
    native = () if fx.prototype else lifecycle_verbs(s, view.rows, decided)
    native_keys = {verb.key for verb in native}
    rows = menu_rows(
        (*chrome_kept(chrome, native), *native),
        guard=lambda v: (
            _with_evidence(view, v, verb_check(s, view.rows, decided, v))
            if v.key in native_keys
            else _assign_refused(view, v)
            or _notice_takes(view, v)
            or verb_available(s, fx, v, principal_refusal=refusal)
        ),
        w=view.w,
    )
    selected = len(s.marked)
    if selected:
        rows.insert(
            0,
            pad(
                f" SELECTED  {selected} selected · one preview, {dv.plural(selected, 'result')}",
                view.w,
            ),
        )
    if not fx.prototype and decided.kind is GateKind.TRANSPORT and menu_entity(s, view.rows):
        rows.append(pad(f" WRITES    not offered · {decided.reason}", view.w))
    return rows


@dataclass(frozen=True, slots=True)
class Focused:
    """The held record the cursor names, as the diagnostics drawers read it.

    Attributes:
        key: The record's stable identifier.
        status: The record's status as the projection stated it, with its provenance.
        facts: The further facts the projection read off the stored record.
        cursor: The committed cursor the record was read through.
    """

    key: str
    status: TruthField[str]
    facts: Mapping[str, str]
    cursor: str


def focused(view: View) -> Focused | None:
    """Return the held record under the cursor, by stable id; ``None`` when none is held.

    A frame about one record -- a Run, a Task -- focuses that record when its own cursor
    names none.
    """
    model = view.register or view.projection
    if model is None:
        return None
    wanted = [key for key in (view.session.sel_id, view.session.subj_id) if key is not None]
    row = next((r for key in wanted for r in model.rows if r.key == key), None)
    if row is None:
        return None
    status = row.status if isinstance(row, ProjectionRow) else row.field("status")
    facts = row.facts if isinstance(row, ProjectionRow | SpineRow) else {}
    return Focused(key=row.key, status=status, facts=facts, cursor=model.source_cursor)


def _native_inspect(record: Focused) -> list[str]:
    """Return the provenance of the focused field: who stated it, at what revision, how fresh."""
    status = record.status
    return [
        f" INSPECT   value       {record.key} status {value_cell(status).full}",
        f"           answered by {status.producer} · revision {group(status.producer_revision)}",
        f"           projection  cursor {group(int(record.cursor))} · {status.truth_kind.value}",
        f"           freshness   {status.freshness.value} · precision {status.precision.value}",
        f"           raw state   {status.state.value}",
    ]


def _native_raw(record: Focused) -> list[str]:
    """Return the focused record's stored facts, bounded and labelled as the raw form."""
    facts = [f"{name}={value}" for name, value in sorted(record.facts.items())][:_RAW_LINES]
    rows = [f" RAW       {record.key} · the stored record's own fields, quoted exactly"]
    rows.append(f"           status={record.status.value}")
    rows.extend(f"           {fact}" for fact in facts)
    hidden = len(record.facts) - len(facts)
    rows.append("           bounded" + (f" · {hidden} more fields not shown" if hidden else ""))
    return rows


def _inspected(session: Session, fixture: Fixture) -> str:
    return "cost ~4.62 of 20.00" if session.route == "run.detail" else copy_target(session, fixture)


def scope_rows(view: View, spine: SpineView) -> list[str]:
    """Return scope home's inspect drawer: the tree itself, as the operator addresses it.

    The header names the project, so the root id the tree is read by and the URN its
    records are spelled under are stated here, where ``y`` copies the URN.
    """
    project = view.scope_name or "∅ unnamed · the header names the tree by its root id"
    return [
        f" SCOPE     project     {project}",
        f"           root        {spine.scope_id}",
        f"           urn         {dv.scope_urn(view.rows) or dv.NO_SCOPE_URN}",
        f"           revision    {group(int(spine.source_cursor))} · the tree was read through it",
    ]


def inspect_rows(view: View) -> list[str]:
    """Return the inspect drawer: the focused field with its provenance.

    On scope home the focus is the tree itself, so the drawer is its scope.
    """
    s, fx = view.session, view.fixture
    if s.route == HOME and isinstance(view.projection, SpineView):
        return scope_rows(view, view.projection)
    record = focused(view)
    if record is not None:
        return _native_inspect(record)
    if not fx.prototype:
        return [
            f" INSPECT   value       {_UNKNOWN} unknown · nothing has been read for this field",
            f"           answered by {_UNKNOWN} unknown · no producer has answered it here",
        ]
    if s.route == "entry":
        state = entry_state(view)
        rows = state.rows or ()
        value = rows[s.path_sel][0] if s.path_sel < len(rows) else state.state.lower()
        return [
            f" INSPECT   value       {value}",
            "           quality     measured · as stored in the snapshot",
            "           answered by none · no projection exists yet",
            f"           revision    {group(fx.proto.revision)} · the last known revision",
            f"           freshness   snapshot · {pt.SNAPSHOT_AGE} old, not live",
        ]
    derived = s.route == "run.detail"
    return [
        f" INSPECT   value       {_inspected(s, fx)}",
        "           quality     " + ("~ derived · provider rate card" if derived else "measured"),
        f"           answered by daemon@1 · revision {group(fx.proto.revision)} · exact",
        "           freshness   as of 14:02 · within this view’s 2s target",  # noqa: RUF001
    ]


def raw_rows(view: View) -> list[str]:
    """Return the raw drawer: a bounded, scrubbed segment of the runner's own words."""
    record = focused(view)
    if record is not None:
        return _native_raw(record)
    if not view.fixture.prototype:
        return [f" RAW       {_UNKNOWN} unknown · no raw segment has been read here"]
    target = dv.target_id(view.session, view.fixture)
    last = view.fixture.proto.revision
    return [
        f" RAW       {target} · the runner’s own words, quoted exactly",  # noqa: RUF001
        f"           seq {group(last - 2)}  kind=progress          ×212 coalesced",  # noqa: RUF001
        f"           seq {group(last - 1)}  kind=heartbeat         bytes=48",
        f"           seq {group(last)}  kind=tool.completed    bytes=380",
        "           bounded · scrubbed · retention-governed",
    ]


DRAWERS: Mapping[str, Callable[[View], list[str]]] = MappingProxyType(
    {"go": go_rows, "actions": action_rows, "inspect": inspect_rows, "raw": raw_rows}
)
