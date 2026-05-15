"""Integration tests for the rich-backed TUI app (P20-I01-W02).

Covers:

* Golden snapshot of the offline single-frame render. The fixture state
  is checked in beside this test (``tests/golden/tui/state.json``) and
  the expected render lives in ``tests/golden/tui/expected.txt``.
* Offline tick mode: ``--no-input`` / ``--plain`` / non-TTY emits one
  static frame via ``build_status_text`` and exits without entering
  ``rich.live.Live``.
* Online tick mode wiring: the loop honours the ``refresh_per_second``
  knob and ``read_key`` test seam (no real ``Live`` opened during
  tests).

When the renderer drifts intentionally, regenerate the snapshot:

    cd <repo>
    uv run python -c "
    import io, json
    from pathlib import Path
    from rich.console import Console
    from eawf.tui.layout import build_frame

    fixture = Path('tests/golden/tui')
    state = json.loads((fixture / 'state.json').read_text())
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=100, record=False).print(
        build_frame(state)
    )
    (fixture / 'expected.txt').write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
import json
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.tui import app as tui_app
from eawf.tui.app import build_status_text, render_layout, run_tui
from eawf.tui.layout import BRAND, FOOTER_KEYMAP, QUADRANT_PANE_NAMES, build_frame

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_state() -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / "state.json").read_text(encoding="utf-8"))


def _render_frame_to_string(state: dict[str, Any]) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=100, record=False)
    console.print(build_frame(state))
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    """Pre-commit's ``end-of-file-fixer`` may add a trailing newline.

    ``rich.console.Console.print`` already emits one, so normalising
    both sides keeps the hook compatible without forcing the renderer
    to mimic an editor convention.
    """
    if text.endswith("\n"):
        return text[:-1]
    return text


# ---------------------------------------------------------------------------
# Golden snapshot (offline render)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_offline_frame_matches_golden_snapshot() -> None:
    """Frozen single-frame snapshot of the quadrant TUI render."""
    state = _load_fixture_state()
    actual = _normalise_trailing_newline(_render_frame_to_string(state))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "expected.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "tui frame drift — regenerate tests/golden/tui/expected.txt with the snippet "
        "at the top of test_tui_app.py."
    )


@pytest.mark.golden
def test_offline_frame_two_renders_byte_stable() -> None:
    """Two consecutive renders produce identical bytes."""
    state = _load_fixture_state()
    first = _render_frame_to_string(state)
    second = _render_frame_to_string(state)
    assert first == second


def test_offline_frame_carries_brand_breadcrumb_and_keymap() -> None:
    """Structural assertions independent of byte-equality."""
    state = _load_fixture_state()
    rendered = _render_frame_to_string(state)
    assert BRAND in rendered
    assert "EAWF" in rendered  # fixture project code
    assert "P20" in rendered
    # Footer keymap fragments — pick a few stable substrings.
    assert "navigate" in rendered
    assert "Esc" in rendered
    # All four pane titles must surface in the body quadrant.
    for title in QUADRANT_PANE_NAMES:
        assert title in rendered, f"pane {title!r} missing from rendered frame"


def test_offline_frame_brand_appears_before_breadcrumb_project_code() -> None:
    """Brand must sit outside-left of the breadcrumb in the header strip."""
    state = _load_fixture_state()
    rendered = _render_frame_to_string(state)
    brand_idx = rendered.find(BRAND)
    crumb_idx = rendered.find("EAWF")
    assert brand_idx >= 0 and crumb_idx > brand_idx, (
        f"brand at {brand_idx}, breadcrumb at {crumb_idx}; brand must precede breadcrumb"
    )


# ---------------------------------------------------------------------------
# build_status_text fallback
# ---------------------------------------------------------------------------


def test_build_status_text_carries_brand_and_keymap() -> None:
    state = _load_fixture_state()
    text = build_status_text(state)
    assert text.startswith(BRAND)
    assert "keymap:" in text
    assert "navigate" in text
    assert "EAWF" in text


def test_build_status_text_handles_empty_state() -> None:
    text = build_status_text({})
    assert text.startswith(BRAND)
    assert "EAWF" in text
    assert FOOTER_KEYMAP in text


# ---------------------------------------------------------------------------
# Offline tick mode via run_tui / CLI
# ---------------------------------------------------------------------------


def test_run_tui_no_input_emits_one_frame_and_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offline tick mode renders exactly one frame and returns 0."""
    rc = run_tui(workspace=tmp_path, no_input=True)
    assert rc == 0
    captured = capsys.readouterr()
    assert captured.out.startswith(BRAND)
    assert "keymap:" in captured.out
    # Frame count = 1: a second BRAND occurrence would mean a loop ran.
    assert captured.out.count(BRAND) == 1


def test_run_tui_plain_emits_one_frame_and_exits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = run_tui(workspace=tmp_path, plain=True)
    assert rc == 0
    captured = capsys.readouterr()
    assert BRAND in captured.out
    assert captured.out.count(BRAND) == 1


def test_run_tui_non_tty_falls_back_to_offline_frame(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: False)
    rc = run_tui(workspace=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()
    assert BRAND in captured.out


def test_bare_cli_non_tty_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["--plain", "-w", str(tmp_path)])
    assert result.exit_code == 0
    assert BRAND in result.stdout
    assert "keymap:" in result.stdout


def test_cli_tui_subcommand_plain_emits_status(tmp_path: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(app, ["-w", str(tmp_path), "--plain", "tui"])
    assert result.exit_code == 0
    assert BRAND in result.stdout


# ---------------------------------------------------------------------------
# Online tick mode (mocked Live + read_key seam)
# ---------------------------------------------------------------------------


def test_run_tui_online_exits_on_esc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "\x1b")
    assert rc == 0


def test_run_tui_online_exits_on_q(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "q")
    assert rc == 0


def test_run_tui_online_exits_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)

    def boom() -> str:
        raise KeyboardInterrupt

    rc = run_tui(workspace=tmp_path, read_key=boom)
    assert rc == 0


def test_run_tui_online_loop_iterates_until_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    keys = iter(["a", "b", "\x1b"])
    rc = run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0


def test_run_tui_online_does_not_exit_on_arrow_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Arrow keys arrive as ESC-prefixed sequences; bare-ESC exit must not fire."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    keys = iter(["\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "q"])

    rc = run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_online_refresh_rate_parameter_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``refresh_per_second`` knob is part of the online tick mode API."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "q", refresh_per_second=2)
    assert rc == 0


# ---------------------------------------------------------------------------
# render_layout console-injection contract
# ---------------------------------------------------------------------------


def test_render_layout_writes_into_buffer() -> None:
    state = _load_fixture_state()
    output = render_layout(state)
    assert BRAND in output
    assert "navigate" in output


def test_render_layout_accepts_external_console() -> None:
    state = _load_fixture_state()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    out = render_layout(state, console=console)
    # When the caller supplies a console, the helper writes into it and
    # returns an empty string — verifying the in/out contract.
    assert out == ""
    assert BRAND in buf.getvalue()
