"""``CommandPalette`` — the ``/``-triggered command palette.

A :class:`~textual.screen.ModalScreen` overlaying any scope screen: the
operator presses ``/`` (wired on the shared
:class:`~eawf.surfaces.tui.scopes.ScopeScreen`), an :class:`~textual.widgets.Input`
opens pre-filled with ``/``, and an :class:`~textual.widgets.OptionList`
below lists the verbs from the static registry
(:mod:`eawf.surfaces.tui.palette.verbs`) filtered + ranked by fuzzy match as the
operator types.

Palette UX:

* The verbs shown are :func:`~eawf.surfaces.tui.palette.verbs.visible_verbs`
  for the App's resolved scope (so profile-gated verbs hide off their
  profile, etc.), then
  :func:`~eawf.surfaces.tui.palette.verbs.rank_verbs` re-orders by the typed
  filter.
* ``Tab`` autocompletes the input to the highlighted option's verb name.
* ``Enter`` parses ``(verb, args)`` via
  :func:`~eawf.surfaces.tui.palette.verbs.split_verb_args`, dismisses the
  palette, and runs the matched verb's handler.
* ``Esc`` dismisses without executing.

The legacy ``:`` alias is gone in v0.3 — only ``/`` opens the palette;
this module never binds ``:``.

The fuzzy filter + the verb resolution live in pure functions on the
registry module so the palette widget stays a thin view: it owns the
Input / OptionList wiring and the keystroke routing, not the matching
logic. That keeps the matcher unit-testable without mounting Textual and
keeps the palette Pilot-testable through ``run_test``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList
from textual.widgets.option_list import Option

from eawf.surfaces.tui.palette.verbs import (
    PaletteVerb,
    ScopeName,
    rank_verbs,
    split_verb_args,
    visible_verbs,
)
from eawf.surfaces.tui.widgets.sigils import Sigil, glyph

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The leading token the palette input is seeded with -- pressing ``/``
#: opens the palette already primed so the operator types the verb tail
#: directly.
PALETTE_PREFIX: str = "/"

#: Verb names that advance the workflow lifecycle (open phase / iter /
#: wave, run the verdict step). They render under the ``Lifecycle``
#: section wearing the stage sigil so the operator reads them as the
#: state-mutating moves, distinct from the navigation / chrome verbs.
LIFECYCLE_VERB_NAMES: frozenset[str] = frozenset({"/roadmap", "/audit", "/init"})

#: The section headers, in display order. ``Recent`` surfaces the verbs
#: the operator ran most recently; ``Lifecycle`` the stage-sigil-bearing
#: lifecycle verbs; ``All`` the remaining navigation / chrome verbs.
SECTION_RECENT: str = "Recent"
SECTION_LIFECYCLE: str = "Lifecycle"
SECTION_ALL: str = "All"

#: The frozen no-match prompt. The separator between ``matches`` and
#: ``Esc`` is a real EM-DASH (U+2014) intentional UI DATA, not an ASCII
#: hyphen, and it carries no trailing period (a prompt is a label, not a
#: sentence). The em-dash is between two ASCII spaces so ruff's
#: ambiguous-unicode lint does not flag it.
NO_MATCH_PROMPT: str = "no verb matches — Esc to cancel"


def _stage_glyph(app: App[None] | None) -> str:
    """Return the stage (running) lifecycle sigil for the app's render mode.

    The lifecycle section prefixes each verb with this mark so the
    operator reads those rows as the active stage-advancing moves. The
    render mode is read off the host App (defaulting to the unicode
    column under a bare harness with no resolved mode).

    Args:
        app: The host App, or ``None`` under a bare harness.

    Returns:
        The single-cell stage sigil glyph in the resolved render column.
    """
    mode = getattr(app, "render_mode", "unicode")
    return glyph(Sigil.RUNNING, mode=mode)


def _option_label(verb: PaletteVerb, *, stage_sigil: str = "") -> str:
    """Render a verb's palette row: ``[sigil] name  grammar -- hint``.

    Args:
        verb: The verb to label.
        stage_sigil: A leading stage sigil glyph for a lifecycle verb;
            empty for a non-lifecycle verb (no prefix).

    Returns:
        A single-line label combining the optional stage sigil, the verb
        name, its (optional) argument grammar, and the one-line hint.
    """
    head = verb.name if not verb.args_grammar else f"{verb.name} {verb.args_grammar}"
    body = f"{head}  —  {verb.hint}"
    return f"{stage_sigil} {body}" if stage_sigil else body


def group_verbs(
    verbs: list[PaletteVerb],
    recents: tuple[str, ...] = (),
) -> list[tuple[str, list[PaletteVerb]]]:
    """Partition *verbs* into the Recent / Lifecycle / All sections.

    The partition preserves the input order of *verbs* within each
    section so the palette's pre-filter display order stays stable. A
    verb appears in exactly one section: ``Recent`` claims it first (when
    its name is in *recents*), then ``Lifecycle`` (when its name is in
    :data:`LIFECYCLE_VERB_NAMES`), else ``All``. Empty sections are
    dropped so the rendered list carries no header without rows beneath
    it.

    Args:
        verbs: The visible, fuzzy-ranked verbs to partition.
        recents: Verb names the operator ran most recently, in
            most-recent-first order; a name not present in *verbs* is
            ignored.

    Returns:
        The non-empty ``(section_title, verbs)`` groups in display order:
        ``Recent`` (if any recents resolve), then ``Lifecycle``, then
        ``All``.
    """
    by_name = {verb.name: verb for verb in verbs}
    recent_names = [name for name in recents if name in by_name]
    recent_set = set(recent_names)

    recent = [by_name[name] for name in recent_names]
    lifecycle = [
        verb for verb in verbs if verb.name not in recent_set and verb.name in LIFECYCLE_VERB_NAMES
    ]
    rest = [
        verb
        for verb in verbs
        if verb.name not in recent_set and verb.name not in LIFECYCLE_VERB_NAMES
    ]

    groups: list[tuple[str, list[PaletteVerb]]] = []
    if recent:
        groups.append((SECTION_RECENT, recent))
    if lifecycle:
        groups.append((SECTION_LIFECYCLE, lifecycle))
    if rest:
        groups.append((SECTION_ALL, rest))
    return groups


class CommandPalette(ModalScreen[None]):
    """The ``/`` command palette overlay (fuzzy-filterable verb list).

    Opened by the shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen`
    ``open_palette`` action (and the ``/`` keypress that triggers it). The
    palette reads the App's resolved scope to decide which verbs to offer,
    re-ranks them on every keystroke, and runs the selected verb's handler
    on ``Enter``.
    """

    #: One palette at a time -- a re-fired ``/`` / ``open_palette`` over an
    #: already-open palette is a no-op (deduped by
    #: :meth:`~eawf.surfaces.tui.app.EaApp.push_modal`) rather than stacking a
    #: second identical palette.
    dedupe_singleton: ClassVar[bool] = True

    DEFAULT_CSS: ClassVar[str] = """
    CommandPalette {
        align: center top;
    }
    CommandPalette > #palette-box {
        width: 80%;
        max-width: 100;
        height: auto;
        margin-top: 2;
        border: solid $accent;
        background: $surface;
        padding: 0 1;
    }
    CommandPalette #palette-input {
        border: none;
        height: 1;
    }
    CommandPalette #palette-options {
        height: auto;
        max-height: 16;
        border: none;
    }
    """

    #: Palette-local bindings. ``Esc`` closes; ``Tab`` autocompletes the
    #: input to the highlighted option; arrows move the option cursor
    #: while the Input keeps focus (handled in ``on_key``).
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "close", "close", show=False),
        Binding("tab", "autocomplete", "autocomplete", show=False),
    ]

    #: How many recently-run verbs the ``Recent`` section surfaces. Small
    #: so the section stays a quick-access shortlist rather than a second
    #: full list.
    RECENT_CAP: ClassVar[int] = 5

    def __init__(self, recents: tuple[str, ...] = ()) -> None:
        """Construct the palette resolving the host App's scope on mount.

        Args:
            recents: Verb names the operator ran most recently, in
                most-recent-first order; surfaced under the ``Recent``
                section. Defaults to empty (no Recent section).
        """
        super().__init__()
        self._scope: ScopeName = "repo"
        self._recents: tuple[str, ...] = recents[: self.RECENT_CAP]

    def compose(self) -> ComposeResult:
        """Yield the input + option-list box."""
        with Vertical(id="palette-box"):
            yield Input(value=PALETTE_PREFIX, id="palette-input")
            yield OptionList(id="palette-options")

    def on_mount(self) -> None:
        """Resolve the active scope, seed the option list, focus the input.

        The cursor is parked at the end of the seeded ``/`` so the
        operator types the verb tail without first clearing the prefix.
        """
        self._scope = self._resolve_scope()
        self._refresh_options(PALETTE_PREFIX)
        palette_input = self.query_one("#palette-input", Input)
        palette_input.focus()
        palette_input.cursor_position = len(palette_input.value)

    def _resolve_scope(self) -> ScopeName:
        """Read the host App's resolved scope name (defaults to ``repo``).

        Returns:
            The App's ``_scope`` when it is a known scope name, else
            ``"repo"`` so the palette degrades gracefully under a bare
            test harness.
        """
        scope = getattr(self.app, "_scope", "repo")
        if scope in ("repo", "workspace", "user"):
            return scope  # type: ignore[return-value]
        return "repo"

    def current_verbs(self, query: str) -> list[PaletteVerb]:
        """Return the scope-visible verbs ranked by *query*.

        Args:
            query: The current palette input text (including the leading
                ``/``).

        Returns:
            The visible verbs for the active scope, fuzzy-ranked by
            *query* (best match first).
        """
        candidates = visible_verbs(self._scope)
        return rank_verbs(candidates, query)

    def _refresh_options(self, query: str) -> None:
        """Repopulate the option list, grouped Recent / Lifecycle / All.

        The ranked verbs for *query* are partitioned into the
        :func:`group_verbs` sections; each section renders a disabled
        header row, then its verb rows, separated by a rule. Lifecycle
        verbs carry the stage sigil prefix (resolved against the host
        App's render mode). A verb option's id is the verb name so
        ``Enter`` / ``Tab`` resolve the selection without re-ranking;
        header rows are disabled so they never take the highlight. When
        no verb matches a non-trivial query, the frozen
        :data:`NO_MATCH_PROMPT` renders as a single disabled row.
        Clearing then re-adding keeps the highlight on the first
        selectable (best) match.

        Args:
            query: The current palette input text.
        """
        option_list = self.query_one("#palette-options", OptionList)
        option_list.clear_options()
        verbs = self.current_verbs(query)
        if not verbs:
            if query.strip() not in ("", PALETTE_PREFIX):
                option_list.add_option(Option(NO_MATCH_PROMPT, disabled=True))
            return
        stage_sigil = _stage_glyph(self.app)
        groups = group_verbs(verbs, self._recents)
        for index, (section, section_verbs) in enumerate(groups):
            if index:
                option_list.add_option(None)
            option_list.add_option(Option(section, disabled=True))
            sigil = stage_sigil if section == SECTION_LIFECYCLE else ""
            for verb in section_verbs:
                option_list.add_option(Option(_option_label(verb, stage_sigil=sigil), id=verb.name))
        option_list.highlighted = self._first_selectable_index(option_list)

    @staticmethod
    def _first_selectable_index(option_list: OptionList) -> int | None:
        """Return the index of the first non-disabled option, or ``None``.

        Section headers (and the no-match prompt) are added as disabled
        options so they never take the highlight; this scans past them to
        the first real, runnable verb row.

        Args:
            option_list: The populated option list.

        Returns:
            The index of the first enabled option, or ``None`` when every
            option is disabled (all-headers / no-match).
        """
        for index in range(option_list.option_count):
            if not option_list.get_option_at_index(index).disabled:
                return index
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        """Re-rank the option list as the operator types.

        Args:
            event: The Textual input-changed event carrying the new value.
        """
        self._refresh_options(event.value)

    def _highlighted_verb_name(self) -> str | None:
        """Return the verb name of the highlighted option, or ``None``.

        Returns:
            The highlighted option's id (the verb name), or ``None`` when
            the list is empty / nothing is highlighted.
        """
        option_list = self.query_one("#palette-options", OptionList)
        index = option_list.highlighted
        if index is None:
            return None
        option = option_list.get_option_at_index(index)
        return option.id

    def action_autocomplete(self) -> None:
        """Complete the input to the highlighted verb name (``Tab``).

        Sets the input to ``"<verb> "`` (trailing space) so the operator
        types args directly, then re-ranks. A no-op when nothing is
        highlighted.
        """
        name = self._highlighted_verb_name()
        if name is None:
            return
        palette_input = self.query_one("#palette-input", Input)
        palette_input.value = f"{name} "
        palette_input.cursor_position = len(palette_input.value)
        self._refresh_options(palette_input.value)

    def on_key(self, event: object) -> None:
        """Route ``up`` / ``down`` to the option list while Input is focused.

        The Input widget would otherwise swallow the arrow keys for its
        own cursor; intercepting them here lets the operator move the
        option highlight without leaving the input. Other keys fall
        through to the bindings / Input.

        Args:
            event: The Textual key event (typed loosely so the bare-key
                attribute access stays import-light).
        """
        key = getattr(event, "key", None)
        if key not in ("up", "down"):
            return
        option_list = self.query_one("#palette-options", OptionList)
        if option_list.option_count == 0:
            return
        if key == "down":
            option_list.action_cursor_down()
        else:
            option_list.action_cursor_up()
        stop = getattr(event, "stop", None)
        if callable(stop):
            stop()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Run the resolved verb on ``Enter`` and dismiss the palette.

        An empty input (blank, or just the seeded ``/`` prefix) on
        ``Enter`` dismisses the palette — there is no verb to run and the
        operator is signalling "never mind", so closing is the least
        surprising response. Otherwise resolves ``(verb, args)`` from the
        current input via
        :func:`~eawf.surfaces.tui.palette.verbs.split_verb_args`, then matches
        the verb name against the visible registry for the active scope. A
        match dismisses the palette and runs the handler; an unknown verb
        toasts and leaves the palette open so the operator can correct it.

        Args:
            event: The Textual input-submitted event carrying the value.
        """
        if event.value.strip() in ("", PALETTE_PREFIX):
            logger.info("palette_submit_empty dismiss=1")
            self.dismiss(None)
            return
        verb_name, args = split_verb_args(event.value)
        verb = self._match_verb(verb_name)
        if verb is None:
            logger.info(f"palette_submit_unknown verb={verb_name!r}")
            self.app.notify(f"unknown verb: {verb_name}", severity="warning")
            return
        app = self.app
        _record_recent(app, verb.name)
        self.dismiss(None)
        logger.info(f"palette_run verb={verb.name!r} args={args!r}")
        verb.handler(app, args)

    def _match_verb(self, verb_name: str) -> PaletteVerb | None:
        """Resolve *verb_name* to a verb visible on the active scope.

        Args:
            verb_name: The verb name parsed from the input.

        Returns:
            The matching :class:`~eawf.surfaces.tui.palette.verbs.PaletteVerb`,
            or ``None`` when no visible verb has that exact name.
        """
        for verb in visible_verbs(self._scope):
            if verb.name == verb_name:
                return verb
        return None

    def action_close(self) -> None:
        """Dismiss the palette without executing (``Esc``)."""
        self.dismiss(None)


#: The host-App attribute the palette stashes its recently-run verb
#: names on, most-recent-first. App-local (never persisted to
#: ``state.json``): the Recent section is a within-session convenience,
#: so the list lives on the running App and resets on relaunch.
_RECENTS_ATTR: str = "_palette_recents"


def _read_recents(app: App[None]) -> tuple[str, ...]:
    """Return the host App's recently-run palette verb names.

    Args:
        app: The running App.

    Returns:
        The recorded verb names, most-recent-first; empty when none have
        run this session (or under a bare harness with no recents attr).
    """
    recents = getattr(app, _RECENTS_ATTR, ())
    return tuple(recents)


def _record_recent(app: App[None], verb_name: str) -> None:
    """Record *verb_name* as the most-recently-run palette verb on *app*.

    Moves *verb_name* to the front of the host App's recents list (de-
    duplicating an earlier run) so the next palette open surfaces it
    first under the ``Recent`` section. The list is capped at
    :data:`CommandPalette.RECENT_CAP`.

    Args:
        app: The running App.
        verb_name: The verb name just run.
    """
    prior = [name for name in _read_recents(app) if name != verb_name]
    updated = (verb_name, *prior)[: CommandPalette.RECENT_CAP]
    setattr(app, _RECENTS_ATTR, updated)


def open_palette(app: App[None]) -> None:
    """Push the command palette onto *app*'s screen stack (cap-checked).

    The shared :class:`~eawf.surfaces.tui.scopes.ScopeScreen` ``open_palette``
    action calls this. It seeds the palette with the host App's recently-
    run verbs (the ``Recent`` section) and routes through the App's
    modal-cap-aware ``push_modal`` helper when present (so the modal-stack
    depth limit is enforced), falling back to a plain ``push_screen`` under
    a bare harness that has no such helper.

    Args:
        app: The running App.
    """
    palette = CommandPalette(recents=_read_recents(app))
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        push_modal(palette)
        return
    app.push_screen(palette)


__all__ = [
    "LIFECYCLE_VERB_NAMES",
    "NO_MATCH_PROMPT",
    "PALETTE_PREFIX",
    "SECTION_ALL",
    "SECTION_LIFECYCLE",
    "SECTION_RECENT",
    "CommandPalette",
    "group_verbs",
    "open_palette",
]
