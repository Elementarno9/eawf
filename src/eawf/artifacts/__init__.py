"""Typed artifact helpers."""

from __future__ import annotations

from eawf.artifacts.references import (
    Citation,
    CitationValidationError,
    citation_numbers_in_text,
    validate_dense_citation_refs,
    validate_dense_citations,
)

__all__ = [
    "Citation",
    "CitationValidationError",
    "citation_numbers_in_text",
    "validate_dense_citation_refs",
    "validate_dense_citations",
]
