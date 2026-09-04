"""Reconciled research-depth vocabulary + ``research.default_depth`` wiring.

Pins the P30-I10-W13 acceptance contract:

- The depth vocabulary is a *single* canonical closed enum
  (``shallow | medium | deep | exhaustive``) -- the historical drift
  (``quick | normal | deep`` runner / ``shallow | normal | deep`` config
  leaf / the four-rung help text) is collapsed onto one
  :class:`~eawf.kernel.spec.research.ResearchDepth` every surface resolves
  against.
- The research stage READS the ``research.default_depth`` layered-config
  leaf when no ``--depth`` flag is supplied (closing the standing idle
  config contract where the leaf was registered but unread).
- A configured-but-unknown depth is *rejected* (``ValueError``), unlike a
  transient ``--depth`` flag typo which falls back to the default.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pytest

from eawf.kernel.spec.research import (
    DEFAULT_RESEARCH_DEPTH,
    RESEARCH_DEPTH_VALUES,
    ResearchDepth,
    coerce_research_depth,
    resolve_default_research_depth,
)
from eawf.workflow.skills.bodies.research import ResearchBody
from eawf.workflow.skills.engine import SkillContext, run_skill
from eawf.workflow.skills.research import ResearchSkill


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect the active state path into a sandbox under ``tmp_path``."""
    state_dir = tmp_path / ".ea"
    state_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("EA_STATE", str(state_dir / "state.json"))
    monkeypatch.setenv("EA_INSTRUMENT_PROBE", str(state_dir / "instrument-probe.json"))
    monkeypatch.delenv("EAWF_BLITZ_DEPTH", raising=False)
    return state_dir


def _ctx() -> SkillContext:
    return SkillContext(
        scope="urn:eawf:v1:state:QR/P00",
        session="urn:eawf:v1:store:QR/sessions/SES-1",
    )


def _patch_merge(monkeypatch: pytest.MonkeyPatch, merged: dict) -> None:
    """Force the research stage's layered-config merge to return *merged*."""
    monkeypatch.setattr(
        "eawf.kernel.config.layered.merge_config",
        lambda **_k: (merged, {}),
    )


# --- single canonical closed enum ----------------------------------------


def test_research_depth_is_the_canonical_closed_set() -> None:
    assert [d.value for d in ResearchDepth] == ["shallow", "medium", "deep", "exhaustive"]
    assert RESEARCH_DEPTH_VALUES == ("shallow", "medium", "deep", "exhaustive")


def test_default_depth_is_a_ladder_member() -> None:
    assert DEFAULT_RESEARCH_DEPTH is ResearchDepth.MEDIUM
    assert DEFAULT_RESEARCH_DEPTH.value in RESEARCH_DEPTH_VALUES


def test_legacy_drift_tokens_are_not_ladder_members() -> None:
    # The pre-reconciliation vocab used ``quick`` / ``normal``; neither is a
    # member of the single canonical ladder.
    for legacy in ("quick", "normal"):
        with pytest.raises(ValueError):
            ResearchDepth(legacy)


# --- resolve_default_research_depth (the config-leaf reader) --------------


def test_resolve_default_honors_config_key() -> None:
    depth = resolve_default_research_depth({"research": {"default_depth": "deep"}})
    assert depth is ResearchDepth.DEEP


def test_resolve_default_missing_research_block_falls_back() -> None:
    assert resolve_default_research_depth({}) is DEFAULT_RESEARCH_DEPTH


def test_resolve_default_missing_leaf_falls_back() -> None:
    resolved = resolve_default_research_depth({"research": {"auto_save": True}})
    assert resolved is DEFAULT_RESEARCH_DEPTH


def test_resolve_default_unknown_value_rejected() -> None:
    with pytest.raises(ValueError, match=r"research\.default_depth"):
        resolve_default_research_depth({"research": {"default_depth": "wat"}})


def test_resolve_default_each_ladder_token_round_trips() -> None:
    for token in RESEARCH_DEPTH_VALUES:
        assert resolve_default_research_depth({"research": {"default_depth": token}}) == token


# --- flag path stays lenient (unchanged contract) ------------------------


def test_flag_typo_falls_back_not_rejected() -> None:
    # A transient ``--depth`` typo must never abort a run -- only the config
    # leaf is strict.
    assert coerce_research_depth("wat") is DEFAULT_RESEARCH_DEPTH


# --- end-to-end through the research stage --------------------------------


def test_stage_reads_config_default_when_no_flag(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_merge(monkeypatch, {"research": {"default_depth": "shallow"}})
    env = run_skill(ResearchSkill(), _ctx())
    assert env.header.status == "ok", env.body
    body = ResearchBody.model_validate(cast(dict, env.body))
    # shallow -> 1 question slot, proving the config leaf (not the bare
    # medium constant) drove the resolution.
    assert len(body.questions) == 1


def test_stage_flag_overrides_config_default(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_merge(monkeypatch, {"research": {"default_depth": "shallow"}})
    ctx = _ctx()
    ctx.args = {"depth": "exhaustive"}
    env = run_skill(ResearchSkill(), ctx)
    assert env.header.status == "ok"
    body = ResearchBody.model_validate(cast(dict, env.body))
    # exhaustive flag wins over the shallow config default -> 4 slots.
    assert len(body.questions) == 4


def test_stage_default_medium_when_config_absent(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_merge(monkeypatch, {})
    env = run_skill(ResearchSkill(), _ctx())
    body = ResearchBody.model_validate(cast(dict, env.body))
    assert len(body.questions) == 2  # falls back to default depth=medium


def test_stage_rejects_unknown_config_depth(
    state_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_merge(monkeypatch, {"research": {"default_depth": "wat"}})
    env = run_skill(ResearchSkill(), _ctx())
    # The engine maps the resolver's ValueError onto a failed envelope.
    assert env.header.status == "failed"
    assert "research.default_depth" in env.body
