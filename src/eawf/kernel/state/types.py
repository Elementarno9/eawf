"""Shared typing helpers for eawf state models."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from pydantic.functional_validators import AfterValidator


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("datetime must be timezone-aware (got naive)")
    if value.utcoffset() != timedelta(0):
        return value.astimezone(UTC)
    return value


UtcDatetime = Annotated[datetime, AfterValidator(_ensure_utc)]
