"""Collapse hard-wrapped Markdown prose into one line per paragraph.

Rendered Markdown is not manually line-wrapped (the ``markdown-no-manual-wrap``
rule, enforced by the EAWF014 lint and the strict ``validate-prose`` gate), but
the Python string literals the renderers carry their bodies in *are* wrapped —
they have to be, to stay inside the source line-length budget. This module owns
the one place that reconciles the two: the render paths pass their literal
through :func:`unwrap_markdown_paragraphs` on the way out, so the authored
source stays readable at 100 columns while the emitted artifact carries one
line per paragraph.

Structure is preserved verbatim: fenced code blocks, headings, blockquotes,
tables, HTML comments, directives, and list markers all keep their own line.
Only consecutive plain-prose lines are joined, plus indented continuation lines
that belong to the list item above them.
"""

from __future__ import annotations

import re

#: A bullet or ordered-list marker at the head of a line. The digit run is
#: matched whole so a list longer than nine entries is not mistaken for prose.
_LIST_ITEM_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")

#: Line-leading tokens that make a line structural rather than prose.
_STRUCTURAL_PREFIXES = ("#", ">", "|", "<!--", "::")


def is_fence(line: str) -> bool:
    """Return whether *line* opens or closes a Markdown fence.

    Args:
        line: One raw Markdown line.

    Returns:
        ``True`` when the stripped line starts a ``` or ``~~~`` fence.
    """
    return line.strip().startswith(("```", "~~~"))


def is_structural_markdown(line: str) -> bool:
    """Return whether *line* must remain a standalone Markdown line.

    Args:
        line: One raw Markdown line.

    Returns:
        ``True`` for blank lines, headings, blockquotes, table rows, HTML
        comments, directives, list items, and fences; ``False`` for prose.
    """
    stripped = line.strip()
    if not stripped:
        return True
    if stripped.startswith(_STRUCTURAL_PREFIXES):
        return True
    if _LIST_ITEM_RE.match(line):
        return True
    return is_fence(line)


def _can_join_to_previous_list_line(previous: str, current: str) -> bool:
    """Return whether *current* is an indented continuation of a list item."""
    if not current.startswith(" "):
        return False
    stripped = current.strip()
    if not stripped or is_structural_markdown(current):
        return False
    return bool(_LIST_ITEM_RE.match(previous))


def unwrap_markdown_paragraphs(body: str) -> str:
    """Collapse hard-wrapped prose in *body* while preserving Markdown structure.

    Args:
        body: Markdown text whose prose paragraphs may be hard-wrapped across
            several source lines.

    Returns:
        The same Markdown with each prose paragraph on exactly one line and
        each list item folded onto its marker line. Fenced blocks, headings,
        blockquotes, tables, and directives are emitted verbatim. A trailing
        newline is not re-added; the caller owns the file-final newline.
    """
    output: list[str] = []
    paragraph: list[str] = []
    in_fence = False

    def flush_paragraph() -> None:
        if paragraph:
            output.append(" ".join(paragraph))
            paragraph.clear()

    for raw_line in body.rstrip("\n").splitlines():
        line = raw_line.rstrip()
        stripped = line.strip()

        if is_fence(line):
            flush_paragraph()
            output.append(line)
            in_fence = not in_fence
            continue
        if in_fence:
            output.append(line)
            continue
        if not stripped:
            flush_paragraph()
            output.append("")
            continue
        if output and _can_join_to_previous_list_line(output[-1], line):
            output[-1] = f"{output[-1]} {stripped}"
            continue
        if is_structural_markdown(line):
            flush_paragraph()
            output.append(line)
            continue
        paragraph.append(stripped)

    flush_paragraph()
    return "\n".join(output)


__all__ = [
    "is_fence",
    "is_structural_markdown",
    "unwrap_markdown_paragraphs",
]
