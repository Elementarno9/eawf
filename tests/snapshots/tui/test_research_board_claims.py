"""Golden snapshot for the research_board Options + Conflicts tabs.

The Research mode (digit ``3``) center pane carries an ``Options`` tab and a
``Conflicts`` tab projecting the scope's claim ledger grouped by evidence
verdict: a claim's :class:`~eawf.kernel.state.enums.ClaimStatus` is its
evidence verdict, so the ``SUPPORTED`` claims (live candidate answers backed
by supporting evidence) render under Options and the ``REFUTED`` claims (the
conflicts the campaign surfaced via contradicting evidence) render under
Conflicts. Each row carries its evidence-ref count.

The pure :func:`~eawf.surfaces.tui.modes.research_board.group_claims_by_evidence`
+ :func:`render_options` / :func:`render_conflicts` helpers are exercised
directly over typed :class:`~eawf.kernel.state.models.Claim` fixtures, and the
mounted pane is snapshotted over a seeded scope so a layout regression on the
populated tabs is caught. The render path never calls
:func:`~eawf.workflow.verify.readiness.compute` (it spawns subprocesses); the
claims come from a typed ``tmp_path`` state.

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_research_board_claims.py -q
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest

from eawf.kernel.spec.research import ResearchDepth
from eawf.kernel.spec.research_campaign import (
    ResearchDomainConfig,
    ResearchProfileBlock,
    stage_campaign,
)
from eawf.kernel.state.enums import (
    ClaimStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
    Claim,
    CurrentPointers,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.research_board import (
    CENTER_BODY_ID,
    NONE_YET,
    claim_sigil_markup,
    group_claims_by_evidence,
    render_conflicts,
    render_options,
)
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.eu_bar import DEFAULT_RENDER_MODE
from eawf.surfaces.tui.widgets.git_pane import GitFields

_SIZE = (120, 40)
_GOLDEN = Path(__file__).resolve().parent / "golden"
_T0 = datetime(2026, 5, 27, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _isolate_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point registry resolution at an empty ``tmp_path`` home."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.fixture(autouse=True)
def _stub_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the workspace git probe to a deterministic clean tree."""
    monkeypatch.setattr(
        "eawf.surfaces.tui.widgets.workspace_table.gather_git_fields",
        lambda _path: GitFields(
            branch="main", dirty="clean", ahead_behind="up-to-date", recent_commits=()
        ),
    )


def _claim(
    claim_id: str,
    title: str,
    *,
    status: ClaimStatus,
    evidence_refs: list[str] | None = None,
) -> Claim:
    """Build a claim row in *status* with optional evidence refs."""
    return Claim(
        id=claim_id,
        scope_id="QR",
        title=title,
        status=status,
        evidence_refs=evidence_refs or [],
        created_at=_T0,
    )


def _mixed_claims() -> tuple[Claim, ...]:
    """Two supported, one refuted, one open claim (each group non-empty)."""
    return (
        _claim(
            "CL-0001",
            "Implied vol surface is downward sloping in strike",
            status=ClaimStatus.SUPPORTED,
            evidence_refs=["docs/vol.md", "urn:eawf:v1:artifact:QR/AR-1"],
        ),
        _claim(
            "CL-0002",
            "Short-tenor smile is well fit by SABR",
            status=ClaimStatus.SUPPORTED,
            evidence_refs=["docs/sabr.md"],
        ),
        _claim(
            "CL-0003",
            "Term-structure arbitrage is exploitable intraday",
            status=ClaimStatus.REFUTED,
            evidence_refs=["docs/arb-refuted.md"],
        ),
        _claim(
            "CL-0004",
            "Overnight gamma decay drives the skew",
            status=ClaimStatus.OPEN,
        ),
    )


def _campaign_payload(campaign_id: str = "RC-0001") -> ResearchCampaignPayload:
    """Stage a two-domain campaign and wrap it in a persistable payload."""
    block = ResearchProfileBlock(
        default_depth=ResearchDepth.MEDIUM,
        domains={
            "market-structure": ResearchDomainConfig(focus="venues + flow"),
            "pricing-models": ResearchDomainConfig(depth=ResearchDepth.DEEP),
        },
    )
    campaign = stage_campaign("Survey the options-pricing landscape", block)
    return ResearchCampaignPayload(campaign_id=campaign_id, config=block, campaign=campaign)


def _seeded_state(claims: tuple[Claim, ...]) -> State:
    """Build a repo state carrying the given claims."""
    return State.model_validate(
        {
            "schema_version": "1.3",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:QR",
            "updated_at": _T0.isoformat(),
            "project": Project(
                code="QR",
                slug="quant-research",
                title="Quant Research",
                domains=["quant"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:QR",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="QR").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "claims": {c.id: c.model_dump(mode="json") for c in claims},
            "open_questions": None,
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_seeded_scope(tmp_path: Path, claims: tuple[Claim, ...]) -> Path:
    """Write a seeded state + a staged campaign store, return the state path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(_seeded_state(claims).model_dump_json(), encoding="utf-8")
    payload = _campaign_payload()
    envelope = Envelope(
        id=payload.campaign_id,
        kind=StoreKind.RESEARCH_CAMPAIGN,
        scope_id="QR",
        created_at=_T0,
        summary=f"campaign {payload.campaign_id}",
        payload=payload.model_dump(mode="json"),
    )
    path = store_path(state_path, StoreKind.RESEARCH_CAMPAIGN)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as handle:
        handle.write(envelope.model_dump_json().encode("utf-8") + b"\n")
    return state_path


# --------------------------------------------------------------------------
# group_claims_by_evidence -- the supporting / contradicting split
# --------------------------------------------------------------------------


def test_group_claims_by_evidence_splits_supported_and_refuted() -> None:
    """Supported claims land supporting; refuted land contradicting; rest drop."""
    supporting, contradicting = group_claims_by_evidence(_mixed_claims())
    assert {c.id for c in supporting} == {"CL-0001", "CL-0002"}
    assert {c.id for c in contradicting} == {"CL-0003"}


def test_group_claims_by_evidence_empty() -> None:
    """An empty ledger yields two empty groups (boundary)."""
    assert group_claims_by_evidence(()) == ((), ())


def test_group_claims_by_evidence_open_and_superseded_drop() -> None:
    """An open / superseded claim joins neither evidence group."""
    claims = (
        _claim("CL-O", "open one", status=ClaimStatus.OPEN),
        _claim("CL-S", "superseded one", status=ClaimStatus.SUPERSEDED),
    )
    supporting, contradicting = group_claims_by_evidence(claims)
    assert supporting == ()
    assert contradicting == ()


# --------------------------------------------------------------------------
# render_options / render_conflicts -- pure render helpers
# --------------------------------------------------------------------------


def test_render_options_lists_supported_with_ref_counts() -> None:
    """Options renders one row per supported claim with its evidence-ref count."""
    rendered = render_options(_mixed_claims())
    assert len(rendered.splitlines()) == 2
    assert "2 ref(s)" in rendered  # CL-0001 carries two refs
    assert "1 ref(s)" in rendered  # CL-0002 carries one ref
    assert "Implied vol surface is downward sloping in strike" in rendered


def test_render_conflicts_lists_refuted_with_ref_counts() -> None:
    """Conflicts renders one row per refuted claim with its lifecycle sigil + ref count."""
    rendered = render_conflicts(_mixed_claims())
    assert len(rendered.splitlines()) == 1
    # The refuted status renders as its lifecycle sigil, never the raw word.
    assert "refuted" not in rendered
    assert claim_sigil_markup(ClaimStatus.REFUTED, mode=DEFAULT_RENDER_MODE) in rendered
    assert "1 ref(s)" in rendered
    assert "Term-structure arbitrage is exploitable intraday" in rendered


def test_render_options_empty_is_none_yet() -> None:
    """No supported claim renders the none-yet sentinel."""
    only_open = (_claim("CL-O", "open one", status=ClaimStatus.OPEN),)
    assert render_options(only_open) == f"[$muted]{NONE_YET}[/]"


def test_render_conflicts_empty_is_none_yet() -> None:
    """No refuted claim renders the none-yet sentinel."""
    only_supported = (
        _claim("CL-S", "supported one", status=ClaimStatus.SUPPORTED, evidence_refs=["d"]),
    )
    assert render_conflicts(only_supported) == f"[$muted]{NONE_YET}[/]"


# --------------------------------------------------------------------------
# Snapshot: the campaign-stats center detail counts claims + conflicts
# --------------------------------------------------------------------------


def test_research_board_claims_snapshot(tmp_path: Path) -> None:
    """The mounted campaign-stats center detail counts the ledger's claims + conflicts.

    W03 retired the always-global Options / Conflicts tabs: the center pane now
    morphs by selection. The default campaign selection renders the campaign
    stats, whose CLAIMS band counts the scope's claims + the refuted-claim
    conflicts (the same evidence split :func:`group_claims_by_evidence` computes),
    scoped to the selection rather than one un-scoped soup.
    """
    claims = _mixed_claims()
    state_path = _write_seeded_scope(tmp_path, claims)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            from textual.widgets import Static

            detail = str(app.screen.query_one(f"#{CENTER_BODY_ID}", Static).render())
            # Four claims, one refuted -> "4 claim(s) . 1 conflict(s)".
            supporting, contradicting = group_claims_by_evidence(claims)
            assert len(supporting) == 2
            assert len(contradicting) == 1
            assert f"{len(claims)} claim(s)" in detail
            assert f"{len(contradicting)} conflict(s)" in detail
            # The stat bands render (a campaign-stats detail, not a claim soup).
            for band in ("ROUNDS", "QUESTIONS", "CLAIMS", "BUDGET"):
                assert band in detail
            assert_screen_snapshot(app, _GOLDEN / "research_board_claims.txt")

    asyncio.run(body())
