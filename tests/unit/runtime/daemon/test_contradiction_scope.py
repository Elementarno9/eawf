"""Tests that scope ``run_campaign``'s contradiction halt to the running campaign.

A research scope is shared by every campaign in it, and nothing ever clears a
REFUTED claim. So the halt must fire only on:

* the running campaign's OWN claims -- its carried ledger is seeded through
  :func:`~eawf.runtime.daemon.methods.research._campaign_claims`, never from
  the whole scope; and
* refutations recorded DURING the current run -- a refutation an earlier run
  already halted on stays REFUTED on the ledger but does not stop the next run
  after one round.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import pydantic
import pytest

from eawf.kernel.spec.campaign_driver import RoundFindings, ledger_saturation_reducer
from eawf.kernel.spec.research_campaign import StagedDispatch
from eawf.kernel.state.enums import CampaignStatus, ClaimStatus, StoreKind
from eawf.kernel.state.models import Claim
from eawf.kernel.store.kinds.research_round import ResearchRoundPayload
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import research as research_mod
from eawf.runtime.daemon.methods.research import (
    RunCampaignParams,
    _campaign_claims,
    create_campaign,
    persist_round,
    read_latest_campaign,
    run_campaign,
)
from eawf.workflow.evidence._io import load_state
from tests.unit.runtime.daemon.test_contradiction_live import (
    _build_ctx,
    _create_params,
    _create_then_refute_producer,
    _noop_producer,
    _pass_body,
)

pytestmark = pytest.mark.unit

_REFUTED_ID = "CLM-r1-market-structure-0"
_SECOND_ID = "CLM-r1-market-structure-1"


def _claim_status(state_path: Path, claim_id: str) -> ClaimStatus:
    claims = load_state(state_path).claims or {}
    return claims[claim_id].status


def _spy_carried_ledger(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Record the carried-ledger ids each ``run_campaign`` call's reducer scores.

    One entry per run: the ledger's ids as the run's first round scores them,
    which is the seed plus whatever that round touched.
    """
    seen: list[list[str]] = []
    real = ledger_saturation_reducer

    def _spy(
        claims_for_round: Callable[[RoundFindings], Sequence[Claim]],
        *args: object,
        **kwargs: object,
    ) -> object:
        first_round: list[bool] = [True]

        def _recording(findings: RoundFindings) -> Sequence[Claim]:
            claims = claims_for_round(findings)
            if first_round[0]:
                seen.append([claim.id for claim in claims])
                first_round[0] = False
            return claims

        return real(_recording, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(research_mod, "ledger_saturation_reducer", _spy)
    return seen


def _halted_campaign(ctx: object, campaign_id: str) -> None:
    """Run *campaign_id* until its own round-2 refutation halts it."""
    result = run_campaign(
        ctx,  # type: ignore[arg-type]
        RunCampaignParams(campaign_id=campaign_id, round_budget=3),
        produce_agent_end=_create_then_refute_producer(),
    )
    assert result["halt_reason"] == "contradiction"


# --------------------------------------------------------------------------
# run_campaign -- the halt is scoped to the running campaign
# --------------------------------------------------------------------------


def test_run_campaign_ignores_a_claim_another_campaign_refuted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: a sibling campaign's refuted claim never enters this run's ledger."""
    ctx, state_path = _build_ctx(tmp_path)
    seen = _spy_carried_ledger(monkeypatch)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-refuter"))
        await create_campaign(ctx, _create_params("rc-bystander"))
        _halted_campaign(ctx, "rc-refuter")

        result = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-bystander", round_budget=2),
            produce_agent_end=_noop_producer,
        )

        assert seen[-1] == []
        assert result["halt_reason"] != "contradiction"
        assert result["contradiction_offenders"] == []
        bystander = read_latest_campaign(state_path, "rc-bystander")
        assert bystander is not None and bystander.status is CampaignStatus.CONVERGED
        assert _claim_status(state_path, _REFUTED_ID) is ClaimStatus.REFUTED

    asyncio.run(body())


def test_run_campaign_resumed_run_proceeds_past_an_already_halted_refutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gate-fire proof: the next run of the same campaign is not stuck at round one."""
    ctx, state_path = _build_ctx(tmp_path)
    seen = _spy_carried_ledger(monkeypatch)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-resume"))
        _halted_campaign(ctx, "rc-resume")

        second = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-resume", round_budget=2),
            produce_agent_end=_noop_producer,
        )

        assert seen[-1] == [_REFUTED_ID]  # the campaign's own row is still seeded
        assert second["rounds_run"] == 2
        assert second["halt_reason"] != "contradiction"
        assert second["contradiction_offenders"] == []
        assert _claim_status(state_path, _REFUTED_ID) is ClaimStatus.REFUTED

    asyncio.run(body())


def _two_claims_then_refute_first() -> Callable[[StagedDispatch], Mapping[str, object]]:
    """Round 1 writes two claims; round 2 refutes the first."""
    calls: list[str] = []

    def _produce(dispatch: StagedDispatch) -> Mapping[str, object]:
        calls.append(dispatch.domain)
        if len(calls) == 1:
            body = dict(_pass_body(None))
            body["findings"] = ["dark pools dominate block volume", "lit venues set the price"]
            return body
        return _pass_body(None, refuted=[_REFUTED_ID])

    return _produce


def test_run_campaign_resumed_run_halts_only_on_its_own_new_refutation(tmp_path: Path) -> None:
    """A refutation recorded in THIS run still halts it, naming only that claim."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-again"))
        first = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-again", round_budget=3),
            produce_agent_end=_two_claims_then_refute_first(),
        )
        assert first["contradiction_offenders"] == [_REFUTED_ID]

        second = run_campaign(
            ctx,
            RunCampaignParams(campaign_id="rc-again", round_budget=3),
            produce_agent_end=lambda _dispatch: _pass_body(None, refuted=[_SECOND_ID]),
        )

        assert second["rounds_run"] == 1
        assert second["halt_reason"] == "contradiction"
        assert second["contradiction_offenders"] == [_SECOND_ID]
        latest = read_latest_campaign(state_path, "rc-again")
        assert latest is not None and latest.status is CampaignStatus.ACTIVE

    asyncio.run(body())


# --------------------------------------------------------------------------
# _campaign_claims -- boundary + error paths of the seed
# --------------------------------------------------------------------------


def test_campaign_claims_empty_without_persisted_rounds(tmp_path: Path) -> None:
    """A campaign that never ran owns no claims, even with claims in the scope."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-owner"))
        _halted_campaign(ctx, "rc-owner")

    asyncio.run(body())

    assert _campaign_claims(state_path, None, "rc-fresh", fold_into_state=True) == []


def test_campaign_claims_empty_when_not_folding_into_state(tmp_path: Path) -> None:
    """No state fold means no persisted ledger to seed from."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-owner"))
        _halted_campaign(ctx, "rc-owner")

    asyncio.run(body())

    assert _campaign_claims(state_path, None, "rc-owner", fold_into_state=False) == []


def test_campaign_claims_returns_only_the_campaigns_own_rows(tmp_path: Path) -> None:
    """A single owned claim comes back; a scope row no round of it named does not."""
    ctx, state_path = _build_ctx(tmp_path)

    async def body() -> None:
        await create_campaign(ctx, _create_params("rc-owner"))
        _halted_campaign(ctx, "rc-owner")

    asyncio.run(body())
    persist_round(
        state_path,
        ResearchRoundPayload(
            campaign_id="rc-other",
            round_number=1,
            domains=["market-structure"],
            finding_lines=[],
            claim_ids=["CLM-not-on-the-ledger"],
            saturated=False,
            checkpoint=False,
            recorded_at=datetime(2026, 9, 25, tzinfo=UTC),
        ),
    )

    owned = _campaign_claims(state_path, None, "rc-owner", fold_into_state=True)
    other = _campaign_claims(state_path, None, "rc-other", fold_into_state=True)

    assert [claim.id for claim in owned] == [_REFUTED_ID]
    assert all(isinstance(claim, Claim) for claim in owned)
    assert other == []


def test_campaign_claims_raises_on_a_corrupt_round_store(tmp_path: Path) -> None:
    """A malformed round row fails loudly rather than seeding a partial ledger."""
    _ctx, state_path = _build_ctx(tmp_path)
    rounds = store_path(state_path, StoreKind.RESEARCH_ROUND)
    rounds.parent.mkdir(parents=True, exist_ok=True)
    rounds.write_text('{"not": "an envelope"}\n', encoding="utf-8")

    with pytest.raises(pydantic.ValidationError):
        _campaign_claims(state_path, None, "rc-owner", fold_into_state=True)
