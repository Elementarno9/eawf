"""``/mockup`` skill body.

``/mockup`` helps the model author UI mockups (ASCII layouts) and
surface them as side-by-side ``AskUserQuestion`` option previews. The
body holds the produced mockup variants plus an optional
:class:`UserQuestion` whose options carry the per-variant ``preview``
content so an operator can compare 2-4 concrete layouts and pick one.
The skill is advisory: it produces mockups and drives no state
mutation.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from eawf.workflow.skills.bodies.user_question import UserQuestion


class MockupVariant(BaseModel):
    """One candidate UI mockup the model authored.

    Attributes:
        name: Short label for the variant (e.g. ``compact`` or
            ``two-column``); reused as the matching option label when the
            variant is surfaced through a :class:`UserQuestion`.
        layout: The ASCII-art layout (multi-line). Box-drawing uses plain
            ASCII characters so the render stays lint-clean.
        notes: Optional one-line rationale for the variant.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    layout: str = Field(min_length=1)
    notes: str | None = None


class MockupBody(BaseModel):
    """Body for ``/mockup``.

    Attributes:
        target_scope: Optional Eä state-scope URN the mockups inform.
        variants: The 2-4 authored mockup variants.
        user_question: Optional :class:`UserQuestion` whose options carry
            each variant's ``preview`` so the operator compares layouts
            side by side. ``None`` when the skill only emits the variants
            without an operator decision.
    """

    model_config = ConfigDict(extra="forbid")

    target_scope: str | None = None
    variants: list[MockupVariant] = Field(default_factory=list)
    user_question: UserQuestion | None = None


__all__ = ["MockupBody", "MockupVariant"]
