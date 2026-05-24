"""ResearchPayload — payload model for StoreKind.RESEARCH records."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from eawf.platform.artifacts.references import Citation, validate_dense_citations


class ResearchPayload(BaseModel):
    """Payload for a research store record."""

    model_config = ConfigDict(extra="forbid")

    topic: str
    findings: list[str]
    references: list[Citation] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _migrate_sources(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        raw = dict(data)
        sources = raw.pop("sources", None)
        if "references" not in raw and sources is not None:
            if not isinstance(sources, list):
                raw["references"] = sources
                return raw
            raw["references"] = [
                Citation.from_legacy_source(i, str(source)).model_dump(mode="json")
                for i, source in enumerate(sources, start=1)
            ]
        return raw

    @model_validator(mode="after")
    def _references_are_dense(self) -> ResearchPayload:
        validate_dense_citations(self.references)
        return self

    @property
    def sources(self) -> list[str]:
        """Legacy read-only projection of ``references[*].ref``."""
        return [citation.ref for citation in self.references]
