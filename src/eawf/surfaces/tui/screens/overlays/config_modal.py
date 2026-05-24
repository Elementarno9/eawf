"""``ConfigModal`` — the registry-driven tabbed config overlay (tui).

The Textual config window: a :class:`~textual.screen.ModalScreen` opened
by the global ``c`` keypress and the ``/config`` palette verb. It renders
**every** operator-tunable key from :data:`eawf.kernel.config.registry.CONFIG_REGISTRY`
in alphabetical tabs (alphabetical fields within each tab), so the surface
auto-covers any key added to the registry later — the registry is the
single source of truth shared with the ``eawf config`` CLI menu, so the
two cannot drift.

Navigation model (keymap conventions — arrows primary, full key names).
The field cursor sits on one row of the active tab; the field's *type*
(not a focus zone) decides what ``Enter`` does, so every key is
unambiguous regardless of which field is highlighted.

* ``↑`` / ``↓`` — move the field cursor up / down within the active tab,
  clamped to the first / last field (no wrap; ``↑`` on the first field
  and ``↓`` on the last are no-ops).
* ``←`` / ``→`` — switch the active tab (previous / next, wrapping). The
  cursor resets to the first field of the new tab.
* ``Enter`` — the **sole mutator**, dispatching on the field's type: a
  ``bool`` toggles in place; a ``choice`` forward-cycles its options
  (``a → b → c → a``); a ``str`` / ``int`` / ``float`` field edits
  **inline** — an :class:`~textual.widgets.Input` mounts in the row,
  ``Enter`` commits the validated value and ``Esc`` cancels. A ``str``
  field that already holds a newline (or whose value is wider than the
  row) routes to the popup
  :class:`~eawf.surfaces.tui.screens.overlays.edit_field.EditFieldModal`
  instead, which gives the operator more room. A ``multichoice`` field
  (e.g. ``ui.dashboard_panes``) expands into an inline ``[X]`` / ``[ ]``
  checklist — ``Space`` toggles the focused item, a second ``Enter``
  commits the staged list, and ``Esc`` cancels.
* ``s`` — save: flush every dirty field through the layered-config writer.
* ``r`` — reset: drop every staged (dirty) edit.
* ``L`` — cycle the writable layer the save targets.
* ``Esc`` — cancels an active inline edit; otherwise on a dirty modal it
  prompts before discarding (V15 dirty-guard) and on a clean modal it
  closes immediately.

**Save path (AGENTS rule 4).** Saving routes through the layered-config
writer — :func:`eawf.surfaces.cli.commands.config._save_value_to_layer`, the same
mutator :command:`eawf config set` uses, which proxies to the daemon. The
modal NEVER touches ``state.json``: it stages edits in an in-memory dirty
map and flushes them to the chosen writable YAML layer on ``s``.

The value logic (current-value resolution, bool toggle, enum cycle, save)
lives in pure module functions so it is unit-testable without mounting
Textual; the modal is a thin view that calls them and repaints. Those
helpers now live in the sibling
:mod:`eawf.surfaces.tui.screens.overlays.config_modal_logic`; this module
re-exports every public name so external importers keep resolving
``from eawf.surfaces.tui.screens.overlays.config_modal import current_value`` (and
friends) unchanged, and :class:`ConfigModal` / :func:`open_config` stay
importable at this dotted path.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TabbedContent, TabPane

from eawf.kernel.config.layered import get_dotted
from eawf.kernel.config.registry import (
    CONFIG_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)
from eawf.surfaces.cli.errors import UserError
from eawf.surfaces.tui.screens.overlays.config_modal_logic import (
    _KEY_COL_FLOOR,
    ConfigModalState,
    EnterAction,
    _values_equal,
    current_value,
    cycle_choice,
    enter_action,
    format_value,
    merged_config,
    needs_popup_edit,
    save_dirty_fields,
    toggle_bool,
    toggle_multichoice_item,
    writable_layers_for,
)
from eawf.surfaces.tui.screens.overlays.multichoice_checklist import MultichoiceChecklist

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)


class ConfigModal(ModalScreen[None]):
    """Registry-driven tabbed config window (Esc/dirty-guard to close).

    Renders every :data:`CONFIG_REGISTRY` key in alphabetical tabs
    (alphabetical fields per tab). The cursor sits on one field row:
    ``↑`` / ``↓`` move it within the tab (clamped to first / last),
    ``←`` / ``→`` switch tabs, and ``Enter`` is the **sole mutator** —
    it toggles a ``bool``, forward-cycles a ``choice``, edits a scalar
    (``int`` / ``float`` / ``str``) inline (a long / multi-line ``str``
    routes to the popup :class:`EditFieldModal`), or expands a
    ``multichoice`` into an inline ``[X]`` / ``[ ]`` checklist (``Space``
    toggles, a second ``Enter`` commits). ``s`` saves through the layered
    writer, ``r`` resets staged edits, ``L`` cycles the writable layer,
    and ``Esc`` cancels an inline edit or else closes (prompting first
    when there are staged edits).
    """

    DEFAULT_CSS: ClassVar[str] = """
    ConfigModal {
        align: center middle;
    }
    ConfigModal > #config-box {
        width: 90%;
        height: 85%;
        border: solid $accent;
        background: $surface;
        padding: 1 2;
    }
    ConfigModal .config-title {
        text-style: bold;
        color: $accent;
        height: 1;
    }
    ConfigModal .config-layer {
        color: $text-muted;
        height: 1;
        margin-bottom: 1;
    }
    ConfigModal .config-field {
        height: 1;
        color: $text;
    }
    ConfigModal .config-field.-selected {
        text-style: bold reverse;
        color: $accent;
    }
    ConfigModal .config-field.-dirty {
        color: $warning;
    }
    ConfigModal .config-edit-row {
        height: 1;
    }
    ConfigModal .config-edit-label {
        width: auto;
        height: 1;
    }
    ConfigModal .config-edit-row #config-inline-input {
        width: 1fr;
        height: 1;
        border: none;
        padding: 0;
        background: $surface;
        color: $accent;
    }
    ConfigModal .config-edit-error {
        color: $error;
        height: 1;
    }
    ConfigModal .config-hint {
        dock: bottom;
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: Keymap (arrows primary; full key names in the footer hint).
    #: ``up`` / ``down`` move the field cursor within the active tab
    #: (clamped), ``left`` / ``right`` switch tabs, ``enter`` mutates the
    #: focused field (toggle / cycle / inline-edit), ``s`` saves, ``r``
    #: resets, ``L`` cycles the layer, and ``escape`` cancels an inline
    #: edit or closes (dirty-guarded). ``j`` / ``k`` ride ``down`` / ``up``
    #: as vim aliases.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "cursor_up", "up", show=False),
        Binding("down", "cursor_down", "down", show=False),
        Binding("k", "cursor_up", "up", show=False),
        Binding("j", "cursor_down", "down", show=False),
        Binding("left", "switch_tab(-1)", "prev tab", show=False),
        Binding("right", "switch_tab(1)", "next tab", show=False),
        Binding("enter", "edit", "edit", show=False),
        Binding("s", "save", "save", show=False),
        Binding("r", "reset", "reset", show=False),
        Binding("L", "cycle_layer", "layer", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the highlighted field inside the active tab — the field
    #: cursor. Opens on field 0 (immediately actionable); ``↑`` / ``↓``
    #: clamp it to the first / last field of the active tab.
    field_index: reactive[int] = reactive(0)

    def __init__(
        self,
        *,
        workspace: Path | None = None,
        repo: Path | None = None,
        save_fn: Callable[..., None] | None = None,
    ) -> None:
        """Construct the config modal for the given anchors.

        Args:
            workspace: Workspace root for the layered merge / writer
                (or ``None``).
            repo: Repo root for the layered merge / writer (or ``None``).
            save_fn: Test seam for the layered writer — defaults to the
                CLI ``_save_value_to_layer`` helper.
        """
        super().__init__()
        self._workspace = workspace
        self._repo = repo
        self._save_fn = save_fn
        self._tabs: tuple[str, ...] = tabs_sorted()
        self._layers: tuple[str, ...] = writable_layers_for(workspace, repo)
        default_layer = "repo" if "repo" in self._layers else self._layers[0]
        self._view = ConfigModalState(layer=default_layer)
        self._merged: dict[str, Any] = merged_config(workspace, repo)
        #: The field key whose row currently hosts an inline ``Input`` /
        #: checklist, or ``None`` when no inline edit is open. Only one row
        #: edits at a time; while set, ``↑`` / ``↓`` / ``←`` / ``→`` are
        #: inert and the editor owns ``Enter`` (commit) / ``Esc`` (cancel).
        self._editing_key: str | None = None
        #: Snapshot of the dirty map taken when a ``multichoice`` checklist
        #: opens. ``Space`` toggles stage live into the dirty map, so a
        #: cancel restores this snapshot rather than blindly dropping the key.
        self._multichoice_dirty_snapshot: dict[str, Any] | None = None

    # -- composition --------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Yield the titled card, the per-tab field panes, and the keymap hint."""
        with Vertical(id="config-box"):
            yield Static("Configuration", classes="config-title")
            yield Static(self._layer_line(), classes="config-layer", id="config-layer")
            with TabbedContent(id="config-tabs"):
                for tab in self._tabs:
                    with TabPane(tab, id=self._tab_pane_id(tab)), VerticalScroll():
                        for index, entry in enumerate(keys_for_tab(tab)):
                            # markup=False — the ``[bool]`` / ``[int]`` type
                            # cells are literal text, not Rich tags.
                            yield Static(
                                self._field_line(entry),
                                classes="config-field",
                                id=self._field_row_id(tab, index),
                                markup=False,
                            )
            yield Static(self._hint_line(), classes="config-hint", id="config-hint")

    def on_mount(self) -> None:
        """Paint the initial highlight; keep key focus on the screen.

        :class:`TabbedContent` auto-focuses its internal tab bar, whose
        own ``←`` / ``→`` bindings would otherwise steal the arrows the
        modal needs for its own tab switching. Clearing focus routes every
        keystroke to this screen's bindings so the modal owns the full
        interaction model (it drives tab switching via
        :meth:`action_switch_tab`). The exception is while an inline edit
        is open: focus then belongs to the mounted :class:`Input`.
        """
        self.set_focus(None)
        self._repaint_fields()

    def on_tabbed_content_tab_activated(self, event: TabbedContent.TabActivated) -> None:
        """Repaint rows whenever the active tab changes.

        Fires for both the keyboard tab switch (:meth:`action_switch_tab`)
        and a programmatic ``tabs.active = ...`` assignment, so the cursor
        caret / ``-selected`` style always tracks the active tab's field
        list rather than going stale on the previously-active pane.
        """
        event.stop()
        if self.is_mounted:
            self._repaint_fields()

    # -- id helpers (stable widget ids) ------------------------------------

    @staticmethod
    def _tab_pane_id(tab: str) -> str:
        """Return the stable :class:`TabPane` id for *tab*."""
        return f"config-tab-{tab}"

    @staticmethod
    def _field_row_id(tab: str, index: int) -> str:
        """Return the stable field-row widget id for *tab* row *index*."""
        return f"config-field-{tab}-{index}"

    # -- rendering ----------------------------------------------------------

    def _layer_line(self) -> str:
        """Render the writable-layer indicator line above the tabs."""
        others = " ".join(layer for layer in self._layers if layer != self._view.layer)
        suffix = f"  (L cycles: {others})" if others else ""
        dirty_count = len(self._view.dirty)
        dirty_note = f"  ·  {dirty_count} unsaved" if dirty_count else ""
        return f"save layer: {self._view.layer}{suffix}{dirty_note}"

    def _key_col_width(self) -> int:
        """Return the key-column width for the active tab.

        Sized to the longest key actually rendered in the active tab so a
        long key (e.g. the ``estimation.display.time_quantum_*`` family,
        well over the legacy 42-cell column) no longer overflows and pushes
        the ``[type]`` tag + value column right on its own row. Never narrows
        below :data:`_KEY_COL_FLOOR` so short-key tabs keep a stable column.
        Both :meth:`_field_line` and :meth:`_meta_line` consume this width so
        the static row and the inline-edit meta line align identically.

        Returns:
            The key-column width in cells (at least :data:`_KEY_COL_FLOOR`).
        """
        widest = max((len(entry.key) for entry in self._active_fields()), default=0)
        return max(_KEY_COL_FLOOR, widest)

    def _field_line(self, entry: ConfigKey, *, selected: bool = False) -> str:
        """Render one field row: focus caret + dirty marker + key + type + value.

        Args:
            entry: The registry entry the row renders.
            selected: ``True`` when this row is the focused field — adds a
                ``>`` caret so the cursor is visible even in a plain-text
                capture (the CSS ``-selected`` style adds the colour on a
                live terminal).
        """
        value = current_value(entry, self._merged, self._view.dirty)
        caret = ">" if selected else " "
        dirty_mark = "*" if entry.key in self._view.dirty else " "
        type_cell = f"[{entry.type}]"
        key_width = self._key_col_width()
        return (
            f"{caret}{dirty_mark} {entry.key:<{key_width}} "
            f"{type_cell:<14} {format_value(entry, value)}"
        )

    def _hint_line(self) -> str:
        """Render the footer keymap hint.

        The hint reflects the type-dispatched keymap: arrows navigate
        (``↑`` / ``↓`` fields, ``←`` / ``→`` tabs) and ``Enter`` is the
        sole mutator. While an inline edit is open the hint flips to the
        commit / cancel keys the mounted :class:`Input` owns; while the
        ``multichoice`` checklist is open it flips to the Space-toggle /
        Enter-commit / Esc-cancel keys the checklist owns.
        """
        if self._editing_key is not None:
            entry = registry_lookup(self._editing_key)
            if entry is not None and entry.type == "multichoice":
                return "[ ↑/↓ item · Space toggle · Enter commit · Esc cancel ]"
            range_hint = self._range_hint(entry) if entry is not None else ""
            return f"[ {range_hint}Enter commit · Esc cancel ]"
        return "[ ↑/↓ field · ←/→ tab · Enter edit · s save · r reset · L layer · Esc close ]"

    # -- active-context resolution -----------------------------------------

    def _active_tab(self) -> str:
        """Return the currently-active tab name (the focused :class:`TabPane`)."""
        try:
            tabs = self.query_one("#config-tabs", TabbedContent)
        except Exception:  # pragma: no cover - pre-mount guard
            return self._tabs[0] if self._tabs else ""
        active = tabs.active
        for tab in self._tabs:
            if self._tab_pane_id(tab) == active:
                return tab
        return self._tabs[0] if self._tabs else ""

    def _active_fields(self) -> tuple[ConfigKey, ...]:
        """Return the field list of the active tab (alphabetical)."""
        tab = self._active_tab()
        return keys_for_tab(tab) if tab else ()

    def _active_field(self) -> ConfigKey | None:
        """Return the field under the cursor, or ``None`` for an empty tab."""
        fields = self._active_fields()
        if not fields:
            return None
        index = max(0, min(self.field_index, len(fields) - 1))
        return fields[index]

    def _persisted_value(self, entry: ConfigKey) -> Any:
        """Resolve *entry*'s persisted value — merged config, then default.

        The baseline a staged edit is compared against (ignoring the dirty
        map, unlike :func:`current_value`) to decide whether an edit is a
        real change.

        Args:
            entry: The field to resolve the persisted value for.

        Returns:
            The merged-config value, or the registry default when the key is
            absent from every layer.
        """
        try:
            return get_dotted(self._merged, entry.key)
        except KeyError:
            return entry.default

    def _drop_if_unchanged(self, entry: ConfigKey, dirty: dict[str, Any]) -> dict[str, Any]:
        """Return *dirty* without *entry*'s key when its value matches persisted.

        An edit that nets no change (the operator typed the current value, or
        cycled / toggled all the way back to it) leaves no dirty mark — no
        ``*`` and no ``-dirty`` tint — so the unsaved indicator only flags
        genuine pending changes.

        Args:
            entry: The field whose staged value to reconcile.
            dirty: The candidate dirty map (already carrying the new value).

        Returns:
            *dirty* unchanged, or a copy with *entry*'s key removed when its
            staged value equals the persisted value.
        """
        if entry.key in dirty and _values_equal(dirty[entry.key], self._persisted_value(entry)):
            return {key: value for key, value in dirty.items() if key != entry.key}
        return dirty

    def _repaint_fields(self) -> None:
        """Repaint the active tab's field rows, layer line, and footer hint.

        The cursor field carries the ``>`` caret + ``-selected`` style; a
        dirty field carries ``-dirty``. A row currently hosting an inline
        :class:`Input` is skipped — that row was swapped out of the static
        layout in :meth:`_begin_inline_edit` and is restored on commit /
        cancel.
        """
        tab = self._active_tab()
        if not tab:
            return
        fields = keys_for_tab(tab)
        for index, entry in enumerate(fields):
            if entry.key == self._editing_key:
                continue
            try:
                row = self.query_one(f"#{self._field_row_id(tab, index)}", Static)
            except Exception:  # pragma: no cover - mid-mount / editing guard
                continue
            selected = index == self.field_index
            row.update(self._field_line(entry, selected=selected))
            row.set_class(selected, "-selected")
            row.set_class(entry.key in self._view.dirty, "-dirty")
        self.query_one("#config-layer", Static).update(self._layer_line())
        self._repaint_hint()

    def _repaint_hint(self) -> None:
        """Repaint the footer hint (navigation vs inline-edit form)."""
        try:
            self.query_one("#config-hint", Static).update(self._hint_line())
        except Exception:  # pragma: no cover - mid-mount guard
            return

    def watch_field_index(self) -> None:
        """Repaint the field rows when the cursor moves."""
        if self.is_mounted:
            self._repaint_fields()

    # -- actions ------------------------------------------------------------

    def action_cursor_up(self) -> None:
        """Move the field cursor up (``↑``), clamped to the first field.

        Inert while an inline edit is open (the mounted :class:`Input`
        owns the keyboard). On the first field it is a no-op — the cursor
        does not wrap, the least-surprising default.
        """
        if self._editing_key is not None:
            return
        if self.field_index > 0:
            self.field_index -= 1

    def action_cursor_down(self) -> None:
        """Move the field cursor down (``↓``), clamped to the last field.

        Inert while an inline edit is open. On the last field it is a
        no-op (no wrap), mirroring :meth:`action_cursor_up`.
        """
        if self._editing_key is not None:
            return
        fields = self._active_fields()
        if not fields:
            return
        self.field_index = min(len(fields) - 1, self.field_index + 1)

    def action_switch_tab(self, step: int) -> None:
        """Switch the active tab by *step* (wrapping), resetting the cursor.

        Bound to ``←`` (``step=-1``) / ``→`` (``step=+1``). Inert while an
        inline edit is open so the keystroke reaches the :class:`Input`.

        Args:
            step: ``-1`` for the previous tab, ``+1`` for the next.
        """
        if self._editing_key is not None:
            return
        if not self._tabs:
            return
        tab = self._active_tab()
        try:
            index = self._tabs.index(tab)
        except ValueError:
            index = 0
        next_tab = self._tabs[(index + step) % len(self._tabs)]
        # Rewind the field cursor so the new pane opens on field 0; the
        # TabActivated handler repaints the rows for the new pane.
        self.field_index = 0
        self.query_one("#config-tabs", TabbedContent).active = self._tab_pane_id(next_tab)
        # Activating a tab re-focuses the tab bar widget; clear it so the
        # modal's own arrow bindings keep winning.
        self.set_focus(None)
        self._repaint_fields()

    def action_edit(self) -> None:
        """Mutate the focused field (``Enter`` — the sole mutator).

        Dispatches on the field type via :func:`enter_action`: a ``bool``
        toggles, a ``choice`` forward-cycles (``a → b → c → a``), a scalar
        (``int`` / ``float`` / ``str``) edits inline — a multi-line /
        over-wide ``str`` routes to the popup :class:`EditFieldModal` — and
        a ``multichoice`` expands into the inline ``[X]`` / ``[ ]`` checklist
        editor (:meth:`_begin_multichoice_edit`). Inert while an inline edit
        is already open and a no-op on an empty tab.
        """
        if self._editing_key is not None:
            return
        entry = self._active_field()
        if entry is None:
            return
        value = current_value(entry, self._merged, self._view.dirty)
        action = enter_action(entry, value, row_width=self._row_width())
        if action == "toggle":
            staged = toggle_bool(entry, self._merged, self._view.dirty)
            self._view.dirty = self._drop_if_unchanged(entry, staged)
            self._repaint_fields()
        elif action == "cycle":
            staged = cycle_choice(entry, self._merged, self._view.dirty, step=1)
            self._view.dirty = self._drop_if_unchanged(entry, staged)
            self._repaint_fields()
        elif action == "inline":
            self._begin_inline_edit(entry, value)
        elif action == "popup":
            self._open_popup_edit(entry, value)
        elif action == "multichoice":
            self._begin_multichoice_edit(entry, value)

    def _row_width(self) -> int:
        """Return the content width of a field row (inline-input budget).

        Falls back to ``0`` (disables the over-wide-``str`` popup check)
        when the row width cannot be resolved pre-layout, so a ``str``
        still edits inline unless it is multi-line.
        """
        entry = self._active_field()
        if entry is None:
            return 0
        try:
            tab = self._active_tab()
            row = self.query_one(f"#{self._field_row_id(tab, self.field_index)}", Static)
        except Exception:  # pragma: no cover - pre-layout guard
            return 0
        return int(row.content_size.width)

    def _open_popup_edit(self, entry: ConfigKey, value: Any) -> None:
        """Push the popup :class:`EditFieldModal` for a multi-line ``str``.

        Args:
            entry: The field to edit.
            value: The field's currently-resolved value (seeds the editor).
        """
        from eawf.surfaces.tui.screens.overlays.edit_field import EditFieldModal

        self.app.push_screen(EditFieldModal(entry, value), self._make_edit_callback(entry))

    def _begin_inline_edit(self, entry: ConfigKey, value: Any) -> None:
        """Swap the focused row for an inline :class:`Input` editor.

        Mounts an :class:`Input` (seeded from *value*) plus an empty error
        row in place of the static field row, then focuses the input.
        ``Enter`` validates + commits via :meth:`on_input_submitted`;
        ``Esc`` cancels via :meth:`_cancel_inline_edit`. Only one row edits
        at a time.

        Args:
            entry: The field being edited.
            value: The field's currently-resolved value (seeds the buffer).
        """
        tab = self._active_tab()
        try:
            row = self.query_one(f"#{self._field_row_id(tab, self.field_index)}", Static)
        except Exception:  # pragma: no cover - pre-layout guard
            return
        self._editing_key = entry.key
        seed = "" if value is None else str(value)
        edit_row = Horizontal(
            Static(self._meta_line(entry), classes="config-edit-label", markup=False),
            Input(value=seed, id="config-inline-input"),
            classes="config-edit-row",
            id="config-edit-row",
        )
        error_row = Static("", classes="config-edit-error", id="config-edit-error", markup=False)
        row.display = False
        self.mount(edit_row, after=row)
        self.mount(error_row, after=edit_row)
        # The mount is processed on the next message-pump cycle, so the
        # Input is not queryable yet — focus it once the refresh lands.
        self.call_after_refresh(self._focus_inline_input)
        self._repaint_hint()

    def _focus_inline_input(self) -> None:
        """Focus the mounted inline :class:`Input` (deferred post-mount)."""
        try:
            self.query_one("#config-inline-input", Input).focus()
        except Exception:  # pragma: no cover - edit torn down before refresh
            return

    def _meta_line(self, entry: ConfigKey) -> str:
        """Render the key + type label shown beside the inline input.

        Reuses the static :meth:`_field_line` column widths exactly — a
        three-cell prefix (caret + dirty marker + separator), then the key
        padded to :meth:`_key_col_width` and the type cell padded to 14 — so
        the trailing inline :class:`Input` lands in the **same column as the
        static value cell**. Without the shared widths the meta line was
        shorter than the static row and the whole inline editor bunched
        against the key (the "row squished after Enter" report). The range
        hint moves to the footer (:meth:`_range_hint`) so it does not push
        the input out of column.

        Args:
            entry: The field being edited.
        """
        type_cell = f"[{entry.type}]"
        key_width = self._key_col_width()
        return f"   {entry.key:<{key_width}} {type_cell:<14} "

    @staticmethod
    def _range_hint(entry: ConfigKey) -> str:
        """Return a ``range LOW..HIGH · `` footer prefix for a bounded numeric field.

        Args:
            entry: The field being edited.

        Returns:
            The range affordance with a trailing separator, or ``""`` when
            the field declares neither bound.
        """
        if entry.min_value is None and entry.max_value is None:
            return ""
        low = "" if entry.min_value is None else f"{entry.min_value:g}"
        high = "" if entry.max_value is None else f"{entry.max_value:g}"
        return f"range {low}..{high} · "

    def on_input_submitted(self, message: Input.Submitted) -> None:
        """Commit the inline edit on ``Enter`` (the Input's ``Submitted``).

        Validates the buffer through :func:`coerce_and_validate`; on
        success folds the typed value into the dirty map and tears the
        inline editor down, on failure renders the error inline and keeps
        the editor open so the operator can correct the value.
        """
        if self._editing_key is None:
            return
        message.stop()
        entry = registry_lookup(self._editing_key)
        if entry is None:  # pragma: no cover - editing key always resolves
            self._teardown_inline_edit()
            return
        raw = message.value
        try:
            coerced = coerce_and_validate(entry, raw)
        except UserError as exc:
            self._report_inline_error(str(exc))
            return
        self._view.dirty = self._drop_if_unchanged(entry, {**self._view.dirty, entry.key: coerced})
        logger.info(f"config_modal inline_commit key={entry.key!r} type={entry.type}")
        self._teardown_inline_edit()

    def _cancel_inline_edit(self) -> None:
        """Abort the inline edit (``Esc``) without mutating the dirty map."""
        if self._editing_key is None:
            return
        logger.info(f"config_modal inline_cancel key={self._editing_key!r}")
        self._teardown_inline_edit()

    def _report_inline_error(self, message: str) -> None:
        """Render *message* in the inline error row below the edited row.

        Args:
            message: The validation error to surface.
        """
        try:
            self.query_one("#config-edit-error", Static).update(message)
        except Exception:  # pragma: no cover - editor teardown race
            return

    def _teardown_inline_edit(self) -> None:
        """Remove the inline editor widgets and restore the static row."""
        for widget_id in ("#config-edit-row", "#config-edit-error"):
            try:
                self.query_one(widget_id).remove()
            except Exception:  # pragma: no cover - already removed
                continue
        tab = self._active_tab()
        try:
            row = self.query_one(f"#{self._field_row_id(tab, self.field_index)}", Static)
            row.display = True
        except Exception:  # pragma: no cover - tab switched mid-edit
            pass
        self._editing_key = None
        self.set_focus(None)
        self._repaint_fields()

    def _make_edit_callback(self, entry: ConfigKey) -> Callable[[Any], None]:
        """Build the dismiss callback that folds a popup-edited value into dirty.

        Args:
            entry: The field that was edited.

        Returns:
            A callback taking the :class:`EditFieldModal` dismiss value;
            it folds a non-``None`` (accepted) value into the dirty map.
        """

        def _on_dismiss(result: Any) -> None:
            if result is None:
                return
            self._view.dirty = self._drop_if_unchanged(
                entry, {**self._view.dirty, entry.key: result}
            )
            self._repaint_fields()

        return _on_dismiss

    # -- multichoice inline checklist --------------------------------------

    def _begin_multichoice_edit(self, entry: ConfigKey, value: Any) -> None:
        """Swap the focused row for the inline ``[X]`` / ``[ ]`` checklist.

        Mounts a :class:`MultichoiceChecklist` (seeded ``[X]`` for the
        items in *value*) in place of the static field row, hides the row,
        and focuses the checklist. ``Space`` toggles a line, ``Enter``
        commits via :meth:`on_multichoice_checklist_committed`, and ``Esc``
        cancels via :meth:`on_multichoice_checklist_cancelled`. Only one row
        edits at a time. A no-op when the key declares no choices.

        Args:
            entry: The multichoice field being edited.
            value: The field's currently-resolved value (seeds the marks).
        """
        if not entry.choices:
            return
        tab = self._active_tab()
        try:
            row = self.query_one(f"#{self._field_row_id(tab, self.field_index)}", Static)
        except Exception:  # pragma: no cover - pre-layout guard
            return
        self._editing_key = entry.key
        self._multichoice_dirty_snapshot = dict(self._view.dirty)
        selected = [str(item) for item in value] if isinstance(value, (list, tuple)) else []
        checklist = MultichoiceChecklist(
            choices=entry.choices,
            selected=selected,
            prefix=self._meta_line(entry),
            id="config-multichoice",
        )
        error_row = Static("", classes="config-edit-error", id="config-edit-error", markup=False)
        row.display = False
        self.mount(checklist, after=row)
        self.mount(error_row, after=checklist)
        self._repaint_hint()

    def on_multichoice_checklist_toggled(self, message: MultichoiceChecklist.Toggled) -> None:
        """Stage the new selection as the checklist toggles a line (``Space``).

        Keeps the dirty map live with each ``Space`` so the field's ``*``
        marker (and the saved value) tracks the in-flight selection; the
        commit path re-validates the final list before teardown.
        """
        message.stop()
        if self._editing_key is None:
            return
        entry = registry_lookup(self._editing_key)
        if entry is None:  # pragma: no cover - editing key always resolves
            return
        staged = toggle_multichoice_item(entry, self._merged, self._view.dirty, item=message.item)
        self._view.dirty = self._drop_if_unchanged(entry, staged)

    def on_multichoice_checklist_committed(self, message: MultichoiceChecklist.Committed) -> None:
        """Validate + stage the selected list on ``Enter``, then tear down.

        Routes the selected list through :func:`coerce_and_validate`; on
        success folds the coerced list into the dirty map and tears the
        checklist down, on failure renders the error inline (reusing the
        inline-edit error row) and keeps the editor open.
        """
        message.stop()
        if self._editing_key is None:
            return
        entry = registry_lookup(self._editing_key)
        if entry is None:  # pragma: no cover - editing key always resolves
            self._teardown_multichoice_edit()
            return
        try:
            coerced = coerce_and_validate(entry, message.selected)
        except UserError as exc:
            self._report_inline_error(str(exc))
            return
        self._view.dirty = self._drop_if_unchanged(entry, {**self._view.dirty, entry.key: coerced})
        logger.info(f"config_modal multichoice_commit key={entry.key!r} count={len(coerced)}")
        self._teardown_multichoice_edit()

    def on_multichoice_checklist_cancelled(self, message: MultichoiceChecklist.Cancelled) -> None:
        """Abort the checklist edit on ``Esc`` without staging."""
        message.stop()
        self._cancel_multichoice_edit()

    def _cancel_multichoice_edit(self) -> None:
        """Abort the checklist edit, restoring the pre-edit dirty map.

        ``Space`` toggles stage live, so a cancel must undo them by
        restoring the dirty snapshot captured when the editor opened, then
        tearing the editor down.
        """
        if self._editing_key is None:
            return
        logger.info(f"config_modal multichoice_cancel key={self._editing_key!r}")
        if self._multichoice_dirty_snapshot is not None:
            self._view.dirty = dict(self._multichoice_dirty_snapshot)
        self._teardown_multichoice_edit()

    def _teardown_multichoice_edit(self) -> None:
        """Remove the checklist widgets and restore the static row."""
        for widget_id in ("#config-multichoice", "#config-edit-error"):
            try:
                self.query_one(widget_id).remove()
            except Exception:  # pragma: no cover - already removed
                continue
        tab = self._active_tab()
        try:
            row = self.query_one(f"#{self._field_row_id(tab, self.field_index)}", Static)
            row.display = True
        except Exception:  # pragma: no cover - tab switched mid-edit
            pass
        self._editing_key = None
        self._multichoice_dirty_snapshot = None
        self.set_focus(None)
        self._repaint_fields()

    def action_save(self) -> None:
        """Flush staged edits through the layered writer (``s``).

        Inert while an edit is open so a stray ``s`` bubbling up from the
        focused checklist does not flush mid-edit.
        """
        if self._editing_key is not None:
            return
        if not self._view.dirty:
            self.app.notify("no changes to save", severity="information")
            return
        try:
            saved = save_dirty_fields(
                self._view.dirty,
                layer=self._view.layer,
                workspace=self._workspace,
                repo=self._repo,
                save_fn=self._save_fn,
            )
        except (ValueError, OSError) as exc:
            self.app.notify(f"save failed: {exc}", severity="error")
            return
        self._view.dirty = {}
        self._merged = merged_config(self._workspace, self._repo)
        self._repaint_fields()
        plural = "" if saved == 1 else "s"
        self.app.notify(f"saved {saved} key{plural} to {self._view.layer}", severity="information")

    def action_reset(self) -> None:
        """Drop every staged edit (``r``).

        Inert while an edit is open so a stray ``r`` bubbling up from the
        focused checklist does not wipe the staged edits mid-edit.
        """
        if self._editing_key is not None:
            return
        if not self._view.dirty:
            return
        self._view.dirty = {}
        self._repaint_fields()
        self.app.notify("staged edits reset", severity="information")

    def action_cycle_layer(self) -> None:
        """Cycle the writable layer the save targets (``L``).

        Inert while an edit is open so a stray ``L`` bubbling up from the
        focused checklist does not cycle the layer mid-edit.
        """
        if self._editing_key is not None:
            return
        if len(self._layers) <= 1:
            return
        index = self._layers.index(self._view.layer)
        self._view.layer = self._layers[(index + 1) % len(self._layers)]
        self.query_one("#config-layer", Static).update(self._layer_line())

    def action_close(self) -> None:
        """Handle ``Esc``: cancel an inline edit, else close (dirty-guarded).

        While an inline edit (scalar ``Input`` or ``multichoice`` checklist)
        is open ``Esc`` aborts just that edit (the modal stays open).
        Otherwise on a clean modal this dismisses immediately, and on a
        dirty modal it pushes a
        :class:`~eawf.surfaces.tui.screens.overlays.confirm.ConfirmModal` (V15
        dirty-guard), dismissing only when the operator confirms
        discarding the staged edits.
        """
        if self._editing_key is not None:
            if self._multichoice_dirty_snapshot is not None:
                self._cancel_multichoice_edit()
            else:
                self._cancel_inline_edit()
            return
        if not self._view.dirty:
            self.dismiss(None)
            return
        from eawf.surfaces.tui.screens.overlays.confirm import ConfirmModal

        count = len(self._view.dirty)
        plural = "" if count == 1 else "s"
        self.app.push_screen(
            ConfirmModal(f"Discard {count} unsaved config change{plural}?"),
            self._on_discard_confirmed,
        )

    def _on_discard_confirmed(self, discard: bool | None) -> None:
        """Dismiss the modal when the dirty-guard confirm returns ``True``.

        Args:
            discard: ``True`` when the operator confirmed discarding;
                ``False`` / ``None`` keeps the modal open with edits intact.
        """
        if discard:
            self.dismiss(None)


def open_config(app: App[None]) -> bool:
    """Push a :class:`ConfigModal` onto *app* (modal-cap-aware).

    Resolves the workspace / repo anchors from the App's bound state path
    (the modal reads / writes layered config relative to the repo root),
    then routes through the App's ``push_modal`` helper so the modal-stack
    depth cap is enforced in one place; falls back to a plain
    ``push_screen`` under a bare harness that lacks the cap helper.

    Args:
        app: The running App.

    Returns:
        ``True`` when the modal was pushed, ``False`` when the cap
        rejected it.
    """
    repo = _resolve_repo(app)
    modal = ConfigModal(workspace=None, repo=repo)
    push_modal = getattr(app, "push_modal", None)
    if callable(push_modal):
        return bool(push_modal(modal))
    app.push_screen(modal)
    return True


def _resolve_repo(app: App[None]) -> Path | None:
    """Resolve the repo root from the App's bound ``state.json`` path.

    The ``state.json`` lives at ``<repo>/.ea/state.json``, so the repo
    root is the parent of the ``.ea`` directory. Falls back to ``None``
    (global-layer-only) when the App exposes no state path.

    Args:
        app: The running App.

    Returns:
        The repo root, or ``None`` when it cannot be resolved.
    """
    state_path = getattr(app, "_state_path", None)
    if state_path is None:
        return None
    path = Path(state_path)
    if path.parent.name == ".ea":
        return path.parent.parent
    return path.parent


__all__ = [
    "CONFIG_REGISTRY",
    "ConfigModal",
    "ConfigModalState",
    "EnterAction",
    "current_value",
    "cycle_choice",
    "enter_action",
    "format_value",
    "merged_config",
    "needs_popup_edit",
    "open_config",
    "save_dirty_fields",
    "toggle_bool",
    "toggle_multichoice_item",
    "writable_layers_for",
]
