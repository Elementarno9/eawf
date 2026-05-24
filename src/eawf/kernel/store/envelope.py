"""JSONL store record envelope.

Every record written to a store JSONL file is wrapped in this envelope.
The ``payload`` field carries the kind-specific data validated separately
via the models in ``eawf.kernel.store.kinds``.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.types import UtcDatetime


class Envelope(BaseModel):
    """Top-level JSONL store record envelope (schema v1.0)."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0"] = "1.0"
    id: Annotated[str, Field(min_length=1)]
    kind: StoreKind
    scope_id: str | None
    created_at: UtcDatetime
    updated_at: UtcDatetime | None = None
    summary: Annotated[str, Field(max_length=500)]
    payload: dict[str, Any]
    blob_refs: list[str] = Field(default_factory=list)
    artifact_ids: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _event_kind_no_updated_at(self) -> Envelope:
        if self.kind == StoreKind.EVENT and self.updated_at is not None:
            raise ValueError("event-kind envelopes must have updated_at=None")
        return self
