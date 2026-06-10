"""``/wave-spec`` skill body.

Mirrors the dict body emitted by
:class:`eawf.workflow.skills.wave_spec.WaveSpecSkill`: an ``init|validate``
WaveSpec operation intent for a wave, plus the optional Mockup-waiver
reason and the optional captured-mockup golden path threaded through. A
missing ``wave_id`` degrades to ``needs_user`` with a ``reason``.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

WaveSpecVerb = Literal["init", "validate"]


class WaveSpecBody(BaseModel):
    """Body for ``/wave-spec`` operations.

    ``mockup_golden_path`` is the repo-relative path to the approved ASCII
    golden captured from the operator's plan-time ``/mockup`` pick (e.g.
    ``tests/snapshots/tui/golden/mockup_<wave-id>.txt``). It is the
    pick-time oracle a later close gate diffs the built screen against, so
    the implementing wave cannot author the golden it is graded against.
    Kept permissive (default ``None``) so legacy bodies validate unchanged.
    """

    model_config = ConfigDict(extra="forbid")

    kind: Literal["wave_spec_operation"] = "wave_spec_operation"
    verb: WaveSpecVerb
    wave_id: str | None = None
    mockup_waiver_reason: str | None = None
    mockup_golden_path: str | None = None
    reason: str | None = None


__all__ = ["WaveSpecBody", "WaveSpecVerb"]
