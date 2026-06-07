"""Golden snapshot + binding parity for the research_board n / x keys (P29-I13-W15).

The Research mode (digit ``3``) advertises two campaign-lifecycle action
keys in its footer: ``n`` (new-campaign) and ``x`` (cancel-campaign). Two
criteria pin the affordance:

* **CR-01** -- the footer hint strip advertises ``n new`` and ``x cancel``;
  a golden snapshot of the mounted pane (wide enough that the full strip
  renders unclipped) proves both labels are present.
* **CR-02** -- both keys resolve to a live :class:`~textual.binding.Binding`
  on the mounted research_board screen (``n`` -> ``new_campaign``, ``x`` ->
  ``cancel_campaign``), so the advertised affordances are never dead. This
  is the affordance_parity guarantee at the binding level.

The snapshot is built from a typed scope state (a staged campaign plus a
claim + an open question, written to a ``tmp_path`` store) so the render is
deterministic and never calls
:func:`~eawf.workflow.verify.readiness.compute` (which spawns subprocesses).

Regenerate the golden after an intentional layout change with::

    EAWF_SNAPSHOT_REGEN=1 uv run pytest tests/snapshots/tui/test_research_board_bindings.py -q
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
    OpenQuestionStatus,
    ProjectStatus,
    ScopeKind,
    StoreKind,
)
from eawf.kernel.state.models import (
    Claim,
    CurrentPointers,
    OpenQuestion,
    Project,
    State,
)
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.kinds.research_campaign import ResearchCampaignPayload
from eawf.kernel.store.paths import store_path
from eawf.surfaces.tui.app import EaApp
from eawf.surfaces.tui.modes.research_board import ResearchBoardModeScreen
from eawf.surfaces.tui.snapshot import (
    assert_screen_snapshot,
    capture_screen_text,
    normalize_snapshot,
    settle_screen,
)
from eawf.surfaces.tui.widgets.git_pane import GitFields

# A wide terminal so the full footer hint strip renders without clipping the
# trailing ``x cancel`` token -- at 120 cols the strip truncates before ``x``.
_SIZE = (200, 40)

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


def _seeded_state() -> State:
    """Build a repo state carrying one claim + one open question."""
    claim = Claim(
        id="CL-0001",
        scope_id="QR",
        title="Implied vol surface is downward sloping in strike",
        status=ClaimStatus.OPEN,
        created_at=_T0,
    )
    question = OpenQuestion(
        id="OQ-0001",
        scope_id="QR",
        title="Which curve model fits the short tenor",
        status=OpenQuestionStatus.OPEN,
        created_at=_T0,
    )
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
            "claims": {"CL-0001": claim.model_dump(mode="json")},
            "open_questions": {"OQ-0001": question.model_dump(mode="json")},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _write_seeded_scope(tmp_path: Path) -> Path:
    """Write a seeded state + a staged campaign store, return the state path."""
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_text(_seeded_state().model_dump_json(), encoding="utf-8")
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
# CR-01 (pure): the authored footer hint tuple advertises n new + x cancel
# --------------------------------------------------------------------------


def test_research_board_footer_advertises_new_and_cancel() -> None:
    """The authored footer hints carry ``n new`` and ``x cancel``."""
    hints = " ".join(ResearchBoardModeScreen.FOOTER_HINTS)
    assert "n new" in hints
    assert "x cancel" in hints


# --------------------------------------------------------------------------
# CR-02: n / x resolve to live screen bindings on the mounted pane
# --------------------------------------------------------------------------


def test_research_board_n_x_resolve_to_live_bindings(tmp_path: Path) -> None:
    """``n`` -> new_campaign and ``x`` -> cancel_campaign resolve on the screen."""
    state_path = _write_seeded_scope(tmp_path)

    async def body() -> tuple[str | None, str | None]:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            assert isinstance(app.screen, ResearchBoardModeScreen)
            n_entry = app.screen.active_bindings.get("n")
            x_entry = app.screen.active_bindings.get("x")
            return (
                n_entry.binding.action if n_entry is not None else None,
                x_entry.binding.action if x_entry is not None else None,
            )

    n_action, x_action = asyncio.run(body())
    assert n_action == "new_campaign"
    assert x_action == "cancel_campaign"


# --------------------------------------------------------------------------
# CR-01 (snapshot): the rendered footer shows n new + x cancel
# --------------------------------------------------------------------------


def test_research_board_bindings_snapshot(tmp_path: Path) -> None:
    """The mounted research_board footer renders ``n new`` and ``x cancel``."""
    state_path = _write_seeded_scope(tmp_path)

    async def body() -> None:
        app = EaApp(scope="repo", state_path=state_path)
        async with app.run_test(size=_SIZE) as pilot:
            await settle_screen(pilot)
            await pilot.press("3")  # -> research_board
            await settle_screen(pilot)
            frame = normalize_snapshot(capture_screen_text(app))
            # The footer advertises both campaign-lifecycle action keys.
            assert "n new" in frame
            assert "x cancel" in frame
            assert_screen_snapshot(app, _GOLDEN / "research_board_bindings.txt")

    asyncio.run(body())
