"""Static ``/`` command-palette verb registry.

The palette reads a **statically declared** registry (the in-code list
is picked over a plugin-discovered one): every operator-reachable verb
is a frozen :class:`PaletteVerb` row carrying its name, one-line hint,
handler callable, the screens it is allowed on, and the optional profile /
runtime gates that hide it when the active project does not enable them.

The registry is the single source the :class:`~eawf.tui_v2.palette.command_palette.CommandPalette`
overlay renders and filters. Keeping it a pure module-level tuple of
frozen dataclasses (no widget imports, no app state) means the filter
predicate :func:`visible_verbs` and the fuzzy ranker
:func:`rank_verbs` are unit-testable without mounting Textual.

Scope of this wave (P26-W19). The palette infrastructure + the
navigation / read-only verbs land here; the mutating + overlay-opening
handlers (``/metrics``, ``/pr``, ``/events``, the ``/wave`` family, the
skill-dispatch passthroughs) are registered with **placeholder handlers**
that surface a "not yet wired" toast. The follow-up waves of this band
(W20 PlanPreview / NeedsUser / Audit overlays, W21 ``/metrics`` + ``/pr``)
replace those placeholders in place — the registry shape and the palette
contract do not change. Per V11 the ``/wave`` palette verbs are
read-only; mutating wave actions never appear in the palette and only
reach the operator through the audit-failed overlay's structured menu.

``allowed_scopes`` is matched against the App's resolved scope name
(``"repo"`` / ``"workspace"`` / ``"user"`` / ``"wave_board"``); the
wave-board scope lands with the wave-board screen in a later wave but is
declared here so the wave verbs carry their final scope set.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from textual.app import App

    from eawf.state.models import State

logger = logging.getLogger(__name__)

#: Scope names a verb may be allowed on. Mirrors the App scope names
#: (``repo`` / ``workspace`` / ``user``) plus ``wave_board`` for the
#: wave-board screen that lands in a later wave of this band.
ScopeName = Literal["repo", "workspace", "user", "wave_board"]

#: All four scopes — the common ``allowed_scopes`` value for cross-screen
#: verbs (search, theme, help, quit, etc.).
SCOPES_ALL: tuple[ScopeName, ...] = ("repo", "workspace", "user", "wave_board")

#: A verb handler: ``(app, args)`` where ``args`` is the raw argument
#: string the operator typed after the verb name (already split off). The
#: handler runs synchronously off the palette's ``Enter`` action; an async
#: handler is scheduled via ``app.call_later`` by the caller when needed.
VerbHandler = Callable[["App[None]", str], None]


def _placeholder(verb_name: str) -> VerbHandler:
    """Build a placeholder handler that toasts a "not yet wired" notice.

    Used for verbs whose concrete behaviour lands in a later wave of this
    band (W20 / W21). The registry shape stays final; only the handler is
    swapped when the real overlay arrives.

    Args:
        verb_name: The verb the placeholder stands in for (for the toast).

    Returns:
        A :data:`VerbHandler` that notifies and logs, mutating nothing.
    """

    def handler(app: App[None], args: str) -> None:
        logger.info(f"palette_verb_placeholder verb={verb_name!r} args={args!r}")
        app.notify(f"{verb_name} is not wired yet", severity="information")

    return handler


@dataclass(frozen=True)
class PaletteVerb:
    """One row in the static palette verb registry.

    Attributes:
        name: The verb token including the leading slash (e.g. ``/find``,
            ``/wave open``). Matched + ranked by the palette filter.
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
    :class:`~eawf.tui_v2.screens.overlays.audit_running.AuditProgress`
    snapshot for the resolved scope + audit id and routes it through the
    App's modal-cap-aware ``push_modal`` (via
    :func:`~eawf.tui_v2.screens.overlays.audit_running.open_audit_running`).
    The per-check rows fill in as the daemon streams ``check_*`` events to
    the overlay's ``update_progress`` once the event subscription lands.
    The ``audit_completed`` (verdict=fail) flow that swaps in the
    audit-failed overlay is daemon-push driven.

    Args:
        app: The running App.
        args: Must be empty — ``/audit`` runs the current scope's audit
            and takes no id. A non-empty value is rejected with a hint.
    """
    from eawf.tui_v2.screens.overlays.audit_running import AuditProgress, open_audit_running

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
    :func:`~eawf.tui_v2.screens.overlays.plan_preview.build_plan_tree` from
    the App's bound state and opens
    :class:`~eawf.tui_v2.screens.overlays.plan_preview.PlanPreviewModal`
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
    from eawf.tui_v2.screens.overlays.plan_preview import build_plan_tree, open_plan_preview

    phase_id = rest.strip()
    if not phase_id:
        app.notify(
            "/roadmap propose needs a phase id (e.g. /roadmap propose P26)", severity="warning"
        )
        return
    plan = build_plan_tree(getattr(app, "state", None), phase_id)
    open_plan_preview(app, plan)


def _active_audit_id(app: App[None]) -> str:
    """Resolve a best-effort audit id from the App's bound state.

    Prefers the active iter's ``audit_id``, then any phase's, falling back
    to ``"audit"`` so the overlay title always renders. Read-only; never
    mutates state.

    Args:
        app: The running App.

    Returns:
        The resolved audit id, or ``"audit"`` when none is present.
    """
    state: State | None = getattr(app, "state", None)
    if state is None:
        return "audit"
    for iteration in state.iters.values():
        if iteration.audit_id:
            return iteration.audit_id
    for phase in state.phases.values():
        if phase.audit_id:
            return phase.audit_id
    return "audit"


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
    :class:`~eawf.tui_v2.screens.overlays.metrics.MetricsModal` through the
    modal-cap-aware ``push_modal`` (via
    :func:`~eawf.tui_v2.screens.overlays.metrics.open_metrics`). The six
    tiles open with their placeholders and arm the 5 s refresh seam; they
    fill in once the daemon telemetry-projection RPC is wired. Read-only
    — opening the dashboard mutates nothing.

    Args:
        app: The running App.
        args: The raw ``/metrics`` arg string (window + scope flags).
    """
    from eawf.tui_v2.screens.overlays.metrics import open_metrics, parse_metrics_args

    open_metrics(app, parse_metrics_args(args))


def _handle_pr(app: App[None], args: str) -> None:
    """Open the ``/pr`` open-PRs overlay (the ``/pr`` verb).

    Opens :class:`~eawf.tui_v2.screens.overlays.pr_list.PrListModal`
    through the modal-cap-aware ``push_modal`` (via
    :func:`~eawf.tui_v2.screens.overlays.pr_list.open_pr_list`). The list
    opens empty with the ``gh``-shell-out placeholder; the lazy
    ``gh pr list --json`` fetch + 60 s cache lands later this band —
    the overlay degrades gracefully when ``gh`` is absent. Read-only.

    Args:
        app: The running App.
        args: The raw ``/pr`` arg string (unused this wave).
    """
    from eawf.tui_v2.screens.overlays.pr_list import open_pr_list

    open_pr_list(app, ())


def _handle_config(app: App[None], args: str) -> None:
    """Open the registry-driven config window (the ``/config`` verb).

    Opens :class:`~eawf.tui_v2.screens.overlays.config_modal.ConfigModal`
    through the modal-cap-aware ``push_modal`` (via
    :func:`~eawf.tui_v2.screens.overlays.config_modal.open_config`). The
    window renders every operator-tunable key from the config registry in
    alphabetical tabs and saves through the layered-config writer (never
    ``state.json``). Takes no argument.

    Args:
        app: The running App.
        args: Unused — ``/config`` opens the full window with no argument.
    """
    from eawf.tui_v2.screens.overlays.config_modal import open_config

    open_config(app)


def _handle_events(app: App[None], args: str) -> None:
    """Open the ``/events`` last-50 event overlay (the ``/events`` verb).

    Reads the tail of the scope's on-disk event store
    (``<state_dir>/store/event.jsonl``) read-only via
    :func:`~eawf.tui_v2.screens.overlays.events.load_recent_events` and
    opens :class:`~eawf.tui_v2.screens.overlays.events.EventsModal`
    through the modal-cap-aware ``push_modal`` (via
    :func:`~eawf.tui_v2.screens.overlays.events.open_events`). The live
    daemon-push ring buffer prepends to this seed tail when the
    subscription lands; the ``f`` filter cycle + render path is reused.

    Args:
        app: The running App.
        args: The raw ``/events`` arg string (unused this wave).
    """
    from eawf.state.enums import StoreKind
    from eawf.store.paths import store_path
    from eawf.tui_v2.screens.overlays.events import load_recent_events, open_events

    state_path = getattr(app, "_state_path", None)
    event_path = store_path(state_path, StoreKind.EVENT) if state_path is not None else None
    open_events(app, load_recent_events(event_path))


#: The static verb registry. Order is display order in the palette
#: before fuzzy ranking. Handlers that land in a later wave use
#: :func:`_placeholder` so the registry is complete now and the follow-up
#: wave swaps the handler in place.
VERBS: tuple[PaletteVerb, ...] = (
    # --- cross-screen navigation + filter ---------------------------------
    PaletteVerb(
        "/find",
        "fuzzy ID + title search",
        _placeholder("/find"),
        SCOPES_ALL,
        args_grammar="<query>",
    ),
    PaletteVerb(
        "/filter",
        "filter pane contents",
        _placeholder("/filter"),
        SCOPES_ALL,
        args_grammar="<pane> <key>",
    ),
    PaletteVerb(
        "/sort", "cycle sort key", _placeholder("/sort"), SCOPES_ALL, args_grammar="<pane> <col>"
    ),
    PaletteVerb(
        "/switch",
        "switch scope",
        _placeholder("/switch"),
        ("workspace", "user"),
        args_grammar="<scope> <id>",
    ),
    PaletteVerb(
        "/theme",
        "theme dark/light/cb/auto",
        _placeholder("/theme"),
        SCOPES_ALL,
        args_grammar="<name>",
    ),
    PaletteVerb("/config", "config window (registry-driven)", _handle_config, SCOPES_ALL),
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
    # --- wave-board read-only verbs (V11 — read-only only) ----------------
    PaletteVerb(
        "/wave",
        "wave-scoped action",
        _placeholder("/wave"),
        ("repo", "wave_board"),
        args_grammar="<verb> [<id>]",
    ),
    PaletteVerb(
        "/wave open",
        "open worktree in $EDITOR",
        _placeholder("/wave open"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave log",
        "tail session log",
        _placeholder("/wave log"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave state",
        "show wave state JSON",
        _placeholder("/wave state"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave report",
        "last agent report",
        _placeholder("/wave report"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave criteria",
        "WaveSpec body",
        _placeholder("/wave criteria"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave deps",
        "wave DAG",
        _placeholder("/wave deps"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave events",
        "events scoped to wave",
        _placeholder("/wave events"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    PaletteVerb(
        "/wave dispatch",
        "session-handle history",
        _placeholder("/wave dispatch"),
        ("repo", "wave_board"),
        args_grammar="[<id>]",
    ),
    # --- worktree ---------------------------------------------------------
    PaletteVerb("/wt", "worktrees overlay", _placeholder("/wt"), SCOPES_ALL),
    # --- skill-dispatch passthrough (CLI verb wrappers) -------------------
    PaletteVerb(
        "/roadmap",
        "roadmap action (sub-verb)",
        _handle_roadmap,
        SCOPES_ALL,
        args_grammar="<sub-verb> ...",
    ),
    PaletteVerb("/prep", "prep phase", _placeholder("/prep"), SCOPES_ALL, args_grammar="<P##>"),
    PaletteVerb(
        "/flow", "flow pipeline", _placeholder("/flow"), SCOPES_ALL, args_grammar="<topic>"
    ),
    PaletteVerb(
        "/research", "research brief", _placeholder("/research"), SCOPES_ALL, args_grammar="<topic>"
    ),
    PaletteVerb(
        "/spike",
        "spike brief",
        _placeholder("/spike"),
        SCOPES_ALL,
        requires_profile=("research",),
        args_grammar="<slug>",
    ),
    PaletteVerb(
        "/design",
        "design pass",
        _placeholder("/design"),
        SCOPES_ALL,
        requires_profile=("research",),
        args_grammar="<surface>",
    ),
    PaletteVerb("/audit", "audit the current scope", _handle_audit, SCOPES_ALL),
    PaletteVerb("/ship", "ship phase", _placeholder("/ship"), SCOPES_ALL, args_grammar="<P##>"),
    PaletteVerb(
        "/review", "review PR", _placeholder("/review"), SCOPES_ALL, args_grammar="[--pr <url>]"
    ),
    PaletteVerb(
        "/polish", "polish sweep", _placeholder("/polish"), SCOPES_ALL, args_grammar="[<scope>]"
    ),
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
    positions plus the index of the first match, so ``"wo"`` ranks
    ``/wt`` (no — not a subsequence) out and ranks ``/wave open`` by how
    tightly ``w`` and ``o`` cluster.

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
    with (so ``/wave open W01`` resolves the two-token ``/wave open`` verb
    rather than the bare ``/wave``), then returns ``(verb_name, args)``
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
    "PaletteVerb",
    "ScopeName",
    "VerbHandler",
    "fuzzy_score",
    "rank_verbs",
    "split_verb_args",
    "visible_verbs",
]
