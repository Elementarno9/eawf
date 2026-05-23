"""``ConfigModal`` — the registry-driven tabbed config overlay (tui).

The Textual config window: a :class:`~textual.screen.ModalScreen` opened
by the global ``c`` keypress and the ``/config`` palette verb. It renders
**every** operator-tunable key from :data:`eawf.config.registry.CONFIG_REGISTRY`
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
  :class:`~eawf.tui.screens.overlays.edit_field.EditFieldModal`
  instead, which gives the operator more room.
* ``s`` — save: flush every dirty field through the layered-config writer.
* ``r`` — reset: drop every staged (dirty) edit.
* ``L`` — cycle the writable layer the save targets.
* ``Esc`` — cancels an active inline edit; otherwise on a dirty modal it
  prompts before discarding (V15 dirty-guard) and on a clean modal it
  closes immediately.

**Save path (AGENTS rule 4).** Saving routes through the layered-config
writer — :func:`eawf.cli.commands.config._save_value_to_layer`, the same
mutator :command:`eawf config set` uses, which proxies to the daemon. The
modal NEVER touches ``state.json``: it stages edits in an in-memory dirty
map and flushes them to the chosen writable YAML layer on ``s``.

The value logic (current-value resolution, bool toggle, enum cycle, save)
lives in pure module functions so it is unit-testable without mounting
Textual; the modal is a thin view that calls them and repaints.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Input, Static, TabbedContent, TabPane

from eawf.cli.errors import UserError
from eawf.config.defaults import built_in_defaults
from eawf.config.layered import (
    WRITABLE_LAYERS,
    get_dotted,
    layer_path,
    merge_config,
)
from eawf.config.registry import (
    CONFIG_REGISTRY,
    ConfigKey,
    coerce_and_validate,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)

if TYPE_CHECKING:
    from textual.app import App

logger = logging.getLogger(__name__)

#: The action ``Enter`` performs on a field, resolved from the field's
#: declared type (and, for ``str``, the current value's shape):
#:
#: * ``"toggle"`` — flip a ``bool`` in place.
#: * ``"cycle"`` — forward-cycle a ``choice`` through its options.
#: * ``"inline"`` — edit a scalar (``int`` / ``float`` / short ``str``)
#:   via an :class:`~textual.widgets.Input` mounted in the row.
#: * ``"popup"`` — edit via the larger
#:   :class:`~eawf.tui.screens.overlays.edit_field.EditFieldModal`
#:   (a ``str`` that is multi-line or wider than the row).
#: * ``"none"`` — no edit affordance (e.g. the dead ``multichoice``
#:   scaffold, which has zero registry entries).
EnterAction = Literal["toggle", "cycle", "inline", "popup", "none"]

#: Field types edited via a text buffer (the inline :class:`Input` or the
#: popup editor) rather than toggled / cycled in place.
_SCALAR_TYPES: frozenset[str] = frozenset({"int", "float", "str"})


def needs_popup_edit(entry: ConfigKey, value: Any, *, row_width: int) -> bool:
    """Return ``True`` when a ``str`` field must use the popup editor.

    A ``str`` edits inline by default, but routes to the larger popup
    :class:`EditFieldModal` when the current value would not fit / read
    well in a single-line in-row input: the registry marks the field
    :attr:`ConfigKey.multiline`, the value already contains a newline, or
    the rendered value is wider than the row. Non-``str`` types never use
    the popup (``bool`` / ``choice`` mutate in place; ``int`` / ``float``
    always edit inline), so this returns ``False`` for them.

    Args:
        entry: The registry entry describing the field.
        value: The field's currently-resolved value.
        row_width: The width (in cells) available for an inline input —
            typically the rendered field-row width. A non-positive width
            disables the width check (only the newline / multiline hints
            then route to the popup).

    Returns:
        ``True`` to route to the popup editor, ``False`` to edit inline.
    """
    if entry.type != "str":
        return False
    if entry.multiline:
        return True
    text = "" if value is None else str(value)
    if "\n" in text:
        return True
    return row_width > 0 and len(text) > row_width


def enter_action(entry: ConfigKey, value: Any, *, row_width: int) -> EnterAction:
    """Resolve the action ``Enter`` performs on *entry* given its value.

    The dispatch is the heart of the Enter-as-sole-mutator model: it maps
    a field's declared :attr:`ConfigKey.type` (and, for ``str``, the
    value's shape via :func:`needs_popup_edit`) onto one
    :data:`EnterAction`. Keeping it a pure function makes the keymap
    unit-testable without mounting Textual.

    Args:
        entry: The registry entry describing the field.
        value: The field's currently-resolved value (drives ``str``
            inline-vs-popup routing).
        row_width: The inline-input width budget passed through to
            :func:`needs_popup_edit`.

    Returns:
        The :data:`EnterAction` for the field.
    """
    if entry.type == "bool":
        return "toggle"
    if entry.type == "choice":
        return "cycle"
    if entry.type in _SCALAR_TYPES:
        return "popup" if needs_popup_edit(entry, value, row_width=row_width) else "inline"
    return "none"


def writable_layers_for(workspace: Path | None, repo: Path | None) -> tuple[str, ...]:
    """Return the writable layers resolvable given the available anchors.

    The ``L`` layer-cycle picks among these. ``global`` is always
    resolvable; ``workspace`` needs the workspace anchor; ``repo`` /
    ``branch`` / ``local`` need the repo anchor. ``branch`` is omitted
    here because it additionally requires a branch name the modal does not
    resolve — the operator targets it from the CLI.

    Args:
        workspace: Workspace root, or ``None`` when not in a workspace.
        repo: Repo root, or ``None`` when no repo is resolved.

    Returns:
        The resolvable writable-layer labels in :data:`WRITABLE_LAYERS`
        order. Always non-empty (``global`` is unconditional).
    """
    available: list[str] = []
    for layer in WRITABLE_LAYERS:
        if (
            layer == "global"
            or (layer == "workspace" and workspace is not None)
            or (layer in ("repo", "local") and repo is not None)
        ):
            available.append(layer)
    return tuple(available)


class ConfigModalState(BaseModel):
    """In-memory edit state for the config modal (strict per AGENTS rule 2).

    Tracks only the operator's staged edits and the chosen writable
    layer — never any persisted config (that is read live from the
    merged layered config / registry defaults). The model is strict
    (``extra="forbid"``) so a typo at construction fails fast.

    Attributes:
        dirty: Map of dotted-key → staged (already-coerced) value for
            fields edited but not yet flushed via the save key.
        layer: The writable layer the next save targets (one of
            :func:`writable_layers_for`).
    """

    model_config = ConfigDict(extra="forbid")

    dirty: dict[str, Any] = Field(default_factory=dict)
    layer: str = "repo"


def current_value(entry: ConfigKey, merged: dict[str, Any], dirty: dict[str, Any]) -> Any:
    """Resolve the value to display for *entry*: dirty wins, then merged, then default.

    Args:
        entry: The registry entry to resolve a value for.
        merged: The merged layered-config map.
        dirty: The staged-edit map (an uncommitted edit shows immediately).

    Returns:
        The resolved value following the dirty → merged → default ladder.
    """
    if entry.key in dirty:
        return dirty[entry.key]
    try:
        return get_dotted(merged, entry.key)
    except KeyError:
        return entry.default


def format_value(entry: ConfigKey, value: Any) -> str:
    """Render *value* for a field row.

    ``bool`` renders lowercase ``true`` / ``false``; ``multichoice``
    comma-joins its items; everything else uses ``str`` (``None`` renders
    as an empty cell).

    Args:
        entry: The registry entry (its type drives the rendering).
        value: The value to render.

    Returns:
        The display string for the value cell.
    """
    if entry.type == "bool":
        return "true" if bool(value) else "false"
    if entry.type == "multichoice":
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return "" if value is None else str(value)
    return "" if value is None else str(value)


def toggle_bool(entry: ConfigKey, merged: dict[str, Any], dirty: dict[str, Any]) -> dict[str, Any]:
    """Return a new dirty map with *entry*'s bool value flipped.

    No-op (returns *dirty* unchanged) when *entry* is not a ``bool`` field
    so the caller can route every ``Enter`` on a non-bool through here
    harmlessly.

    Args:
        entry: The field to toggle.
        merged: The merged config (resolves the pre-toggle value).
        dirty: The current staged-edit map.

    Returns:
        A new dirty map (the input is not mutated).
    """
    if entry.type != "bool":
        return dict(dirty)
    flipped = not bool(current_value(entry, merged, dirty))
    return {**dirty, entry.key: flipped}


def cycle_choice(
    entry: ConfigKey,
    merged: dict[str, Any],
    dirty: dict[str, Any],
    *,
    step: int,
) -> dict[str, Any]:
    """Return a new dirty map with *entry*'s choice cycled by *step*.

    Cycles a ``choice`` field through its declared :attr:`ConfigKey.choices`
    (wrapping at both ends). The ``Enter`` mutator forward-cycles with
    ``step=+1`` (``a → b → c → a``). For the dead ``multichoice`` scaffold
    the *first* current item is cycled — no registry entry exercises this
    today. No-op for other types.

    Args:
        entry: The field to cycle.
        merged: The merged config (resolves the pre-cycle value).
        dirty: The current staged-edit map.
        step: ``+1`` for the next choice, ``-1`` for the previous.

    Returns:
        A new dirty map (the input is not mutated).
    """
    if entry.type not in ("choice", "multichoice") or not entry.choices:
        return dict(dirty)
    choices = list(entry.choices)
    value = current_value(entry, merged, dirty)
    if entry.type == "choice":
        try:
            index = choices.index(str(value))
        except ValueError:
            index = 0
        next_value: Any = choices[(index + step) % len(choices)]
        return {**dirty, entry.key: next_value}
    # multichoice: cycle the first selected item (or seed the first choice).
    items = [str(item) for item in value] if isinstance(value, (list, tuple)) else []
    head = items[0] if items else choices[0]
    try:
        index = choices.index(head)
    except ValueError:
        index = 0
    cycled = choices[(index + step) % len(choices)]
    return {**dirty, entry.key: [cycled, *items[1:]]}


def merged_config(workspace: Path | None, repo: Path | None) -> dict[str, Any]:
    """Best-effort load of the merged layered config.

    Falls back to the built-in defaults when the layered merge fails (e.g.
    malformed YAML in a layer) so the modal stays informational rather
    than crashing — the operator can still edit + save, and the save path
    runs its own validation.

    Args:
        workspace: Workspace root (or ``None``).
        repo: Repo root (or ``None``).

    Returns:
        The merged config map, or the built-in defaults on merge failure.
    """
    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning(f"merged_config failed workspace={workspace!r} repo={repo!r} exc={exc!r}")
        return built_in_defaults()
    return merged


def save_dirty_fields(
    dirty: dict[str, Any],
    *,
    layer: str,
    workspace: Path | None,
    repo: Path | None,
    save_fn: Callable[..., None] | None = None,
) -> int:
    """Flush every staged field through the layered-config writer.

    Routes each key through :func:`eawf.cli.commands.config._save_value_to_layer`
    — the same mutator :command:`eawf config set` uses, which proxies to
    the daemon — so the modal and the CLI share one writer. ``state.json``
    is never touched.

    Args:
        dirty: The staged-edit map to flush.
        layer: The writable layer to write to (resolved to a YAML path
            via :func:`eawf.config.layered.layer_path`).
        workspace: Workspace anchor for the ``workspace`` layer.
        repo: Repo anchor for the ``repo`` / ``local`` layers.
        save_fn: Test seam — defaults to
            :func:`eawf.cli.commands.config._save_value_to_layer`. The
            keyword-only kwargs ``target_path`` / ``key`` / ``value``
            match the CLI helper exactly.

    Returns:
        The number of fields written.

    Raises:
        ValueError: When *layer* is not a resolvable writable layer.
    """
    if not dirty:
        return 0
    target_path = layer_path(layer, workspace=workspace, repo=repo)
    if save_fn is None:
        from eawf.cli.commands.config import _save_value_to_layer as _default_save_fn

        save_fn = _default_save_fn
    saved = 0
    for key, value in dirty.items():
        entry = registry_lookup(key)
        if entry is None:
            logger.warning(f"save_dirty_fields key={key!r} not in registry; skipping")
            continue
        coerced = coerce_and_validate(entry, value)
        save_fn(target_path=target_path, key=key, value=coerced, repo_root=repo)
        saved += 1
    logger.info(f"save_dirty_fields layer={layer} saved={saved}")
    return saved


class ConfigModal(ModalScreen[None]):
    """Registry-driven tabbed config window (Esc/dirty-guard to close).

    Renders every :data:`CONFIG_REGISTRY` key in alphabetical tabs
    (alphabetical fields per tab). The cursor sits on one field row:
    ``↑`` / ``↓`` move it within the tab (clamped to first / last),
    ``←`` / ``→`` switch tabs, and ``Enter`` is the **sole mutator** —
    it toggles a ``bool``, forward-cycles a ``choice``, or edits a scalar
    (``int`` / ``float`` / ``str``) inline (a long / multi-line ``str``
    routes to the popup :class:`EditFieldModal`). ``s`` saves through the
    layered writer, ``r`` resets staged edits, ``L`` cycles the writable
    layer, and ``Esc`` cancels an inline edit or else closes (prompting
    first when there are staged edits).
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
        #: The field key whose row currently hosts an inline ``Input``, or
        #: ``None`` when no inline edit is open. Only one row edits at a
        #: time; while set, ``↑`` / ``↓`` / ``←`` / ``→`` are inert and the
        #: Input owns ``Enter`` (commit) / ``Esc`` (cancel).
        self._editing_key: str | None = None

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
        return f"{caret}{dirty_mark} {entry.key:<42} {type_cell:<14} {format_value(entry, value)}"

    def _hint_line(self) -> str:
        """Render the footer keymap hint.

        The hint reflects the type-dispatched keymap: arrows navigate
        (``↑`` / ``↓`` fields, ``←`` / ``→`` tabs) and ``Enter`` is the
        sole mutator. While an inline edit is open the hint flips to the
        commit / cancel keys the mounted :class:`Input` owns.
        """
        if self._editing_key is not None:
            entry = registry_lookup(self._editing_key)
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
        if entry.key in dirty and dirty[entry.key] == self._persisted_value(entry):
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
        toggles, a ``choice`` forward-cycles (``a → b → c → a``), and a
        scalar (``int`` / ``float`` / ``str``) edits inline — a multi-line
        / over-wide ``str`` routes to the popup :class:`EditFieldModal`
        instead. Inert while an inline edit is already open and a no-op on
        an empty tab.
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
        from eawf.tui.screens.overlays.edit_field import EditFieldModal

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

    @staticmethod
    def _meta_line(entry: ConfigKey) -> str:
        """Render the key + type label shown beside the inline input.

        Reuses the static :meth:`_field_line` column widths exactly — a
        three-cell prefix (caret + dirty marker + separator), then the key
        padded to 42 and the type cell padded to 14 — so the trailing inline
        :class:`Input` lands in the **same column as the static value cell**.
        Without the shared widths the meta line was shorter than the static
        row and the whole inline editor bunched against the key (the "row
        squished after Enter" report). The range hint moves to the footer
        (:meth:`_range_hint`) so it does not push the input out of column.

        Args:
            entry: The field being edited.
        """
        type_cell = f"[{entry.type}]"
        return f"   {entry.key:<42} {type_cell:<14} "

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

    def action_save(self) -> None:
        """Flush staged edits through the layered writer (``s``)."""
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
        """Drop every staged edit (``r``)."""
        if not self._view.dirty:
            return
        self._view.dirty = {}
        self._repaint_fields()
        self.app.notify("staged edits reset", severity="information")

    def action_cycle_layer(self) -> None:
        """Cycle the writable layer the save targets (``L``)."""
        if len(self._layers) <= 1:
            return
        index = self._layers.index(self._view.layer)
        self._view.layer = self._layers[(index + 1) % len(self._layers)]
        self.query_one("#config-layer", Static).update(self._layer_line())

    def action_close(self) -> None:
        """Handle ``Esc``: cancel an inline edit, else close (dirty-guarded).

        While an inline edit is open ``Esc`` aborts just that edit (the
        modal stays open). Otherwise on a clean modal this dismisses
        immediately, and on a dirty modal it pushes a
        :class:`~eawf.tui.screens.overlays.confirm.ConfirmModal` (V15
        dirty-guard), dismissing only when the operator confirms
        discarding the staged edits.
        """
        if self._editing_key is not None:
            self._cancel_inline_edit()
            return
        if not self._view.dirty:
            self.dismiss(None)
            return
        from eawf.tui.screens.overlays.confirm import ConfirmModal

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
    "writable_layers_for",
]
