"""Tests for the ``tui`` non-interactive text renderers.

Covers the two deterministic surfaces in :mod:`eawf.surfaces.tui.offline`:

* :func:`build_status_text` + :func:`emit_status` — the non-TTY status
  frame contract (``Eä`` brand + ``keymap:`` line + lifecycle counts);
* :func:`offline_render` — the ``workspace registry-status`` text frame
  contract (``Eä`` / ``roadmap`` / ``backlog`` titles, ``--width`` wrap,
  ``registry unavailable`` placeholder).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson

from eawf.kernel.state.models import State
from eawf.surfaces.render.brand import render_wordmark_ansi
from eawf.surfaces.tui.offline import build_status_text, emit_status, offline_render

#: The two-tone green brand head every offline frame now leads with. Asserting
#: the full wordmark (not the bare ``Eä`` literal) keeps these contracts in
#: lockstep with the W32 reskin -- the bare literal is no longer contiguous
#: because the ANSI accent escape sits between the ``E`` and the ``ä``.
_WORDMARK = render_wordmark_ansi()

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "states" / "valid"
_PHASE_ITER_WAVE = _FIXTURES / "03-phase-iter-wave-active.json"


def _load(path: Path) -> State:
    return State.model_validate(orjson.loads(path.read_bytes()))


# --------------------------------------------------------------------------
# build_status_text
# --------------------------------------------------------------------------


def test_build_status_text_none_state_carries_brand_and_keymap() -> None:
    text = build_status_text(None)
    assert text.startswith(_WORDMARK)
    assert "keymap:" in text
    # Fresh-workspace placeholder code + all-zero counts.
    assert "project=EAWF" in text
    assert "phases_open=0" in text
    assert "waves_pending=0" in text


def test_build_status_text_active_fixture_counts() -> None:
    text = build_status_text(_load(_PHASE_ITER_WAVE))
    assert text.startswith(_WORDMARK)
    assert "project=QR" in text
    assert "phases_open=1" in text
    assert "iters_open=1" in text
    assert "iters_closed=0" in text
    # The fixture's single wave is in_progress, so pending is zero.
    assert "waves_pending=0" in text
    assert "audits=0" in text


def test_build_status_text_has_three_lines() -> None:
    lines = build_status_text(None).split("\n")
    assert len(lines) == 3
    assert lines[0].startswith(_WORDMARK)
    assert lines[2].startswith("keymap:")


# --------------------------------------------------------------------------
# emit_status
# --------------------------------------------------------------------------


def test_emit_status_missing_state_returns_zero(tmp_path: Path, capsys: object) -> None:
    rc = emit_status(workspace=tmp_path, no_input=False, plain=True)
    assert rc == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert _WORDMARK in captured.out
    assert "keymap:" in captured.out


def test_emit_status_reads_workspace_state(tmp_path: Path, capsys: object) -> None:
    ea = tmp_path / ".ea"
    ea.mkdir()
    (ea / "state.json").write_bytes(_PHASE_ITER_WAVE.read_bytes())
    rc = emit_status(workspace=tmp_path)
    assert rc == 0
    captured = capsys.readouterr()  # type: ignore[attr-defined]
    assert "project=QR" in captured.out
    assert "phases_open=1" in captured.out


# --------------------------------------------------------------------------
# offline_render — registry dashboard text frame
# --------------------------------------------------------------------------


def _write_registry(tmp_path: Path, *, active: str | None) -> Path:
    repo = tmp_path / "eawf"
    (repo / ".ea").mkdir(parents=True)
    (repo / ".ea" / "state.json").write_text(
        orjson.dumps({"scope_kind": "repo", "project": {"code": "EAWF"}}).decode()
    )
    payload = {
        "version": "1",
        "updated_at": datetime.now(UTC).isoformat(),
        "active_code": active,
        "repos": {"EAWF": {"code": "EAWF", "path": str(repo), "title": "Eä"}},
    }
    target = tmp_path / "registry.json"
    target.write_bytes(orjson.dumps(payload))
    return target


def test_offline_render_carries_brand_and_section_titles(tmp_path: Path) -> None:
    target = _write_registry(tmp_path, active="EAWF")
    rendered = offline_render(registry_path=target, now=datetime.now(UTC))
    assert _WORDMARK in rendered
    assert "EAWF" in rendered
    # Quadrant section titles the CLI contract asserts on.
    assert "roadmap" in rendered
    assert "backlog" in rendered
    assert "(active)" in rendered


def test_offline_render_missing_registry_placeholder(tmp_path: Path) -> None:
    rendered = offline_render(registry_path=tmp_path / "absent.json")
    assert "registry unavailable" in rendered
    assert _WORDMARK in rendered
    # Section titles still render so the frame stays deterministic.
    assert "roadmap" in rendered
    assert "backlog" in rendered


def test_offline_render_width_changes_output(tmp_path: Path) -> None:
    target = _write_registry(tmp_path, active="EAWF")
    now = datetime.now(UTC)
    narrow = offline_render(registry_path=target, width=20, now=now)
    wide = offline_render(registry_path=target, width=200, now=now)
    assert narrow != wide


def test_offline_render_ends_with_newline(tmp_path: Path) -> None:
    rendered = offline_render(registry_path=tmp_path / "absent.json")
    assert rendered.endswith("\n")
