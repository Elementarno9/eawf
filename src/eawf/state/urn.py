"""URN parser/builder for ``urn:eawf:v1:*`` identifiers.

Grammar::

    urn:eawf:v1:<kind>:<owner>[/<id>][?=k=v&...][#fragment]

``identity()`` strips the query/fragment so equality reflects "same target".
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, quote, unquote

URN_KINDS = frozenset(
    {
        "workspace",
        "repo",
        "state",
        "artifact",
        "store",
        "blob",
        "pr",
        "commit",
        "branch",
        "secret",
    }
)
URN_VERSION = "v1"
_URN_RE = re.compile(
    r"^urn:eawf:(?P<version>v\d+):(?P<kind>[a-z]+):(?P<rest>[^?#]*)"
    r"(?:\?=(?P<query>[^#]*))?(?:#(?P<fragment>.*))?$"
)
_SLASH_KINDS = frozenset({"repo", "artifact"})


@dataclass(frozen=True)
class Urn:
    """Parsed eawf URN."""

    kind: str
    owner: str
    id: str | None = None
    query: dict[str, str] = field(default_factory=dict)
    fragment: str | None = None

    def identity(self) -> str:
        """Return the URN string without query/fragment (equality-by-target)."""
        if self.id is None:
            return f"urn:eawf:{URN_VERSION}:{self.kind}:{self.owner}"
        return f"urn:eawf:{URN_VERSION}:{self.kind}:{self.owner}/{self.id}"


def parse(raw: str) -> Urn:
    """Parse a ``urn:eawf:v1:*`` string into a :class:`Urn`.

    Raises ``ValueError`` for malformed strings, unknown kinds, or unsupported versions.
    """
    match = _URN_RE.match(raw)
    if not match:
        raise ValueError(f"not a urn:eawf URN: {raw!r}")
    if match.group("version") != URN_VERSION:
        raise ValueError(f"unsupported URN version: {raw!r}")
    kind = match.group("kind")
    if not kind or kind not in URN_KINDS:
        raise ValueError(f"unknown URN kind: {kind!r}")
    rest = match.group("rest")
    if not rest:
        raise ValueError(f"empty URN owner: {raw!r}")
    owner, sep, urn_id = rest.partition("/")
    owner = unquote(owner)
    if not owner:
        raise ValueError(f"empty URN owner: {raw!r}")
    return Urn(
        kind=kind,
        owner=owner,
        id=unquote(urn_id) if sep else None,
        query=dict(parse_qsl(match.group("query") or "", keep_blank_values=True)),
        fragment=match.group("fragment"),
    )


def build(kind: str, *, owner: str, id: str | None = None) -> str:
    """Build a ``urn:eawf:v1:<kind>:<owner>[/<id>]`` string.

    For non-``repo`` and non-``artifact`` kinds, ``id`` MUST NOT contain ``/``.

    Raises ``ValueError`` if ``kind`` is unknown, ``owner`` is empty, or ``id``
    contains a forbidden ``/``.
    """
    if kind not in URN_KINDS:
        raise ValueError(f"unknown URN kind: {kind!r}")
    if not owner:
        raise ValueError("URN owner must be non-empty")
    if id and kind not in _SLASH_KINDS and "/" in id:
        raise ValueError(f"id may not contain '/': {id!r}")
    if id is None or id == "":
        suffix = ""
    else:
        safe = "/-_.~" if kind in _SLASH_KINDS else "-_.~"
        suffix = f"/{quote(id, safe=safe)}"
    return f"urn:eawf:{URN_VERSION}:{kind}:{quote(owner, safe='-_.~')}{suffix}"


def build_from(parsed: Urn) -> str:
    """Re-emit a :class:`Urn` as a string, preserving query and fragment."""
    base = parsed.identity()
    query = "&".join(f"{k}={quote(v, safe='-_.~')}" for k, v in parsed.query.items())
    fragment = f"#{parsed.fragment}" if parsed.fragment else ""
    if query:
        return f"{base}?={query}{fragment}"
    return f"{base}{fragment}"
