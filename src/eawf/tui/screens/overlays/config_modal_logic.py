"""Pure value logic for the registry-driven config overlay (tui).

Holds the unit-testable, Textual-free half of
:mod:`eawf.tui.screens.overlays.config_modal`: the ``Enter``-action
resolver, the current-value ladder, the bool / choice / multichoice
stagers, the value formatter, the writable-layer resolver, the
:class:`ConfigModalState` strict edit-state model, and the
layered-config save path. The :class:`ConfigModal` screen in the sibling
``config_modal`` module is a thin view that calls these and repaints; it
re-exports every public name here so external importers keep resolving
``from eawf.tui.screens.overlays.config_modal import current_value`` (and
friends) unchanged.

**Save path (AGENTS rule 4).** Saving routes through the layered-config
writer — :func:`eawf.cli.commands.config._save_value_to_layer`, the same
mutator :command:`eawf config set` uses, which proxies to the daemon. The
modal NEVER touches ``state.json``: it stages edits in an in-memory dirty
map and flushes them to the chosen writable YAML layer on ``s``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.layered import (
    WRITABLE_LAYERS,
    get_dotted,
    layer_path,
    merge_config,
)
from eawf.kernel.config.registry import (
    ConfigKey,
    coerce_and_validate,
    registry_lookup,
)

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
#: * ``"multichoice"`` — expand the row into an inline ``[X]`` / ``[ ]``
#:   checklist of the key's :attr:`ConfigKey.choices` (e.g.
#:   ``ui.dashboard_panes``): ``Space`` toggles the focused item, a second
#:   ``Enter`` commits the staged list, ``Esc`` cancels.
#: * ``"none"`` — no edit affordance; the field is surfaced read-only.
EnterAction = Literal["toggle", "cycle", "inline", "popup", "multichoice", "none"]

#: Field types edited via a text buffer (the inline :class:`Input` or the
#: popup editor) rather than toggled / cycled in place.
_SCALAR_TYPES: frozenset[str] = frozenset({"int", "float", "str"})

#: Minimum width (in cells) for the field-row key column. The per-tab key
#: column widens to the longest key actually rendered in the active tab, but
#: never narrows below this floor so short-key tabs (e.g. ``runtime``) keep a
#: stable, uncramped column position rather than collapsing tight against the
#: ``[type]`` cell.
_KEY_COL_FLOOR: int = 24


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
    if entry.type == "multichoice":
        return "multichoice"
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


def _values_equal(left: Any, right: Any) -> bool:
    """Return ``True`` when *left* and *right* are equal, list/tuple-agnostic.

    A ``multichoice`` value is staged as a ``list`` (from
    :func:`coerce_and_validate`) but its registry default / persisted form
    is a ``tuple`` — ``[] == ()`` is ``False`` in Python, so a naive ``==``
    would flag a net-unchanged empty selection as dirty. Normalising
    sequence operands to lists before comparing closes that gap without
    affecting scalar (``bool`` / ``int`` / ``float`` / ``str``) comparisons.

    Args:
        left: The staged value.
        right: The persisted value to compare against.

    Returns:
        ``True`` when the two are equal under list/tuple normalisation.
    """
    if isinstance(left, (list, tuple)) and isinstance(right, (list, tuple)):
        return list(left) == list(right)
    return bool(left == right)


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


def toggle_multichoice_item(
    entry: ConfigKey,
    merged: dict[str, Any],
    dirty: dict[str, Any],
    *,
    item: str,
) -> dict[str, Any]:
    """Return a new dirty map with *item* added to / removed from *entry*'s list.

    The inline checklist editor's space-toggle: resolves the current
    selected list via :func:`current_value`, flips *item*'s membership, and
    rebuilds the list in :attr:`ConfigKey.choices` declaration order so the
    staged value is stable regardless of toggle order. No-op (returns
    *dirty* unchanged) when *entry* is not a ``multichoice`` field or *item*
    is not one of its declared choices, so the caller can route every
    toggle through here harmlessly.

    Args:
        entry: The multichoice field to toggle an item on.
        merged: The merged config (resolves the pre-toggle selected list).
        dirty: The current staged-edit map.
        item: The choice to add (if absent) or remove (if present).

    Returns:
        A new dirty map (the input is not mutated), keyed by ``entry.key``
        with the new selected list in ``choices`` order.
    """
    if entry.type != "multichoice" or not entry.choices or item not in entry.choices:
        return dict(dirty)
    value = current_value(entry, merged, dirty)
    selected = {str(member) for member in value} if isinstance(value, (list, tuple)) else set()
    if item in selected:
        selected.discard(item)
    else:
        selected.add(item)
    ordered = [choice for choice in entry.choices if choice in selected]
    return {**dirty, entry.key: ordered}


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
            via :func:`eawf.kernel.config.layered.layer_path`).
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


__all__ = [
    "ConfigModalState",
    "EnterAction",
    "current_value",
    "cycle_choice",
    "enter_action",
    "format_value",
    "merged_config",
    "needs_popup_edit",
    "save_dirty_fields",
    "toggle_bool",
    "toggle_multichoice_item",
    "writable_layers_for",
]
