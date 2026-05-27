"""Shared render-block conventions for managed documentation surfaces."""

from __future__ import annotations

from typing import Literal

RenderBlockTier = Literal["tier0", "reference"]

DEFAULT_RENDER_BLOCK_TIER: RenderBlockTier = "reference"
DEFAULT_TIER0_TOKEN_CAP = 1200

__all__ = [
    "DEFAULT_RENDER_BLOCK_TIER",
    "DEFAULT_TIER0_TOKEN_CAP",
    "RenderBlockTier",
]
