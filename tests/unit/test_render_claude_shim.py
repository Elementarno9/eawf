"""Unit tests for ``eawf.render.claude_shim``.

Covers:

- The shim writes exactly ``"@AGENTS.md\\n"``.
- Two consecutive calls leave the file byte-stable (idempotent).
- The write goes through tempfile + ``os.replace`` (atomic discipline).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from eawf.render.agents_md import RenderResult
from eawf.render.claude_shim import render_claude_md


def test_claude_shim_writes_at_agents_md_literal(tmp_path: Path) -> None:
    """File content is the exact literal ``@AGENTS.md`` followed by one newline."""
    target = tmp_path / "CLAUDE.md"
    result = render_claude_md(target)
    assert isinstance(result, RenderResult)
    assert result.target == target
    assert result.regions_added == []
    assert result.regions_updated == []
    assert result.regions_unchanged == []
    assert result.hand_edits_preserved is False

    payload = target.read_bytes()
    assert payload == b"@AGENTS.md\n"
    # A common reader path: text mode round-trip is the literal string.
    assert target.read_text(encoding="utf-8") == "@AGENTS.md\n"


def test_claude_shim_idempotent(tmp_path: Path) -> None:
    """Two render calls produce the same byte content; no exception either time."""
    target = tmp_path / "CLAUDE.md"
    render_claude_md(target)
    first = target.read_bytes()
    render_claude_md(target)
    second = target.read_bytes()
    assert first == second
    assert second == b"@AGENTS.md\n"


def test_claude_shim_atomic_write(tmp_path: Path) -> None:
    """The shim writes via a sibling tempfile then ``os.replace``."""
    target = tmp_path / "CLAUDE.md"
    with patch(
        "eawf.render.claude_shim.os.replace",
        wraps=__import__("os").replace,
    ) as spy:
        render_claude_md(target)

    assert spy.called
    src_arg = spy.call_args.args[0]
    dst_arg = spy.call_args.args[1]
    assert str(dst_arg) == str(target)
    assert str(src_arg).startswith(str(target) + ".tmp.")


def test_claude_shim_creates_parent_dirs(tmp_path: Path) -> None:
    """Missing parent directories are created on demand."""
    target = tmp_path / "deep" / "nested" / "CLAUDE.md"
    render_claude_md(target)
    assert target.exists()
    assert target.read_bytes() == b"@AGENTS.md\n"


def test_claude_shim_overwrites_existing_content(tmp_path: Path) -> None:
    """Pre-existing CLAUDE.md content is replaced wholesale (no managed regions)."""
    target = tmp_path / "CLAUDE.md"
    target.write_text("user wrote this\n", encoding="utf-8")
    render_claude_md(target)
    assert target.read_bytes() == b"@AGENTS.md\n"
