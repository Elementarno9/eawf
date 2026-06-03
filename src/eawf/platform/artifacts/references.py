"""Citation model and dense-reference validation."""

from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from eawf.kernel.state.urn import parse as parse_urn

CitationKind = Literal["repo", "url", "urn"]

#: Inline dense-citation marker grammar. Matches the bare ``[N]`` marker and
#: the render-time linkified ``[\[N\]](#ref-N)`` form
#: (:func:`eawf.surfaces.render.link_wrap.linkify_citations`) so the same
#: marker is counted once whether or not it has been turned into an anchor
#: link. The ``(?<!\!)`` guard keeps image alt-text (``![1](img.png)``) from
#: counting as a citation.
_CITATION_REF_RE = re.compile(
    r"(?<!\!)"
    r"(?:\[\\\[(?P<n_link>[1-9][0-9]*)\\\]\]\(#ref-\d+\)"
    r"|\[(?P<n>[1-9][0-9]*)\])"
)
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


class CitationValidationError(ValueError):
    """Raised when citation numbering or references are invalid."""


class Citation(BaseModel):
    """Dense-numbered reference used by typed artifact payloads."""

    model_config = ConfigDict(extra="forbid")

    n: Annotated[int, Field(ge=1)]
    ref: Annotated[str, Field(min_length=1)]
    kind: CitationKind = "repo"
    title: str | None = None
    note: str | None = None

    @field_validator("ref")
    @classmethod
    def _ref_must_be_portable(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme == "file"
            or PurePosixPath(value).is_absolute()
            or PureWindowsPath(value).is_absolute()
            or value.startswith("~/")
        ):
            raise ValueError("citation ref must be repo-relative, URL, or Eawf URN")
        if parsed.scheme in {"http", "https"} and (parsed.hostname or "") in _LOCAL_HOSTS:
            raise ValueError("citation URL must not point at a local host")
        return value

    @field_validator("kind")
    @classmethod
    def _kind_matches_ref(cls, value: CitationKind, info: object) -> CitationKind:
        del info
        return value

    def validate_kind(self) -> None:
        """Validate ``kind`` against ``ref`` after construction."""
        parsed = urlsplit(self.ref)
        if self.kind == "url" and parsed.scheme not in {"http", "https"}:
            raise CitationValidationError(f"citation {self.n} kind=url needs http(s) ref")
        if self.kind == "urn":
            try:
                parse_urn(self.ref)
            except ValueError as exc:
                raise CitationValidationError(f"citation {self.n} has invalid URN") from exc
        if self.kind == "repo" and parsed.scheme:
            raise CitationValidationError(f"citation {self.n} kind=repo must not have scheme")

    @classmethod
    def from_legacy_source(cls, n: int, source: str) -> Citation:
        """Build a citation from legacy ``ResearchPayload.sources`` strings."""
        parsed = urlsplit(source)
        if parsed.scheme in {"http", "https"}:
            kind: CitationKind = "url"
        elif source.startswith("urn:eawf:"):
            kind = "urn"
        else:
            kind = "repo"
        return cls(n=n, ref=source, kind=kind)


def validate_dense_citations(citations: list[Citation]) -> None:
    """Require citation numbers to be exactly ``1..N`` without gaps."""
    for citation in citations:
        citation.validate_kind()
    expected = list(range(1, len(citations) + 1))
    actual = sorted(c.n for c in citations)
    if actual != expected:
        raise CitationValidationError(
            f"citation numbers must be dense 1..{len(citations)}; got {actual}"
        )


def citation_numbers_in_text(text: str) -> list[int]:
    """Return citation numbers referenced in markdown prose.

    Counts both the bare ``[N]`` marker and the render-time linkified
    ``[\\[N\\]](#ref-N)`` form, so dense-citation validation passes equally
    on a pre-render body and on one whose markers have been turned into
    anchor links.

    Args:
        text: The markdown prose to scan.

    Returns:
        The cited numbers in document order (one entry per marker).
    """
    return [
        int(match.group("n") or match.group("n_link")) for match in _CITATION_REF_RE.finditer(text)
    ]


def validate_dense_citation_refs(text: str, citations: list[Citation]) -> None:
    """Validate prose references against dense citation rows."""
    validate_dense_citations(citations)
    available = {citation.n for citation in citations}
    used = set(citation_numbers_in_text(text))
    missing = sorted(used - available)
    if missing:
        raise CitationValidationError(f"citation references missing rows: {missing}")
    if citations and used != available:
        unused = sorted(available - used)
        raise CitationValidationError(f"citation rows unused by prose: {unused}")
