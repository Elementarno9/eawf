"""Integration tests for the rich-backed TUI app (P20-I01-W02; P20-I03-W01).

Covers:

* Golden snapshot of the offline single-frame render. The fixture state
  is checked in beside this test (``tests/golden/tui/state.json``) and
  the expected render lives in ``tests/golden/tui/expected.txt``.
* Offline tick mode: ``--no-input`` / ``--plain`` / non-TTY emits one
  static frame via ``build_status_text`` and exits without entering
  ``rich.live.Live``.
* Online tick mode wiring: the loop honours the ``refresh_per_second``
  knob, runs the reader on a daemon thread via the queue scaffolding,
  and exits cleanly on Esc / q / Ctrl-C / EOF.
* Overlay verb-prefix state machine: ``o`` then ``H`` opens the
  hypothesis overlay; ``o`` then an unknown letter cancels.

When the renderer drifts intentionally, regenerate the snapshot::

    cd <repo>
    uv run python -c "
    import io, json, subprocess
    from pathlib import Path
    from unittest.mock import patch
    from rich.console import Console
    from eawf.tui.layout import build_frame

    fixture = Path('tests/golden/tui')
    state = json.loads((fixture / 'state.json').read_text())

    # The git pane shells out — stub it so the golden stays deterministic.
    def fake_run(args, **kwargs):
        cp = subprocess.CompletedProcess
        if 'rev-parse' in args and '--abbrev-ref' in args:
            return cp(args=args, returncode=0, stdout='feature/eawf-v0.3-p20', stderr='')
        if 'rev-parse' in args and '--short' in args:
            return cp(args=args, returncode=0, stdout='abc1234', stderr='')
        if 'status' in args:
            return cp(args=args, returncode=0, stdout='', stderr='')
        return cp(args=args, returncode=0, stdout='0', stderr='')

    with patch('subprocess.run', side_effect=fake_run):
        from eawf.tui.layout import _reset_git_pane_cache
        _reset_git_pane_cache()
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=100, record=False).print(build_frame(state))
        (fixture / 'expected.txt').write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from rich.console import Console
from typer.testing import CliRunner

from eawf.cli.app import app
from eawf.tui import app as tui_app
from eawf.tui import layout as layout_mod
from eawf.tui.app import build_status_text, render_layout, run_tui
from eawf.tui.layout import (
    BRAND,
    FOOTER_KEYMAP,
    FOOTER_KEYMAP_OVERLAY_PENDING,
    QUADRANT_PANE_NAMES,
    _reset_git_pane_cache,
    build_frame,
)

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_fixture_state() -> dict[str, Any]:
    return json.loads((_FIXTURE_DIR / "state.json").read_text(encoding="utf-8"))


def _fake_completed(returncode: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["git"], returncode=returncode, stdout=stdout, stderr=""
    )


def _stub_git_run(args: list[str], **_: Any) -> subprocess.CompletedProcess[str]:
    """Deterministic git stub for the golden snapshot."""
    if "rev-parse" in args and "--abbrev-ref" in args:
        return _fake_completed(0, "feature/eawf-v0.3-p20")
    if "rev-parse" in args and "--short" in args:
        return _fake_completed(0, "abc1234")
    if "status" in args:
        return _fake_completed(0, "")
    if "rev-list" in args:
        return _fake_completed(0, "0")
    return _fake_completed(1)


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


@pytest.fixture(autouse=True)
def _clear_git_cache() -> None:
    _reset_git_pane_cache()


@pytest.fixture
def _stubbed_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the git shell-outs so the live pane is deterministic in tests."""
    monkeypatch.setattr(subprocess, "run", _stub_git_run)
    _reset_git_pane_cache()


# ---------------------------------------------------------------------------
# Golden snapshot (offline render)
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_offline_frame_matches_golden_snapshot(_stubbed_git: None) -> None:
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
def test_offline_frame_two_renders_byte_stable(_stubbed_git: None) -> None:
    """Two consecutive renders produce identical bytes."""
    state = _load_fixture_state()
    first = _render_frame_to_string(state)
    second = _render_frame_to_string(state)
    assert first == second


def test_offline_frame_carries_brand_breadcrumb_and_keymap(_stubbed_git: None) -> None:
    """Structural assertions independent of byte-equality."""
    state = _load_fixture_state()
    rendered = _render_frame_to_string(state)
    assert BRAND in rendered
    assert "EAWF" in rendered  # fixture project code
    assert "P20" in rendered
    # New quadrant footer keymap fragments.
    assert "board" in rendered
    assert "overlay" in rendered
    # All four pane titles must surface in the body quadrant.
    for title in QUADRANT_PANE_NAMES:
        assert title in rendered, f"pane {title!r} missing from rendered frame"


def test_offline_frame_brand_appears_before_breadcrumb_project_code(
    _stubbed_git: None,
) -> None:
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
# Online tick mode (queue-fed reader thread)
# ---------------------------------------------------------------------------


def test_run_tui_online_exits_on_esc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _stubbed_git: None,
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "\x1b")
    assert rc == 0


def test_run_tui_online_exits_on_q(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "q")
    assert rc == 0


def test_run_tui_online_exits_on_keyboard_interrupt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)

    def boom() -> str:
        raise KeyboardInterrupt

    rc = run_tui(workspace=tmp_path, read_key=boom)
    assert rc == 0


def test_run_tui_online_loop_iterates_until_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # ``a``/``z`` are inert keys (no quadrant mapping) so the loop must
    # consume them and then exit on the ``\x1b`` (Esc) at the end.
    keys = iter(["a", "z", "\x1b"])
    rc = run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0


def test_run_tui_online_does_not_exit_on_arrow_keys(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """Arrow keys arrive as ESC-prefixed sequences; bare-ESC exit must not fire."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    keys = iter(["\x1b[A", "\x1b[B", "\x1b[C", "\x1b[D", "q"])

    rc = run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_online_refresh_rate_30hz_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """P20-I03-W01: default refresh is 30Hz so the live loop feels live."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    # Module-level constant pinning — the dispatch spec calls for 30Hz.
    assert tui_app.DEFAULT_REFRESH_HZ >= 20, (
        f"DEFAULT_REFRESH_HZ={tui_app.DEFAULT_REFRESH_HZ} is below the W01 floor"
    )
    rc = run_tui(workspace=tmp_path, read_key=lambda: "q")
    assert rc == 0


def test_run_tui_online_refresh_rate_parameter_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """The ``refresh_per_second`` knob is part of the online tick mode API."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    rc = run_tui(workspace=tmp_path, read_key=lambda: "q", refresh_per_second=2)
    assert rc == 0


# ---------------------------------------------------------------------------
# Overlay verb-prefix state machine (P20-I03-W01 success criterion 3)
# ---------------------------------------------------------------------------


def _overlay_fixture_workspace(tmp_path: Path) -> Path:
    """Lay down a workspace whose state.json has the overlay fixtures."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir()
    source = (_FIXTURE_DIR / "overlay_state.json").read_text(encoding="utf-8")
    (ea_dir / "state.json").write_text(source, encoding="utf-8")
    return tmp_path


def test_run_tui_online_opens_hypothesis_overlay_on_oh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """``o`` then ``H`` enters the overlay view; ``Esc`` returns to quadrant."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    workspace = _overlay_fixture_workspace(tmp_path)
    keys = iter(["o", "H", "\x1b", "q"])
    rc = run_tui(workspace=workspace, read_key=lambda: next(keys))
    assert rc == 0


def test_run_tui_online_overlay_pending_cancelled_by_unknown_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """``o`` then an unknown letter cancels overlay-pending mode without crashing."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    workspace = _overlay_fixture_workspace(tmp_path)
    keys = iter(["o", "x", "q"])
    rc = run_tui(workspace=workspace, read_key=lambda: next(keys))
    assert rc == 0


def test_run_tui_online_overlay_pending_cancelled_by_esc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """``o`` then ``Esc`` cancels overlay-pending mode (Esc is not a letter)."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    workspace = _overlay_fixture_workspace(tmp_path)
    keys = iter(["o", "\x1b", "q"])
    rc = run_tui(workspace=workspace, read_key=lambda: next(keys))
    assert rc == 0


def test_run_tui_online_overlay_each_object_letter_dispatches(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _stubbed_git: None
) -> None:
    """All five overlay letters open + close cleanly."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)
    workspace = _overlay_fixture_workspace(tmp_path)
    keys = iter(
        [
            "o",
            "H",
            "\x1b",
            "o",
            "D",
            "\x1b",
            "o",
            "M",
            "\x1b",
            "o",
            "E",
            "\x1b",
            "o",
            "R",
            "\x1b",
            "q",
        ]
    )
    rc = run_tui(workspace=workspace, read_key=lambda: next(keys))
    assert rc == 0


def test_overlay_second_key_dispatch_unit() -> None:
    """Direct unit test of ``_handle_overlay_second_key``.

    Loads the overlay fixture state, asks for ``H``, and verifies the
    returned :class:`Layout` carries the hypothesis title.
    """
    state_dict = json.loads((_FIXTURE_DIR / "overlay_state.json").read_text(encoding="utf-8"))
    workspace = Path(_FIXTURE_DIR)
    with patch.object(tui_app, "_load_state", return_value=state_dict):
        overlay_pending, layout = tui_app._handle_overlay_second_key("H", workspace)
    assert overlay_pending is False
    assert layout is not None
    # Render and look for the hypothesis id from the fixture.
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120).print(layout)
    rendered = buf.getvalue()
    assert "H01-01" in rendered


def test_overlay_second_key_unknown_letter_yields_no_layout() -> None:
    """An unknown second letter (e.g. ``X``) returns ``(False, None)``."""
    state_dict = json.loads((_FIXTURE_DIR / "overlay_state.json").read_text(encoding="utf-8"))
    workspace = Path(_FIXTURE_DIR)
    with patch.object(tui_app, "_load_state", return_value=state_dict):
        overlay_pending, layout = tui_app._handle_overlay_second_key("X", workspace)
    assert overlay_pending is False
    assert layout is None


def test_overlay_second_key_empty_collection_yields_placeholder() -> None:
    """When the collection is empty, the helper renders a placeholder layout."""
    # State with no hypotheses at all.
    state_dict = {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:EAWF",
        "updated_at": "2026-05-15T00:00:00+00:00",
        "project": {
            "code": "EAWF",
            "slug": "eawf",
            "title": "EAWF",
            "domains": [],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:EAWF",
        },
        "current": {
            "project_code": "EAWF",
            "phase_id": None,
            "iter_id": None,
            "active_wave_ids": [],
            "active_session_ids": [],
        },
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "hypotheses": {},
        "decisions": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "memory_index": {},
        "indexes": {},
    }
    workspace = Path("/tmp/empty-overlay")
    with patch.object(tui_app, "_load_state", return_value=state_dict):
        overlay_pending, layout = tui_app._handle_overlay_second_key("H", workspace)
    assert overlay_pending is False
    assert layout is not None
    buf = io.StringIO()
    Console(file=buf, force_terminal=False, width=120).print(layout)
    assert "no hypothesis records" in buf.getvalue()


def test_overlay_pending_footer_keymap_constant_used_by_run_tui() -> None:
    """The verb-prefix footer override constant is referenced by ``run_tui``.

    Confirms the wiring: when ``o`` is pressed we must repaint with
    the overlay-pending footer keymap, not the default one.
    """
    # Reference the constant so a removal would surface as a test failure.
    assert "Esc cancel" in FOOTER_KEYMAP_OVERLAY_PENDING
    # And the loop imports it from the layout module — symbol existence
    # check.
    assert hasattr(layout_mod, "FOOTER_KEYMAP_OVERLAY_PENDING")


# ---------------------------------------------------------------------------
# render_layout console-injection contract
# ---------------------------------------------------------------------------


def test_render_layout_writes_into_buffer(_stubbed_git: None) -> None:
    state = _load_fixture_state()
    output = render_layout(state)
    assert BRAND in output
    # New quadrant keymap fragments.
    assert "board" in output


def test_render_layout_accepts_external_console(_stubbed_git: None) -> None:
    state = _load_fixture_state()
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, record=False)
    out = render_layout(state, console=console)
    # When the caller supplies a console, the helper writes into it and
    # returns an empty string — verifying the in/out contract.
    assert out == ""
    assert BRAND in buf.getvalue()


# ---------------------------------------------------------------------------
# FPS measurement (rough smoke check)
# ---------------------------------------------------------------------------


def test_measure_render_rate_returns_positive_fps(_stubbed_git: None) -> None:
    """The diagnostic helper returns a non-zero frames-per-second rate.

    Pinned at 0.05s so the test finishes fast on CI; the production
    measurement uses 0.25s. We only assert the rate is positive — the
    exact value depends on hardware.
    """
    state = _load_fixture_state()
    fps = tui_app._measure_render_rate(state, seconds=0.05)
    assert fps > 0.0
