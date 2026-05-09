"""Profile YAML loader for ``data/*.yaml``.

Profile bodies ship as YAML files under :mod:`eawf.profiles.data`. They are
read via :func:`importlib.resources.files` so the package works equally well
from a wheel install, an editable install, and the source tree.

Public API:

    load_profile(profile_id) -> ProfileBody     # raises InvalidInput
    list_profiles()           -> list[str]      # sorted, stable
"""

from __future__ import annotations

import functools
import logging
from importlib.resources import files
from typing import Any

import yaml
from pydantic import ValidationError

from eawf.cli.errors import InvalidInput, ValidationFailed
from eawf.profiles.models import ProfileBody

logger = logging.getLogger(__name__)


_DATA_PACKAGE: str = "eawf.profiles.data"
_YAML_SUFFIX: str = ".yaml"


@functools.cache
def list_profiles() -> tuple[str, ...]:
    """Enumerate available profile ids under ``data/``.

    Returns:
        Tuple of profile ids (YAML filename stems) in deterministic sorted
        order. The result is cached because the data directory is read-only
        at runtime — adding a new profile requires a fresh interpreter.

    The return type is a tuple (immutable) so the lru_cache contract is safe:
    callers cannot mutate the cached value. Convert to ``list`` if you need
    a list at a call site.
    """
    data = files(_DATA_PACKAGE)
    ids: list[str] = []
    for entry in data.iterdir():
        # ``entry`` is a Traversable — only files with the YAML suffix count.
        if not entry.is_file():
            continue
        name = entry.name
        if not name.endswith(_YAML_SUFFIX):
            continue
        ids.append(name.removesuffix(_YAML_SUFFIX))
    return tuple(sorted(ids))


@functools.cache
def load_profile(profile_id: str) -> ProfileBody:
    """Read and validate ``data/<profile_id>.yaml``.

    Args:
        profile_id: Profile name (must appear in :func:`list_profiles`).

    Returns:
        Parsed and Pydantic-validated :class:`ProfileBody`. Cached on first
        call so repeat reads are a dict lookup.

    Raises:
        InvalidInput: ``profile_id`` is not a known profile.
        ValidationFailed: The YAML body fails Pydantic validation (typo, wrong
            type, ``extra="forbid"`` violation). Mapped to exit-code 4 by the
            CLI surface.
    """
    if profile_id not in list_profiles():
        raise InvalidInput(f"unknown profile {profile_id!r}; choose from {list(list_profiles())}")

    data = files(_DATA_PACKAGE)
    raw = data.joinpath(f"{profile_id}{_YAML_SUFFIX}").read_text(encoding="utf-8")
    parsed: Any
    try:
        parsed = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ValidationFailed(f"profile {profile_id!r}: malformed YAML: {exc}") from exc
    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ValidationFailed(
            f"profile {profile_id!r}: top-level must be a mapping, got {type(parsed).__name__}"
        )

    try:
        body = ProfileBody.model_validate(parsed)
    except ValidationError as exc:
        raise ValidationFailed(f"profile {profile_id!r}: schema rejected: {exc}") from exc

    logger.debug(
        f"load_profile: loaded {profile_id!r} "
        f"(render_blocks={len(body.render_blocks)}, "
        f"state_keys={len(body.state_extensions.fields_required)})"
    )
    return body
