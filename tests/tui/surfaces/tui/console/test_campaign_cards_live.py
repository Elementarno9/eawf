"""The Campaign route, its step and artifact cards and its replaying frame, drawn live.

CON-138, CON-140, CON-142, UI-073 and PRX-066 on the console. A Campaign is written
through the real ``runtime.campaign.*`` verbs over a provisioned canary: a three-step
plan, step 1 run to done, the report it wrote recorded as an artifact and one finding
promoted. The console is served that tree over a private socket as ``eawf ui`` would, and
the seam reads it through ``projection.campaign.view`` and ``projection.campaign.artifact``.
Every card below is opened with the keys an operator presses, so every word asserted came
from the producer, not from a fixture.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.campaign import promoting_sequences
from eawf.kernel.projection.connection import ConnectionValue, ReconnectDisposition
from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.methods.campaign import (
    CAMPAIGN_ARTIFACT_RECORD_METHOD,
    CAMPAIGN_FINDING_PROMOTE_METHOD,
    CAMPAIGN_PLAN_APPROVE_METHOD,
    CAMPAIGN_STEP_UPDATE_METHOD,
)
from eawf.runtime.daemon.methods.run import RUN_EVENT_APPEND_METHOD
from eawf.surfaces.tui.console.decisions import size_words
from eawf.surfaces.tui.console.live_reads import ARTIFACT_READ, CAMPAIGN_READ
from eawf.surfaces.tui.console.session import SIZES, SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import live_console, render_setup

SLOT: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
CAMPAIGN: Final = f"{SLOT}/campaign/CAM-0001"
RUNNER: Final = f"{SLOT}/run/RUN-00000010"
QUESTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"
CONTRADICTION: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/claim/CLM-0004"
REPORT_NAME: Final = "research/replay-order.md"
REPORT: Final = (
    "# Replay order\n\n- 40 events compared\n- 0 inversions\n\n| run | order |\n|---|---|\n"
)
LONG: Final = "".join(f"line {n} of the long report · kept exactly as written\n" for n in range(60))
OUTCOME: Final = "Order holds across restarts"
LEARNED: Final = "Replay keeps event order across daemon restarts"
LATER: Final = "Replay order survives a provider switch"
SAID: Final = ("Replaying forty recorded events", "No event arrived out of order")


def _call(root: Path, runtime: Path, method: str, **params: Any) -> dict[str, Any]:
    ctx = method_context(runtime)
    answer: dict[str, Any] = asyncio.run(
        methods.dispatch(method, ctx, {"repo_root": str(root), **params})
    )
    return answer


def _step(ordinal: int, **extra: Any) -> dict[str, Any]:
    return {
        "ordinal": ordinal,
        "title": f"Survey replay order source {ordinal}",
        "method": "survey",
        "question_ref": QUESTION,
        "bound": {"axes": [{"axis_kind": "wall_time", "limit": 2, "unit": "h"}]},
        **extra,
    }


def _update(root: Path, runtime: Path, document: Path, **params: Any) -> None:
    revision = read_document(document)["campaign"]["CAM-0001"]["revision"]
    _call(
        root,
        runtime,
        CAMPAIGN_STEP_UPDATE_METHOD,
        actor="AG-0001",
        urn=CAMPAIGN,
        expected_revision=revision,
        **params,
    )


def _record(root: Path, runtime: Path, name: str, content: str) -> None:
    (root / name).parent.mkdir(parents=True, exist_ok=True)
    (root / name).write_text(content)
    _call(
        root,
        runtime,
        CAMPAIGN_ARTIFACT_RECORD_METHOD,
        actor="AG-0001",
        urn=CAMPAIGN,
        file_name=name,
        media_kind="markdown",
        run_ref=RUNNER,
        step_ordinal=1,
    )


def _promote(root: Path, runtime: Path, statement: str) -> None:
    _call(
        root,
        runtime,
        CAMPAIGN_FINDING_PROMOTE_METHOD,
        actor="OP-0001",
        urn=CAMPAIGN,
        statement=statement,
    )


@pytest.fixture
def tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A canary holding one Campaign: step 1 done with its report, one finding promoted."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    canary = provision(tmp_path / "tree", code="CAM")
    seed(
        canary,
        {
            "track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")},
            "run": {"RUN-00000010": seed_row("run", "RUNNING")},
        },
    )
    root, runtime, document = canary.root, tmp_path / "runtime", document_path(canary)
    _call(
        root,
        runtime,
        CAMPAIGN_PLAN_APPROVE_METHOD,
        actor="OP-0001",
        track_ref=f"{SLOT}/track/TRK-RUNTIME",
        title="Establish whether replay preserves event order",
        evidence_budget={"axes": [{"axis_kind": "wall_time", "limit": 6, "unit": "h"}]},
        seed_questions=[{"urn": QUESTION, "question": "Does replay preserve event order?"}],
        plan_steps=[
            _step(1),
            _step(2, depends_on=[1]),
            _step(3, blocking_contradiction_refs=[CONTRADICTION]),
        ],
    )
    _update(
        root,
        runtime,
        document,
        ordinal=1,
        to_state="running",
        run_ref=RUNNER,
        spent={"wall_time": 1},
    )
    for sequence, summary in enumerate(SAID, start=1):
        _call(
            root,
            runtime,
            RUN_EVENT_APPEND_METHOD,
            urn=RUNNER,
            actor="OP-0001",
            event_ref=f"EVT-0000000{sequence}",
            run_sequence=sequence,
            event_kind="message_summarized",
            payload={
                "payload_kind": "message_summary",
                "message_role": "assistant",
                "summary": summary,
            },
        )
    _record(root, runtime, REPORT_NAME, REPORT)
    _record(root, runtime, "research/long-report.md", LONG)
    _update(root, runtime, document, ordinal=1, to_state="done", outcome=OUTCOME)
    _promote(root, runtime, LEARNED)
    return root, runtime


async def _settle(app: Any, pilot: Any) -> str:
    # the console's own sync worker reads the route, then the Campaign it owes
    for _ in range(3):
        await app.workers.wait_for_complete()
        app.render_frame()
        await pilot.pause()
    return "\n".join(app.frame_rows)


def _drive(root: Path, runtime: Path, setup: SessionSetup, keys: tuple[str, ...] = ()) -> list[str]:
    """Open *setup* on a live console, then press *keys*; return the frame after each."""

    async def body() -> list[str]:
        async with (
            live_console(root, runtime) as (app, _seam),
            app.run_test(size=SIZES[setup.size]) as pilot,
        ):
            await render_setup(app, pilot, setup)
            frames = [await _settle(app, pilot)]
            for key in keys:
                app.press_key(key)
                frames.append(await _settle(app, pilot))
            return frames

    return asyncio.run(body())


def _row(frame: str, start: str) -> str:
    return next(line for line in frame.splitlines() if line.lstrip(" ▸│").startswith(start))


def _on(size: int = 2) -> SessionSetup:
    return SessionSetup(route="campaign", subj_id="CAM-0001", size=size)


def test_con_140_live_the_campaign_route_draws_the_plan_the_verbs_wrote(
    tree: tuple[Path, Path],
) -> None:
    (frame,) = _drive(*tree, _on())
    assert "the campaign plan has not been read yet" not in frame
    assert "1 of 3 steps done · 0 running · 1 blocked by CLM-0004" in _row(frame, "PLAN")
    assert "✓ done" in _row(frame, "1 Survey replay order source 1")
    assert "CLM-0004" in _row(frame, "3 Survey replay order source 3")
    assert "replay-order.md" in frame and "by step 1" in _row(frame, "research/replay-order.md")
    assert LEARNED in _row(frame, "CFN-0001")
    assert "held · promoted at" in _row(frame, "CFN-0001")


def test_con_140_live_the_bounds_row_draws_the_budget_the_accountant_charged(
    tree: tuple[Path, Path],
) -> None:
    (frame,) = _drive(*tree, _on())
    bounds = _row(frame, "BOUNDS")
    assert "wall time 1 of 6 h · ≈5 left" in bounds
    assert "no bound is stated" not in frame


def test_con_140_live_enter_on_a_step_opens_its_card_and_escape_returns_to_the_row(
    tree: tuple[Path, Path],
) -> None:
    _route, card, back = _drive(*tree, _on(), ("Enter", "Escape"))
    assert "Campaign CAM-0001 · step 1 of 3 · as of" in card
    assert "▸ CAM-0001 ▸ Step 1" in card
    assert "1 Survey replay order source 1 · ✓ done" in _row(card, "STEP")
    assert "Nothing — it could start at once." in _row(card, "WAITS ON")
    assert "RUN-00000010" in _row(card, "RUNNER")
    assert "1 of ≤2 h" in _row(card, "SPENT")
    assert OUTCOME in _row(card, "OUTCOME")
    # the history region is the runner Run's own event lines, in the order it appended them
    history = _row(card, "HISTORY")
    assert "WHAT HAPPENED" in history
    assert f"message summarized · {SAID[0]}" in card and f"message summarized · {SAID[1]}" in card
    assert "Tab region" in card.splitlines()[-1] and "↑↓ line" in card.splitlines()[-1]
    # the step's products are its artifact rows by file name, never a count
    assert "research/replay-order.md" in card and "artifact · kept with CAM-0001" in card
    assert "Esc back" in card and "y copy" in card
    assert "PLAN" in back and "Campaign CAM-0001" in back


def test_con_140_live_a_blocked_step_names_what_blocks_it(tree: tuple[Path, Path]) -> None:
    _route, _one, _two, card = _drive(*tree, _on(), ("ArrowDown", "ArrowDown", "Enter"))
    assert "○ blocked · never started" in _row(card, "STEP")
    assert "CLM-0004" in _row(card, "WAITS ON")
    assert "It waits on CLM-0004." in _row(card, "WAITING")
    assert "∅ none · –" in _row(card, "RUNNER")  # noqa: RUF001
    # no region has rows, so the card promises only copy and back
    assert "↑↓" not in card.splitlines()[-1] and "Tab" not in card.splitlines()[-1]


def test_con_138_live_enter_on_an_artifact_opens_its_file_as_written(
    tree: tuple[Path, Path],
) -> None:
    *_, card = _drive(*tree, _on(), ("Tab", "Enter"))
    assert "▸ CAM-0001 ▸ research/replay-order.md" in card
    assert "Campaign CAM-0001 · artifact 1 of 2 · as of" in card
    source = _row(card, "SOURCE")
    assert "research/replay-order.md · markdown" in source and "by step 1" in source
    assert f"{len(REPORT.encode())} B" in source
    assert "Kept with CAM-0001 · sha256 " in _row(card, "RECORD")
    # markdown keeps its headings, bullets and table rows exactly as the file holds them
    for line in ("# Replay order", "- 40 events compared", "| run | order |", "|---|---|"):
        assert line in card
    assert "the file is the record · the console renders it, it does not rewrite it" in card
    # the file fits, so no scroll key is promised
    assert "scroll" not in card.splitlines()[-1] and "Esc close" in card


def test_con_138_live_a_long_file_scrolls_and_counts_what_it_hides(
    tree: tuple[Path, Path],
) -> None:
    *_, top, scrolled = _drive(*tree, _on(0), ("Tab", "ArrowDown", "Enter", "ArrowDown"))
    assert "long-report.md" in top and "lines below" in top and "lines above" not in top
    assert "↑↓ scroll" in top
    assert "lines above" in scrolled


def test_con_142_live_a_card_for_a_step_that_does_not_exist_states_its_absence(
    tree: tuple[Path, Path],
) -> None:
    async def body() -> str:
        root, runtime = tree
        async with (
            live_console(root, runtime) as (app, _seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            await render_setup(
                app, pilot, SessionSetup(route="campaign.step", subj_id="CAM-0001", size=2)
            )
            app.session.cam_step = 8
            return await _settle(app, pilot)

    card = asyncio.run(body())
    assert "CAM-0001 · step 9" in card
    assert not any(line.lstrip().startswith("STEP ") for line in card.splitlines())


def test_con_142_live_the_same_enter_twice_replaces_rather_than_stacks(
    tree: tuple[Path, Path],
) -> None:
    async def body() -> tuple[int, str]:
        root, runtime = tree
        async with (
            live_console(root, runtime) as (app, _seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            await render_setup(app, pilot, _on())
            await _settle(app, pilot)
            app.press_key("Enter")
            await _settle(app, pilot)
            depth = len(app.session.back)
            app.press_key("Enter")
            await _settle(app, pilot)
            assert len(app.session.back) == depth
            app.press_key("Escape")
            return depth, await _settle(app, pilot)

    depth, back = asyncio.run(body())
    assert depth >= 1 and "PLAN" in back


def test_ui_073_live_the_seam_owes_the_campaign_then_the_artifact_it_opens(
    tree: tuple[Path, Path],
) -> None:
    async def body() -> tuple[tuple[str, ...], tuple[str, ...]]:
        root, runtime = tree
        async with (
            live_console(root, runtime) as (app, seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            await render_setup(app, pilot, _on())
            await _settle(app, pilot)
            on_route = seam.live_on_screen()
            app.press_key("Tab")
            app.press_key("Enter")
            await _settle(app, pilot)
            return on_route, seam.live_on_screen()

    on_route, on_card = asyncio.run(body())
    assert on_route == (CAMPAIGN_READ,)
    assert on_card == (ARTIFACT_READ,)


def test_prx_066_live_a_replaying_frame_hides_the_later_finding_and_counts_it(
    tree: tuple[Path, Path],
) -> None:
    """UI-073/PRX-066: drawn under REPLAYING as of the cursor, then promoted once it closes."""
    root, runtime = tree

    async def body() -> tuple[Any, list[str], str]:
        async with (
            live_console(root, runtime) as (app, seam),
            app.run_test(size=SIZES[2]) as pilot,
        ):
            await render_setup(app, pilot, _on())
            before = await _settle(app, pilot)
            assert LEARNED in before and LATER not in before
            # promoted behind the console's back: the push never reaches it, so a gap opens
            await asyncio.to_thread(_promote, root, runtime, LATER)
            drawn: list[str] = []
            seam.watch(lambda _routes: drawn.append("\n".join(app.frame_rows)))
            outcome = await seam.reconnect()
            app._follow_route()
            return outcome, drawn, await _settle(app, pilot)

    outcome, drawn, closed = asyncio.run(body())
    assert outcome.negotiation.disposition is ReconnectDisposition.REPLAY
    first, head = outcome.negotiation.client_cursor, outcome.negotiation.server_cursor
    replaying = drawn[0]
    assert "REPLAYING" in replaying
    assert f"replaying {first:,} → {head:,} · 1 findings promoted after this point" in replaying
    # the later finding is a fact past the cursor: not drawn, but counted
    assert LATER not in replaying and LEARNED in replaying
    assert outcome.connection is not ConnectionValue.REPLAYING
    assert "REPLAYING" not in closed
    assert LATER in _row(closed, "CFN-0002")
    assert f"promoted at {head:,}" in _row(closed, "CFN-0002")


# ---------- the pure pieces the live path leans on ----------


def test_ui_073_a_finding_is_dated_by_its_first_promoting_row_only() -> None:
    finding = Epoch2Collection.CAMPAIGN_FINDING.value
    rows = [
        {"collection": finding, "record_key": "CFN-0001", "canonical_sequence": 4},
        {"collection": finding, "record_key": "CFN-0001", "canonical_sequence": 9},
        {"collection": "artifact", "record_key": "ART-0001", "canonical_sequence": 5},
        {"collection": finding, "record_key": "CFN-0002", "canonical_sequence": True},
        {"collection": finding, "record_key": "CFN-0003"},
    ]
    assert promoting_sequences(rows) == {"CFN-0001": 4}
    assert promoting_sequences([]) == {}


@pytest.mark.parametrize(
    ("size", "words"),
    [(0, "0 B"), (1023, "1023 B"), (1024, "1.0 KB"), (2150, "2.1 KB"), (1024**2, "1.0 MB")],
)
def test_con_138_the_card_states_a_size_in_its_largest_whole_unit(size: int, words: str) -> None:
    assert size_words(size) == words
