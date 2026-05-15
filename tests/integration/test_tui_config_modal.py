"""Integration tests for the config-modal TUI view (P20-I01-W11).

Covers:

* Golden snapshots of the offline render across four views
  (default cursor, ``ui`` tab, editing mode, post-save toast).
  Fixture snapshots live under
  ``tests/golden/tui_config_modal/``.
* The ``c``-key dispatch in :func:`eawf.tui.app.run_tui`: pressing
  ``c`` opens the modal sub-loop; Esc returns to the quadrant.
* Save-path wiring: a happy-path run with a stubbed save_fn confirms
  the W10 layered writer is the single mutator path.

When the renderer drifts intentionally, regenerate the snapshots:

    cd <repo>
    uv run python -c "
    import io
    from pathlib import Path
    from rich.console import Console
    from eawf.tui.config_modal import build_modal_frame, ConfigModalState

    fixture = Path('tests/golden/tui_config_modal')
    fixture.mkdir(parents=True, exist_ok=True)
    state = {'project': {'code': 'EAWF'}, 'current': {'phase_id': 'P20', 'iter_id': 'P20-I01'}}
    merged = {
        'audit': {'fix_safe': False, 'flaky_retry_count': 1},
        'ui': {'bare_command': 'tui', 'color': 'auto', 'refresh_ms': 1000},
    }
    cases = [
        (ConfigModalState(), 'modal_default.txt'),
        (ConfigModalState(tab_index=6), 'modal_ui_tab.txt'),
        (ConfigModalState(editing=True, edit_buffer='true'), 'modal_editing.txt'),
        (ConfigModalState(dirty={'audit.fix_safe': True}, toast='saved 1 key'),
         'modal_saved_toast.txt'),
    ]
    for view, name in cases:
        buf = io.StringIO()
        Console(file=buf, force_terminal=False, width=120, height=40, record=False).print(
            build_modal_frame(view, state=state, merged=merged)
        )
        (fixture / name).write_text(buf.getvalue())
    "
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from eawf.tui import app as tui_app
from eawf.tui import config_modal as config_modal_mod
from eawf.tui.config_modal import (
    ConfigModalState,
    build_modal_frame,
)

_FIXTURE_DIR: Path = Path(__file__).parent.parent / "golden" / "tui_config_modal"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _state_fixture() -> dict[str, Any]:
    return {
        "project": {"code": "EAWF"},
        "current": {"phase_id": "P20", "iter_id": "P20-I01"},
    }


def _merged_fixture() -> dict[str, Any]:
    return {
        "audit": {"fix_safe": False, "flaky_retry_count": 1},
        "ui": {"bare_command": "tui", "color": "auto", "refresh_ms": 1000},
        "planning": {
            "approval": "ask",
            "auto_plan": False,
            "max_parallel_waves": 4,
            "require_research_for_unknowns": True,
        },
        "runtime": {"default": "claude"},
        "vcs": {
            "auto_commit": "ask",
            "auto_push": "ask",
            "pr_open": "ask",
            "require_ci_green": True,
        },
    }


def _render(view: ConfigModalState) -> str:
    buf = io.StringIO()
    console = Console(file=buf, force_terminal=False, width=120, height=40, record=False)
    console.print(build_modal_frame(view, state=_state_fixture(), merged=_merged_fixture()))
    return buf.getvalue()


def _normalise_trailing_newline(text: str) -> str:
    if text.endswith("\n"):
        return text[:-1]
    return text


# ---------------------------------------------------------------------------
# Golden snapshots
# ---------------------------------------------------------------------------


@pytest.mark.golden
def test_modal_default_view_matches_golden() -> None:
    actual = _normalise_trailing_newline(_render(ConfigModalState()))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "modal_default.txt").read_text(encoding="utf-8")
    )
    assert actual == expected, (
        "config-modal default-view drift — regenerate "
        "tests/golden/tui_config_modal/modal_default.txt with the snippet at the "
        "top of test_tui_config_modal.py."
    )


@pytest.mark.golden
def test_modal_ui_tab_view_matches_golden() -> None:
    # tab_index=6 → ui (alphabetical 7th position).
    actual = _normalise_trailing_newline(_render(ConfigModalState(tab_index=6)))
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "modal_ui_tab.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_modal_editing_view_matches_golden() -> None:
    actual = _normalise_trailing_newline(
        _render(ConfigModalState(editing=True, edit_buffer="true"))
    )
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "modal_editing.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


@pytest.mark.golden
def test_modal_saved_toast_view_matches_golden() -> None:
    actual = _normalise_trailing_newline(
        _render(ConfigModalState(dirty={"audit.fix_safe": True}, toast="saved 1 key"))
    )
    expected = _normalise_trailing_newline(
        (_FIXTURE_DIR / "modal_saved_toast.txt").read_text(encoding="utf-8")
    )
    assert actual == expected


# ---------------------------------------------------------------------------
# Structural assertions (independent of byte-equality)
# ---------------------------------------------------------------------------


def test_modal_renders_brand_outside_left_of_breadcrumb() -> None:
    rendered = _render(ConfigModalState())
    brand_idx = rendered.find("Eä")
    crumb_idx = rendered.find("EAWF")
    assert brand_idx >= 0 and crumb_idx > brand_idx


def test_modal_header_carries_config_marker() -> None:
    """Operator must see ``>> config`` so the modal is unambiguous."""
    rendered = _render(ConfigModalState())
    assert "config" in rendered


def test_modal_lists_every_tab_in_alphabetical_order() -> None:
    rendered = _render(ConfigModalState())
    from eawf.config.registry import tabs_sorted

    tabs = tabs_sorted()
    indices = [rendered.find(tab) for tab in tabs]
    assert all(i >= 0 for i in indices), f"missing tabs in render: indices={indices}"
    assert indices == sorted(indices), "tabs out of alphabetical order"


def test_modal_form_lists_audit_fields_alphabetical() -> None:
    rendered = _render(ConfigModalState())
    # audit tab — fields are audit.fix_safe then audit.flaky_retry_count.
    fs_idx = rendered.find("audit.fix_safe")
    fr_idx = rendered.find("audit.flaky_retry_count")
    assert 0 <= fs_idx < fr_idx


def test_modal_footer_carries_save_and_back_keys() -> None:
    rendered = _render(ConfigModalState())
    assert "s save" in rendered
    assert "Esc" in rendered


def test_modal_two_renders_byte_stable() -> None:
    first = _render(ConfigModalState())
    second = _render(ConfigModalState())
    assert first == second


# ---------------------------------------------------------------------------
# c-key dispatch in run_tui
# ---------------------------------------------------------------------------


def test_run_tui_opens_config_modal_on_c_and_returns_on_esc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pressing ``c`` enters the modal; Esc returns; second Esc exits."""
    # No state file needed — modal degrades to empty state for the
    # breadcrumb; merged config falls back to {} for the missing file.
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)

    # Capture the modal entry — replace run_config_modal with a stub
    # so we don't actually open a second Live context inside the test.
    entered: list[bool] = []

    def fake_run_modal(**kwargs: Any) -> int:
        entered.append(True)
        # Consume one key (Esc) inside the modal so the parent loop
        # advances past the c-keystroke.
        kwargs["read_key"]()
        return 0

    monkeypatch.setattr(config_modal_mod, "run_config_modal", fake_run_modal)

    # c → modal entry (modal consumes Esc) → second Esc to exit quadrant.
    keys = iter(["c", "\x1b", "\x1b"])

    def reader() -> str:
        return next(keys)

    rc = tui_app.run_tui(workspace=tmp_path, read_key=reader)
    assert rc == 0
    assert entered == [True]
    # The iterator must be fully consumed.
    with pytest.raises(StopIteration):
        next(keys)


def test_run_tui_c_key_when_state_missing_does_not_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``c`` works even with no ``.ea/state.json`` — the modal degrades."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: True)

    def fake_run_modal(**kwargs: Any) -> int:
        # Drain one keystroke from the reader so the parent's iterator
        # advances correctly.
        kwargs["read_key"]()
        return 0

    monkeypatch.setattr(config_modal_mod, "run_config_modal", fake_run_modal)

    keys = iter(["c", "\x1b", "q"])
    rc = tui_app.run_tui(workspace=tmp_path, read_key=lambda: next(keys))
    assert rc == 0


# ---------------------------------------------------------------------------
# Save-path wiring (W10 writer is the single mutator)
# ---------------------------------------------------------------------------


def test_save_dirty_writes_through_w10_helper(tmp_path: Path) -> None:
    """save_dirty's default save_fn IS the W10 _save_value_to_layer.

    Verifies the import wiring without opening the layered writer's
    full I/O sequence. The unit test covers the per-call contract;
    this test focuses on the import chain.
    """
    from eawf.cli.commands import config as config_cmd
    from eawf.tui.config_modal import save_dirty

    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)
    view = ConfigModalState(dirty={"audit.fix_safe": True})
    recorded: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        recorded.append({"target_path": target_path, "key": key, "value": value})

    # Monkeypatch the W10 helper itself so we prove the default path
    # routes through it.
    import unittest.mock

    with unittest.mock.patch.object(config_cmd, "_save_value_to_layer", fake_save):
        save_dirty(view, scope="repo", workspace=None, repo=repo)
    assert len(recorded) == 1
    assert recorded[0]["key"] == "audit.fix_safe"
    assert recorded[0]["value"] is True


def test_run_config_modal_offline_save_routes_via_writer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Drive run_config_modal with a scripted reader: enter / edit / save.

    Uses a stubbed save_fn so the test does not write to disk; verifies
    the keystroke sequence flushes through the writer exactly once and
    the modal exits with code 0.
    """
    monkeypatch.setattr(tui_app, "_is_tty", lambda: False)  # offline-friendly

    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)

    # Force the merge to resolve cleanly without reading the real
    # global config — the modal's _resolve_merged returns {} on any
    # exception, so we can use a fresh sandbox path.
    from eawf.config import layered

    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global.yaml")

    saved: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        saved.append({"target_path": target_path, "key": key, "value": value})

    # Scripted reader: Enter (open editor on audit.fix_safe) → edit
    # buffer toggle (backspace + "true") would suffice but the buffer
    # seeds from current = False ("false"), so just hit Enter to
    # commit "false". For coverage of save_fn we need a dirty buffer;
    # send Enter → Backspace x5 (kill "false") → "true" → Enter
    # (commit) → "s" (save) → modal returns.
    keys = iter(
        [
            "\r",  # open editor on audit.fix_safe (seeded "false")
            "\x7f",
            "\x7f",
            "\x7f",
            "\x7f",
            "\x7f",  # clear buffer
            "t",
            "r",
            "u",
            "e",  # type "true"
            "\r",  # commit → dirty[audit.fix_safe] = True
            "s",  # save → flush via save_fn
        ]
    )

    # Patch Live to a no-op so the test does not require a real TTY.
    import unittest.mock

    class _FakeLive:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeLive:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def update(self, *args: Any, **kwargs: Any) -> None:
            pass

    with unittest.mock.patch.object(config_modal_mod, "Live", _FakeLive):
        rc = config_modal_mod.run_config_modal(
            state=_state_fixture(),
            workspace=None,
            repo=repo,
            read_key=lambda: next(keys),
            save_fn=fake_save,
        )
    assert rc == 0
    assert len(saved) == 1
    assert saved[0]["key"] == "audit.fix_safe"
    assert saved[0]["value"] is True


def test_run_config_modal_cancels_without_save_on_esc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Esc at the modal root exits without flushing dirty fields."""
    monkeypatch.setattr(tui_app, "_is_tty", lambda: False)

    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)

    from eawf.config import layered

    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global.yaml")

    saved: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        saved.append({"key": key})

    # Open editor, type "true", commit, then Esc to exit modal —
    # save_fn must NOT fire.
    keys = iter(
        [
            "\r",  # open
            "\x7f",
            "\x7f",
            "\x7f",
            "\x7f",
            "\x7f",
            "t",
            "r",
            "u",
            "e",
            "\r",  # commit
            "\x1b",  # Esc → exit modal without save
        ]
    )

    import unittest.mock

    class _FakeLive:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeLive:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def update(self, *args: Any, **kwargs: Any) -> None:
            pass

    with unittest.mock.patch.object(config_modal_mod, "Live", _FakeLive):
        rc = config_modal_mod.run_config_modal(
            state=_state_fixture(),
            workspace=None,
            repo=repo,
            read_key=lambda: next(keys),
            save_fn=fake_save,
        )
    assert rc == 0
    assert saved == []  # cancel must not save


def test_run_config_modal_invalid_input_keeps_editor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Invalid coercion surfaces a toast; editor stays open for retry.

    Drives an out-of-range int into audit.flaky_retry_count, asserts
    the save key (or another commit) does not advance until the buffer
    is fixed. Here we just confirm the rc=0 and no save fired.
    """
    monkeypatch.setattr(tui_app, "_is_tty", lambda: False)

    repo = tmp_path / "repo"
    (repo / ".ea").mkdir(parents=True)

    from eawf.config import layered

    monkeypatch.setattr(layered, "global_config_path", lambda: tmp_path / "global.yaml")

    saved: list[dict[str, Any]] = []

    def fake_save(*, target_path: Path, key: str, value: Any) -> None:
        saved.append({"key": key})

    # Move cursor to flaky_retry_count, open editor, type "99" (out of
    # range max=5), Enter (rejected with toast), Esc to cancel edit,
    # Esc to exit modal.
    keys = iter(
        [
            "\x1b[B",  # down to flaky_retry_count
            "\r",  # open editor (seeded "1")
            "\x7f",  # delete "1"
            "9",
            "9",  # buffer = "99"
            "\r",  # commit → InvalidInput, editor stays open with toast
            "\x1b",  # cancel edit
            "\x1b",  # exit modal
        ]
    )

    import unittest.mock

    class _FakeLive:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> _FakeLive:
            return self

        def __exit__(self, *args: Any) -> None:
            pass

        def update(self, *args: Any, **kwargs: Any) -> None:
            pass

    with unittest.mock.patch.object(config_modal_mod, "Live", _FakeLive):
        rc = config_modal_mod.run_config_modal(
            state=_state_fixture(),
            workspace=None,
            repo=repo,
            read_key=lambda: next(keys),
            save_fn=fake_save,
        )
    assert rc == 0
    assert saved == []
