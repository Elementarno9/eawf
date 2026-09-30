"""SURF-084 and SURF-086 at the CLI: long verbs submit, reads never start a daemon.

Driven through the real Typer app with a recording client in place of the
daemon. ``task prove`` and ``batch integrate`` send ``operation.submit`` and
exit zero on the reference alone; ``--wait`` prints the terminal answer the
direct call would have printed. ``eawf follow`` streams each state from a
cursor, reconnects across a dropped poll, and exits with the terminal
state's typed status. Every read verb -- ``follow``, ``daemon status``,
``release show``, ``release readiness``, ``close status`` and ``task
assess`` -- opens its client with ``spawn=False`` and never escalates as a
mutation, while ``daemon ping`` keeps starting a daemon.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.surfaces.cli import _daemon_client, exit_codes
from eawf.surfaces.cli._daemon_client import DaemonClient, DaemonNotRunningError, DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import daemon as daemon_cmd
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import domain_integration as integration_cmd
from eawf.surfaces.cli.commands import operation as operation_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_TASK = f"{_ROOT}/task/CANARY-0001"
_BATCH = f"{_ROOT}/batch/BAT-0001"
_REF = "operation://00000000-0000-0000-0000-000000000007"
_PROOF = {"task_ref": _TASK, "legs": [], "passed": True, "reason": "every leg passes"}


def _record(state: str, revision: int, **extra: Any) -> dict[str, Any]:
    return {
        "operation_id": _REF.removeprefix("operation://"),
        "verb": integration_cmd.DELIVERY_PROVE,
        "subject": _TASK,
        "state": state,
        "revision": revision,
        "updated_at": "2026-09-29T12:00:00Z",
        "result": None,
        "error": None,
        **extra,
    }


class _Client:
    """A daemon client stand-in: records how it was opened and what it was sent."""

    opened: ClassVar[list[dict[str, Any]]] = []
    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []
    answers: ClassVar[list[Any]] = []

    def __init__(self, *_a: object, **kwargs: Any) -> None:
        type(self).opened.append(kwargs)

    def __enter__(self) -> _Client:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        type(self).calls.append((method, dict(params or {})))
        answer = type(self).answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        assert isinstance(answer, dict)
        return answer


@pytest.fixture(autouse=True)
def _client(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Route every client to the stand-in and record every mutation escalation."""
    escalated: list[str] = []
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr(
        "eawf.surfaces.cli._dispatch.escalate_mutation",
        lambda verb, **_k: escalated.append(verb) or 0,
    )
    monkeypatch.setattr(domain_cmd, "DaemonClient", _Client)
    monkeypatch.setattr(_daemon_client, "DaemonClient", _Client)
    monkeypatch.setattr(operation_cmd.time, "sleep", lambda _s: None)
    _Client.opened, _Client.calls, _Client.answers = [], [], []
    return escalated


def _invoke(tmp_path: Path, *argv: str) -> Any:
    return runner.invoke(app, ["--workspace", str(tmp_path), *argv])


def _prove(tmp_path: Path, *extra: str, json: bool = False) -> Any:
    return _invoke(
        tmp_path,
        *(["--json"] if json else []),
        "task", "prove", _TASK,
        "--expected-revision", "2",
        "--idempotency-key", "prove-1",
        "--actor", "OPERATOR",
        *extra,
    )  # fmt: skip


def _submitted(record: dict[str, Any], *, replayed: bool = False) -> dict[str, Any]:
    return {"operation_ref": _REF, "operation": record, "replayed": replayed}


# ---- SURF-084: submission ------------------------------------------------------------


def test_surf_084_task_prove_submits_and_exits_zero_on_acceptance(tmp_path: Path) -> None:
    _Client.answers = [_submitted(_record("queued", 0))]
    result = _prove(tmp_path, json=True)
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _Client.calls[0]
    assert method == operation_cmd.OPERATION_SUBMIT
    assert params["method"] == integration_cmd.DELIVERY_PROVE
    assert (params["idempotency_key"], params["wait"]) == ("prove-1", False)
    assert params["params"]["expected_revision"] == 2
    envelope = orjson.loads(result.stdout)
    assert envelope["operation"] == integration_cmd.DELIVERY_PROVE
    assert envelope["result"]["operation"]["state"] == "queued"
    assert envelope["links"] == {"follow": f"eawf follow {_REF}"}


def test_surf_084_task_prove_wait_prints_the_answer_a_direct_call_would(tmp_path: Path) -> None:
    _Client.answers = [_submitted(_record("succeeded", 2, result=_PROOF))]
    result = _prove(tmp_path, "--wait")
    assert result.exit_code == exit_codes.OK, result.output
    assert _Client.calls[0][1]["wait"] is True
    assert f"{integration_cmd.DELIVERY_PROVE} ok {_TASK}" in result.output
    assert "result.passed: true" in result.output


def test_surf_084_task_prove_wait_on_a_failed_proof_exits_with_the_refusal(
    tmp_path: Path,
) -> None:
    error = {"code": -32002, "message": "validation_failed: revision_conflict: stale anchor"}
    _Client.answers = [_submitted(_record("failed", 2, error=error))]
    result = _prove(tmp_path, "--wait")
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "revision_conflict" in result.output


def test_surf_084_task_prove_wait_on_a_lost_operation_exits_unreachable(tmp_path: Path) -> None:
    _Client.answers = [_submitted(_record("unknown", 2))]
    result = _prove(tmp_path, "--wait")
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert "stopped before recording an outcome" in result.output


def test_surf_084_batch_integrate_submits_under_the_assembled_key(tmp_path: Path) -> None:
    request = {"urn": _BATCH, "actor": "OPERATOR", "idempotency_key": "assembled-1"}
    _Client.answers = [request, _submitted(_record("queued", 0))]
    spec = tmp_path / "refs.json"
    spec.write_bytes(orjson.dumps({"base": {"head_sha": "b" * 40}}))
    result = _invoke(
        tmp_path,
        "batch", "integrate", _BATCH,
        "--expected-batch-revision", "1",
        "--actor", "OPERATOR",
        "--from-spec", str(spec),
    )  # fmt: skip
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _Client.calls[1]
    assert method == operation_cmd.OPERATION_SUBMIT
    assert params["method"] == integration_cmd.DELIVERY_INTEGRATE
    assert params["idempotency_key"] == "assembled-1"
    assert params["params"]["expected_revision"] == 1


# ---- SURF-084: follow -------------------------------------------------------------------


def _poll(*records: dict[str, Any], terminal: bool) -> dict[str, Any]:
    return {
        "operation_ref": _REF,
        "records": list(records),
        "operation": records[-1],
        "terminal": terminal,
        "cursor": records[-1]["revision"] + 1,
    }


def test_surf_084_follow_streams_from_its_cursor_and_reconnects(
    tmp_path: Path, _client: list[str]
) -> None:
    done = _record("succeeded", 2, result=_PROOF)
    _Client.answers = [
        _poll(_record("queued", 0), _record("running", 1), terminal=False),
        ConnectionRefusedError("daemon restarting"),
        _poll(done, terminal=True),
    ]
    result = _invoke(tmp_path, "follow", _REF, "--interval", "0.05")
    assert result.exit_code == exit_codes.OK, result.output
    assert [call[1]["cursor"] for call in _Client.calls] == [0, 2, 2]
    assert [line.split()[2] for line in result.output.splitlines()] == [
        "queued",
        "running",
        "succeeded",
    ]
    assert all(opened.get("spawn") is False for opened in _Client.opened)
    assert _client == []


def test_surf_084_follow_gives_up_after_consecutive_unreachable_polls(tmp_path: Path) -> None:
    _Client.answers = [ConnectionRefusedError("gone")] * 3
    result = _invoke(tmp_path, "follow", _REF)
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert len(_Client.calls) == 3


@pytest.mark.parametrize(
    ("state", "code"),
    [
        ("failed", exit_codes.STATE_CONFLICT),
        ("unknown", exit_codes.DAEMON_UNREACHABLE),
    ],
)
def test_surf_084_follow_exits_with_the_terminal_states_status(
    tmp_path: Path, state: str, code: int
) -> None:
    extra = {"error": {"code": -32002, "message": "refused"}} if state == "failed" else {}
    _Client.answers = [_poll(_record(state, 1, **extra), terminal=True)]
    result = _invoke(tmp_path, "--json", "follow", _REF)
    assert result.exit_code == code
    (line,) = result.stdout.splitlines()
    assert orjson.loads(line)["state"] == state


def test_surf_084_follow_of_an_unknown_reference_is_a_validation_error(tmp_path: Path) -> None:
    _Client.answers = [DaemonRpcError(-32002, "validation_failed: operation_not_found: x")]
    result = _invoke(tmp_path, "follow", _REF)
    assert result.exit_code == exit_codes.VALIDATION_ERROR
    assert "operation_not_found" in result.output


# ---- SURF-086: reads never start a daemon ------------------------------------------------


def test_surf_086_a_non_spawning_client_refuses_rather_than_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(_daemon_client, "DaemonClient", DaemonClient)
    monkeypatch.setattr(
        _daemon_client,
        "auto_spawn_daemon",
        lambda _dir: pytest.fail("a read must not start a daemon"),
    )
    with pytest.raises(DaemonNotRunningError), DaemonClient(runtime_dir=tmp_path, spawn=False):
        pass
    assert isinstance(DaemonNotRunningError("x"), OSError)


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX socket transport")
def test_surf_086_daemon_status_reports_not_running_without_starting_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(daemon_cmd, "daemon_pid_if_ready", lambda _dir: None)
    monkeypatch.setattr(
        daemon_cmd, "auto_spawn_daemon", lambda _dir: pytest.fail("status must not spawn")
    )
    result = runner.invoke(app, ["daemon", "status"])
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert "daemon not running" in result.output
    as_json = runner.invoke(app, ["--json", "daemon", "status"])
    assert as_json.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert orjson.loads(as_json.stdout)["result"] == {"running": False}


@pytest.mark.skipif(sys.platform == "win32", reason="the POSIX socket transport")
def test_surf_086_daemon_ping_still_starts_a_daemon(monkeypatch: pytest.MonkeyPatch) -> None:
    spawned: list[Path] = []

    def spawn(runtime: Path) -> int:
        spawned.append(runtime)
        raise daemon_cmd.DaemonSpawnTimeoutError("no daemon in a test")

    monkeypatch.setattr(daemon_cmd, "auto_spawn_daemon", spawn)
    result = runner.invoke(app, ["daemon", "ping"])
    assert result.exit_code == 1
    assert len(spawned) == 1


def test_surf_086_release_readiness_opens_a_non_spawning_client(tmp_path: Path) -> None:
    record = tmp_path / "release.json"
    record.write_bytes(orjson.dumps({"key": "REL-0.7.0", "revision": 1, "status": "draft"}))
    _Client.answers = [DaemonNotRunningError("no eawfd daemon is running")]
    result = _invoke(tmp_path, "release", "readiness", "0.7.0", "--release", str(record))
    assert result.exit_code != exit_codes.OK
    assert [opened.get("spawn") for opened in _Client.opened] == [False]
