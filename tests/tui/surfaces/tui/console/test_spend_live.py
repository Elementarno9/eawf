"""Live-test the spend the console reads: real dispatches, a real daemon, the real launch path.

UI-046 and CON-077 are held elsewhere against the prototype registers. Here the numbers
come from what produces them. A disposable canary holds three Runs of one kind: one is
dispatched through the real ``dispatch_run`` and crosses its sealed token cap mid-turn,
so the in-flight meter reaps it and states the crossing; one is dispatched and stays
under its cap, so it is live and has spent; one ended an hour after it started, which is
the typical duration of the kind. The launcher is the scripted streaming launcher the
meter suite drives -- a real child relaying cumulative usage -- so no model is called.
A real daemon on a private socket serves the tree, and the console is opened by ``eawf
ui``'s own launch function.
"""

from __future__ import annotations

import asyncio
import tempfile
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import native_dispatch
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.native_dispatch import DispatchParams, compile_launchers
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SessionSetup
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import seed
from tests.integration.runtime.daemon.test_host_subagent_adoption import repository_row
from tests.integration.runtime.daemon.test_native_dispatch import (
    PROVIDER_KIND,
    RUN_KEY,
    capsule_request,
    dispatch_params,
    make_canary,
    method_ctx,
    run_row,
    run_urn,
)
from tests.integration.runtime.test_run_meter_producer import (
    BASE_INPUT,
    CAP,
    StreamingLauncher,
    readings,
)
from tests.tui.surfaces.tui.console.test_console_live_smoke import _socket_dir
from tests.tui.surfaces.tui.console.test_transcript_live import _Daemon, _launch, _until

#: The Run that stays under its cap, and the Run of the same kind that already ended.
LIVE_RUN: Final = "RUN-00000011"
ENDED_RUN: Final = "RUN-00000012"

#: What the reaped Run had spent when it crossed, and what the live one has spent.
CROSSED: Final = CAP + 50
UNDER: Final = CAP - 1


def _dispatch(
    canary: CanaryProvision, tmp_path: Path, key: str, launcher: StreamingLauncher
) -> None:
    """Dispatch Run *key* with a sealed cap, through the real driver."""
    supplied = dispatch_params(
        canary,
        key=f"dispatch-{key}",
        urn=str(run_urn(key)),
        capsule=capsule_request(token_budget=CAP),
    )
    args = DispatchParams.model_validate(
        {name: value for name, value in supplied.items() if name != "repo_root"}
    )
    context = method_ctx(tmp_path / "runtime").native_root_context(canary.root / ".ea")
    asyncio.run(
        native_dispatch.dispatch_run(
            context, args, now=datetime.now(UTC), launchers=dict(compile_launchers((launcher,)))
        )
    )


def _ended() -> dict[str, Any]:
    """Return a Run of the same kind that ran for one hour and completed."""
    row = run_row("COMPLETED", key=ENDED_RUN)
    row["uid"] = str(uuid.uuid4())
    row["started_at"], row["ended_at"] = "2026-09-08T01:00:00Z", "2026-09-08T02:00:00Z"
    return row


@pytest.fixture
def spend_tree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Path, _Daemon]]:
    """A canary whose Runs really spent, one of them past its cap, served by a private daemon."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    live = run_row(key=LIVE_RUN)
    live["uid"] = str(uuid.uuid4())
    canary = make_canary(
        tmp_path / "repo",
        rows={RUN_KEY: run_row(), LIVE_RUN: live, ENDED_RUN: _ended()},
    )
    seed(canary, {Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()}})
    with pytest.raises(DaemonValidationError, match="budget_exhausted"):
        _dispatch(canary, tmp_path, RUN_KEY, StreamingLauncher(readings(200, CROSSED - BASE_INPUT)))
    _dispatch(canary, tmp_path, LIVE_RUN, StreamingLauncher(readings(300, UNDER - BASE_INPUT)))
    socket_dir = _socket_dir()
    socket_dir.mkdir(mode=0o700)
    monkeypatch.setenv("EAWF_RUNTIME_DIR", str(socket_dir))
    with _Daemon(tmp_path / "daemon", socket_dir / "eawfd.sock") as daemon:
        monkeypatch.setattr("eawf.surfaces.tui.chassis.state_binding.DaemonClient", daemon.client)
        yield canary.root, daemon
    for leftover in socket_dir.iterdir():
        leftover.unlink()
    socket_dir.rmdir()


def _row(frame: str, label: str) -> str:
    return next(row for row in frame.split("\n") if row.lstrip(" │|").startswith(label))


def _rows_after(frame: str, label: str, count: int) -> list[str]:
    rows = frame.split("\n")
    start = next(i for i, row in enumerate(rows) if row.lstrip(" │|").startswith(label))
    return rows[start : start + count]


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_ui_046_con_077_the_ceiling_reads_the_governor_the_stops_and_the_spend(
    spend_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """UI-046, CON-077: spent of the governor ceiling with an estimated remainder.

    The live Run's spend stands against the shipped in-flight token ceiling, and the
    remainder is what its reservation leaves, marked estimated. The reaped Run is listed
    as stopped at a hard limit with the reading that crossed, and the provider's spend
    reads unmetered, never zero, because no reading priced it. Enter opens the stopped
    Run, and nothing on the route moves a limit.
    """
    repo, _daemon = spend_tree

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        app.reset(SessionSetup(route="cost.ceiling"))
        await pilot.pause()
        frame = await _until(app, pilot, lambda f: RUN_KEY in f and "SPEND" in f)
        ceiling = _rows_after(frame, "CEILING", 3)
        assert f"tokens {UNDER:,} of 4,000,000" in ceiling[0], frame
        assert f"≈{4_000_000 - CAP:,} left" in ceiling[0], frame
        assert "cost ∅ unmetered · no limit is set" in ceiling[1], frame
        assert "1 live Run" in ceiling[2], frame
        assert "settings ▸ economics.governor" in _row(frame, "OWNED BY")
        stopped = _row(frame, f"▸ {RUN_KEY}")
        assert "hard limit · stopped" in stopped, frame
        spend = _row(frame, "SPEND")
        assert PROVIDER_KIND in spend and "∅ unmetered" in spend, frame
        assert f"tokens {CROSSED + UNDER:,}" in spend.replace("\n", ""), frame
        assert "%" not in frame and "eighty" not in frame
        await pilot.press("enter")
        await pilot.pause()
        assert app.session.route == "run.detail" and app.session.subj_id == RUN_KEY

    _launch(repo, monkeypatch, scenario, size)


@pytest.mark.parametrize("size", [(80, 24), (120, 30)], ids=["80x24", "120x30"])
def test_con_077_a_run_reads_spent_of_its_cap_and_elapsed_against_its_typical(
    spend_tree: tuple[Path, _Daemon], monkeypatch: pytest.MonkeyPatch, size: tuple[int, int]
) -> None:
    """CON-077: the Run's usage pane is budget lines, and time never counts down.

    Tokens read spent of the sealed cap with an estimated remainder; the time basis reads
    the elapsed time against the sealed wall limit and the typical duration of the kind,
    marked ``~``; a cost no reading priced reads unmetered. No remaining time is drawn.
    """
    repo, _daemon = spend_tree

    async def scenario(app: ConsoleApp, seam: ProjectionSeam, pilot: Any) -> None:
        app.reset(SessionSetup(route="run.detail", subjId=LIVE_RUN))
        await pilot.pause()
        frame = await _until(app, pilot, lambda f: "typical" in f)
        usage = _rows_after(frame, "USAGE", 3)
        assert "typical ~1h" in usage[0], frame
        assert " of " in usage[0].split("typical")[0], frame
        assert f"tokens {UNDER:,} of {CAP:,} · ≈1 left" in usage[1], frame
        assert "cost ∅ unmetered" in usage[2], frame
        assert "left" not in usage[0], frame

    _launch(repo, monkeypatch, scenario, size)
