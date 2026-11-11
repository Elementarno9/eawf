"""Byte-exact goldens of the status line rows, and the page that documents them.

Each golden renders one recorded payload through the line-1 builders and the
meter-row producers at a fixed clock, in one theme, glyph set and width:

- ``ansi-{120,80,40}.ansi``: the default ``ansi`` theme at three widths;
- ``ansi-missing-rows-120.ansi``: no five-hour window, and a seven-day reset
  that cannot be read;
- ``ansi-unknown-reset-120.ansi``: both resets unreadable;
- ``unicode-120.ansi`` and ``ascii-120.ansi``: the other two glyph sets;
- ``catppuccin-{latte,mocha}-120.ansi``: two Catppuccin flavours;
- ``none-120.txt``: no colour at all.

Regenerate them by running this file with ``EAWF_SNAPSHOT_REGEN=1`` set, then
re-run it without the variable to confirm the committed files match.
"""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

from eawf.runtime.runtimes.claude import statusline as orchestrator
from eawf.surfaces.render.statusline import (
    ColourTheme,
    GlyphSet,
    LineStyle,
    SegmentSource,
    StatuslineSegment,
    load_theme,
    render_powerline,
    sourced_segment,
)

_ROOT: Final = Path(__file__).resolve().parents[5]
_GOLDEN_DIR: Final = _ROOT / "tests" / "golden" / "statusline"
_ARCHITECTURE_DOC: Final = _ROOT / "docs" / "architecture" / "statusline.md"
_SGR: Final = re.compile(r"\x1b\[[0-9;]*m")
_NOW: Final = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)
_SOURCE: Final = SegmentSource(producer="test-producer", provenance="test#field")

_FIVE_HOUR: Final = {
    "used_percentage": 31,
    "resets_at": (_NOW + timedelta(hours=2, minutes=5, seconds=30)).isoformat(),
}
_SEVEN_DAY: Final = {
    "used_percentage": 62,
    "resets_at": (_NOW + timedelta(days=2, hours=4, minutes=10)).isoformat(),
}
PAYLOAD: Final[dict[str, Any]] = {
    "session_id": "ses-goldens",
    "model": {"id": "claude-opus-4-7", "display_name": "Opus 4.7"},
    "workspace": {"project_dir": "/work/abc"},
    "cost": {
        "total_cost_usd": 0.52,
        "total_api_duration_ms": 1_482_000,
        "total_duration_ms": 3_900_000,
    },
    "context_window": {
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 1_000,
            "cache_creation_input_tokens": 4_440,
            "cache_read_input_tokens": 62_560,
        },
    },
    "rate_limits": {"five_hour": _FIVE_HOUR, "seven_day": _SEVEN_DAY},
}

#: What the registry and the hook health record state for the fixture session.
_STATED: Final[dict[str, StatuslineSegment]] = {
    "mcp_health": sourced_segment("mcp_health", "mcp", "3/6", _SOURCE, status="warn"),
    "hooks_plugins": sourced_segment(
        "hooks_plugins", "hooks", "4 plugins:2 hooks!12", _SOURCE, status="degraded"
    ),
}


def _payload(**rate_limits: Any) -> dict[str, Any]:
    return {**PAYLOAD, "rate_limits": rate_limits}


#: Each golden's file name, payload, theme (``None`` for no colour), glyphs and width.
_GOLDENS: Final[tuple[tuple[str, dict[str, Any], ColourTheme | None, GlyphSet, int], ...]] = (
    ("ansi-120.ansi", PAYLOAD, "ansi", "nerd", 120),
    ("ansi-80.ansi", PAYLOAD, "ansi", "nerd", 80),
    ("ansi-40.ansi", PAYLOAD, "ansi", "nerd", 40),
    (
        "ansi-missing-rows-120.ansi",
        _payload(seven_day={**_SEVEN_DAY, "resets_at": "soon"}),
        "ansi",
        "nerd",
        120,
    ),
    (
        "ansi-unknown-reset-120.ansi",
        _payload(
            five_hour={**_FIVE_HOUR, "resets_at": None},
            seven_day={**_SEVEN_DAY, "resets_at": "soon"},
        ),
        "ansi",
        "nerd",
        120,
    ),
    ("unicode-120.ansi", PAYLOAD, "ansi", "unicode", 120),
    ("ascii-120.ansi", PAYLOAD, "ansi", "ascii", 120),
    ("catppuccin-latte-120.ansi", PAYLOAD, "catppuccin-latte", "nerd", 120),
    ("catppuccin-mocha-120.ansi", PAYLOAD, "catppuccin-mocha", "nerd", 120),
    ("none-120.txt", PAYLOAD, None, "nerd", 120),
)

#: Where the detail column starts on every meter row of a 20-cell bar.
_DETAIL_OFFSET: Final = 36


def _render(
    payload: dict[str, Any], theme: ColourTheme | None, glyphs: GlyphSet, width: int
) -> list[str]:
    segments = [
        _STATED.get(segment.module, segment)
        for segment in orchestrator._build_segments(payload, None)
    ]
    style = LineStyle(theme=None if theme is None else load_theme(theme), glyphs=glyphs)
    return render_powerline(segments, orchestrator._build_bars(payload, _NOW), style, width=width)


def test_goldens() -> None:
    regen = bool(os.environ.get("EAWF_SNAPSHOT_REGEN"))
    for name, payload, theme, glyphs, width in _GOLDENS:
        rendered = ("\n".join(_render(payload, theme, glyphs, width)) + "\n").encode("utf-8")
        path = _GOLDEN_DIR / name
        if regen:
            path.write_bytes(rendered)
        assert path.read_bytes() == rendered, name
    assert sorted(path.name for path in _GOLDEN_DIR.iterdir()) == sorted(
        name for name, *_ in _GOLDENS
    )
    assert b"\x1b" not in (_GOLDEN_DIR / "none-120.txt").read_bytes()

    meter_rows = [
        _SGR.sub("", row)
        for row in (_GOLDEN_DIR / "ansi-120.ansi").read_text(encoding="utf-8").splitlines()[1:]
    ]
    assert [row[:3] for row in meter_rows] == ["ctx", "5h ", "7d "]
    for row in meter_rows:
        assert row[_DETAIL_OFFSET - 1] == " ", row
        assert row[_DETAIL_OFFSET] != " ", row


#: Phrases the architecture page must carry, one per topic it documents.
_DOC_TERMS: Final = (
    "eawf statusline install",
    "~/.claude/settings.json",
    "eawf statusline doctor",
    "`ansi`",
    "16 ANSI colours",
    "offset 36",
    "pace",
    "24 hours",
    "eawf hook health ack",
    "250 ms",
)

#: Headings and phrases of the replaced design that must be gone.
_RETIRED_TERMS: Final = (
    "## Wiring",
    "## Palettes",
    "## Cache and refresh",
    "stale-while-revalidate",
    "24-bit palette",
)


def test_architecture_doc_terms() -> None:
    text = _ARCHITECTURE_DOC.read_text(encoding="utf-8")
    headings = [line for line in text.splitlines() if line.startswith("## ")]
    assert {"## Install", "## Theme", "## Meter rows and grid", "## Freshness"} <= set(headings)
    assert [term for term in _DOC_TERMS if term not in text] == []
    assert [term for term in _RETIRED_TERMS if term in text] == []
