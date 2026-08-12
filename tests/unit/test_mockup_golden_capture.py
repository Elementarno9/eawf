"""Unit tests for the ``/mockup`` pick-time golden capture.

The pick handler :func:`eawf.workflow.skills.bodies.mockup.resolve_mockup_pick`
captures the operator-picked variant as an approved ASCII golden and
stamps the repo-relative path onto the wave-spec body. Pinned here:

- the written golden's bytes equal
  :func:`~eawf.surfaces.render.snapshot_normalize.normalize_snapshot` of
  the chosen variant layout (trailing newline aside), and the returned
  body carries the canonical ``mockup_golden_path``;
- the stamped path is rooted at the committed-tree golden home regardless
  of the filesystem ``output_dir`` the capture writes to;
- an invalid wave id and an empty (whitespace-only) layout are rejected
  with a contract-message substring.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from eawf.surfaces.render.snapshot_normalize import normalize_snapshot
from eawf.workflow.skills.bodies.mockup import (
    GOLDEN_DIR_REPO_REL,
    MockupVariant,
    mockup_golden_filename,
    resolve_mockup_pick,
)
from eawf.workflow.skills.bodies.wave_spec import WaveSpecBody

_WAVE_ID = "P30-I04-W07"

_LAYOUT = (
    "+----------------------+\n"
    "| Eae  P30 > I04 > W07 |\n"
    "+----------------------+\n"
    "| roadmap  16:04 UTC   |\n"
    "+----------------------+\n"
)


def _body() -> WaveSpecBody:
    return WaveSpecBody(verb="init", wave_id=_WAVE_ID)


def test_mockup_golden_filename_canonical_shape() -> None:
    """The golden filename keys on the wave id with the ``mockup_`` stem."""
    assert mockup_golden_filename(_WAVE_ID) == "mockup_P30-I04-W07.txt"


def test_mockup_golden_filename_rejects_non_wave_id() -> None:
    """A non-canonical wave id is rejected before any path is built."""
    with pytest.raises(ValueError, match="invalid wave id"):
        mockup_golden_filename("not-a-wave")


def test_resolve_mockup_pick_writes_normalized_golden(tmp_path: Path) -> None:
    """The written golden bytes equal normalize_snapshot of the layout."""
    chosen = MockupVariant(name="compact", layout=_LAYOUT)
    updated = resolve_mockup_pick(chosen, wave_id=_WAVE_ID, body=_body(), output_dir=tmp_path)
    golden_file = tmp_path / "mockup_P30-I04-W07.txt"
    assert golden_file.is_file()
    written = golden_file.read_text(encoding="utf-8")
    # The capture appends a single trailing newline (mirroring the snapshot
    # regen hatch); the content otherwise equals the normalised layout.
    assert written == normalize_snapshot(_LAYOUT) + "\n"
    # The volatile wall-clock cell was neutralised in the written golden.
    assert "16:04 UTC" not in written
    assert "HH:MM UTC" in written
    # The body is stamped with the repo-relative golden path.
    assert updated.mockup_golden_path == f"{GOLDEN_DIR_REPO_REL}/mockup_P30-I04-W07.txt"


def test_resolve_mockup_pick_stamps_repo_relative_path_not_output_dir(
    tmp_path: Path,
) -> None:
    """The stamped path is committed-tree-rooted, independent of output_dir."""
    chosen = MockupVariant(name="wide", layout=_LAYOUT)
    nested = tmp_path / "some" / "other" / "write" / "root"
    updated = resolve_mockup_pick(chosen, wave_id=_WAVE_ID, body=_body(), output_dir=nested)
    # The file lands under the (created) nested write root...
    assert (nested / "mockup_P30-I04-W07.txt").is_file()
    # ...but the stamped path always points at the committed-tree home.
    assert updated.mockup_golden_path == ("tests/snapshots/tui/golden/mockup_P30-I04-W07.txt")


def test_resolve_mockup_pick_returns_copy_leaving_input_unmutated(
    tmp_path: Path,
) -> None:
    """The input body is not mutated; a stamped copy is returned."""
    chosen = MockupVariant(name="compact", layout=_LAYOUT)
    original = _body()
    updated = resolve_mockup_pick(chosen, wave_id=_WAVE_ID, body=original, output_dir=tmp_path)
    assert original.mockup_golden_path is None
    assert updated.mockup_golden_path is not None
    assert updated is not original


def test_resolve_mockup_pick_rejects_empty_layout(tmp_path: Path) -> None:
    """A whitespace-only layout normalises to empty and is rejected."""
    # ``MockupVariant.layout`` has min_length=1, so a single space passes the
    # model floor yet carries no oracle: the pick handler rejects it.
    chosen = MockupVariant(name="blank", layout="   \n  \n")
    with pytest.raises(ValueError, match="empty mockup layout"):
        resolve_mockup_pick(chosen, wave_id=_WAVE_ID, body=_body(), output_dir=tmp_path)
    # No golden is written on the rejected path.
    assert not (tmp_path / "mockup_P30-I04-W07.txt").exists()


def test_resolve_mockup_pick_rejects_non_wave_id(tmp_path: Path) -> None:
    """A non-canonical wave id is rejected before any write."""
    chosen = MockupVariant(name="compact", layout=_LAYOUT)
    with pytest.raises(ValueError, match="invalid wave id"):
        resolve_mockup_pick(chosen, wave_id="P30-W07", body=_body(), output_dir=tmp_path)
