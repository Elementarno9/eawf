"""Whole-section validation of a composed config against each section's strict model.

A leaf write is checked on its own against the registry's type, range and choices, but
some sections hold rules no single leaf states: a co-author identity needs its name and
its email together, and ``mode: project`` needs that identity. A writer composes the
config as it would stand after the write and holds every section a written leaf belongs
to against that section's model, so a write that would leave a reader unable to load its
section is refused before any file changes.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any, Final

from pydantic import BaseModel
from pydantic import ValidationError as PydValidationError

from eawf.kernel.config import layered
from eawf.kernel.config.loader import load_yaml_layer
from eawf.kernel.config.migration import migrate_config_payload
from eawf.kernel.config.registry import LEAF_KEY_REGISTRY, LeafKey
from eawf.kernel.config.registry.leaf_catalog import DEPRECATED_LEAF_KEYS
from eawf.kernel.config.schema import (
    STALL_INTERVAL_RUNTIMES,
    AgentsConfig,
    EstimationConfig,
    OperatorConfig,
    PreferencesConfig,
    RuntimeLivenessConfig,
    RuntimeModelsConfig,
    VerifyConfig,
)
from eawf.runtime.vcs.coauthor import VcsConfig

#: Each section a strict model reads, by the dotted path of its block, most specific
#: first so a leaf is held to the innermost block that owns it.
SECTION_MODELS: Final[tuple[tuple[str, type[BaseModel]], ...]] = (
    ("runtime.models", RuntimeModelsConfig),
    *((f"runtime.{runtime}", RuntimeLivenessConfig) for runtime in STALL_INTERVAL_RUNTIMES),
    ("agents", AgentsConfig),
    ("estimation", EstimationConfig),
    ("operator", OperatorConfig),
    ("preferences", PreferencesConfig),
    ("verify", VerifyConfig),
    ("vcs", VcsConfig),
)


class ConfigSectionError(ValueError):
    """A write that leaves a config section its model refuses.

    Attributes:
        section: The dotted path of the refused section's block.
        field: The dotted path of the field the model names first.
        message: The model's own message for that field.
    """

    def __init__(self, *, section: str, field: str, message: str) -> None:
        self.section = section
        self.field = field
        self.message = message
        super().__init__(f"config_section_invalid: {field}: {message} (section {section})")


def section_of(key: str) -> tuple[str, type[BaseModel]] | None:
    """Return the section block ``key`` belongs to and its model, or ``None``.

    Args:
        key: A dotted config key.
    """
    for section, model in SECTION_MODELS:
        if key == section or key.startswith(f"{section}."):
            return section, model
    return None


def _block(merged: Mapping[str, Any], section: str) -> Any:
    """Return the block at ``section`` in ``merged``; ``None`` where no layer states one."""
    node: Any = merged
    for part in section.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def check_sections(merged: Mapping[str, Any], keys: Iterable[str]) -> None:
    """Hold every section one of ``keys`` belongs to against its model in ``merged``.

    A section no layer states is left to its model's defaults, which every reader
    applies, so only a stated block is checked.

    Args:
        merged: The config composed as it stands after the write.
        keys: The dotted keys the write sets or removes.

    Raises:
        ConfigSectionError: A touched section fails its model; the error names the
            model's first failing field and its message.
    """
    touched = dict.fromkeys(found for key in keys if (found := section_of(key)) is not None)
    for section, model in touched:
        block = _block(merged, section)
        if block is None:
            continue
        try:
            model.model_validate(block)
        except PydValidationError as exc:
            first = exc.errors()[0]
            path = ".".join(str(part) for part in first["loc"])
            raise ConfigSectionError(
                section=section,
                field=f"{section}.{path}" if path else section,
                message=first["msg"],
            ) from exc


#: Python types a value of each catalog shape may hold. ``bool`` is an ``int`` subclass,
#: so the number shapes refuse it separately.
_SHAPE_TYPES: Final[dict[str, tuple[type, ...]]] = {
    "bool": (bool,),
    "int": (int,),
    "float": (int, float),
    "str": (str,),
    "list_str": (list,),
    "list_any": (list,),
    "mapping": (dict,),
}


def _catalog_blocks() -> frozenset[str]:
    """Return every dotted block that holds a catalog leaf, such as ``ui`` or ``runtime.claude``."""
    return frozenset(
        key.rsplit(".", depth)[0]
        for key in LEAF_KEY_REGISTRY
        for depth in range(1, key.count(".") + 1)
    )


def _leaf_problem(entry: LeafKey, value: Any) -> str | None:
    """Return why ``value`` is not one ``entry`` may hold, or ``None`` when it is."""
    if value is None:
        return None if entry.default is None else "must not be null"
    if entry.type == "literal":
        if entry.choices is not None and value not in entry.choices:
            return f"{value!r} is not one of {list(entry.choices)}"
        return None
    allowed = _SHAPE_TYPES.get(entry.type)
    if allowed is not None and (
        not isinstance(value, allowed) or (entry.type in {"int", "float"} and type(value) is bool)
    ):
        return f"{value!r} is not of type {entry.type}"
    if entry.type == "list_str" and not all(isinstance(item, str) for item in value):
        return f"{value!r} is not a list of strings"
    if entry.value_range is not None and entry.type in {"int", "float"}:
        low, high = entry.value_range
        if (low is not None and value < low) or (high is not None and value > high):
            return f"{value!r} is outside the range [{low}, {high}]"
    return None


def layer_leaf_problems(body: Mapping[str, Any]) -> list[str]:
    """Return every leaf of one config layer's body the leaf catalog refuses.

    A key is refused when the catalog names no leaf and no block at its path, so a
    misspelt key such as ``ui.tehme`` is reported rather than silently ignored; a
    catalogued leaf is refused when its value is not of the leaf's type, not one of its
    choices or outside its range. A value set by its block's strict model, such as a
    liveness interval's bounds, is held there by :func:`check_sections`.

    Args:
        body: One layer file's parsed body.

    Returns:
        One ``"<dotted key>: <reason>"`` line per refused leaf, in body order; empty when
        every leaf is valid.
    """
    blocks = _catalog_blocks()
    problems: list[str] = []

    def walk(node: Mapping[str, Any], prefix: str) -> None:
        for name, value in node.items():
            dotted = f"{prefix}.{name}" if prefix else str(name)
            entry = LEAF_KEY_REGISTRY.get(dotted)
            if entry is not None:
                problem = _leaf_problem(entry, value)
                if problem is not None:
                    problems.append(f"{dotted}: {problem}")
            elif dotted not in blocks:
                problems.append(f"{dotted}: unknown config key")
            elif isinstance(value, Mapping):
                walk(value, dotted)
            elif value is not None:
                problems.append(f"{dotted}: {value!r} is not a mapping")

    walk(body, "")
    return problems


def _file_layers(*, repo: Path, workspace: Path | None) -> list[tuple[str, Path]]:
    """Return each config file layer as ``(layer label, path)``, lowest precedence first."""
    layers: list[tuple[str, Path]] = [("global", layered.global_config_path())]
    if workspace is not None:
        layers.append(("workspace", layered.workspace_config_path(workspace)))
    layers.append(("repo", layered.repo_config_path(repo)))
    branch = layered.detect_current_branch(repo)
    if branch is not None:
        layers.append(("branch", layered.branch_config_path(repo, branch)))
    layers.append(("local", layered.local_config_path(repo)))
    return layers


def file_layer_problems(*, repo: Path, workspace: Path | None) -> list[str]:
    """Return every leaf a config file layer states that the leaf catalog refuses.

    Each layer is held on its own, so a bad value a later layer overrides is still
    reported against the file that states it.

    Args:
        repo: Repository root anchoring the repo, branch and local layers.
        workspace: Workspace root, or ``None`` when no workspace layer applies.

    Returns:
        One ``"<layer>: <dotted key>: <reason>"`` line per refused leaf.

    Raises:
        ValidationError: A layer file is malformed YAML or states an unknown
            schema marker.
    """
    problems: list[str] = []
    for label, path in _file_layers(repo=repo, workspace=workspace):
        body = load_yaml_layer(path)
        if not body:
            continue
        # The merge drops legacy keys on read, so they are not refused here either.
        normalised, _changed = migrate_config_payload(body)
        problems.extend(f"{label}: {problem}" for problem in layer_leaf_problems(normalised))
    return problems


def retired_leaves_stated(*, repo: Path) -> list[tuple[str, Any, str, Path]]:
    """Return each retired leaf a config file layer still states.

    The merge drops a retired leaf on read, so only the layer files show that one is
    still written down.

    Args:
        repo: Repository root anchoring the repo, branch and local layers.

    Returns:
        One ``(dotted key, value, layer label, layer path)`` per stated leaf, in layer
        then key order.

    Raises:
        ValidationError: A layer file is malformed YAML.
    """
    retired = sorted(DEPRECATED_LEAF_KEYS - LEAF_KEY_REGISTRY.keys())
    stated: list[tuple[str, Any, str, Path]] = []
    for label, path in _file_layers(repo=repo, workspace=None):
        body = load_yaml_layer(path)
        for key in retired:
            try:
                value = layered.get_dotted(body, key)
            except KeyError:
                continue
            stated.append((key, value, label, path))
    return stated


__all__ = [
    "SECTION_MODELS",
    "ConfigSectionError",
    "check_sections",
    "file_layer_problems",
    "layer_leaf_problems",
    "retired_leaves_stated",
    "section_of",
]
