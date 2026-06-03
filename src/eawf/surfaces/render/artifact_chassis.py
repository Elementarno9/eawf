"""Shared markdown chassis for durable artifacts."""

from __future__ import annotations

from collections.abc import Iterable

from eawf.platform.artifacts.references import Citation


def reference_anchor(n: int) -> str:
    """Return the stable in-document anchor id for citation *n*.

    The anchor is the link target a ``[N]`` self-link (in the row) and an
    inline ``[N]`` citation marker (rewritten by
    :func:`eawf.surfaces.render.link_wrap.linkify_citations`) both point at,
    so a reader can fast-travel from any inline marker to its row.

    Args:
        n: The dense citation number (``>= 1``).

    Returns:
        The anchor id, e.g. ``ref-3`` for ``n == 3``.
    """
    return f"ref-{n}"


def render_references(references: Iterable[Citation]) -> list[str]:
    """Render dense citations as a numbered, anchored, self-linking list.

    Each row is an ordered-list item carrying a stable ``#ref-N`` anchor
    (an inline ``<a id="ref-N"></a>`` span markdown viewers honour as a
    jump target) and a ``[N]`` self-link back to that anchor, so an inline
    ``[N]`` citation marker can link to its row and the row marks where the
    jump lands. The validator's ``_REFERENCE_ROW_RE`` round-trips this shape
    (and still accepts legacy bare-``[N]`` rows).

    Args:
        references: The dense citation rows, in number order.

    Returns:
        The ``## References`` heading, a blank line, then one ordered-list
        row per citation (or ``(none)`` when there are no rows).
    """
    rows = list(references)
    if not rows:
        return ["## References", "", "(none)"]
    lines = ["## References", ""]
    for citation in rows:
        title = f" — {citation.title}" if citation.title else ""
        note = f" ({citation.note})" if citation.note else ""
        anchor = reference_anchor(citation.n)
        self_link = rf"[\[{citation.n}\]](#{anchor})"
        lines.append(f'{citation.n}. <a id="{anchor}"></a>{self_link} {citation.ref}{title}{note}')
    return lines


def render_provenance(*, kind: str, record_id: str, scope_id: str | None) -> list[str]:
    """Render provenance block for a store-backed artifact."""
    return [
        "## Provenance",
        "",
        f"- kind: {kind}",
        f"- record_id: {record_id}",
        f"- scope_id: {scope_id or '-'}",
    ]


def render_scrub_status(*, status: str = "clean") -> list[str]:
    """Render scrub block."""
    return ["## Scrub", "", f"- status: {status}"]
