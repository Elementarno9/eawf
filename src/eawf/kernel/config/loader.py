"""Single-layer YAML loader for the layered config stack.

Each layer in :mod:`eawf.kernel.config.layered` resolves to a single YAML file path.
This module reads the file (if present) and returns the parsed mapping. Any
parse error is surfaced as :class:`eawf.cli.errors.ValidationError` so the
CLI surface maps cleanly onto exit code ``4``.

Public API:
    load_yaml_layer(path) -> dict[str, Any]
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml

from eawf.cli.errors import ValidationError

logger = logging.getLogger(__name__)


def load_yaml_layer(path: Path) -> dict[str, Any]:
    """Read and parse a YAML config file for one layer.

    Args:
        path: Absolute or repo-relative path to the layer's ``config.yaml``.

    Returns:
        Parsed mapping. An empty dict is returned when:

        - the file does not exist;
        - the file is empty (``yaml.safe_load`` returns ``None``);
        - the file contains an explicit empty document.

    Raises:
        ValidationError: When the file is unreadable due to a YAML parse
            error, or when the parsed document is not a mapping (top-level
            list/scalar/null-after-content). Mapping every malformed-input
            case to ``ValidationError`` lets ``eawf config validate`` emit
            exit-code ``4`` consistently.
    """
    if not path.exists():
        logger.debug(f"load_yaml_layer missing-file path={path}; treating as empty")
        return {}

    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValidationError(f"cannot read config file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationError(f"malformed YAML in {path}: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, dict):
        raise ValidationError(
            f"config file {path} must contain a YAML mapping at the top level, "
            f"got {type(parsed).__name__}"
        )
    # Coerce keys to strings; reject non-string keys explicitly.
    for key in parsed:
        if not isinstance(key, str):
            raise ValidationError(f"config file {path}: top-level key {key!r} is not a string")
    return parsed
