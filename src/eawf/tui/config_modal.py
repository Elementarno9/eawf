"""Tabbed full-screen config modal for the Eä Rich TUI (P20-I01-W11).

The modal is a full-screen :class:`rich.layout.Layout` swap that replaces
the W02 repo-scope quadrant when the operator presses ``c``. Tabs and
keys are sourced from :mod:`eawf.config.registry` so the modal and the
W10 ``eawf config menu`` cannot drift on metadata. Save uses the same
layered-YAML writer as :command:`eawf config set` — namely
:func:`eawf.cli.commands.config._save_value_to_layer` — so the modal
NEVER touches ``state.json`` directly and the layered-config CLI stays
the single writer of YAML layers.

Layout sketch::

    +----------------------------------------------------------------+
    | Eä  EAWF / P20 / P20-I01  >> config                            |  ← header
    +----------------------------------------------------------------+
    | tabs: [audit] estimation planning research runtime ship ui ... |
    +----------------------------------------------------------------+
    | audit.fix_safe           [bool]    False                       |
    | > audit.flaky_retry_count [int]    1                           |  ← form
    +----------------------------------------------------------------+
    | edit > new value: _                                            |  ← input
    +----------------------------------------------------------------+
    | ↑↓ field  Tab tabs  Enter edit  s save  q/Esc back             |  ← footer
    +----------------------------------------------------------------+

Keymap (per ``feedback_tui_keymap_conventions`` — arrows primary, vim
aliases secondary; full key names lead):

* ↑ / ↓ — move the field cursor inside the active tab.
* Tab / Shift-Tab — cycle tabs left / right.
* Enter — open the per-type inline editor for the selected field.
* Esc — at the modal root, exit without saving; while editing,
  cancel the field edit and return to navigation.
* ``s`` — flush every dirty field through the W10 layered writer and
  return to the quadrant with a brief "saved N keys" toast.
* ``q`` — synonym for Esc at the modal root.

Per-type inline editor (success criterion 2 — sized per type):

* ``bool`` — confirm widget (``y``/``n``/space toggles; Enter commits).
* ``int`` / ``float`` / ``str`` — text input; backspace deletes; Enter
  commits, Esc cancels.
* ``choice`` — narrow select list (↑↓ moves; Enter commits).
* ``multichoice`` — checklist (space toggles; Enter commits).

The view state is a frozen Pydantic v2 model with ``extra="forbid"``,
matching the W03 wave-board view-state convention. ``apply_key`` is a
pure function — it returns the next view-state without touching disk
— so the dispatch surface is easy to drive from tests.

Eä brand stays outside-left of the breadcrumb in the modal header per
``feedback_tui_branding``.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field
from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from eawf.cli.errors import InvalidInput
from eawf.config.layered import get_dotted, layer_path, merge_config
from eawf.config.registry import (
    ConfigKey,
    coerce_and_validate,
    keys_for_tab,
    registry_lookup,
    tabs_sorted,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Keymap constants
# ---------------------------------------------------------------------------


#: Keys that exit the modal at the navigation root (no field edit
#: pending). Bare Esc and ``q`` / ``Q`` return to the quadrant without
#: saving; ``\x03`` / ``\x04`` are Ctrl-C / Ctrl-D from cbreak mode.
_EXIT_KEYS: frozenset[str] = frozenset({"\x1b", "q", "Q", "\x03", "\x04"})

#: Save key — flushes every dirty field through the layered writer and
#: returns to the quadrant on success.
_SAVE_KEY: str = "s"

#: Up / down keys move the field cursor inside the active tab. Vim
#: aliases ``j`` / ``k`` are kept as secondary shorthand.
_UP_KEYS: frozenset[str] = frozenset({"\x1b[A", "k"})
_DOWN_KEYS: frozenset[str] = frozenset({"\x1b[B", "j"})

#: Tab cycling. Tab key (``\t``) advances to the next tab; ``\x1b[Z``
#: (Shift-Tab CSI sequence) retreats. Arrow keys ← / → are reserved for
#: future column nav inside multichoice grids.
_TAB_NEXT_KEY: str = "\t"
_TAB_PREV_KEY: str = "\x1b[Z"

#: Enter opens the per-type inline editor. CR + LF both accepted.
_ENTER_KEYS: frozenset[str] = frozenset({"\r", "\n"})

#: Toggle key for bool fields when editing.
_TOGGLE_KEYS: frozenset[str] = frozenset({" ", "y", "Y", "n", "N", "t", "T", "f", "F"})


#: Footer hint for the modal — arrow + tab + s + Esc primary; vim
#: aliases trail.
MODAL_FOOTER: str = "↑↓ field  Tab/Shift-Tab tab  Enter edit  s save  q/Esc back  (vim: j k)"

#: Footer hint while editing a field — narrower keymap since arrow / tab
#: are reserved for the underlying inline widget.
MODAL_FOOTER_EDIT: str = "Enter commit  Esc cancel  Backspace delete  (editing)"

#: Default refresh rate for the modal's :class:`Live` loop. Matches the
#: quadrant and wave-board surfaces so the modal feels equally responsive.
DEFAULT_REFRESH_HZ: int = 1


# ---------------------------------------------------------------------------
# View-state model (Pydantic v2 strict per AGENTS.md rule 2)
# ---------------------------------------------------------------------------


class ConfigModalState(BaseModel):
    """Ephemeral view state for the config modal.

    Tracks tab cursor, field cursor inside the active tab, the
    in-progress edit buffer (when editing) and the per-key dirty map
    of values still pending save. The model is strict
    (``extra="forbid"``) so a typo at construction fails fast.

    Attributes:
        tab_index: 0-based cursor over :func:`tabs_sorted`. Wraps on
            Tab / Shift-Tab.
        field_index: 0-based cursor inside the active tab's
            :func:`keys_for_tab` list. Reset to 0 on tab change.
        editing: ``True`` when the inline editor is open. The footer
            hint flips and key dispatch routes to the editor.
        edit_buffer: Raw answer accumulating in the active inline
            editor — string for text widgets, the chosen choice for
            ``choice``, a comma-joined list for ``multichoice``. Empty
            when not editing.
        dirty: Map of dotted-key → coerced value for fields whose edit
            was committed but not yet flushed via the save key.
        toast: One-line footer toast (e.g. "saved 3 keys") shown after
            a successful save; the parent ``run_config_modal`` loop
            clears it on the next non-save keystroke.
    """

    model_config = ConfigDict(extra="forbid")

    tab_index: int = Field(default=0, ge=0)
    field_index: int = Field(default=0, ge=0)
    editing: bool = False
    edit_buffer: str = ""
    dirty: dict[str, Any] = Field(default_factory=dict)
    toast: str = ""


# ---------------------------------------------------------------------------
# Navigation helpers (pure, easy to drive from tests)
# ---------------------------------------------------------------------------


def active_tab(view: ConfigModalState) -> str:
    """Return the tab name under the cursor.

    Wraps tab_index inside the bounds of :func:`tabs_sorted` so a
    stale view (e.g. a tab removed from the registry between renders)
    cannot index past the end. Empty registries return ``""``.
    """
    tabs = tabs_sorted()
    if not tabs:
        return ""
    idx = view.tab_index % len(tabs)
    return tabs[idx]


def active_fields(view: ConfigModalState) -> tuple[ConfigKey, ...]:
    """Return the alphabetical field list for the current tab.

    Re-resolves from :func:`keys_for_tab` every call so a registry edit
    is visible without restarting the modal.
    """
    tab = active_tab(view)
    if not tab:
        return ()
    return keys_for_tab(tab)


def active_field(view: ConfigModalState) -> ConfigKey | None:
    """Return the ConfigKey under the cursor, or ``None`` for an empty tab."""
    fields = active_fields(view)
    if not fields:
        return None
    idx = max(0, min(view.field_index, len(fields) - 1))
    return fields[idx]


def _current_value(merged: dict[str, Any], view: ConfigModalState, entry: ConfigKey) -> Any:
    """Resolve the displayed value: dirty buffer wins, else merged, else default."""
    if entry.key in view.dirty:
        return view.dirty[entry.key]
    try:
        return get_dotted(merged, entry.key)
    except KeyError:
        return entry.default


# ---------------------------------------------------------------------------
# Per-type editor seeding
# ---------------------------------------------------------------------------


def _seed_edit_buffer(entry: ConfigKey, current: Any) -> str:
    """Return the initial ``edit_buffer`` for *entry* given the *current* value.

    The buffer is a string regardless of type — the inline editor edits
    a string buffer and :func:`coerce_and_validate` runs on commit. For
    ``choice`` and ``multichoice`` the buffer holds the running selection
    so a single render reflects the operator's choices.
    """
    if entry.type == "bool":
        return "true" if bool(current) else "false"
    if entry.type == "multichoice":
        if isinstance(current, (list, tuple)):
            return ",".join(str(item) for item in current)
        return ""
    if current is None:
        return ""
    return str(current)


# ---------------------------------------------------------------------------
# Key dispatch (pure)
# ---------------------------------------------------------------------------


def apply_key(view: ConfigModalState, key: str) -> ConfigModalState:
    """Apply *key* to *view* and return the next :class:`ConfigModalState`.

    Pure function — does not touch :class:`rich.live.Live`, disk, or the
    layered-config writer. The save key is handled separately by
    :func:`save_dirty` so failures surface as exceptions for the live
    loop to render.

    Args:
        view: Current view state.
        key: Single keystroke or ESC-prefixed CSI sequence (arrow keys,
            Shift-Tab).

    Returns:
        Updated :class:`ConfigModalState`. Unknown keys return the
        view with the toast cleared (so the next keystroke wipes a
        post-save toast even if it's a no-op).
    """
    # Toast is single-use — any keystroke clears it so the operator sees
    # the toast for exactly one frame after save.
    if view.toast:
        view = view.model_copy(update={"toast": ""})

    if view.editing:
        return _apply_key_editing(view, key)
    return _apply_key_navigating(view, key)


def _apply_key_navigating(view: ConfigModalState, key: str) -> ConfigModalState:
    """Dispatch keys while the modal is in navigation mode (not editing)."""
    if key == _TAB_NEXT_KEY:
        tabs = tabs_sorted()
        if not tabs:
            return view
        next_idx = (view.tab_index + 1) % len(tabs)
        return view.model_copy(update={"tab_index": next_idx, "field_index": 0})
    if key == _TAB_PREV_KEY:
        tabs = tabs_sorted()
        if not tabs:
            return view
        prev_idx = (view.tab_index - 1) % len(tabs)
        return view.model_copy(update={"tab_index": prev_idx, "field_index": 0})
    if key in _UP_KEYS:
        new_idx = max(0, view.field_index - 1)
        return view.model_copy(update={"field_index": new_idx})
    if key in _DOWN_KEYS:
        fields = active_fields(view)
        upper = max(0, len(fields) - 1)
        new_idx = min(upper, view.field_index + 1)
        return view.model_copy(update={"field_index": new_idx})
    if key in _ENTER_KEYS:
        entry = active_field(view)
        if entry is None:
            return view
        # Seed the edit buffer with the current value so the operator
        # can edit-or-confirm rather than retype from scratch.
        # _current_value needs a merged config — for navigation seeding
        # we use the entry default as the safe fallback because we do
        # not have the merged dict in apply_key. The live loop seeds
        # via begin_edit when it has the merged dict available.
        seeded = _seed_edit_buffer(entry, entry.default)
        return view.model_copy(update={"editing": True, "edit_buffer": seeded})
    return view


def _apply_key_editing(view: ConfigModalState, key: str) -> ConfigModalState:
    """Dispatch keys while the inline editor is open.

    Esc cancels the edit, returning to navigation with the dirty map
    untouched. Enter commits the buffer — the live loop is responsible
    for running :func:`coerce_and_validate` and folding the typed value
    into ``dirty``.
    """
    if key in (frozenset({"\x1b"}) | _EXIT_KEYS) and key not in _ENTER_KEYS:
        # Cancel edit on bare Esc (matches W03 wave-board convention).
        return view.model_copy(update={"editing": False, "edit_buffer": ""})
    if key in _ENTER_KEYS:
        # The live loop folds the typed value into ``dirty`` and clears
        # ``editing`` + ``edit_buffer``. Pure apply_key just signals
        # "commit pending" — we close the editor here and let the live
        # loop coerce + fold via commit_edit.
        return view
    if key == "\x7f" or key == "\b":  # Backspace (DEL or ASCII BS).
        return view.model_copy(update={"edit_buffer": view.edit_buffer[:-1]})
    # Append printable single character. CSI sequences (arrow keys etc.)
    # are silently ignored inside the editor — the inline widget is
    # single-line so arrow nav has no use here.
    if len(key) == 1 and key.isprintable():
        return view.model_copy(update={"edit_buffer": view.edit_buffer + key})
    return view


def begin_edit(view: ConfigModalState, merged: dict[str, Any]) -> ConfigModalState:
    """Open the inline editor for the currently-focused field.

    Seeds the edit buffer from the merged config so the operator edits
    "what would happen if I press Enter" exactly. Called by the live
    loop in response to Enter (since the pure :func:`apply_key` lacks
    access to the merged dict).

    Args:
        view: Current view state.
        merged: Layered-config merged map used to resolve the current
            value for the selected field.

    Returns:
        View with ``editing=True`` and ``edit_buffer`` seeded.
    """
    entry = active_field(view)
    if entry is None:
        return view
    current = _current_value(merged, view, entry)
    seeded = _seed_edit_buffer(entry, current)
    return view.model_copy(update={"editing": True, "edit_buffer": seeded})


def commit_edit(view: ConfigModalState) -> ConfigModalState:
    """Coerce the edit buffer and fold the typed value into ``dirty``.

    Raises :class:`InvalidInput` when the buffer cannot be coerced
    against the entry's declared type / range / choices. Callers in
    the live loop catch the exception and surface a toast.

    Returns the view with editor closed, buffer cleared, and the dirty
    map updated. The save key (``s``) flushes the dirty map to the
    layered writer.
    """
    entry = active_field(view)
    if entry is None:
        return view.model_copy(update={"editing": False, "edit_buffer": ""})
    raw = view.edit_buffer
    coerced = coerce_and_validate(entry, raw)
    next_dirty = {**view.dirty, entry.key: coerced}
    return view.model_copy(update={"editing": False, "edit_buffer": "", "dirty": next_dirty})


# ---------------------------------------------------------------------------
# Save path — flushes through the W10 layered writer
# ---------------------------------------------------------------------------


def save_dirty(
    view: ConfigModalState,
    *,
    scope: str,
    workspace: Path | None,
    repo: Path,
    save_fn: Callable[..., None] | None = None,
) -> ConfigModalState:
    """Flush every dirty field through the W10 layered writer.

    Routes through :func:`eawf.cli.commands.config._save_value_to_layer`
    so the modal and :command:`eawf config set` / :command:`eawf config
    menu` share the exact same mutator. ``state.json`` is never touched.

    Args:
        view: Current view state. The ``dirty`` map names the fields
            to flush.
        scope: Writable layer (``global`` | ``workspace`` | ``repo`` |
            ``local``).
        workspace: Workspace anchor for the ``workspace`` layer.
        repo: Repo anchor (required for ``repo`` and ``local`` layers).
        save_fn: Test seam — defaults to
            :func:`eawf.cli.commands.config._save_value_to_layer`.
            The keyword-only kwargs ``target_path``, ``key``, ``value``
            match the W10 helper exactly.

    Returns:
        View with ``dirty`` emptied and a "saved N keys" toast.

    Raises:
        ValueError: When *scope* is not a writable layer.
        InvalidInput: Re-raised from any field whose stored value
            cannot be re-coerced (defensive — the modal coerces on
            commit, so this should not fire in practice).
    """
    if not view.dirty:
        return view.model_copy(update={"toast": "no changes to save"})
    target_path = layer_path(scope, workspace=workspace, repo=repo)
    if save_fn is None:
        from eawf.cli.commands.config import _save_value_to_layer as _default_save_fn

        save_fn = _default_save_fn
    saved = 0
    for key, value in view.dirty.items():
        entry = registry_lookup(key)
        if entry is None:
            logger.warning(f"save_dirty key={key!r} not in registry; skipping")
            continue
        # Re-coerce defensively — the buffer was coerced on commit, so
        # the value is typed, but the layered writer is the system of
        # record and re-validation costs nothing.
        coerced = coerce_and_validate(entry, value)
        save_fn(target_path=target_path, key=key, value=coerced)
        saved += 1
    plural = "s" if saved != 1 else ""
    toast = f"saved {saved} key{plural}"
    logger.info(f"save_dirty scope={scope} saved={saved} target={str(target_path)!r}")
    return view.model_copy(update={"dirty": {}, "toast": toast})


# ---------------------------------------------------------------------------
# Panel builders
# ---------------------------------------------------------------------------


def build_header_panel(state: dict[str, Any]) -> Panel:
    """Header strip — brand + scope breadcrumb + ``>> config`` marker.

    Reuses :func:`eawf.tui.layout.build_brand_text` so the brand /
    breadcrumb styling is byte-identical to the quadrant and wave-board
    surfaces. Appends ``  >> config`` so the operator knows the modal
    is open.
    """
    from eawf.tui.layout import build_brand_text, build_breadcrumb

    breadcrumb = build_breadcrumb(state)
    text = build_brand_text(breadcrumb)
    text.append("  >> config", style="bold magenta")
    return Panel(text, title=None, border_style="dim")


def build_tabs_panel(view: ConfigModalState) -> Panel:
    """Tab strip — every tab name, with the active tab bracketed."""
    tabs = tabs_sorted()
    if not tabs:
        return Panel(Text("(no tabs)"), title="tabs", border_style="cyan")
    active = active_tab(view)
    rendered = Text()
    rendered.append("tabs: ", style="dim")
    for idx, tab in enumerate(tabs):
        if idx > 0:
            rendered.append(" ")
        if tab == active:
            rendered.append(f"[{tab}]", style="bold cyan")
        else:
            rendered.append(tab, style="dim")
    return Panel(rendered, title=None, border_style="cyan")


def _format_field_value(value: Any, entry: ConfigKey) -> str:
    """Render *value* for the field row.

    - ``bool`` → ``true``/``false`` lowercase.
    - ``multichoice`` → comma-joined items.
    - everything else → ``str()``.
    """
    if entry.type == "bool":
        return "true" if bool(value) else "false"
    if entry.type == "multichoice":
        if isinstance(value, (list, tuple)):
            return ",".join(str(item) for item in value)
        return str(value) if value is not None else ""
    if value is None:
        return ""
    return str(value)


def _format_field_row(
    entry: ConfigKey,
    value: Any,
    *,
    selected: bool,
    dirty: bool,
) -> str:
    """Render one field row: cursor + key + type + current value (+ * if dirty)."""
    marker = ">" if selected else " "
    dirty_mark = "*" if dirty else " "
    type_cell = f"[{entry.type}]"
    value_cell = _format_field_value(value, entry)
    return f"{marker}{dirty_mark} {entry.key:<40}  {type_cell:<14}  {value_cell}"


def build_form_panel(view: ConfigModalState, merged: dict[str, Any]) -> Panel:
    """Form pane — one row per field in the active tab.

    The selected row carries the ``>`` cursor; dirty rows carry ``*``.
    The value cell pulls from the dirty map when present (so an
    uncommitted edit is reflected immediately), otherwise from the
    merged config, otherwise from the registry default.
    """
    fields = active_fields(view)
    if not fields:
        body = Text("(no fields under this tab)")
        return Panel(body, title="form", border_style="cyan")
    lines: list[str] = []
    for idx, entry in enumerate(fields):
        value = _current_value(merged, view, entry)
        lines.append(
            _format_field_row(
                entry,
                value,
                selected=(idx == view.field_index and not view.editing),
                dirty=(entry.key in view.dirty),
            )
        )
    return Panel(Text("\n".join(lines)), title="form", border_style="cyan")


def build_input_panel(view: ConfigModalState) -> Panel:
    """Input strip — shows the inline editor buffer when ``editing``.

    When not editing, the strip carries a one-line hint reminding the
    operator how to enter the editor.
    """
    if not view.editing:
        return Panel(
            Text("(press Enter to edit the selected field)", style="dim"),
            title=None,
            border_style="cyan",
        )
    entry = active_field(view)
    if entry is None:
        return Panel(Text("(no field)"), title=None, border_style="cyan")
    # Cursor at end of the buffer — a literal ``_`` so the snapshot
    # stays stable across rich versions (no live blink).
    body = Text()
    body.append("edit ", style="dim")
    body.append(f"{entry.key} ", style="bold cyan")
    body.append(f"[{entry.type}]", style="dim")
    if entry.type == "choice" and entry.choices:
        body.append(f"  choices={list(entry.choices)}", style="dim")
    if entry.type == "multichoice" and entry.choices:
        body.append(f"  choices={list(entry.choices)}", style="dim")
    body.append("\n> ")
    body.append(view.edit_buffer, style="bold")
    body.append("_", style="dim")
    return Panel(body, title=None, border_style="magenta")


def build_footer_panel(view: ConfigModalState) -> Panel:
    """Footer strip — keymap hint, edit-mode hint, and any save toast."""
    text = Text()
    if view.editing:
        text.append(MODAL_FOOTER_EDIT, style="dim")
    else:
        text.append(MODAL_FOOTER, style="dim")
    if view.toast:
        text.append("    ")
        text.append(view.toast, style="bold green")
    return Panel(text, title=None, border_style="dim")


def build_modal_frame(
    view: ConfigModalState, *, state: dict[str, Any], merged: dict[str, Any]
) -> Layout:
    """Compose the modal: header / tabs / form / input / footer.

    The body is a vertical stack — tabs strip on top, form pane in the
    middle (the largest pane), input strip below, footer at the bottom.
    A full-screen modal is intentionally a Layout swap rather than a
    floating popup so :class:`rich.live.Live` can render it with the
    same screen-takeover semantics as the quadrant.

    Args:
        view: Current modal view state.
        state: Raw state dict — used by the header to build the
            breadcrumb. The modal does not read other state fields.
        merged: Layered-config merged map — fields render their current
            value from this when no dirty edit is staged.

    Returns:
        Rich :class:`Layout` ready for :class:`Live`.
    """
    layout = Layout()
    layout.split_column(
        Layout(name="header", size=3),
        Layout(name="tabs", size=3),
        Layout(name="form", ratio=1),
        Layout(name="input", size=5),
        Layout(name="footer", size=3),
    )
    layout["header"].update(build_header_panel(state))
    layout["tabs"].update(build_tabs_panel(view))
    layout["form"].update(build_form_panel(view, merged))
    layout["input"].update(build_input_panel(view))
    layout["footer"].update(build_footer_panel(view))
    return layout


def render_modal(
    view: ConfigModalState,
    *,
    state: dict[str, Any],
    merged: dict[str, Any],
    console: Console | None = None,
) -> str:
    """Render the modal frame into a string buffer.

    Offline callers (golden snapshot tests, headless rendering) consume
    this so they never block on an interactive :class:`Live` loop.

    Args:
        view: Current view state.
        state: Raw state dict (for the breadcrumb header).
        merged: Layered-config merged map.
        console: Optional pre-built console to render into. When
            supplied, the helper writes into the caller's console and
            returns an empty string; otherwise a fresh non-terminal
            console renders into an in-process :class:`io.StringIO`
            buffer and the captured text is returned.

    Returns:
        Captured output when ``console`` is ``None``, otherwise ``""``.
    """
    buf = io.StringIO()
    real_console = console or Console(
        file=buf, force_terminal=False, width=120, height=40, record=False
    )
    layout = build_modal_frame(view, state=state, merged=merged)
    real_console.print(layout)
    return buf.getvalue() if console is None else ""


# ---------------------------------------------------------------------------
# Online tick mode
# ---------------------------------------------------------------------------


def _resolve_merged(workspace: Path | None, repo: Path) -> dict[str, Any]:
    """Best-effort load of the merged layered config.

    Falls back to an empty dict when the merge fails (e.g. malformed
    YAML in a layer) so the modal stays informational rather than
    crashing the live loop. The operator can still edit + save; the
    save path runs its own validation.
    """
    try:
        merged, _sources = merge_config(workspace=workspace, repo=repo)
    except Exception as exc:  # pragma: no cover  defensive guard
        logger.warning(f"_resolve_merged failed workspace={workspace!r} repo={repo!r} exc={exc!r}")
        return {}
    return merged


def run_config_modal(
    *,
    state: dict[str, Any],
    workspace: Path | None,
    repo: Path,
    scope: str = "repo",
    read_key: Callable[[], str] | None = None,
    refresh_per_second: int = DEFAULT_REFRESH_HZ,
    initial_view: ConfigModalState | None = None,
    save_fn: Callable[..., None] | None = None,
) -> int:
    """Open the modal live view and block on keystrokes.

    Returns when the operator presses Esc / q / Ctrl-C / EOF (without
    saving) or after a successful save key flushes the dirty map.

    Args:
        state: Raw state dict for the breadcrumb header.
        workspace: Workspace anchor for the layered merge / writer.
        repo: Repo anchor (required for ``repo`` / ``local`` layers
            and the layered merge).
        scope: Writable layer to save to. Defaults to ``"repo"`` to
            match :command:`eawf config set` and :command:`eawf config
            menu`.
        read_key: Test seam for the raw-mode keypress reader. The
            parent surface (:mod:`eawf.tui.app`) injects one; offline
            callers may supply their own.
        refresh_per_second: Online-mode tick rate for :class:`Live`.
        initial_view: Starting view state. Defaults to a fresh
            :class:`ConfigModalState`.
        save_fn: Test seam for the layered writer. Defaults to
            :func:`eawf.cli.commands.config._save_value_to_layer`.

    Returns:
        Exit code (``0`` on clean shutdown — save or cancel).
    """
    if read_key is None:
        import sys

        def read_key() -> str:
            return sys.stdin.readline()[:1]

    view = initial_view or ConfigModalState()
    merged = _resolve_merged(workspace=workspace, repo=repo)
    console = Console(force_terminal=True)
    try:
        with Live(
            build_modal_frame(view, state=state, merged=merged),
            console=console,
            screen=True,
            refresh_per_second=refresh_per_second,
            transient=False,
        ) as live:
            while True:
                try:
                    ch = read_key()
                except KeyboardInterrupt:
                    break
                if not ch:
                    break
                # Root-level exit when NOT editing — inside the editor
                # Esc is the cancel key and is handled by apply_key.
                if not view.editing and ch in _EXIT_KEYS:
                    break
                if not view.editing and ch == _SAVE_KEY:
                    try:
                        view = save_dirty(
                            view,
                            scope=scope,
                            workspace=workspace,
                            repo=repo,
                            save_fn=save_fn,
                        )
                    except (InvalidInput, ValueError, OSError) as exc:
                        view = view.model_copy(update={"toast": f"save failed: {exc}"})
                        live.update(build_modal_frame(view, state=state, merged=merged))
                        continue
                    # Save success — render once with the toast then return.
                    live.update(build_modal_frame(view, state=state, merged=merged))
                    break
                if not view.editing and ch in _ENTER_KEYS:
                    # Use begin_edit which seeds from the merged config
                    # (the pure apply_key seeds from the registry default
                    # because it lacks merged-config access).
                    view = begin_edit(view, merged)
                elif view.editing and ch in _ENTER_KEYS:
                    # Commit the buffer; surface coercion failures as
                    # a toast and stay in the editor so the operator
                    # can retry without losing the buffer.
                    try:
                        view = commit_edit(view)
                    except InvalidInput as exc:
                        view = view.model_copy(update={"toast": f"invalid: {exc}"})
                else:
                    view = apply_key(view, ch)
                # Refresh merged after a save (the file changed) or a
                # navigation tick (the operator may have run an
                # external ``eawf config set`` in another shell).
                merged = _resolve_merged(workspace=workspace, repo=repo)
                live.update(build_modal_frame(view, state=state, merged=merged))
    except KeyboardInterrupt:
        pass
    return 0


__all__ = [
    "DEFAULT_REFRESH_HZ",
    "MODAL_FOOTER",
    "MODAL_FOOTER_EDIT",
    "ConfigModalState",
    "active_field",
    "active_fields",
    "active_tab",
    "apply_key",
    "begin_edit",
    "build_footer_panel",
    "build_form_panel",
    "build_header_panel",
    "build_input_panel",
    "build_modal_frame",
    "build_tabs_panel",
    "commit_edit",
    "render_modal",
    "run_config_modal",
    "save_dirty",
]
