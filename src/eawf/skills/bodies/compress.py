"""``/compress`` skill body.

Mirrors the dict body emitted by
:class:`eawf.skills.compress.CompressSkill`: the before/after token
counts of a session-compression pass, the realised ratio, and the
per-runtime cache-control wiring. A missing/zero ``tokens_before`` or an
unknown ``runtime`` degrades to ``needs_user`` with a ``reason``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict


class CompressBody(BaseModel):
    """Body for ``/compress`` session-compression results."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["compression_result"] = "compression_result"
    tokens_before: int | None = None
    tokens_after: int | None = None
    ratio: float | None = None
    runtime: str | None = None
    cache_control_applied: bool | None = None
    reason: str | None = None


__all__ = ["CompressBody"]
