"""Typed provenance for a historical wave commit re-pin."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from eawf.kernel.state.models import DigestStr, ShaStr


class CommitRepinProvenance(BaseModel):
    """Immutable proof that a historical pin moved without re-running work."""

    model_config = ConfigDict(extra="forbid")

    wave_id: str
    old_commit: Annotated[str, Field(pattern=r"^[0-9a-f]{7,40}$")] | None
    new_commit: ShaStr
    commit_identity_digest: DigestStr | None = None
    disposition: Literal["historical_repin"] = "historical_repin"
    basis: Literal[
        "semantic_identity",
        "unique_first_parent",
        "unique_legacy_title_match",
    ]
    status: Literal["planned", "applied"]
    repaired_at: datetime


__all__ = ["CommitRepinProvenance"]
