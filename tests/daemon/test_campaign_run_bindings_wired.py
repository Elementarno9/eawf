"""Tests for the P30-I18 campaign-run bindings + their idle-contract gate rows.

Covers the two formerly-idle contracts the binding pass turned on inside
:func:`eawf.runtime.daemon.methods.research.run_campaign`:

* the **state.claims fold** -- a live round reconciles its findings into the
  canonical ``state.claims`` through the daemon-owned writer
  (:func:`eawf.runtime.daemon.methods.state._commit_worktree_state`), not a
  throwaway ``State.model_construct`` shadow.
* the **L1 carryover prune** -- ``run_campaign`` calls
  :func:`eawf.kernel.spec.pruning.prune_round_carryover` between rounds, giving
  the formerly-zero-caller reducer a production caller.

The source-scan tests below are the asserting-test discharge the idle-contract
meta-gate requires for the two new ``check_*`` contracts.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    StagedDispatch,
    stage_campaign,
)
from eawf.kernel.state.enums import ClaimStatus, StoreKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    create_campaign,
    run_campaign,
)
from eawf.workflow.evidence._io import load_state

pytestmark = pytest.mark.unit


def _state_payload() -> dict[str, object]:
    """Minimal valid State payload with a project + no claims yet."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:QR",
        "updated_at": "2026-06-11T12:00:00+00:00",
        "project": {
            "code": "QR",
            "slug": "qr",
            "title": "QR",
            "description": None,
            "domains": ["market-structure", "pricing-models"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:QR",
        },
        "current": {"project_code": "QR"},
        "workspace": None,
        "phases": {},
        "iters": {},
        "waves": {},
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _build_ctx(tmp_path: Path) -> tuple[MethodContext, Path]:
    """A daemon context wired for the canonical state writer (state + WAL + bus)."""
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_bytes(orjson.dumps(_state_payload()))
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    ctx = MethodContext(
        started_at="2026-06-11T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=store_path(state_path, StoreKind.EVENT),
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    return ctx, state_path


def _run(body: Callable[[], Awaitable[None]]) -> None:
    asyncio.run(body())


def _block() -> ResearchProfileBlock:
    return ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )


def _stage_params(campaign_id: str) -> dict[str, Any]:
    block = _block()
    campaign = stage_campaign("options-pricing landscape", block)
    return {
        "campaign_id": campaign_id,
        "config": block.model_dump(mode="json"),
        "campaign": campaign.model_dump(mode="json"),
    }


def _agent_end_body(domain: str) -> dict[str, object]:
    return {
        "role": "researcher",
        "verdict": "pass",
        "confidence": "medium",
        "summary": f"surveyed {domain}",
        "question": f"what does {domain} reveal",
        "findings": [f"{domain} converges on a stable answer"],
        "recommendation": f"pursue {domain}",
        "evidence_refs": [{"kind": "store_record", "ref": f"src/{domain}.py:1"}],
    }


def _produce_findings(dispatch: StagedDispatch) -> Mapping[str, object]:
    return _agent_end_body(dispatch.domain)


# --------------------------------------------------------------------------
# Behavioural binding: a live run folds claims into canonical state.claims
# --------------------------------------------------------------------------


def test_run_campaign_folds_claims_into_canonical_state(tmp_path: Path) -> None:
    """A live round writes Claim rows to state.claims via the canonical writer.

    The W05/W06 binding: with a real on-disk state + a WAL dir (the canonical
    writer's preconditions) ``run_campaign`` folds each round's reconciled claims
    into ``state.claims`` -- the rows are reconstructible off the persisted state,
    not lost to a throwaway shadow.
    """
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _stage_params("campaign-fold"))
        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="campaign-fold", round_budget=2),
            produce_agent_end=_produce_findings,
        )
        # 1 finding/domain x 2 domains = 2 distinct claims; round 2 re-surfaced
        # the same two findings and was compacted (W20 dedup), so the ledger
        # stays at 2 rather than growing to 4.
        assert len(result["claim_ids"]) == 2
        state = load_state(state_path)
        assert state.claims is not None
        # Every reconciled claim id resolves to a real OPEN Claim row on state.
        for claim_id in result["claim_ids"]:
            assert claim_id in state.claims, claim_id
            assert state.claims[claim_id].status is ClaimStatus.OPEN
        assert len(state.claims) == 2

    _run(body)


# --------------------------------------------------------------------------
# Idle-contract gate rows: the two new source-scan checks
# --------------------------------------------------------------------------


def _load_idle_gate() -> Any:
    """Load ``tools/idle_contract_gate.py`` by path (``tools/`` is not a package).

    Mirrors the loader in ``tests/daemon/test_fleet_ladders_wired.py`` so the new
    P30-I18 source-scan checks (``check_campaign_claim_fold_wired`` /
    ``check_campaign_carryover_prune_wired``) carry an asserting test -- the
    idle-contract meta-gate requires every newly-defined ``check_*`` contract to
    be referenced by a test, and this is that reference.
    """
    import importlib.util
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    gate_path = repo_root / "tools" / "idle_contract_gate.py"
    if str(gate_path.parent) not in sys.path:
        sys.path.insert(0, str(gate_path.parent))
    spec = importlib.util.spec_from_file_location("idle_contract_gate", gate_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["idle_contract_gate"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_claim_fold_gate_passes_on_wired_source() -> None:
    """The claim-fold idle gate passes on the live wired source.

    The gate scans ``run_campaign`` for both the ``_commit_worktree_state(`` call
    and the ``reconcile_round_claims(state`` fold against the live state; the
    shipped source carries both, so the gate is green.
    """
    mod = _load_idle_gate()
    assert mod.check_campaign_claim_fold_wired().passed


def test_claim_fold_gate_reds_when_reverted_to_shadow() -> None:
    """The claim-fold idle gate reds when the reconcile reverts to the shadow.

    A source that reconciles only against a throwaway ``State.model_construct``
    shadow (no canonical writer, no fold against the live state) re-idles the
    binding; the gate catches that regression.
    """
    mod = _load_idle_gate()
    regressed = (
        "    shadow = State.model_construct(claims={}, open_questions={}, project=None)\n"
        "    reconcile_round_claims(shadow, findings, scope_id=args.scope_id, now=now)\n"
    )
    result = mod.check_campaign_claim_fold_wired(module_text=regressed)
    assert not result.passed
    assert result.failure is mod.GateFailure.CAMPAIGN_CLAIM_FOLD_IDLE


def test_carryover_prune_gate_passes_on_wired_source() -> None:
    """The carryover-prune idle gate passes on the live wired source.

    The gate scans ``run_campaign`` for the ``prune_round_carryover(`` call that
    gives the L1 between-rounds reducer a production caller; the shipped source
    carries it.
    """
    mod = _load_idle_gate()
    assert mod.check_campaign_carryover_prune_wired().passed


def test_carryover_prune_gate_reds_when_call_dropped() -> None:
    """The carryover-prune idle gate reds when the prune call is dropped.

    A source with no ``prune_round_carryover(...)`` call leaves the L1 reducer at
    its prior zero-caller (idle) state; the gate catches the regression.
    """
    mod = _load_idle_gate()
    regressed = "    carried_claims.extend(round_claims)\n"
    result = mod.check_campaign_carryover_prune_wired(module_text=regressed)
    assert not result.passed
    assert result.failure is mod.GateFailure.CAMPAIGN_CARRYOVER_PRUNE_IDLE
