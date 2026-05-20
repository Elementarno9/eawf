"""Static ``/`` command-palette verb registry (C06 §5.6 / Decision D13).

The palette reads a **statically declared** registry (D13 picked the
in-code list over a plugin-discovered one): every operator-reachable verb
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
    """One row in the static palette verb registry (D13).

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


#: The static verb registry (D13). Order is display order in the palette
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
    PaletteVerb("/events", "last 50 events overlay", _placeholder("/events"), SCOPES_ALL),
    PaletteVerb(
        "/metrics",
        "metrics dashboard",
        _placeholder("/metrics"),
        SCOPES_ALL,
        args_grammar="[--window 7d|30d|90d]",
    ),
    PaletteVerb("/pr", "open PRs (gh shell-out)", _placeholder("/pr"), SCOPES_ALL),
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
        _placeholder("/roadmap"),
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
    PaletteVerb(
        "/audit", "audit a scope", _placeholder("/audit"), SCOPES_ALL, args_grammar="<scope-urn>"
    ),
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
