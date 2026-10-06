"""The P30-I20/I21 legacy-criteria drain corpus.

I20/I21 predate the typed-criteria floor, so their 130 criterion rows were
grandfathered as ``kind == legacy`` at migration. W25 drains that corpus
through the W24 converter (``eawf spec convert-legacy``): every row in the
range is now typed with a falsifying gate, or stays legacy carrying a named
non-convertible reason on ``waiver_reason`` — no silent legacy rows. With
the corpus honest, the repo opts into ODR blocking at iter close. The
drained corpus lives in the frozen epoch-1 document, which git no longer
carries, so what stays pinned here is the opt-in and its threading.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CONFIG_PATH = _REPO_ROOT / ".ea" / "config.yaml"


def test_repo_opts_into_odr_blocking_and_config_validates() -> None:
    """CR-02: .ea/config.yaml carries odr_blocking: true; layered load clean."""
    raw = yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8"))
    assert (raw.get("verify") or {}).get("odr_blocking") is True

    from eawf.kernel.config.layered import merge_config

    merged, _sources = merge_config(workspace=_REPO_ROOT, repo=_REPO_ROOT)
    assert (merged.get("verify") or {}).get("odr_blocking") is True


def test_repo_overlay_tightens_the_merged_verify_block(tmp_path: Path) -> None:
    """The repo opt-in ORs onto the profile block — tighten-only."""
    from eawf.platform.profiles.models import VerifyBlock
    from eawf.workflow.verify.readiness import _overlay_repo_verify_leaves

    base = VerifyBlock()
    assert base.odr_blocking is False
    tightened = _overlay_repo_verify_leaves(base, {"verify": {"odr_blocking": True}})
    assert tightened is not None and tightened.odr_blocking is True
    # A falsy overlay never loosens an already-blocking profile.
    blocking = VerifyBlock(odr_blocking=True)
    kept = _overlay_repo_verify_leaves(blocking, {"verify": {"odr_blocking": False}})
    assert kept is not None and kept.odr_blocking is True
    # No verify section leaves the block untouched.
    untouched = _overlay_repo_verify_leaves(base, {})
    assert untouched is base
