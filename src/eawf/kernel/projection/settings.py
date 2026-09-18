"""The effective-settings read model: every leaf, its layer, and the layers it overrode.

An operator looking at a setting asks two questions -- what is in force, and who set it
-- and the second one is the whole reason the stack exists: a value shown without its
layer cannot be changed with any confidence, because the layer that wins is the only
place editing it has an effect.

The merge is not repeated here. :func:`~eawf.kernel.config.layered.merge_config` is the
one engine that composes the layers, so the effective value and the winning layer are
taken from its answer; this module reads each layer's own overlay a second time only to
say which lower layers also stated the leaf and lost. Nothing is written: every path is
a read, which is what lets the console open the surface without touching an operator's
config.

The view carries the same header shape a route projection does, so a console holds one
kind of answer. Its digest deliberately does not cover the cursor: config is not ordered
by ``canonical_sequence``, and a digest that included the cursor would call two different
configurations equal whenever the tree had not moved between them.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Final, Literal

from pydantic import ConfigDict

from eawf.kernel.config.layered import (
    LAYER_ORDER,
    branch_config_path,
    detect_current_branch,
    global_config_path,
    local_config_path,
    merge_config,
    repo_config_path,
    workspace_config_path,
)
from eawf.kernel.config.loader import load_yaml_layer
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
from eawf.kernel.spec.release import Sha256DigestStr
from eawf.kernel.state.enums import MeasurementQuality
from eawf.kernel.state.epoch2.base import Epoch2Model, NonEmptyStr

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

#: Why an effective value reads as unknown. Only a leaf the merge engine placed with no
#: layer behind it can reach this, which is a defect in the source map rather than a
#: configuration an operator wrote.
UNSOURCED_REASON: Final = "no layer states this leaf"


class _SettingsModel(Epoch2Model):
    """Strict and immutable, like every other projection shape."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class SettingsLayerEntry(_SettingsModel):
    """One layer's statement about one leaf.

    Attributes:
        layer: The canonical layer label, one of
            :data:`~eawf.kernel.config.layered.LAYER_ORDER`.
        value: What the layer states for the leaf, as the console prints it.
        wins: Whether this is the layer whose value is in force. Exactly one entry of a
            leaf's stack wins; every other entry is a layer the winner overrode.
    """

    layer: NonEmptyStr
    value: NonEmptyStr
    wins: bool


class SettingsLeaf(_SettingsModel):
    """One configuration leaf, with the layer that set it and the layers it overrode.

    Attributes:
        key: The dotted key, as ``eawf config get`` addresses it.
        effective: The value in force, as a truth field so a leaf with no layer behind
            it reads as unknown rather than as a blank the console would draw as empty.
        winning_layer: The layer whose value is in force.
        stack: Every layer that states the leaf, lowest precedence first, so the winner
            is the last entry and the entries before it are what it overrode.
    """

    key: NonEmptyStr
    effective: TruthField[str]
    winning_layer: NonEmptyStr
    stack: tuple[SettingsLayerEntry, ...]

    def overridden(self) -> tuple[str, ...]:
        """Return the layers that state the leaf and lose, lowest precedence first."""
        return tuple(entry.layer for entry in self.stack if not entry.wins)


class SettingsView(_SettingsModel):
    """The whole effective-settings read model, as the console draws it.

    Attributes:
        schema_version: The projection schema this shape is spelled in.
        route: The console route key the leaves were gathered for.
        header: The projection header, whose ``source_cursor`` is the cursor the console
            held when the config was read. Config is not ordered by that cursor; it is
            carried so one frame can state one cursor for everything it shows.
        digest: The digest two surfaces compare on. It covers the route and the leaves
            and deliberately not the cursor, because config changes without the tree.
        leaves: Every leaf the merge placed, by dotted key.
    """

    schema_version: Literal["1.0"]
    route: NonEmptyStr
    header: ProjectionHeader
    digest: Sha256DigestStr
    leaves: tuple[SettingsLeaf, ...]

    def leaf(self, key: str) -> SettingsLeaf:
        """Return the leaf ``key`` addresses.

        Raises:
            KeyError: The merge placed no leaf under ``key``, so the view never stated
                one; a console asking for it is addressing a key that does not exist.
        """
        for leaf in self.leaves:
            if leaf.key == key:
                return leaf
        raise KeyError(key)

    def sections(self) -> tuple[str, ...]:
        """Return the first segment of every leaf key, in first-seen order."""
        seen: dict[str, None] = {}
        for leaf in self.leaves:
            seen.setdefault(leaf.key.split(".", 1)[0], None)
        return tuple(seen)


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
    if isinstance(value, list):
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


def _stack_for(
    *,
    key: str,
    winner: str,
    winning_value: Any,
    overlays: Mapping[str, Mapping[str, Any]],
) -> tuple[SettingsLayerEntry, ...]:
    """Return one leaf's stack: every layer that states it, lowest precedence first.

    A layer above the winner is never in the stack, because a layer that stated the leaf
    and sits above the winner would be the winner.
    """
    entries: list[SettingsLayerEntry] = []
    for layer in LAYER_ORDER:
        if layer == winner:
            break
        overlay = overlays.get(layer)
        if overlay is not None and key in overlay:
            entries.append(
                SettingsLayerEntry(layer=layer, value=render_value(overlay[key]), wins=False)
            )
    entries.append(SettingsLayerEntry(layer=winner, value=render_value(winning_value), wins=True))
    return tuple(entries)


def _effective_field(*, key: str, value: Any, winner: str | None) -> TruthField[str]:
    """Return a leaf's effective value as a truth field, known or honestly missing."""
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


def build_settings_view(
    *,
    workspace: Path | None,
    repo: Path | None,
    scope_id: str,
    cursor: int,
    generated_at: datetime,
    env: Mapping[str, str] | None = None,
    branch: str | None = None,
) -> SettingsView:
    """Return the effective settings with the layer behind every leaf.

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
        Every leaf the merge placed, sorted by dotted key, each with the layer that set
        it and the layers it overrode.

    Raises:
        ValueError: The cursor is negative, so it is not a committed ordinal.
    """
    if cursor < 0:
        raise ValueError(f"a projection cursor is a committed canonical_sequence, never {cursor}")
    merged, sources = merge_config(workspace=workspace, repo=repo, env=env, branch=branch)
    overlays = layer_overlays(workspace=workspace, repo=repo, branch=branch)
    leaves: list[SettingsLeaf] = []
    for key, value in sorted(_flatten(merged).items()):
        winner = sources.get(key)
        leaves.append(
            SettingsLeaf(
                key=key,
                effective=_effective_field(key=key, value=value, winner=winner),
                # a leaf the source map does not name is one no layer claims; it keeps
                # the built-in label so the row still addresses a layer an operator knows
                winning_layer=winner or LAYER_ORDER[0],
                stack=_stack_for(
                    key=key,
                    winner=winner or LAYER_ORDER[0],
                    winning_value=value,
                    overlays=overlays,
                ),
            )
        )
    logger.debug(f"build_settings_view leaves={len(leaves)} layers={len(overlays)}")
    return SettingsView(
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
    "SETTINGS_PRODUCER",
    "SETTINGS_ROUTE",
    "SETTINGS_ROUTES",
    "SETTINGS_STACK_ROUTE",
    "UNSET_TEXT",
    "UNSOURCED_REASON",
    "SettingsLayerEntry",
    "SettingsLeaf",
    "SettingsView",
    "build_settings_view",
    "layer_overlays",
    "render_value",
]
