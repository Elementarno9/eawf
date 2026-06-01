"""Static ``/`` command-palette verb registry.

The palette reads a **statically declared** registry (the in-code list
is picked over a plugin-discovered one): every operator-reachable verb
is a frozen :class:`PaletteVerb` row carrying its name, one-line hint,
handler callable, the screens it is allowed on, and the optional profile /
runtime gates that hide it when the active project does not enable them.

The registry is the single source the
:class:`~eawf.surfaces.tui.palette.command_palette.CommandPalette`
overlay renders and filters. Keeping it a pure module-level tuple of
frozen dataclasses (no widget imports, no app state) means the filter
predicate :func:`visible_verbs` and the fuzzy ranker
:func:`rank_verbs` are unit-testable without mounting Textual.

``allowed_scopes`` is matched against the App's resolved scope name
(``"repo"`` / ``"workspace"`` / ``"user"``).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

from eawf.surfaces.render.link_wrap import ReferenceKind, iter_refs

if TYPE_CHECKING:
    from textual.app import App

    from eawf.kernel.state.models import State
    from eawf.surfaces.tui.widgets.backlog_table import BacklogTable

logger = logging.getLogger(__name__)

#: Scope names a verb may be allowed on. Mirrors the App scope names.
ScopeName = Literal["repo", "workspace", "user"]

#: All scopes — the common ``allowed_scopes`` value for cross-screen
#: verbs (search, theme, help, quit, etc.).
SCOPES_ALL: tuple[ScopeName, ...] = ("repo", "workspace", "user")

#: A verb handler: ``(app, args)`` where ``args`` is the raw argument
#: string the operator typed after the verb name (already split off). The
#: handler runs synchronously off the palette's ``Enter`` action; an async
#: handler is scheduled via ``app.call_later`` by the caller when needed.
VerbHandler = Callable[["App[None]", str], None]


@dataclass(frozen=True)
class PaletteVerb:
    """One row in the static palette verb registry.

    Attributes:
        name: The verb token including the leading slash (e.g. ``/find``).
            Matched + ranked by the palette filter.
        hint: A one-line description shown beside the verb in the palette.
        handler: The callable run on ``Enter`` — ``(app, args)``.
        allowed_scopes: Scope names the verb is offered on; a verb is
            hidden on any scope not in this tuple.
        requires_profile: Profile ids that gate the verb — it is hidden
            unless at least one is enabled. Empty = all profiles.
        requires_runtime: Runtime ids that gate the verb — hidden unless
            the active runtime matches. Empty = all runtimes.
        args_grammar: A display-only argument hint (e.g. ``<id>``); never
            parsed, only shown to cue the operator.
    """

    name: str
    hint: str
    handler: VerbHandler
    allowed_scopes: tuple[ScopeName, ...]
    requires_profile: tuple[str, ...] = field(default=())
    requires_runtime: tuple[str, ...] = field(default=())
    args_grammar: str = ""


@dataclass(frozen=True)
class GotoTarget:
    """One resolved ``/goto`` target."""

    kind: ReferenceKind
    target: str


def _handle_quit(app: App[None], args: str) -> None:
    """Quit the app (the ``/quit`` verb)."""
    app.exit()


def _handle_help(app: App[None], args: str) -> None:
    """Open the help overlay (the ``/help`` verb).

    Delegates to the App's ``open_help`` action so the help overlay is
    opened through the same modal-stack-capped path as the ``?`` keypress.
    """
    action = getattr(app, "action_open_help", None)
    if callable(action):
        action()
        return
    app.notify("/help is not wired yet", severity="information")


def _handle_audit(app: App[None], args: str) -> None:
    """Open the live audit-progress overlay (the ``/audit`` verb).

    The audit always targets the **current scope** — the audit id is
    derived from the App's bound state by :func:`_active_audit_id`, never
    typed. ``/audit`` therefore takes no argument: when the operator types
    a trailing token it is rejected with a one-line hint (rather than
    silently ignored, which is the confusing behaviour this surface
    replaces). With no argument it seeds an
    :class:`~eawf.surfaces.tui.screens.overlays.audit_running.AuditProgress`
    snapshot for the resolved scope + audit id and routes it through the
    App's modal-cap-aware ``push_modal`` (via
    :func:`~eawf.surfaces.tui.screens.overlays.audit_running.open_audit_running`).
    The per-check rows fill in as the daemon streams ``check_*`` events to
    the overlay's ``update_progress`` once the event subscription lands.
    The ``audit_completed`` (verdict=fail) flow that swaps in the
    audit-failed overlay is daemon-push driven.

    Args:
        app: The running App.
        args: Must be empty — ``/audit`` runs the current scope's audit
            and takes no id. A non-empty value is rejected with a hint.
    """
    from eawf.surfaces.tui.screens.overlays.audit_running import AuditProgress, open_audit_running

    trailing = args.strip()
    if trailing:
        logger.info(f"palette_verb_audit_rejected_arg arg={trailing!r}")
        app.notify(
            f"/audit takes no id — it audits the current scope (drop {trailing!r})",
            severity="warning",
        )
        return
    audit_id = _active_audit_id(app)
    scope_label = _active_scope_label(app)
    progress = AuditProgress(audit_id=audit_id, scope_label=scope_label, checks=())
    open_audit_running(app, progress)


def _handle_roadmap(app: App[None], args: str) -> None:
    """Open the plan-mode preview for ``/roadmap propose`` (the ``/roadmap`` verb).

    The plan-mode surface: when the sub-verb is ``propose`` (the path
    that returns ``status=needs_user``), this builds
    the proposed phase's wave-DAG tree via
    :func:`~eawf.surfaces.tui.screens.overlays.plan_preview.build_plan_tree` from
    the App's bound state and opens
    :class:`~eawf.surfaces.tui.screens.overlays.plan_preview.PlanPreviewModal`
    through the modal-cap-aware ``push_modal``. Other ``/roadmap``
    sub-verbs (``revise`` / ``apply`` / ``drop``) dispatch to their CLI
    verbs in the wave that lands them; until then they toast.

    Args:
        app: The running App.
        args: The ``/roadmap`` sub-verb plus its arguments (e.g.
            ``propose P26``).
    """
    sub_verb, _, rest = args.strip().partition(" ")
    if sub_verb != "propose":
        logger.info(f"palette_verb_roadmap_unwired sub_verb={sub_verb!r}")
        app.notify(f"/roadmap {sub_verb} is not wired yet", severity="information")
        return
    from eawf.surfaces.tui.screens.overlays.plan_preview import build_plan_tree, open_plan_preview

    phase_id = rest.strip()
    if not phase_id:
        app.notify(
            "/roadmap propose needs a phase id (e.g. /roadmap propose P26)", severity="warning"
        )
        return
    plan = build_plan_tree(getattr(app, "state", None), phase_id)
    open_plan_preview(app, plan)


def _active_audit_id(app: App[None]) -> str:
    """Resolve the most relevant audit id from the App's bound state.

    Resolves the newest audit (by ``created_at``) whose ``scope_id``
    matches the current phase — either exactly (``P26``) or as a
    descendant prefix (``P26-...``) so an iter- or wave-scoped audit under
    the phase still counts. Falls back to the newest audit overall when no
    audit matches the current phase, and to the ``"audit"`` placeholder
    when no state or no audits are present so the overlay title always
    renders. Read-only; never mutates state.

    Args:
        app: The running App.

    Returns:
        The resolved audit id, or ``"audit"`` when none is present.
    """
    state: State | None = getattr(app, "state", None)
    if state is None:
        return "audit"
    audits = state.audits or {}
    if not audits:
        return "audit"
    phase_id = state.current.phase_id
    scoped = [
        audit
        for audit in audits.values()
        if phase_id and (audit.scope_id == phase_id or audit.scope_id.startswith(f"{phase_id}-"))
    ]
    pool = scoped if scoped else list(audits.values())
    chosen = max(pool, key=lambda audit: audit.created_at)
    return chosen.id


def _active_scope_label(app: App[None]) -> str:
    """Resolve a non-typeable scope label for the audit overlay title.

    Reads the App's resolved scope name (the same value the palette
    filters verbs by) so the overlay always shows which scope's audit is
    running. Read-only; never mutates state and never reflects operator
    free-text.

    Args:
        app: The running App.

    Returns:
        The resolved scope name, or ``"scope"`` when the App exposes none
        (a bare test harness).
    """
    scope = getattr(app, "_scope", None)
    if isinstance(scope, str) and scope:
        return scope
    return "scope"


def _handle_metrics(app: App[None], args: str) -> None:
    """Open the V7 ``/metrics`` 3x2 dashboard overlay (the ``/metrics`` verb).

    Parses the ``--window 7d|30d|90d`` + ``--scope <urn>`` flags from
    *args* and opens
    :class:`~eawf.surfaces.tui.screens.overlays.metrics.MetricsModal` through the
    modal-cap-aware ``push_modal`` (via
    :func:`~eawf.surfaces.tui.screens.overlays.metrics.open_metrics`). The six
    tiles open with their placeholders and arm the 5 s refresh seam; they
    fill in once the daemon telemetry-projection RPC is wired. Read-only
    — opening the dashboard mutates nothing.

    Args:
        app: The running App.
        args: The raw ``/metrics`` arg string (window + scope flags).
    """
    from eawf.surfaces.tui.screens.overlays.metrics import open_metrics, parse_metrics_args

    open_metrics(app, parse_metrics_args(args))


def _handle_pr(app: App[None], args: str) -> None:
    """Open the ``/pr`` open-PRs overlay (the ``/pr`` verb).

    Fetches the repo's open PRs off the event loop via
    :func:`~eawf.surfaces.tui.screens.overlays.pr_list.request_pr_list` (a worker
    around the read-only ``gh pr list --json`` shell-out cached for 60 s) and
    opens :class:`~eawf.surfaces.tui.screens.overlays.pr_list.PrListModal` from the
    worker once the fetch lands — the keypress returns immediately so a slow
    ``gh`` never freezes the UI. The fetch degrades gracefully when ``gh`` is
    missing or errors — the overlay then renders the ``gh``-unavailable
    placeholder. Read-only.

    Args:
        app: The running App.
        args: The raw ``/pr`` arg string (unused).
    """
    from eawf.surfaces.tui.screens.overlays.pr_list import request_pr_list

    request_pr_list(app)


def _handle_config(app: App[None], args: str) -> None:
    """Open the registry-driven config window (the ``/config`` verb).

    Opens :class:`~eawf.surfaces.tui.screens.overlays.config_modal.ConfigModal`
    through the modal-cap-aware ``push_modal`` (via
    :func:`~eawf.surfaces.tui.screens.overlays.config_modal.open_config`). The
    window renders every operator-tunable key from the config registry in
    alphabetical tabs and saves through the layered-config writer (never
    ``state.json``). Takes no argument.

    Args:
        app: The running App.
        args: Unused — ``/config`` opens the full window with no argument.
    """
    from eawf.surfaces.tui.screens.overlays.config_modal import open_config

    open_config(app)


def _handle_init(app: App[None], args: str) -> None:
    """Open the TUI init wizard (the ``/init`` verb).

    The modal returns a concrete command plan; the App owns the callback so
    palette and auto-open paths share the same single-instance guard.
    """
    action = getattr(app, "action_open_init_wizard", None)
    if callable(action):
        action()
        return
    from eawf.surfaces.tui.screens.overlays.init_wizard import open_init_wizard

    open_init_wizard(app)


def _handle_events(app: App[None], args: str) -> None:
    """Open the ``/events`` last-50 event overlay (the ``/events`` verb).

    Reads the tail of the scope's on-disk event store
    (``<state_dir>/store/event.jsonl``) read-only via
    :func:`~eawf.surfaces.tui.screens.overlays.events.load_recent_events` and
    opens :class:`~eawf.surfaces.tui.screens.overlays.events.EventsModal`
    through the modal-cap-aware ``push_modal`` (via
    :func:`~eawf.surfaces.tui.screens.overlays.events.open_events`). The live
    daemon-push ring buffer prepends to this seed tail when the
    subscription lands; the ``f`` filter cycle + render path is reused.

    Args:
        app: The running App.
        args: The raw ``/events`` arg string (unused this wave).
    """
    from eawf.kernel.state.enums import StoreKind
    from eawf.kernel.store.paths import store_path
    from eawf.surfaces.tui.screens.overlays.events import load_recent_events, open_events

    state_path = getattr(app, "_state_path", None)
    event_path = store_path(state_path, StoreKind.EVENT) if state_path is not None else None
    open_events(app, load_recent_events(event_path))


def _handle_inbox(app: App[None], args: str) -> None:
    """Open the global needs_user inbox (the ``/inbox`` verb).

    Delegates to the App's ``action_open_inbox`` so the ``i`` keypress and
    this verb share the same cap-checked path that lists every open
    needs_user pause across scopes ranked by urgency. Takes no argument.

    Args:
        app: The running App.
        args: Unused — ``/inbox`` opens the full inbox with no argument.
    """
    action = getattr(app, "action_open_inbox", None)
    if callable(action):
        action()
        return
    app.notify("/inbox is not wired yet", severity="information")


def _handle_theme(app: App[None], args: str) -> None:
    """Swap the active theme + persist the choice (the ``/theme`` verb).

    Parses the logical theme name (``dark`` / ``light`` / ``cb`` /
    ``auto``) from *args* and applies it through the App's
    ``apply_theme``, which rebinds every semantic ``$var`` the structural
    CSS references. An unknown name is rejected with a warning toast and
    leaves the theme unchanged. The accepted choice is then persisted to
    the global config layer's ``ui.theme`` through the same
    daemon-mediated layered writer the config window uses, so the palette
    survives the next launch. The persist is best-effort: a swap is a
    cosmetic preference, so a writer failure (daemon unreachable in a
    non-daemonless context, malformed layer) downgrades to an
    applied-but-not-saved toast rather than aborting the swap.

    Args:
        app: The running App.
        args: The raw ``/theme`` arg string — its first token is the
            logical theme name. Empty / unknown names are rejected.
    """
    name = args.strip().split()[0] if args.strip() else ""
    apply = getattr(app, "apply_theme", None)
    if not callable(apply) or not apply(name):
        logger.info(f"palette_verb_theme_rejected name={name!r}")
        app.notify(f"unknown theme: {name!r} (choose dark/light/cb/auto)", severity="warning")
        return
    _persist_theme_choice(app, name)


def _persist_theme_choice(app: App[None], name: str) -> None:
    """Persist the applied logical theme name to the global ``ui.theme`` layer.

    Best-effort: routes through the CLI ``_save_value_to_layer`` (the
    daemon-mediated layered writer), and on any failure logs + toasts that
    the theme applied but was not saved, leaving the live swap intact.

    Args:
        app: The running App (for the toast on a writer failure).
        name: The accepted logical theme name to persist.
    """
    from eawf.kernel.config.layered import global_config_path
    from eawf.surfaces.cli.commands.config import _save_value_to_layer

    try:
        _save_value_to_layer(target_path=global_config_path(), key="ui.theme", value=name)
    except Exception as exc:
        logger.warning(f"_persist_theme_choice name={name!r} not saved exc={exc!r}")
        app.notify(f"theme applied (not saved: {exc})", severity="warning")


def rank_find_hits(state: State | None, query: str) -> list[str]:
    """Fuzzy-rank wave + backlog ids by *query* against their id and title.

    Pools every wave and backlog item, scoring each against *query* with
    :func:`fuzzy_score` over **both** its id and its title and keeping the
    better (lower) of the two. Entities that match neither field are
    dropped. Survivors sort by ascending score (best first) with the id as
    a stable tie-break so equal scores order deterministically. An empty /
    whitespace *query* returns no hits (a ``/find`` with no text has nothing
    to drill into).

    Args:
        state: The bound state, or ``None`` when no state is loaded.
        query: The operator's search text.

    Returns:
        The matching entity ids, best fuzzy match first.
    """
    trimmed = query.strip()
    if state is None or not trimmed:
        return []
    scored: list[tuple[int, str]] = []
    candidates: list[tuple[str, str]] = [(wave.id, wave.title) for wave in state.waves.values()]
    if state.backlog is not None:
        candidates.extend((item.id, item.title) for item in state.backlog.values())
    for entity_id, title in candidates:
        id_score = fuzzy_score(trimmed, entity_id)
        title_score = fuzzy_score(trimmed, title)
        best = _best_score(id_score, title_score)
        if best is None:
            continue
        scored.append((best, entity_id))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [entity_id for _, entity_id in scored]


def _best_score(left: int | None, right: int | None) -> int | None:
    """Return the better (lower) of two :func:`fuzzy_score` results.

    Args:
        left: A score, or ``None`` when that field did not match.
        right: A score, or ``None`` when that field did not match.

    Returns:
        The lower score, or ``None`` when both inputs are ``None``.
    """
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)


def _handle_find(app: App[None], args: str) -> None:
    """Fuzzy-search waves + backlog and drill into the best hit (``/find``).

    Ranks every wave and backlog item against the typed query by id + title
    (:func:`rank_find_hits`) and opens the existing
    :class:`~eawf.surfaces.tui.screens.overlays.detail.DetailModal` for the
    top-ranked entity, resolved through
    :func:`~eawf.surfaces.tui.screens.overlays.detail.resolve_detail`. The modal
    routes through the App's modal-cap-aware ``push_modal`` (falling back to
    ``push_screen`` on a bare host) so the drill-in honours the stack-depth
    limit. An empty query or a query that matches nothing toasts a hint and
    opens nothing. Read-only — searching mutates no state.

    Args:
        app: The running App.
        args: The raw ``/find`` query text.
    """
    from eawf.surfaces.tui.screens.overlays.detail import DetailModal, resolve_detail

    query = args.strip()
    if not query:
        app.notify("/find needs a query (e.g. /find W19)", severity="warning")
        return
    state = getattr(app, "state", None)
    hits = rank_find_hits(state, query)
    if not hits:
        logger.info(f"palette_verb_find_no_match query={query!r}")
        app.notify(f"no waves or backlog match {query!r}", severity="information")
        return
    card = resolve_detail(state, hits[0])
    logger.info(f"palette_verb_find query={query!r} hit={hits[0]!r}")
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(DetailModal(card, state=state))
        return
    app.push_screen(DetailModal(card, state=state))


def _candidate_title(record: object) -> str:
    """Return the best fuzzy-search title field for a state row."""
    for field_name in ("title", "summary", "uri", "slug", "rationale"):
        value = getattr(record, field_name, None)
        if isinstance(value, str) and value:
            return value
    return ""


def _goto_candidates(state: State | None) -> list[tuple[ReferenceKind, str, str]]:
    """Return typed ``/goto`` candidates from the bound state."""
    if state is None:
        return []
    candidates: list[tuple[ReferenceKind, str, str]] = []
    if state.project is not None:
        title = _candidate_title(state.project)
        candidates.append(("repo", state.project.code, title))
        candidates.append(("project", state.project.code, title))
    candidates.extend(("phase", row.id, row.title) for row in state.phases.values())
    candidates.extend(("iter", row.id, row.title) for row in state.iters.values())
    candidates.extend(("wave", row.id, row.title) for row in state.waves.values())
    if state.hypotheses is not None:
        candidates.extend(("hypothesis", row.id, row.title) for row in state.hypotheses.values())
    if state.audits is not None:
        candidates.extend(("audit", row.id, _candidate_title(row)) for row in state.audits.values())
    if state.incidents is not None:
        candidates.extend(("event", row.id, row.title) for row in state.incidents.values())
    candidates.extend(
        ("artifact", row.id, _candidate_title(row)) for row in state.artifacts.values()
    )
    candidates.extend(("decision", row.id, row.title) for row in state.decisions.values())
    if state.backlog is not None:
        candidates.extend(("event", row.id, row.title) for row in state.backlog.values())
    candidates.extend(
        ("report", row.id, _candidate_title(row)) for row in state.agent_sessions.values()
    )
    if state.plugins:
        candidates.extend(
            ("profile", row.id, _candidate_title(row)) for row in state.plugins.values()
        )
    if state.memory_index is not None:
        candidates.extend(("memory", row.id, row.summary) for row in state.memory_index.values())
    candidates.extend(("spec", row.id, row.title) for row in state.phases.values())
    candidates.extend(("spec", row.id, row.title) for row in state.iters.values())
    candidates.extend(("spec", row.id, row.title) for row in state.waves.values())
    return candidates


def rank_goto_refs(state: State | None, query: str) -> list[GotoTarget]:
    """Rank typed reference targets for the ``/goto`` palette verb."""
    trimmed = query.strip()
    if not trimmed:
        return []
    explicit = iter_refs(trimmed)
    if explicit:
        return [GotoTarget(ref.kind, ref.target) for ref in explicit]
    scored: list[tuple[int, str, str, GotoTarget]] = []
    for kind, target, title in _goto_candidates(state):
        id_score = fuzzy_score(trimmed, target)
        title_score = fuzzy_score(trimmed, title)
        best = _best_score(id_score, title_score)
        if best is None:
            continue
        scored.append((best, kind, target, GotoTarget(kind, target)))
    scored.sort(key=lambda row: (row[0], row[1], row[2]))
    return [row[3] for row in scored]


def _handle_goto(app: App[None], args: str) -> None:
    """Open a typed reference by id, URN, or fuzzy entity title (``/goto``)."""
    query = args.strip()
    if not query:
        app.notify("/goto needs a reference (e.g. /goto P28-I03-W35)", severity="warning")
        return
    hit = next(iter(rank_goto_refs(getattr(app, "state", None), query)), None)
    if hit is None:
        logger.info(f"palette_verb_goto_no_match query={query!r}")
        app.notify(f"no reference matches {query!r}", severity="information")
        return
    action = getattr(app, "action_open_ref", None)
    if callable(action):
        action(hit.kind, hit.target)
        return
    from eawf.surfaces.tui.screens.overlays.reference import ReferenceModal, resolve_reference

    card = resolve_reference(getattr(app, "state", None), hit.kind, hit.target)
    app.push_screen(ReferenceModal(card))


def _handle_filter(app: App[None], args: str) -> None:
    """Apply a substring filter to the named pane (the ``/filter`` verb).

    The only filterable pane is ``backlog`` — ``/filter backlog <needle>``
    drives :meth:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable.apply_filter`.
    An empty needle clears the filter (restores all rows). An unknown pane
    or a missing backlog widget (e.g. on a scope that does not mount it)
    toasts a hint and changes nothing.

    Args:
        app: The running App.
        args: ``<pane> <needle>`` — the first token is the pane name, the
            remainder the substring to match.
    """
    pane, _, needle = args.strip().partition(" ")
    if pane != "backlog":
        logger.info(f"palette_verb_filter_unknown_pane pane={pane!r}")
        app.notify(f"/filter only supports the backlog pane (got {pane!r})", severity="warning")
        return
    table = _backlog_table(app)
    if table is None:
        app.notify("/filter backlog: no backlog pane on this screen", severity="warning")
        return
    table.apply_filter(needle.strip())


def _handle_sort(app: App[None], args: str) -> None:
    """Cycle the sort key of the named pane (the ``/sort`` verb).

    The only sortable pane is ``backlog`` — ``/sort backlog`` advances
    :meth:`~eawf.surfaces.tui.widgets.backlog_table.BacklogTable.cycle_sort` to
    the next sort key (priority → id → status → wrap). An unknown pane or a
    missing backlog widget toasts a hint and changes nothing.

    Args:
        app: The running App.
        args: ``<pane>`` — the pane name to cycle (only ``backlog``).
    """
    pane = args.strip().split()[0] if args.strip() else ""
    if pane != "backlog":
        logger.info(f"palette_verb_sort_unknown_pane pane={pane!r}")
        app.notify(f"/sort only supports the backlog pane (got {pane!r})", severity="warning")
        return
    table = _backlog_table(app)
    if table is None:
        app.notify("/sort backlog: no backlog pane on this screen", severity="warning")
        return
    table.cycle_sort()


def _backlog_table(app: App[None]) -> BacklogTable | None:
    """Return the mounted :class:`BacklogTable`, or ``None`` when absent.

    The backlog pane lives on the scope screen, which may sit beneath an
    open overlay (the palette itself, or a stacked modal), so this walks
    the whole screen stack rather than only the topmost screen. It is only
    mounted on scopes that render it (the repo scope today); on any other
    scope no screen yields a match and the caller degrades to a hint rather
    than raising.

    Args:
        app: The running App to query.

    Returns:
        The first mounted :class:`BacklogTable`, or ``None``.
    """
    from eawf.surfaces.tui.widgets.backlog_table import BacklogTable

    for screen in app.screen_stack:
        tables = screen.query(BacklogTable)
        if tables:
            return tables.first()
    return None


#: The static verb registry. Order is display order in the palette before
#: fuzzy ranking.
VERBS: tuple[PaletteVerb, ...] = (
    # --- cross-screen navigation + filter ---------------------------------
    PaletteVerb(
        "/find",
        "fuzzy ID + title search",
        _handle_find,
        SCOPES_ALL,
        args_grammar="<query>",
    ),
    PaletteVerb(
        "/goto",
        "open typed reference",
        _handle_goto,
        SCOPES_ALL,
        args_grammar="<id|urn|query>",
    ),
    PaletteVerb(
        "/filter",
        "filter pane contents",
        _handle_filter,
        SCOPES_ALL,
        args_grammar="<pane> <key>",
    ),
    PaletteVerb("/sort", "cycle sort key", _handle_sort, SCOPES_ALL, args_grammar="<pane> <col>"),
    PaletteVerb(
        "/theme",
        "theme dark/light/cb/auto",
        _handle_theme,
        SCOPES_ALL,
        args_grammar="<name>",
    ),
    PaletteVerb("/config", "config window (registry-driven)", _handle_config, SCOPES_ALL),
    PaletteVerb("/init", "init wizard", _handle_init, SCOPES_ALL),
    PaletteVerb("/inbox", "needs_user pause inbox (by urgency)", _handle_inbox, SCOPES_ALL),
    PaletteVerb("/events", "last 50 events overlay", _handle_events, SCOPES_ALL),
    PaletteVerb(
        "/metrics",
        "metrics dashboard (3x2 tiles)",
        _handle_metrics,
        SCOPES_ALL,
        args_grammar="[--window 7d|30d|90d] [--scope <urn>]",
    ),
    PaletteVerb("/pr", "open PRs (gh shell-out, cached)", _handle_pr, SCOPES_ALL),
    PaletteVerb("/help", "verb help / keymap", _handle_help, SCOPES_ALL, args_grammar="[verb]"),
    PaletteVerb("/quit", "quit", _handle_quit, SCOPES_ALL),
    # --- skill-dispatch passthrough (CLI verb wrappers) -------------------
    PaletteVerb(
        "/roadmap",
        "roadmap action (sub-verb)",
        _handle_roadmap,
        SCOPES_ALL,
        args_grammar="<sub-verb> ...",
    ),
    PaletteVerb("/audit", "audit the current scope", _handle_audit, SCOPES_ALL),
)


def visible_verbs(
    scope: ScopeName,
    profiles: Iterable[str] = (),
    runtime: str = "",
) -> list[PaletteVerb]:
    """Return the verbs offered on *scope* under the active gates.

    A verb is offered when (a) *scope* is in its ``allowed_scopes``, (b)
    its ``requires_profile`` is empty or intersects *profiles*, and (c)
    its ``requires_runtime`` is empty or contains *runtime*. The order of
    :data:`VERBS` is preserved so the palette's pre-filter display order is
    stable; :func:`rank_verbs` re-orders by fuzzy score once the operator
    types.

    Args:
        scope: The active scope name.
        profiles: The project's enabled profile ids (empty = none, so
            profile-gated verbs are hidden).
        runtime: The active runtime id (empty = unknown, so
            runtime-gated verbs are hidden).

    Returns:
        The visible verbs in registry order.
    """
    profile_set = set(profiles)
    out: list[PaletteVerb] = []
    for verb in VERBS:
        if scope not in verb.allowed_scopes:
            continue
        if verb.requires_profile and not profile_set.intersection(verb.requires_profile):
            continue
        if verb.requires_runtime and runtime not in verb.requires_runtime:
            continue
        out.append(verb)
    return out


def fuzzy_score(needle: str, haystack: str) -> int | None:
    """Score a subsequence fuzzy match of *needle* in *haystack*.

    Returns ``None`` when *needle* is not a subsequence of *haystack*
    (case-insensitive). Otherwise returns a non-negative score where a
    lower number is a *better* match: contiguous, early matches score
    near ``0``; scattered, late matches score higher. An empty *needle*
    scores ``0`` (everything matches equally).

    The score is the sum of the gaps between consecutive matched
    positions plus the index of the first match, so ``"cf"`` ranks
    ``/theme`` (no — not a subsequence) out and ranks ``/config`` by how
    tightly ``c`` and ``f`` cluster.

    Args:
        needle: The operator's filter text (the leading ``/`` is matched
            literally like any other char).
        haystack: The verb name to score against.

    Returns:
        The match score (lower is better), or ``None`` when no match.
    """
    needle = needle.lower()
    haystack_low = haystack.lower()
    if not needle:
        return 0
    score = 0
    last = -1
    start = 0
    for char in needle:
        found = haystack_low.find(char, last + 1)
        if found == -1:
            return None
        if last == -1:
            start = found
        else:
            score += found - last - 1
        last = found
    return score + start


def rank_verbs(verbs: Iterable[PaletteVerb], needle: str) -> list[PaletteVerb]:
    """Filter *verbs* to fuzzy matches of *needle*, best match first.

    Verbs whose name is not a subsequence match of *needle* are dropped.
    The survivors sort by ascending :func:`fuzzy_score` (best first), with
    the verb name as a stable tie-break so equal scores order
    deterministically. An empty / whitespace *needle* returns all *verbs*
    in input order (every score is ``0``).

    Args:
        verbs: The candidate verbs (typically :func:`visible_verbs`
            output).
        needle: The operator's filter text.

    Returns:
        The matching verbs, best fuzzy match first.
    """
    trimmed = needle.strip()
    if not trimmed:
        return list(verbs)
    scored: list[tuple[int, str, PaletteVerb]] = []
    for verb in verbs:
        score = fuzzy_score(trimmed, verb.name)
        if score is None:
            continue
        scored.append((score, verb.name, verb))
    scored.sort(key=lambda row: (row[0], row[1]))
    return [verb for _, _, verb in scored]


def split_verb_args(text: str) -> tuple[str, str]:
    """Split palette input into the matched verb name and its arg string.

    Resolves the **longest** registered verb name that the input starts
    with, then returns ``(verb_name, args)``
    with the remaining text trimmed. When no registered verb prefixes the
    input, the first whitespace-delimited token is returned as the name
    and the remainder as args (so an unknown verb still parses cleanly).

    Args:
        text: The raw palette input (including the leading ``/``).

    Returns:
        A ``(verb_name, args)`` tuple; ``args`` is empty when none were
        typed.
    """
    stripped = text.strip()
    best: str | None = None
    for verb in VERBS:
        matches = stripped == verb.name or stripped.startswith(verb.name + " ")
        if matches and (best is None or len(verb.name) > len(best)):
            best = verb.name
    if best is not None:
        return best, stripped[len(best) :].strip()
    head, _, tail = stripped.partition(" ")
    return head, tail.strip()


__all__ = [
    "SCOPES_ALL",
    "VERBS",
    "GotoTarget",
    "PaletteVerb",
    "ScopeName",
    "VerbHandler",
    "fuzzy_score",
    "rank_find_hits",
    "rank_goto_refs",
    "rank_verbs",
    "split_verb_args",
    "visible_verbs",
]
