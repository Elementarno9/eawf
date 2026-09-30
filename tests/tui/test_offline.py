"""Tests for the ``workspace registry-status`` text frame.

Covers :func:`~eawf.surfaces.tui.chassis.offline.offline_render`: the ``Eä`` /
``roadmap`` / ``backlog`` titles, the ``--width`` wrap and the ``registry
unavailable`` placeholder.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import orjson

from eawf.surfaces.render.brand import render_wordmark_ansi
from eawf.surfaces.tui.chassis.offline import offline_render
from eawf.surfaces.tui.chassis.sigils import chrome

#: The two-tone green brand head every offline frame now leads with. Asserting
#: the full wordmark (not the bare ``Eä`` literal) keeps these contracts in
#: lockstep with the W32 reskin -- the bare literal is no longer contiguous
#: because the ANSI accent escape sits between the ``E`` and the ``ä``.
_WORDMARK = render_wordmark_ansi()

#: The leading brand glyph (UX-19): the offline frame now gains the ``◉`` brand
#: mark the live header leads with, a single space before the wordmark. The
#: frame heads ``◉ E<accent>ä<reset>  <breadcrumb>``.
_BRAND_HEAD = f"{chrome('brand', mode='unicode')} {_WORDMARK}"

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


# --------------------------------------------------------------------------
# UX-19: the offline frame GAINS the header brand glyph
# --------------------------------------------------------------------------


def test_dashboard_frame_gains_header_brand_glyph(tmp_path: Path) -> None:
    rendered = offline_render(registry_path=tmp_path / "absent.json")
    assert rendered.startswith(_BRAND_HEAD)
    assert rendered.startswith(chrome("brand", mode="unicode"))
