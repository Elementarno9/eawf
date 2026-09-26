"""A launched console acts as the operator named at launch, or refuses every write with why.

The console is opened through :func:`eawf.surfaces.tui.launch.launch_tui` exactly as
``eawf tui`` opens it, on a temporary epoch-2 tree. Only two things are stood in for:
the event loop the console would block on (``_run_console`` hands back the app and seam
it was given) and the daemon behind the binding's JSON-RPC client, which serves route
reads and records every write. A verb is then handed to the running app the way a
confirmed key hands it, so the one path under test is launcher -> seam -> daemon verb.
"""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from typer.testing import CliRunner

import eawf.surfaces.tui.chassis.state_binding as state_binding
import eawf.surfaces.tui.launch as launch
from eawf.kernel.migration.epoch2.canary import (
    CANARY_DECLARATION_FILENAME,
    GENERATIONS_DIRNAME,
    MARKER_FILENAME,
)
from eawf.kernel.projection.compute import ROUTE_COLLECTIONS, build_route_projection
from eawf.kernel.projection.connection import READ_METHOD_TEMPLATE
from eawf.kernel.runtime.provider import ControlKind
from eawf.surfaces.cli.app import app as cli
from eawf.surfaces.tui.console.app import ConsoleApp
from eawf.surfaces.tui.console.operations import (
    CONTROL_METHOD,
    SEAL_METHOD,
    AnswerRequest,
    ControlRequest,
    Operator,
    VerbRequest,
)
from eawf.surfaces.tui.console.seam import ProjectionSeam
from eawf.surfaces.tui.console.session import SIZES, SessionSetup

AT = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)
CONTAINER = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
ACTOR = "OP-0001"
RECEIPT = f"{CONTAINER}/evidence/EVD-0003"
ACTION = "ACT-0034"
RUN_KEY = "RUN-538453eb"

_GENERATION_ID = "gen-" + "ab" * 8
_DIGEST = "cd" * 32


def _activate_epoch2(root: Path) -> None:
    """Declare and activate ``root`` as an epoch-2 canary tree."""
    (root / CANARY_DECLARATION_FILENAME).write_text(
        json.dumps({"disposable": True, "declared_by": "test", "purpose": "operator launch"})
    )
    (root / GENERATIONS_DIRNAME).mkdir(parents=True, exist_ok=True)
    (root / GENERATIONS_DIRNAME / MARKER_FILENAME).write_text(
        json.dumps(
            {
                "schema_version": "1",
                "epoch": 2,
                "generation_id": _GENERATION_ID,
                "manifest_digest": _DIGEST,
                "generation_digest": _DIGEST,
                "written_at": "2026-09-26T00:00:00Z",
            }
        )
    )


def _projection(route: str) -> dict[str, Any]:
    document: dict[str, Any] = {}
    if route == "attention":
        document = {
            "pending_action": {
                ACTION: {
                    "urn": f"{CONTAINER}/pending-action/{ACTION}",
                    "revision": 3,
                    "status": "WAITING",
                }
            }
        }
    elif route == "run.detail":
        document = {
            "run": {
                RUN_KEY: {"urn": f"{CONTAINER}/run/{RUN_KEY}", "revision": 1, "status": "RUNNING"}
            }
        }
    return build_route_projection(
        route=route, document=document, cursor=5, scope_id="EAWF", generated_at=AT
    ).model_dump(mode="json")


#: Every write the stand-in daemon was sent, in order; cleared per launch.
WRITES: list[tuple[str, dict[str, Any]]] = []

_READS = {READ_METHOD_TEMPLATE.format(route=r): r for r in ROUTE_COLLECTIONS}


class _Daemon:
    """Serves route reads and records every write, standing in for the socket client."""

    def __init__(self, **_options: Any) -> None:
        return None

    def __enter__(self) -> _Daemon:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if method in _READS:
            return _projection(_READS[method])
        WRITES.append((method, dict(params or {})))
        if method == SEAL_METHOD:
            return {"outcome": "sealed", "reason": f"{ACTION} was answered"}
        return {"disposition": "requesting"}


@pytest.fixture
def launched(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> list[tuple[ConsoleApp, Any]]:
    """Put a TTY over an epoch-2 tree and catch the app and seam the launcher runs."""
    _activate_epoch2(tmp_path)
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    monkeypatch.delenv("EAWF_ACTOR", raising=False)
    monkeypatch.delenv("EAWF_RECEIPT_REF", raising=False)

    class _Stdout:
        @staticmethod
        def isatty() -> bool:
            return True

    # Patched on the launcher's own ``sys`` handle: pytest swaps ``sys.stdout`` back
    # between fixture setup and the test call, which would undo a global patch.
    monkeypatch.setattr(launch, "sys", SimpleNamespace(stdout=_Stdout(), stderr=sys.stderr))
    WRITES.clear()
    monkeypatch.setattr(state_binding, "DaemonClient", _Daemon)
    runs: list[tuple[ConsoleApp, Any]] = []

    def _caught(app: ConsoleApp, seam: Any) -> int:
        runs.append((app, seam))
        return 0

    monkeypatch.setattr(launch, "_run_console", _caught)
    return runs


def _launch(runs: list[tuple[ConsoleApp, Any]], operator: Operator | None) -> ProjectionSeam:
    rc = launch.launch_tui(workspace=None, no_input=False, plain=False, operator=operator)
    assert rc == 0
    ((_app, seam),) = runs
    assert isinstance(seam, ProjectionSeam)
    return seam


def _send(app: ConsoleApp, seam: ProjectionSeam, route: str, request: VerbRequest) -> list[str]:
    """Run ``app`` on ``route`` and hand it ``request`` as a confirmed key does."""

    async def drive() -> list[str]:
        app.reset(SessionSetup(route=route))
        seam.retarget(route)
        async with app.run_test(size=SIZES[1]) as pilot:
            await seam.load(route)
            assert app.send(request) is True
            await app.workers.wait_for_complete()
            await pilot.pause()
        return [row.note for row in app.session.log]

    return asyncio.run(drive())


def test_launch_tui_answer_reaches_the_approval_seal(
    launched: list[tuple[ConsoleApp, Any]],
) -> None:
    """Gate-fire proof: the launcher's seam carries the operator the answer is sealed as."""
    seam = _launch(launched, launch.resolve_operator(actor=ACTOR, receipt_ref=RECEIPT))
    app = launched[0][0]

    notes = _send(app, seam, "attention", AnswerRequest(target=ACTION, option_id="approve"))

    ((method, params),) = WRITES
    assert method == SEAL_METHOD
    assert params["actor"] == ACTOR
    assert params["resolver"] == {"principal_kind": "human", "principal_id": ACTOR}
    assert params["receipt_ref"] == RECEIPT
    assert params["urn"] == f"{CONTAINER}/pending-action/{ACTION}"
    assert params["expected_revision"] == 3
    assert any(note.startswith("applied") for note in notes)


def test_launch_tui_run_control_reaches_the_control_verb(
    launched: list[tuple[ConsoleApp, Any]],
) -> None:
    seam = _launch(launched, launch.resolve_operator(actor=ACTOR, receipt_ref=None))
    app = launched[0][0]

    _send(app, seam, "run.detail", ControlRequest(target=RUN_KEY, control=ControlKind.CANCEL))

    ((method, params),) = WRITES
    assert method == CONTROL_METHOD
    assert params["actor"] == ACTOR
    assert params["urn"] == f"{CONTAINER}/run/{RUN_KEY}"


def test_launch_tui_without_operator_refuses_with_the_reason(
    launched: list[tuple[ConsoleApp, Any]],
) -> None:
    seam = _launch(launched, None)
    app = launched[0][0]

    notes = _send(app, seam, "attention", AnswerRequest(target=ACTION, option_id="approve"))

    assert WRITES == []
    refusal = next(note for note in notes if note.startswith("refused"))
    assert "no operator principal" in refusal
    assert "--actor" in refusal


def test_tui_command_passes_the_named_operator_to_the_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Operator | None] = []

    def _launched(**kwargs: Any) -> int:
        seen.append(kwargs["operator"])
        return 0

    monkeypatch.setattr(launch, "launch_tui", _launched)
    monkeypatch.delenv("EAWF_ACTOR", raising=False)
    monkeypatch.setenv("EAWF_RECEIPT_REF", RECEIPT)

    flagged = CliRunner().invoke(cli, ["tui", "--actor", ACTOR])
    bare = CliRunner().invoke(cli, ["tui"], env={"EAWF_RECEIPT_REF": None})

    assert flagged.exit_code == 0, flagged.output
    assert bare.exit_code == 0, bare.output
    assert seen == [Operator(principal=ACTOR, receipt_ref=RECEIPT), None]


def test_tui_command_rejects_a_malformed_principal() -> None:
    result = CliRunner().invoke(cli, ["tui", "--actor", "someone@example.test"])

    assert result.exit_code == 2
    assert "not a principal key" in result.output


# ---------- resolve_operator: boundary and error paths ----------


def test_resolve_operator_nothing_named_is_no_operator() -> None:
    assert launch.resolve_operator(actor=None, receipt_ref=None) is None


def test_resolve_operator_principal_alone_has_no_receipt() -> None:
    assert launch.resolve_operator(actor=ACTOR, receipt_ref=None) == Operator(principal=ACTOR)


def test_resolve_operator_principal_at_the_length_limit() -> None:
    longest = "O" + "P" * 31
    assert launch.resolve_operator(actor=longest, receipt_ref=RECEIPT) == Operator(
        principal=longest, receipt_ref=RECEIPT
    )


@pytest.mark.parametrize("actor", ["", "O", "O" + "P" * 32, "op-0001", "someone@example.test"])
def test_resolve_operator_malformed_principal_raises(actor: str) -> None:
    with pytest.raises(ValueError, match="not a principal key"):
        launch.resolve_operator(actor=actor, receipt_ref=None)


@pytest.mark.parametrize("receipt", ["EVD-0003", f"{CONTAINER}/run/{RUN_KEY}", ""])
def test_resolve_operator_malformed_receipt_raises(receipt: str) -> None:
    with pytest.raises(ValueError, match="not a qualified evidence URN"):
        launch.resolve_operator(actor=ACTOR, receipt_ref=receipt)


def test_resolve_operator_receipt_without_principal_raises() -> None:
    with pytest.raises(ValueError, match="needs a principal"):
        launch.resolve_operator(actor=None, receipt_ref=RECEIPT)
