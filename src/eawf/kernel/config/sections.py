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
from typing import Any, Final

from pydantic import BaseModel
from pydantic import ValidationError as PydValidationError

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


__all__ = ["SECTION_MODELS", "ConfigSectionError", "check_sections", "section_of"]
