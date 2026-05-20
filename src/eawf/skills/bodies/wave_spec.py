"""``/wave-spec`` skill body (C04b §5.5).

Mirrors the dict body emitted by
:class:`eawf.skills.wave_spec.WaveSpecSkill`: an ``init|validate``
WaveSpec operation intent for a wave, plus the optional Mockup-waiver
reason (C03 D11) threaded through for non-UI waves. A missing
``wave_id`` degrades to ``needs_user`` with a ``reason``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

WaveSpecVerb = Literal["init", "validate"]


class WaveSpecBody(BaseModel):
    """Body for ``/wave-spec`` operations."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["wave_spec_operation"] = "wave_spec_operation"
    verb: WaveSpecVerb
    wave_id: str | None = None
    mockup_waiver_reason: str | None = None
    reason: str | None = None


__all__ = ["WaveSpecBody", "WaveSpecVerb"]
