"""The P30-I20/I21 legacy-criteria drain corpus.

I20/I21 predate the typed-criteria floor, so their 130 criterion rows were
grandfathered as ``kind == legacy`` at migration. W25 drains that corpus
through the W24 converter (``eawf spec convert-legacy``): every row in the
range is now typed with a falsifying gate, or stays legacy carrying a named
non-convertible reason on ``waiver_reason`` — no silent legacy rows. With
the corpus honest, the repo opts into ODR blocking at iter close.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_REPO_ROOT = Path(__file__).resolve().parents[2]
_STATE_PATH = _REPO_ROOT / ".ea" / "state.json"
_CONFIG_PATH = _REPO_ROOT / ".ea" / "config.yaml"

#: P30-I21-W22 is the pending v0.6.0 phase re-close wave — retyped by the
#: W36 runbook, deliberately outside this drain corpus.
_EXCLUDED = frozenset({"P30-I21-W22"})


def _corpus_waves(state: dict) -> dict[str, dict]:
    return {
        wave_id: wave
        for wave_id, wave in (state.get("waves") or {}).items()
        if (wave_id.startswith("P30-I20-") or wave_id.startswith("P30-I21-"))
        and wave_id not in _EXCLUDED
    }


def test_no_silent_legacy_rows_survive_in_the_corpus() -> None:
    """CR-01: every corpus row is typed, or reason-tagged legacy."""
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    corpus = _corpus_waves(state)
    assert corpus, "the I20/I21 corpus is empty — the drain premise is gone"
    silent: list[str] = []
    rows = 0
    for wave_id, wave in corpus.items():
        for criterion in wave.get("success_criteria") or []:
            rows += 1
            if criterion.get("kind") == "legacy" and not criterion.get("waiver_reason"):
                silent.append(f"{wave_id}/{criterion.get('id')}")
    assert rows >= 100, f"corpus unexpectedly thin ({rows} rows)"
    assert not silent, f"silent legacy rows survive: {silent[:10]}"


def test_converted_rows_carry_a_falsifying_gate() -> None:
    """CR-01: a retyped row is gated — conversion is not a kind rename."""
    state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
    for wave_id, wave in _corpus_waves(state).items():
        gate_ids = {gate.get("id") for gate in wave.get("gates") or []}
        for criterion in wave.get("success_criteria") or []:
            if criterion.get("kind") == "legacy":
                continue
            refs = criterion.get("gate_ids") or []
            assert refs, f"{wave_id}/{criterion.get('id')} converted but gateless"
            missing = [ref for ref in refs if ref not in gate_ids]
            assert not missing, f"{wave_id}/{criterion.get('id')} dangling gates {missing}"


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


def test_daemon_iter_close_threads_the_odr_leaves() -> None:
    """The absorbed W23 follow-up: _apply_iter_close consumes the threaded
    params instead of dropping them at close_iter's defaults."""
    from unittest.mock import patch

    from eawf.kernel.state.models import State
    from eawf.kernel.state.mutations import Mutation, MutationKind
    from eawf.runtime.daemon.methods.state import _apply_iter_close

    state = State.model_validate_json(_STATE_PATH.read_text(encoding="utf-8"))
    mutation = Mutation(
        kind=MutationKind.ITER_CLOSE,
        scope_id="P30-I23",
        mutation_id="m" * 32,
        params={
            "iter_id": "P30-I23",
            "audit_id": "A-P30-i22",
            "odr_floor": 0.85,
            "odr_blocking": True,
        },
    )
    with patch("eawf.runtime.daemon.methods.state.close_iter") as close_iter_mock:
        _apply_iter_close(state, mutation)
    kwargs = close_iter_mock.call_args.kwargs
    assert kwargs["odr_floor"] == pytest.approx(0.85)
    assert kwargs["odr_blocking"] is True
