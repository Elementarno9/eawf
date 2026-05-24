"""Unit tests for the shared pane content-markup helpers.

Covers :func:`escape_markup` (the literal-bracket guarantee that keeps a
``[P##-W##]`` commit subject from being parsed as a style tag) and
:func:`style_labeled_line` (the ``[$accent]label[/] value`` tint shared by
the status pane, the git pane, and the detail modal). The strongest check
round-trips each styled string through Textual's content-markup parser and
asserts the rendered plain text equals the original line.
"""

from __future__ import annotations

from textual.content import Content

from eawf.tui.widgets.markup import escape_markup, style_labeled_line


def _plain(markup: str) -> str:
    """Return the plain text Textual renders for a content-markup string."""
    return Content.from_markup(markup).plain


# --------------------------------------------------------------------------
# escape_markup — literal-bracket guarantee
# --------------------------------------------------------------------------


def test_escape_markup_empty_is_noop() -> None:
    assert escape_markup("") == ""


def test_escape_markup_bracket_free_is_unchanged() -> None:
    assert escape_markup("branch:   main") == "branch:   main"


def test_escape_markup_escapes_every_open_bracket() -> None:
    assert escape_markup("[a] [b]") == "\\[a] \\[b]"


def test_escape_markup_commit_subject_renders_literally() -> None:
    """A ``[P27-W30]`` subject survives the markup parser intact."""
    subject = "[P27-W30] fix: align glyph runs"
    assert _plain(escape_markup(subject)) == subject


def test_escape_markup_style_like_run_renders_literally() -> None:
    """A run that looks like a real style tag still renders literally."""
    assert _plain(escape_markup("a [red]r[/red] b")) == "a [red]r[/red] b"


# --------------------------------------------------------------------------
# style_labeled_line — accent label tint
# --------------------------------------------------------------------------


def test_style_labeled_line_tints_label_keeps_gap() -> None:
    assert style_labeled_line("branch:   main") == "[$accent]branch:[/]   main"


def test_style_labeled_line_bare_label_no_value() -> None:
    assert style_labeled_line("recent:") == "[$accent]recent:[/]"


def test_style_labeled_line_custom_style() -> None:
    assert style_labeled_line("phase:  P27", style="$err") == "[$err]phase:[/]  P27"


def test_style_labeled_line_no_label_escaped_only() -> None:
    """An indented (label-less) line is escaped, never tinted."""
    assert style_labeled_line("  [P27-W30] fix") == "  \\[P27-W30] fix"


def test_style_labeled_line_uppercase_not_treated_as_label() -> None:
    """Lowercase-only: an uppercase band header falls through to escape."""
    assert style_labeled_line("NOW") == "NOW"


def test_style_labeled_line_empty_is_empty() -> None:
    assert style_labeled_line("") == ""


def test_style_labeled_line_value_brackets_render_literally() -> None:
    """The tinted label drops out of plain; the bracketed value survives."""
    line = "upstream: [P27-W30] note"
    assert _plain(style_labeled_line(line)) == line


def test_style_labeled_line_colon_in_value_only_first_matches() -> None:
    """A colon inside the value does not start a second label."""
    assert style_labeled_line("eta:  2026-05-24: soon") == "[$accent]eta:[/]  2026-05-24: soon"
