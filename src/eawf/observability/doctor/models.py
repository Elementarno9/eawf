"""Typed result models shared by doctor checks and renderers."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

CheckStatus = Literal["ok", "warn", "fail"]


class CheckResult(BaseModel):
    """Single doctor check outcome.

    Attributes:
        name: Stable machine identifier (``"tools_available"``, ...).
        status: ``ok`` (everything fine), ``warn`` (functional but degraded),
            or ``fail`` (broken — the doctor surface still completes, but the
            CLI exits non-zero).
        detail: Short human message. ``None`` when the check has nothing
            interesting to add beyond ``status``.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    status: CheckStatus
    detail: str | None = None


__all__ = ["CheckResult", "CheckStatus"]
