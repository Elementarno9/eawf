"""``ConfigModal`` — the registry-driven tabbed config overlay (tui_v2).

The Textual config window: a :class:`~textual.screen.ModalScreen` opened
by the global ``c`` keypress and the ``/config`` palette verb. It renders
**every** operator-tunable key from :data:`eawf.config.registry.CONFIG_REGISTRY`
in alphabetical tabs (alphabetical fields within each tab), so the surface
auto-covers any key added to the registry later — the registry is the
single source of truth shared with the ``eawf config`` CLI menu, so the
two cannot drift.

Per-type interaction (keymap conventions — arrows primary, full key
names; ``Space`` toggles a flag, ``←`` / ``→`` cycle an enum):

* ``↑`` / ``↓`` — move the field cursor inside the active tab.
* ``←`` / ``→`` — on a ``choice`` / ``multichoice`` field, cycle its
  value; otherwise they switch the active tab.
* ``Space`` — toggle a ``bool`` field in place.
* ``Enter`` — open the
  :class:`~eawf.tui_v2.screens.overlays.edit_field.EditFieldModal` for a
  ``str`` / ``int`` / ``float`` / path field (the scalar editor).
* ``s`` — save: flush every dirty field through the layered-config writer.
* ``r`` — reset: drop every staged (dirty) edit.
* ``L`` — cycle the writable layer the save targets.
* ``Esc`` — on a dirty modal, prompt before discarding (V15 dirty-guard);
  on a clean modal, close immediately.

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
from typing import TYPE_CHECKING, Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field
from textual.app import ComposeResult
from textual.binding import Binding, BindingType
from textual.containers import Vertical, VerticalScroll
from textual.reactive import reactive
from textual.screen import ModalScreen
from textual.widgets import Static, TabbedContent, TabPane

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

#: Field types that toggle / cycle in place inside the modal (never open
#: the scalar :class:`EditFieldModal`). ``bool`` toggles on ``Space``;
#: ``choice`` / ``multichoice`` cycle on ``←`` / ``→``.
_INLINE_TYPES: frozenset[str] = frozenset({"bool", "choice", "multichoice"})


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
    so the caller can route every ``Space`` press through here.

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
    (wrapping at both ends). For ``multichoice`` the *first* current item
    is cycled (a minimal in-place affordance; full multi-select edits go
    through the scalar editor). No-op for other types.

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
    (alphabetical fields per tab). ``Space`` toggles a bool, ``←`` / ``→``
    cycle an enum (or switch tabs on a non-enum field), ``Enter`` opens
    the scalar :class:`EditFieldModal`, ``s`` saves through the layered
    writer, ``r`` resets staged edits, ``L`` cycles the writable layer,
    and ``Esc`` closes (prompting first when there are staged edits).
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
    ConfigModal .config-hint {
        color: $text-muted;
        height: 1;
        margin-top: 1;
    }
    """

    #: Keymap (arrows primary; full key names in the footer hint). ``space``
    #: toggles, ``left`` / ``right`` cycle-or-switch-tab, ``enter`` edits,
    #: ``s`` saves, ``r`` resets, ``L`` cycles the layer, ``escape`` closes
    #: (dirty-guarded). ``↑`` / ``↓`` move the field cursor; ``j`` / ``k``
    #: ride them as aliases.
    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("up", "move_field(-1)", "up", show=False),
        Binding("down", "move_field(1)", "down", show=False),
        Binding("k", "move_field(-1)", "up", show=False),
        Binding("j", "move_field(1)", "down", show=False),
        Binding("left", "cycle_or_tab(-1)", "prev", show=False),
        Binding("right", "cycle_or_tab(1)", "next", show=False),
        Binding("space", "toggle_bool", "toggle", show=False),
        Binding("enter", "edit", "edit", show=False),
        Binding("s", "save", "save", show=False),
        Binding("r", "reset", "reset", show=False),
        Binding("L", "cycle_layer", "layer", show=False),
        Binding("escape", "close", "close", show=False),
    ]

    #: Index of the highlighted field inside the active tab.
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
            yield Static(self._hint_line(), classes="config-hint")

    def on_mount(self) -> None:
        """Paint the initial highlight; keep key focus on the screen.

        :class:`TabbedContent` auto-focuses its internal tab bar, whose
        own ``←`` / ``→`` bindings would otherwise steal the arrows the
        modal needs for in-place enum cycling. Clearing focus routes every
        keystroke to this screen's bindings so the modal owns the full
        interaction model (and drives tab switching itself via
        :meth:`action_cycle_or_tab`).
        """
        self.set_focus(None)
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

    def _field_line(self, entry: ConfigKey) -> str:
        """Render one field row: dirty marker + key + type + value."""
        value = current_value(entry, self._merged, self._view.dirty)
        dirty_mark = "*" if entry.key in self._view.dirty else " "
        type_cell = f"[{entry.type}]"
        return f"{dirty_mark} {entry.key:<42} {type_cell:<14} {format_value(entry, value)}"

    def _hint_line(self) -> str:
        """Render the footer keymap hint (full key names; arrows primary)."""
        return (
            "[ ↑/↓ field · ←/→ tab/cycle · Space toggle · Enter edit · "
            "s save · r reset · L layer · Esc close ]"
        )

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

    def _repaint_fields(self) -> None:
        """Repaint every field row of the active tab (cursor + dirty marks)."""
        tab = self._active_tab()
        if not tab:
            return
        fields = keys_for_tab(tab)
        for index, entry in enumerate(fields):
            try:
                row = self.query_one(f"#{self._field_row_id(tab, index)}", Static)
            except Exception:  # pragma: no cover - mid-mount guard
                continue
            row.update(self._field_line(entry))
            row.set_class(index == self.field_index, "-selected")
            row.set_class(entry.key in self._view.dirty, "-dirty")
        self.query_one("#config-layer", Static).update(self._layer_line())

    def watch_field_index(self) -> None:
        """Repaint the field rows when the cursor moves."""
        if self.is_mounted:
            self._repaint_fields()

    # -- actions ------------------------------------------------------------

    def action_move_field(self, delta: int) -> None:
        """Move the field cursor by *delta*, clamped to the tab's bounds.

        Args:
            delta: ``-1`` for the previous field, ``+1`` for the next.
        """
        fields = self._active_fields()
        if not fields:
            return
        self.field_index = max(0, min(len(fields) - 1, self.field_index + delta))

    def action_cycle_or_tab(self, step: int) -> None:
        """Cycle an enum field's value, or switch tabs for a non-enum field.

        ``←`` / ``→`` cycle a ``choice`` / ``multichoice`` field through
        its options; on any other field type they switch the active tab so
        the arrows stay useful everywhere.

        Args:
            step: ``-1`` for previous, ``+1`` for next.
        """
        entry = self._active_field()
        if entry is not None and entry.type in ("choice", "multichoice"):
            self._view.dirty = cycle_choice(entry, self._merged, self._view.dirty, step=step)
            self._repaint_fields()
            return
        self._switch_tab(step)

    def _switch_tab(self, step: int) -> None:
        """Switch the active tab by *step* (wrapping), resetting the cursor.

        Args:
            step: ``-1`` for the previous tab, ``+1`` for the next.
        """
        if not self._tabs:
            return
        tab = self._active_tab()
        try:
            index = self._tabs.index(tab)
        except ValueError:
            index = 0
        next_tab = self._tabs[(index + step) % len(self._tabs)]
        self.query_one("#config-tabs", TabbedContent).active = self._tab_pane_id(next_tab)
        # Activating a tab re-focuses the tab bar; clear it so the modal's
        # own arrow bindings keep winning over the tab bar's.
        self.set_focus(None)
        self.field_index = 0
        self._repaint_fields()

    def action_toggle_bool(self) -> None:
        """Toggle the selected ``bool`` field in place (``Space``).

        Named ``toggle_bool`` (not ``toggle``) to avoid clashing with the
        Textual ``DOMNode.action_toggle(attribute_name)`` reactive helper.
        """
        entry = self._active_field()
        if entry is None or entry.type != "bool":
            return
        self._view.dirty = toggle_bool(entry, self._merged, self._view.dirty)
        self._repaint_fields()

    def action_edit(self) -> None:
        """Open the scalar :class:`EditFieldModal` for the selected field.

        Only ``str`` / ``int`` / ``float`` / path fields open the editor;
        ``bool`` / ``choice`` / ``multichoice`` mutate in place (``Space``
        / ``←`` / ``→``) so ``Enter`` on them is a no-op.
        """
        entry = self._active_field()
        if entry is None or entry.type in _INLINE_TYPES:
            return
        from eawf.tui_v2.screens.overlays.edit_field import EditFieldModal

        value = current_value(entry, self._merged, self._view.dirty)
        self.app.push_screen(EditFieldModal(entry, value), self._make_edit_callback(entry))

    def _make_edit_callback(self, entry: ConfigKey) -> Callable[[Any], None]:
        """Build the dismiss callback that folds an edited value into dirty.

        Args:
            entry: The field that was edited.

        Returns:
            A callback taking the :class:`EditFieldModal` dismiss value;
            it folds a non-``None`` (accepted) value into the dirty map.
        """

        def _on_dismiss(result: Any) -> None:
            if result is None:
                return
            self._view.dirty = {**self._view.dirty, entry.key: result}
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
        """Close the modal (``Esc``); prompt first when there are staged edits.

        On a clean modal this dismisses immediately. On a dirty modal it
        pushes a :class:`~eawf.tui_v2.screens.overlays.confirm.ConfirmModal`
        (V15 dirty-guard) and only dismisses when the operator confirms
        discarding the staged edits.
        """
        if not self._view.dirty:
            self.dismiss(None)
            return
        from eawf.tui_v2.screens.overlays.confirm import ConfirmModal

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
    "current_value",
    "cycle_choice",
    "format_value",
    "merged_config",
    "open_config",
    "save_dirty_fields",
    "toggle_bool",
    "writable_layers_for",
]
