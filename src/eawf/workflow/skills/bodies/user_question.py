"""Shared :class:`UserQuestion` body fragment.

When a skill terminates with ``header.status == "needs_user"`` it MUST
populate ``body.user_question`` (per `docs/architecture/envelope.md` render rules).
The strict validator (:mod:`eawf.kernel.validate.strict`) enforces this.

The shape is the 2-4-option ``AskUserQuestion`` payload from the
proposal: a prompt plus an enumerated list of options the user can pick.

A question presented from a durable pending action is *bound*: it names
the action and the revision it was read at, and every option names the
persisted option id the host answer is sealed with. The label is what the
operator reads; the id is what the seal verb accepts, so a free-text reply
has nothing to bind to and is never consent.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from eawf.kernel.state.enums import Urgency


class UserQuestionOption(BaseModel):
    """One option in a 2-4-option :class:`UserQuestion` payload.

    Attributes:
        label: Short label the runtime renders next to the option.
        description: Optional longer description shown when the runtime
            has space.
        preview: Optional multi-line markdown/ASCII content the runtime
            renders in a side-by-side monospace box when the operator
            compares options. Mirrors the harness ``AskUserQuestion``
            ``preview`` semantics (multi-line markdown, single-select
            only); a skill that surfaces UI mockups populates this with
            the rendered mockup variant for the option.
        option_id: The persisted option id a bound question's answer is
            sealed with; ``None`` on a question no pending action backs.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    description: str | None = None
    preview: str | None = None
    option_id: str | None = None


class UserQuestion(BaseModel):
    """``body.user_question`` payload required for ``status=needs_user``.

    Attributes:
        question: The prompt shown to the user (one sentence; the runtime
            renders it as the title of the picker).
        options: 2-4 :class:`UserQuestionOption` entries the user can
            choose between. Validation rejects fewer than 2 or more
            than 4 options.
        urgency: Where the question sits on the shared closed
            :class:`~eawf.kernel.state.enums.Urgency` ladder -- how soon the
            operator needs to act on it. The balanced-autonomy interrupt
            surfaces only a genuine fork above the routine prompts, so a
            :attr:`~eawf.kernel.state.enums.Urgency.URGENT` (blocking) question
            ranks above a :attr:`~eawf.kernel.state.enums.Urgency.NORMAL`
            (routine) one in the attention feed. Defaults to
            :attr:`~eawf.kernel.state.enums.Urgency.NORMAL` so an
            ordinarily-surfaced prompt -- and every legacy question that
            predates the field -- ranks as routine unless the author escalates
            it.
        action_ref: The pending action the question is bound to, or
            ``None`` when no pending action backs it.
        action_revision: The revision the action was presented at, which
            the answer is sealed against.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    options: list[UserQuestionOption]
    urgency: Urgency = Urgency.NORMAL
    action_ref: str | None = None
    action_revision: int | None = Field(default=None, gt=0)

    @field_validator("options")
    @classmethod
    def _check_option_count(cls, value: list[UserQuestionOption]) -> list[UserQuestionOption]:
        if not 2 <= len(value) <= 4:
            raise ValueError(f"options must contain 2-4 entries; got {len(value)}")
        return value

    @model_validator(mode="after")
    def _binding_is_whole(self) -> Self:
        """Require a bound question to name its revision and one distinct id per option.

        Raises:
            ValueError: The action and its revision are not given together,
                or a bound option carries no id or repeats one.
        """
        if (self.action_ref is None) != (self.action_revision is None):
            raise ValueError("action_ref and action_revision are given together or not at all")
        if self.action_ref is None:
            return self
        ids = [option.option_id for option in self.options]
        if None in ids or len(set(ids)) != len(ids):
            raise ValueError("every option of a bound question names its own option_id")
        return self


__all__ = ["UserQuestion", "UserQuestionOption"]
