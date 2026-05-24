"""Shared Textual content-markup helpers for the status / git panes.

The pane widgets render plain ``label: value`` lines that must be
markup-escaped before they reach Textual's content-markup parser — a git
commit subject such as ``[P27-W30] fix`` would otherwise be parsed as a
style tag and dropped from the rendered line — and want their leading
``label:`` token tinted with the theme accent, matching the detail
modal's ``[$accent]label[/] value`` rows
(:meth:`eawf.tui.screens.overlays.detail.DetailModal._compose_pane`).

Keeping the escape + label-tint in one place lets both panes share the
same ``label:`` convention and the same literal-bracket guarantee.
"""

from __future__ import annotations

import re

#: A leading ``label:`` token: a lowercase word (``branch`` / ``progress``)
#: ending in a colon, then its (possibly empty) value. Anchored at the
#: start so a colon inside a value (an ahead/behind ``+2 / -0``) never
#: matches, and lowercase-only so an uppercase band header (``NOW``) or a
#: section header is left to its own styling branch.
_LABEL_RE = re.compile(r"^([a-z][\w-]*:)(\s*)(.*)$")


def escape_markup(text: str) -> str:
    """Backslash-escape ``[`` so Textual renders *text* literally.

    Textual content markup treats ``[...]`` as a style tag, so a git
    commit subject such as ``[P27-W30] fix`` would be parsed as a tag and
    dropped from the rendered line. ``[[``-doubling renders literally in
    this dialect (it does not collapse to one ``[``), and
    :func:`textual.markup.escape` skips bracket runs it does not recognise
    as tags, so escape every ``[`` with a backslash — the dialect's
    literal-bracket escape.

    Args:
        text: The raw text to render literally.

    Returns:
        *text* with every ``[`` backslash-escaped.
    """
    return text.replace("[", "\\[")


def style_labeled_line(line: str, *, style: str = "$accent") -> str:
    """Tint a leading ``label:`` token *style*; escape the rest literally.

    Mirrors the detail modal's ``[$accent]label[/] value`` rows: a line
    that opens with a lowercase ``label:`` token returns that token
    wrapped in a ``[style]…[/]`` span with the value markup-escaped after
    it. A line with no leading label (an indented commit subject, a blank
    separator) returns escaped-only so it still renders literally.

    Args:
        line: The raw ``label: value`` line (or a label-less line).
        style: The content-markup style applied to the label token;
            defaults to the theme accent palette var.

    Returns:
        The line with its label tinted and its value escaped, or the
        whole line escaped when it carries no leading label.
    """
    match = _LABEL_RE.match(line)
    if match is None:
        return escape_markup(line)
    label, gap, value = match.groups()
    return f"[{style}]{label}[/]{gap}{escape_markup(value)}"


__all__ = [
    "escape_markup",
    "style_labeled_line",
]
