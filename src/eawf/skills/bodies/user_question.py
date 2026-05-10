"""Shared :class:`UserQuestion` body fragment.

When a skill terminates with ``header.status == "needs_user"`` it MUST
populate ``body.user_question`` (per `docs/architecture/envelope.md` render rules).
The strict validator (:mod:`eawf.validate.strict`) enforces this.

The shape is the 2-4-option ``AskUserQuestion`` payload from the
proposal: a prompt plus an enumerated list of options the user can pick.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator


class UserQuestionOption(BaseModel):
    """One option in a 2-4-option :class:`UserQuestion` payload.

    Attributes:
        label: Short label the runtime renders next to the option.
        description: Optional longer description shown when the runtime
            has space.
    """

    model_config = ConfigDict(extra="forbid")

    label: str = Field(min_length=1)
    description: str | None = None


class UserQuestion(BaseModel):
    """``body.user_question`` payload required for ``status=needs_user``.

    Attributes:
        question: The prompt shown to the user (one sentence; the runtime
            renders it as the title of the picker).
        options: 2-4 :class:`UserQuestionOption` entries the user can
            choose between. Validation rejects fewer than 2 or more
            than 4 options.
    """

    model_config = ConfigDict(extra="forbid")

    question: str = Field(min_length=1)
    options: list[UserQuestionOption]

    @field_validator("options")
    @classmethod
    def _check_option_count(cls, value: list[UserQuestionOption]) -> list[UserQuestionOption]:
        if not 2 <= len(value) <= 4:
            raise ValueError(f"UserQuestion.options must contain 2-4 entries; got {len(value)}")
        return value


__all__ = ["UserQuestion", "UserQuestionOption"]
