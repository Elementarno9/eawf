"""Repo-wide active-wave capacity resolution for claim and fleet boundaries."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Final

from pydantic import Field, TypeAdapter

from eawf.kernel.config.defaults import BUILT_IN_DEFAULTS
from eawf.kernel.config.layered import get_dotted, merge_config

DEFAULT_MAX_PARALLEL_WAVES: Final[int] = int(BUILT_IN_DEFAULTS["planning"]["max_parallel_waves"])

_MAX_PARALLEL_WAVES_ADAPTER: Final[TypeAdapter[int]] = TypeAdapter(
    Annotated[int, Field(strict=True, ge=1, le=16)]
)


def resolve_max_parallel_waves(repo_root: Path | None) -> int:
    """Return the effective repo-wide active-wave cap.

    The built-in value remains four when no repository root is available.
    Callers that mutate lifecycle state invoke this resolver while holding the
    canonical state lock, so a request payload cannot weaken the configured
    cap between the read and the claim/fleet write.

    Args:
        repo_root: Repository root supplying the layered configuration, or
            ``None`` for the compatibility default.

    Returns:
        The validated ``planning.max_parallel_waves`` value.

    Raises:
        pydantic.ValidationError: When the resolved leaf is not an integer in
            the supported ``1..16`` range.
    """
    if repo_root is None:
        return DEFAULT_MAX_PARALLEL_WAVES
    merged, _sources = merge_config(workspace=repo_root, repo=repo_root)
    raw = get_dotted(merged, "planning.max_parallel_waves")
    return _MAX_PARALLEL_WAVES_ADAPTER.validate_python(raw)


__all__ = ["DEFAULT_MAX_PARALLEL_WAVES", "resolve_max_parallel_waves"]
