"""The effective-settings read model: every catalog key, its layer, and the layers it overrode.

An operator looking at a setting asks two questions -- what is in force, and who set it
-- and the second one is the whole reason the stack exists: a value shown without its
layer cannot be changed with any confidence, because the layer that wins is the only
place editing it has an effect.

The merge is not repeated here. :func:`~eawf.kernel.config.layered.merge_config` is the
one engine that composes the layers, so the effective value and the winning layer are
taken from its answer; this module reads each layer's own overlay a second time only to
say which lower layers also stated the key and lost. Nothing is written: every path is
a read, which is what lets the console open the surface without touching an operator's
config.

The rows are the leaf catalog's keys, because the catalog is what the daemon lets a
layer write: a key the merge holds no value for is still a key an operator can set. A
merged leaf no catalog key covers is kept too, under no section, so the view never drops
a value that is in force.

The view carries the same header shape a route projection does, so a console holds one
kind of answer. Its digest deliberately does not cover the cursor: config is not ordered
by ``canonical_sequence``, and a digest that included the cursor would call two different
configurations equal whenever the tree had not moved between them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterable, Mapping
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, Literal

from pydantic import ConfigDict, JsonValue

from eawf.kernel.config.defaults import built_in_defaults
from eawf.kernel.config.layered import (
    LAYER_ORDER,
    WRITABLE_LAYERS,
    Layer,
    branch_config_path,
    detect_current_branch,
    global_config_path,
    local_config_path,
    merge_config,
    repo_config_path,
    workspace_config_path,
)
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.config.registry.leaf_catalog import LEAF_KEY_REGISTRY
from eawf.kernel.config.registry.leaf_keys import (
    ChoicesFrom,
    ConsumerKind,
    EditorKind,
    LeafKey,
    LeafKeyType,
)
from eawf.kernel.projection.compute import (
    PROJECTION_POLICY_REVISION,
    PROJECTION_SCHEMA_VERSION,
)
from eawf.kernel.projection.read_models import ReadModelKind
from eawf.kernel.projection.truth import (
    Completeness,
    ConnectionState,
    Freshness,
    Precision,
    ProjectionHeader,
    TruthField,
    TruthKind,
    TruthState,
)
from eawf.kernel.runtime.certification import CertificationFailureCode, CertifiedRuntimeFacts
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr
from eawf.observability.telemetry.models import RuntimeName
from eawf.platform.rules.host_facts import load_host_facts
from eawf.runtime.mcp.env_ref import ENV_REF_RE

logger = logging.getLogger(__name__)


#: The console route this view is read for. The layer-stack card is a sub-surface of it
#: rather than a second read: one config read answers both, so the card cannot disagree
#: with the list it was opened from.
SETTINGS_ROUTE: Final = "settings"

#: The sub-surface the stack card is drawn on, served by the same read.
SETTINGS_STACK_ROUTE: Final = "settings.stack"

#: Both routes this view serves, in registry order.
SETTINGS_ROUTES: Final[tuple[str, ...]] = (SETTINGS_ROUTE, SETTINGS_STACK_ROUTE)

#: What a settings view names as the producer of its leaves: the merge engine, because
#: that is what decided which layer won.
SETTINGS_PRODUCER: Final = "eawf.kernel.config.layered"

#: How a layer that holds nothing for a leaf is rendered. A leaf no layer states is not
#: a leaf with an empty value, and the console draws the difference.
UNSET_TEXT: Final = "unset"

#: Why an effective value reads as unknown: no layer states the key, not even the
#: built-in defaults, so there is no value in force to show.
UNSOURCED_REASON: Final = "no layer states this leaf"

#: The layers the lens cycles and a console edit may target, in precedence order: the
#: file layers between the built-in defaults and the runtime layers.
LENS_LAYERS: Final[tuple[Layer, ...]] = tuple(Layer(layer) for layer in WRITABLE_LAYERS)

#: The five orientation categories of the settings rail and the catalog sections each
#: holds. Nothing is configured at the category level; the table only files every
#: catalog section under exactly one heading, alphabetical at both levels.
SETTINGS_CATEGORIES: Final[tuple[tuple[str, tuple[str, ...]], ...]] = (
    # agents configures who performs work and economics what work may cost, so both
    # are filed with execution
    (
        "execution",
        (
            "agents",
            "dispatch",
            "economics",
            "flow",
            "planning",
            "research",
            "runtime",
            "ship",
        ),
    ),
    ("identity", ("preferences", "profiles")),
    ("interface", ("tui", "ui")),
    ("quality", ("audit", "estimation", "verify")),
    ("system", ("config", "daemon", "telemetry", "vcs")),
)


class LayerKind(StrEnum):
    """What kind of place a config layer is: shipped, a file, or the running process."""

    READ_ONLY = "read-only"
    FILE = "file"
    RUNTIME = "runtime"


#: Where each layer lives, as a template or a runtime name and never a machine path, so a
#: frame that shows it names the same place on every machine.
LAYER_PLACES: Final[Mapping[Layer, tuple[LayerKind, str]]] = MappingProxyType(
    {
        Layer.BUILT_IN: (LayerKind.READ_ONLY, "compiled defaults"),
        Layer.GLOBAL: (LayerKind.FILE, "~/.config/eawf/config.yaml"),
        Layer.WORKSPACE: (LayerKind.FILE, "<workspace>/.ea/config.yaml"),
        Layer.REPO: (LayerKind.FILE, "<repo>/.ea/config.yaml"),
        Layer.BRANCH: (LayerKind.FILE, "<repo>/.ea/branches/<branch>.yaml"),
        Layer.LOCAL: (LayerKind.FILE, "<repo>/.ea/local/config.yaml"),
        Layer.WAVE: (LayerKind.RUNTIME, "daemon memory · dropped on wave close"),
        Layer.ENV: (LayerKind.RUNTIME, "EAWF_SECTION__KEY"),
        Layer.CLI: (LayerKind.RUNTIME, "--flag on the invocation"),
    }
)

_ABSENT: Any = object()


class _SettingsModel(Epoch2Model):
    """Strict and immutable, like every other projection shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SettingsLayerEntry(_SettingsModel):
    """One layer's statement about one key.

    Attributes:
        layer: The layer that states the key.
        value: What the layer states for the key, as the console prints it.
        wins: Whether this is the layer whose value is in force. Exactly one entry of a
            key's stack wins; every other entry is a layer the winner overrode.
        data: What the layer itself states, as structured data, for a key whose console
            editor rebuilds the whole value from it; ``None`` for every other key, whose
            printed ``value`` is all an edit starts from.
    """

    layer: Layer
    value: NonEmptyStr
    wins: bool
    data: JsonValue = None


class SettingsLeaf(_SettingsModel):
    """One configuration key, with the layer that set it and the layers it overrode.

    Attributes:
        key: The dotted key, as ``eawf config get`` addresses it.
        section: The catalog section the key is filed under; ``None`` for a merged leaf
            no catalog key covers, which no layer write can address.
        effective: The value in force, as a truth field so a key no layer states reads
            as unknown rather than as a blank the console would draw as empty.
        source_layer: The layer whose value is in force; ``None`` when no layer states
            the key.
        override_chain: The layers that state the key and lose to ``source_layer``,
            lowest precedence first.
        editable_at: The layers a write may target for this key, in precedence order;
            empty for a locked, reserved or uncatalogued key.
        stack: Every layer that states the key, lowest precedence first, so the winner
            is the last entry and the entries before it are what it overrode.
        value_type: The catalog's value shape for the key; ``None`` outside the catalog.
        meaning: The catalog's one-line description; empty when it states none.
        allowed: The values the catalog admits for a literal key; empty otherwise.
        deny_chain: The policies that deny the key, innermost first.
        constraint_chain: The policies that constrain the key's value, innermost first.
        capability_requirement: The capability the key needs to take effect.
        certification_state: Whether that capability is certified on this runtime.
        secret_ref: The typed reference of a secret value; the value itself never
            reaches a read model.
        consumer_kind: How the catalog says the value is read; ``None`` outside the
            catalog, where nothing reads it.
        value_range: The inclusive range the catalog holds a number to, either end open;
            ``None`` when it states none.
        editor: The console editor for a list or mapping key; ``None`` otherwise.
        candidates: For a ``pin`` key, each member that may be pinned and the digest the
            console would pin for it now, read with the view.
    """

    key: NonEmptyStr
    section: NonEmptyStr | None
    effective: TruthField[str]
    source_layer: Layer | None
    override_chain: tuple[Layer, ...]
    editable_at: tuple[Layer, ...]
    stack: tuple[SettingsLayerEntry, ...]
    value_type: LeafKeyType | None = None
    meaning: str = ""
    allowed: tuple[str, ...] = ()
    deny_chain: tuple[NonEmptyStr, ...] = ()
    constraint_chain: tuple[NonEmptyStr, ...] = ()
    capability_requirement: NonEmptyStr | None = None
    certification_state: NonEmptyStr | None = None
    secret_ref: NonEmptyStr | None = None
    consumer_kind: ConsumerKind | None = None
    value_range: tuple[float | None, float | None] | None = None
    editor: EditorKind | None = None
    candidates: tuple[tuple[NonEmptyStr, NonEmptyStr], ...] = ()

    def stated_at(self, layer: Layer) -> str | None:
        """Return what ``layer`` states for the key, or ``None`` when it states nothing."""
        return next((entry.value for entry in self.stack if entry.layer is layer), None)

    def data_at(self, layer: Layer) -> JsonValue:
        """Return the structured value ``layer`` states, or ``None`` when it states none."""
        return next((entry.data for entry in self.stack if entry.layer is layer), None)

    @property
    def unread(self) -> bool:
        """Return whether no code reads the key: it is off the catalog or deprecated."""
        return self.consumer_kind in (None, "deprecated")

    def drawn_in(self) -> str | None:
        """Return the rail section the key is listed under, or ``None`` when it is not listed.

        A deprecated, reserved or uncatalogued key is listed only while a file layer still
        states it, since that statement is then the one thing left to remove; an
        uncatalogued key is listed under its first segment.
        """
        if self.consumer_kind not in (None, "deprecated", "reserved"):
            return self.section
        if not any(entry.layer in LENS_LAYERS for entry in self.stack):
            return None
        return self.section or self.key.split(".", 1)[0]


class SettingsCategory(_SettingsModel):
    """One rail heading and the catalog sections filed under it, alphabetical.

    Attributes:
        name: The category word; never selectable, because nothing is set at its level.
        sections: The catalog sections under it that the view holds keys for.
    """

    name: NonEmptyStr
    sections: tuple[NonEmptyStr, ...]


class EffectiveSettingsView(_SettingsModel):
    """The whole effective-settings read model, as the console draws it.

    Attributes:
        schema_version: The projection schema this shape is spelled in.
        route: The console route key the leaves were gathered for.
        header: The projection header, whose ``source_cursor`` is the cursor the console
            held when the config was read. Config is not ordered by that cursor; it is
            carried so one frame can state one cursor for everything it shows.
        digest: The digest two surfaces compare on. It covers the route and the leaves
            and deliberately not the cursor, because config changes without the tree.
        branch: The branch whose layer was read, which is the one a ``branch`` edit
            writes; ``None`` when the tree has no current branch.
        lens_layers: The file layers the lens cycles, in precedence order: every one of
            them unless the workspace file is the repo file, when the workspace layer is
            not a separate place to write.
        rail: The five categories and the sections each holds, in rail order.
        leaves: Every catalog key, then every merged leaf outside the catalog, by key.
    """

    schema_version: Literal["1.0"]
    route: NonEmptyStr
    header: ProjectionHeader
    digest: Sha256DigestStr
    branch: NonEmptyStr | None
    lens_layers: tuple[Layer, ...] = LENS_LAYERS
    rail: tuple[SettingsCategory, ...]
    leaves: tuple[SettingsLeaf, ...]

    def leaf(self, key: str) -> SettingsLeaf:
        """Return the leaf ``key`` addresses.

        Raises:
            KeyError: The view states no leaf under ``key``; a console asking for it is
                addressing a key that does not exist.
        """
        for leaf in self.leaves:
            if leaf.key == key:
                return leaf
        raise KeyError(key)

    def sections(self) -> tuple[str, ...]:
        """Return every rail section, in rail order: the order Tab cycles them in."""
        return tuple(section for category in self.rail for section in category.sections)

    def keys_of(self, section: str) -> tuple[SettingsLeaf, ...]:
        """Return the leaves listed under ``section``, by key; empty for an unknown one."""
        return tuple(leaf for leaf in self.leaves if leaf.drawn_in() == section)

    def category_of(self, section: str) -> str:
        """Return the category ``section`` is filed under; empty for an unknown one."""
        return next((c.name for c in self.rail if section in c.sections), "")

    def uncatalogued(self) -> tuple[SettingsLeaf, ...]:
        """Return the merged leaves no catalog key covers."""
        return tuple(leaf for leaf in self.leaves if leaf.section is None)


def render_value(value: Any) -> str:
    """Return one config value as the console prints it.

    Args:
        value: Any YAML scalar, list or mapping the merge produced.

    Returns:
        Non-empty text for every value, so an empty string and an empty list are told
        apart from a layer that states nothing at all.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return "null"
    if isinstance(value, str):
        return value if value else '""'
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(render_value(item) for item in value) + "]" if value else "[]"
    if isinstance(value, dict):
        return "{" + ", ".join(sorted(value)) + "}" if value else "{}"
    return str(value)


def _flatten(mapping: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Return ``mapping`` as dotted leaves, mirroring what the merge engine keys on."""
    flat: dict[str, Any] = {}
    for key, value in mapping.items():
        dotted = f"{prefix}.{key}" if prefix else f"{key}"
        if isinstance(value, dict) and value:
            flat.update(_flatten(value, dotted))
        else:
            flat[dotted] = value
    return flat


def _value_at(flat: Mapping[str, Any], key: str) -> Any:
    """Return what ``flat`` states at ``key``: the leaf, the subtree under it, or absent.

    A mapping-typed catalog key is stated by the leaves beneath it, so its value is the
    subtree those leaves rebuild.
    """
    if key in flat:
        return flat[key]
    prefix = f"{key}."
    below = {
        dotted[len(prefix) :]: value for dotted, value in flat.items() if dotted.startswith(prefix)
    }
    if not below:
        return _ABSENT
    tree: dict[str, Any] = {}
    for dotted, value in below.items():
        node = tree
        *parents, last = dotted.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[last] = value
    return tree


def layer_overlays(
    *,
    workspace: Path | None,
    repo: Path | None,
    branch: str | None = None,
) -> Mapping[str, Mapping[str, Any]]:
    """Return what each file layer states, keyed by layer label.

    The built-in, wave, env and cli layers are absent: they have no file, and the merge
    engine's source map already names them when one of them wins. This is a read of the
    same files the merge reads, and it writes nothing.

    Args:
        workspace: The workspace root, or ``None`` to skip the workspace layer.
        repo: The repo root, or ``None`` to skip the repo, branch and local layers.
        branch: The branch whose layer to read; resolved from the repo when ``None``.

    Returns:
        One flattened overlay per layer that has a file, layers with no file omitted.
    """
    paths: dict[str, Path] = {"global": global_config_path()}
    if workspace is not None:
        paths["workspace"] = workspace_config_path(workspace)
    if repo is not None:
        paths["repo"] = repo_config_path(repo)
        named = branch if branch is not None else detect_current_branch(repo)
        if named:
            paths["branch"] = branch_config_path(repo, named)
        paths["local"] = local_config_path(repo)
    overlays: dict[str, Mapping[str, Any]] = {}
    seen: set[Path] = set()
    # highest layer first, so a file two layers share is credited to the layer that wins
    # the merge; naming it twice would read as two layers agreeing rather than as one
    # statement, and the lower label is the one the merge never credits
    for layer, path in reversed(paths.items()):
        if not path.exists() or path in seen:
            continue
        seen.add(path)
        overlays[layer] = _flatten(load_yaml_layer(path))
    return {layer: overlays[layer] for layer in paths if layer in overlays}


def _winner(key: str, sources: Mapping[str, str]) -> Layer | None:
    """Return the highest layer the merge credits with ``key`` or any leaf beneath it."""
    prefix = f"{key}."
    credited = [
        layer for dotted, layer in sources.items() if dotted == key or dotted.startswith(prefix)
    ]
    if not credited:
        return None
    return Layer(max(credited, key=LAYER_ORDER.index))


def _stack_for(
    *,
    key: str,
    winner: Layer,
    winning_value: Any,
    overlays: Mapping[str, Mapping[str, Any]],
    structured: bool,
) -> tuple[SettingsLayerEntry, ...]:
    """Return one key's stack: every layer that states it, lowest precedence first.

    A layer above the winner is never in the stack, because a layer that stated the key
    and sits above the winner would be the winner. With ``structured`` each entry also
    carries what its own layer states, which for the winner is its overlay rather than
    the merged value, because a mapping merges with the layers below it.
    """
    entries: list[SettingsLayerEntry] = []
    for layer in LAYER_ORDER:
        overlay = overlays.get(layer)
        stated = _ABSENT if overlay is None else _value_at(overlay, key)
        if layer == winner:
            own = winning_value if stated is _ABSENT else stated
            entries.append(
                SettingsLayerEntry(
                    layer=winner,
                    value=render_value(winning_value),
                    wins=True,
                    data=own if structured else None,
                )
            )
            break
        if stated is not _ABSENT:
            entries.append(
                SettingsLayerEntry(
                    layer=Layer(layer),
                    value=render_value(stated),
                    wins=False,
                    data=stated if structured else None,
                )
            )
    return tuple(entries)


def _effective_field(*, key: str, value: Any, winner: Layer | None) -> TruthField[str]:
    """Return a key's effective value as a truth field, known or honestly missing."""
    stated = winner is not None
    return TruthField[str](
        value=render_value(value) if stated else None,
        state=TruthState.KNOWN if stated else TruthState.UNKNOWN,
        truth_kind=TruthKind.STORED,
        producer=SETTINGS_PRODUCER,
        producer_revision=PROJECTION_POLICY_REVISION,
        precision=Precision.EXACT if stated else Precision.UNAVAILABLE,
        measurement_quality=(
            MeasurementQuality.EXACT if stated else MeasurementQuality.UNAVAILABLE
        ),
        freshness=Freshness.LIVE,
        provenance_refs=(f"{SETTINGS_PRODUCER}:{key}",),
        missing_reason=None if stated else UNSOURCED_REASON,
    )


def _constraint_chain(entry: LeafKey) -> tuple[str, ...]:
    """Return the range the compiled config registry holds a key's value to, if any."""
    if entry.value_range is None:
        return ()
    low, high = entry.value_range
    if low is not None and high is not None:
        span = f"{low:g} to {high:g}"
    else:
        span = f"at least {low:g}" if low is not None else f"at most {high:g}"
    return (f"config registry range {span} · {Layer.BUILT_IN}",)


def _certification_state(runtime: RuntimeName) -> str:
    """Return whether ``runtime`` can be certified, read off its certified runtime facts.

    The conformance runner refuses to certify a runtime none of whose facts is certified,
    so that refusal is the state a key needing the runtime is in.
    """
    facts = CertifiedRuntimeFacts.from_host_facts(load_host_facts().runtime(runtime))
    if facts is None:
        return CertificationFailureCode.RUNTIME_FACTS_UNCERTIFIED.value
    return "runtime facts certified"


def _strings(value: Any) -> Iterable[str]:
    """Yield every string a config value holds, however deeply nested."""
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def _secret_ref(entry: LeafKey, value: Any) -> str | None:
    """Return the credential references a key's value names; never a credential itself."""
    if not entry.secret_refs or value is _ABSENT:
        return None
    refs = sorted({text for text in _strings(value) if ENV_REF_RE.match(text)})
    return ", ".join(refs) or None


def _leaf(
    *,
    key: str,
    entry: LeafKey | None,
    merged: Mapping[str, Any],
    sources: Mapping[str, str],
    overlays: Mapping[str, Mapping[str, Any]],
    resolved: Mapping[str, tuple[tuple[str, str], ...]],
) -> SettingsLeaf:
    """Return one key's leaf: its value in force, its stack and what the catalog says of it.

    ``resolved`` holds, per :data:`~eawf.kernel.config.registry.leaf_keys.ChoicesFrom`
    source, the members read for this view and the digest each would be pinned under.
    """
    value = _value_at(merged, key)
    winner = _winner(key, sources) if value is not _ABSENT else None
    editor = entry.editor if entry is not None else None
    stack = (
        _stack_for(
            key=key,
            winner=winner,
            winning_value=value,
            overlays=overlays,
            structured=editor in ("rows", "pin"),
        )
        if winner is not None
        else ()
    )
    writable = () if entry is None or entry.reserved else entry.writable_layers
    runtime = entry.runtime if entry is not None else None
    members = resolved.get(entry.choices_from, ()) if entry and entry.choices_from else ()
    allowed = tuple(member for member, _digest in members) or (
        (entry.choices or ()) if entry is not None else ()
    )
    return SettingsLeaf(
        key=key,
        section=entry.domain if entry is not None else None,
        effective=_effective_field(key=key, value=value, winner=winner),
        source_layer=winner,
        override_chain=tuple(item.layer for item in stack if not item.wins),
        editable_at=tuple(Layer(layer) for layer in LAYER_ORDER if layer in writable),
        stack=stack,
        value_type=entry.type if entry is not None else None,
        meaning=entry.description if entry is not None else "",
        allowed=() if editor == "pin" else allowed,
        constraint_chain=_constraint_chain(entry) if entry is not None else (),
        capability_requirement=f"{runtime} runtime" if runtime is not None else None,
        certification_state=_certification_state(runtime) if runtime is not None else None,
        secret_ref=_secret_ref(entry, value) if entry is not None else None,
        consumer_kind=entry.consumer_kind if entry is not None else None,
        value_range=entry.value_range if entry is not None else None,
        editor=editor,
        candidates=tuple(pair for pair in members if pair[1]) if editor == "pin" else (),
    )


#: The category an uncatalogued key's first segment is filed under when the table names
#: no category for it: such a key is listed only so a file's stale statement can be
#: removed, which is housekeeping of the system.
_STRAY_CATEGORY: Final = "system"


def _rail(sections: Iterable[str]) -> tuple[SettingsCategory, ...]:
    """Return the five categories, each holding the table's sections the view has keys for.

    A section the table does not file, the first segment of a listed uncatalogued key,
    goes under :data:`_STRAY_CATEGORY`.
    """
    held = set(sections)
    filed = {section for _name, members in SETTINGS_CATEGORIES for section in members}
    stray = held - filed
    return tuple(
        SettingsCategory(
            name=name,
            sections=tuple(
                s
                for s in sorted(set(members) | (stray if name == _STRAY_CATEGORY else set()))
                if s in held
            ),
        )
        for name, members in SETTINGS_CATEGORIES
    )


def _profile_members(
    source: ChoicesFrom, *, workspace: Path | None, repo: Path | None
) -> tuple[tuple[str, str], ...]:
    """Return the profiles ``source`` ranges over, each with the digest it would be pinned by.

    The profiles are those discoverable from the tree now. ``profile_trust`` pins the file
    digest of every profile outside the bundled set, which is what the trust ledger holds;
    ``profile_certification`` pins the content digest of every enriched profile, which is
    what certification compares. A profile whose file does not validate has no digest to
    pin and is left out; ``profiles`` lists every id with no digest.
    """
    from eawf.platform.profiles.certification import profile_digest
    from eawf.platform.profiles.discovery import discover_profile
    from eawf.platform.profiles.loader import list_profiles, load_profile
    from eawf.platform.profiles.trust import is_bundled, profile_sha256
    from eawf.surfaces.cli.errors import UserError, ValidationError

    ids = list_profiles(repo=repo, workspace=workspace)
    if source == "profiles":
        return tuple((pid, "") for pid in ids)
    members: list[tuple[str, str]] = []
    for pid in ids:
        if source == "profile_trust":
            path = discover_profile(pid, repo=repo, workspace=workspace).path
            if path is not None and not is_bundled(pid):
                members.append((pid, profile_sha256(path)))
            continue
        try:
            body = load_profile(pid, repo=repo, workspace=workspace)
        except (UserError, ValidationError) as error:
            logger.debug(f"_profile_members skipped={pid!r} reason={error}")
            continue
        if body.is_enriched:
            members.append((pid, profile_digest(body)))
    return tuple(members)


def _resolve_members(
    sources: Iterable[ChoicesFrom], *, workspace: Path | None, repo: Path | None
) -> dict[str, tuple[tuple[str, str], ...]]:
    """Return the members of every value set the catalog reads per view, keyed by source."""
    return {
        source: _profile_members(source, workspace=workspace, repo=repo) for source in set(sources)
    }


def _lens_layers(*, workspace: Path | None, repo: Path | None) -> tuple[Layer, ...]:
    """Return the lens's layers: every file layer, less the workspace when it is the repo file.

    The workspace and repo layers name one file when the workspace root is the repo root,
    and the daemon then writes both to it; cycling through both would offer one place
    twice under two names.
    """
    same = (
        workspace is not None
        and repo is not None
        and workspace_config_path(workspace).resolve() == repo_config_path(repo).resolve()
    )
    return tuple(layer for layer in LENS_LAYERS if not (same and layer is Layer.WORKSPACE))


def catalog_section_order() -> tuple[str, ...]:
    """Return every catalog section in rail order, the order a built view's rail lists them.

    The rail is filed from the catalog alone, so a console can place its section cursor
    before the daemon has served a view. A deprecated or reserved leaf is listed only while
    a layer states it, so a section holding nothing else has no rail entry here either.
    """
    rail = _rail(
        entry.domain
        for entry in LEAF_KEY_REGISTRY.values()
        if entry.consumer_kind not in ("deprecated", "reserved")
    )
    return tuple(section for category in rail for section in category.sections)


def build_settings_view(
    *,
    workspace: Path | None,
    repo: Path | None,
    scope_id: str,
    cursor: int,
    generated_at: datetime,
    env: Mapping[str, str] | None = None,
    branch: str | None = None,
) -> EffectiveSettingsView:
    """Return the effective settings with the layer behind every catalog key.

    Args:
        workspace: The workspace root the layers are composed against.
        repo: The repo root the layers are composed against.
        scope_id: The scope the view is stated for.
        cursor: The committed ``canonical_sequence`` the console holds; carried in the
            header so one frame states one cursor, never as part of the digest.
        generated_at: When the view was generated, so the header's two stamps agree.
        env: The environment the ``env`` layer is read from; the process environment
            when absent, and an empty mapping to leave the layer out entirely.
        branch: The branch whose layer to read; resolved from the repo when ``None``.

    Returns:
        Every catalog key, sorted, then every merged leaf outside the catalog, each with
        the layer that set it, the layers it overrode and the layers it may be edited at.

    Raises:
        ValueError: The cursor is negative, so it is not a committed ordinal.
    """
    if cursor < 0:
        raise ValueError(f"a projection cursor is a committed canonical_sequence, never {cursor}")
    named = branch if branch is not None or repo is None else detect_current_branch(repo)
    merged_tree, sources = merge_config(workspace=workspace, repo=repo, env=env, branch=named)
    merged = _flatten(merged_tree)
    overlays = {
        Layer.BUILT_IN.value: _flatten(built_in_defaults()),
        **layer_overlays(workspace=workspace, repo=repo, branch=named),
    }
    catalog = sorted(LEAF_KEY_REGISTRY)
    resolved = _resolve_members(
        (entry.choices_from for entry in LEAF_KEY_REGISTRY.values() if entry.choices_from),
        workspace=workspace,
        repo=repo,
    )
    leaves = [
        _leaf(
            key=key,
            entry=LEAF_KEY_REGISTRY[key],
            merged=merged,
            sources=sources,
            overlays=overlays,
            resolved=resolved,
        )
        for key in catalog
    ]
    # a merged leaf is covered when it is a catalog key, sits under one, or is the empty
    # parent of catalog keys that nothing has set yet
    covered = [
        dotted
        for dotted in sorted(merged)
        if not any(
            dotted == key or dotted.startswith(f"{key}.") or key.startswith(f"{dotted}.")
            for key in catalog
        )
    ]
    leaves += [
        _leaf(
            key=dotted,
            entry=None,
            merged=merged,
            sources=sources,
            overlays=overlays,
            resolved=resolved,
        )
        for dotted in covered
    ]
    logger.debug(f"build_settings_view leaves={len(leaves)} uncatalogued={len(covered)}")
    return EffectiveSettingsView(
        schema_version=PROJECTION_SCHEMA_VERSION,
        route=SETTINGS_ROUTE,
        header=ProjectionHeader(
            schema_version=PROJECTION_SCHEMA_VERSION,
            projection_kind=ReadModelKind.EFFECTIVE_SETTINGS_VIEW,
            scope_id=scope_id,
            projection_revision=cursor + 1,
            source_cursor=str(cursor),
            generated_at=generated_at,
            observed_at=generated_at,
            connection_state=ConnectionState.LIVE,
            completeness=Completeness.COMPLETE,
            freshness=Freshness.LIVE,
            producer_refs=(SETTINGS_PRODUCER,),
            policy_revision=PROJECTION_POLICY_REVISION,
        ),
        digest=_digest(leaves),
        branch=named or None,
        lens_layers=_lens_layers(workspace=workspace, repo=repo),
        rail=_rail(section for leaf in leaves if (section := leaf.drawn_in()) is not None),
        leaves=tuple(leaves),
    )


def _digest(leaves: list[SettingsLeaf]) -> str:
    """Return the digest of one settings read: the route and every leaf it states."""
    encoded = json.dumps(
        {
            "route": SETTINGS_ROUTE,
            "leaves": [leaf.model_dump(mode="json") for leaf in leaves],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


__all__ = [
    "LAYER_PLACES",
    "LENS_LAYERS",
    "SETTINGS_CATEGORIES",
    "SETTINGS_PRODUCER",
    "SETTINGS_ROUTE",
    "SETTINGS_ROUTES",
    "SETTINGS_STACK_ROUTE",
    "UNSET_TEXT",
    "UNSOURCED_REASON",
    "EffectiveSettingsView",
    "LayerKind",
    "SettingsCategory",
    "SettingsLayerEntry",
    "SettingsLeaf",
    "build_settings_view",
    "catalog_section_order",
    "layer_overlays",
    "render_value",
]
