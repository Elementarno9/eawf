"""The epoch-1 Track verbs keep their behaviour beside the native ones.

``eawf track`` now carries a fourth verb, ``retire``, that forwards to a
native epoch-2 RPC. The three epoch-1 verbs it joins -- ``add``,
``switch`` and ``sync`` -- run against an epoch-1 ``state.json`` exactly
as before, and the native verbs are unreachable on such a tree: the
daemon's fence turns the request away before a handler reads a byte, so
nothing under ``.ea/`` moves.

The refusal these tests assert against is not a string written here. It
is produced by calling the daemon's own fence
(:func:`~eawf.runtime.daemon.native_guard.require_native_call`) against
the temporary epoch-1 root and handing the CLI exactly what the daemon
would have answered.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.domain_envelope import DomainErrorCode
from eawf.runtime.daemon.native_guard import NativeAuthorityRefusedError, require_native_call
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"

#: One command line per native verb, all aimed at the epoch-1 tree.
_NATIVE_INVOCATIONS: tuple[tuple[str, list[str]], ...] = (
    (
        domain_cmd.TRACK_RETIRE,
        ["track", "retire", f"{_ROOT}/track/TRK-CANARY", "--expected-track-revision", "1"],
    ),
    (
        domain_cmd.MILESTONE_ACTIVATE,
        [
            "milestone",
            "activate",
            f"{_ROOT}/milestone/MLS-0001",
            "--expected-milestone-revision",
            "1",
        ],
    ),
    (
        domain_cmd.MILESTONE_OPEN_REVIEW,
        [
            "milestone",
            "open-review",
            f"{_ROOT}/milestone/MLS-0001",
            "--expected-milestone-revision",
            "1",
        ],
    ),
    (
        domain_cmd.MILESTONE_ACCEPT,
        [
            "milestone",
            "accept",
            f"{_ROOT}/milestone/MLS-0001",
            "--expected-milestone-revision",
            "1",
        ],
    ),
    (
        domain_cmd.MILESTONE_CANCEL,
        [
            "milestone",
            "cancel",
            f"{_ROOT}/milestone/MLS-0001",
            "--expected-milestone-revision",
            "1",
        ],
    ),
    (
        domain_cmd.BATCH_ACTIVATE,
        ["batch", "activate", f"{_ROOT}/batch/BAT-0001", "--expected-batch-revision", "1"],
    ),
    (
        domain_cmd.BATCH_READY,
        ["batch", "ready", f"{_ROOT}/batch/BAT-0001", "--expected-batch-revision", "1"],
    ),
    (
        domain_cmd.TASK_PROMOTE,
        ["task", "promote", f"{_ROOT}/task/CANARY-0001", "--expected-task-revision", "1"],
    ),
    (
        domain_cmd.TASK_START,
        ["task", "start", f"{_ROOT}/task/CANARY-0001", "--expected-task-revision", "1"],
    ),
)


class _FakeClient:
    """A stand-in daemon client answering one canned result or error."""

    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append((method, dict(params)))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


@pytest.fixture
def epoch1_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Yield a workspace holding a freshly initialised epoch-1 state tree."""
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    _FakeClient.calls = []
    result = runner.invoke(app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"])
    assert result.exit_code == exit_codes.OK, result.output
    yield tmp_path


def _tree_digest(root: Path) -> str:
    """Return a digest over every file under *root*.

    Args:
        root: The directory to fingerprint.

    Returns:
        A hex digest covering each file's repo-relative path and bytes,
        so any write anywhere under the tree changes it.
    """
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        digest.update(str(path.relative_to(root)).encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _fence_refusal(workspace: Path) -> NativeAuthorityRefusedError:
    """Return the refusal the daemon's fence gives this epoch-1 root.

    Args:
        workspace: The workspace root whose ``.ea`` tree is epoch 1.

    Returns:
        The typed refusal, whose message is what the daemon puts on the
        wire.

    Raises:
        AssertionError: The root was not refused, which would mean the
            fixture is not an epoch-1 tree at all.
    """
    ctx = MethodContext(
        started_at="2026-09-18T00:00:00+00:00",
        pid=0,
        protocol_version=PROTOCOL_VERSION,
        version="test",
    )
    try:
        require_native_call(ctx, {"repo_root": str(workspace)})
    except NativeAuthorityRefusedError as refusal:
        return refusal
    raise AssertionError("the epoch-1 fixture holds native authority")


# ---- the epoch-1 verbs ------------------------------------------------------


def test_track_add_then_switch_keep_their_output(epoch1_workspace: Path) -> None:
    """``track add`` / ``track switch`` render and persist exactly as before."""
    added = runner.invoke(
        app,
        ["track", "add", "COLLAR", "--kind", "strategy", "--title", "Collar"],
    )
    assert added.exit_code == exit_codes.OK, added.output
    assert added.output.strip() == "track add COLLAR title='Collar'"

    switched = runner.invoke(app, ["--json", "track", "switch", "COLLAR"])
    assert switched.exit_code == exit_codes.OK, switched.output
    assert orjson.loads(switched.stdout) == {"track": "COLLAR", "current": True}

    state = orjson.loads((epoch1_workspace / ".ea" / "state.json").read_bytes())
    assert "COLLAR" in state["tracks"]
    assert state["current"]["track_id"] == "COLLAR"


def test_track_add_unknown_kind_keeps_its_refusal(epoch1_workspace: Path) -> None:
    """Error path: the epoch-1 kind check still answers before any write."""
    result = runner.invoke(
        app, ["track", "add", "COLLAR", "--kind", "research-line", "--title", "Collar"]
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "unknown track kind" in result.output


def test_track_sync_keeps_its_output(
    epoch1_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``track sync`` still forwards ``track.sync`` and renders its result."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *a, **k: 0)
    # ``track sync`` resolves its client inside the handler body, so the
    # stand-in is installed on the client module rather than on the
    # command module.
    monkeypatch.setattr(
        "eawf.surfaces.cli._daemon_client.DaemonClient",
        lambda *a, **k: _FakeClient(
            result={"track_id": "COLLAR", "changed_outcome_ids": [], "changed": 0}
        ),
    )
    result = runner.invoke(app, ["track", "sync", "COLLAR"])
    assert result.exit_code == exit_codes.OK, result.output
    assert result.output.strip() == "track sync COLLAR (changed 0 outcome statuses)"
    assert _FakeClient.calls[0][0] == "track.sync"


# ---- the native verbs on the same tree --------------------------------------


def test_epoch1_root_is_refused_by_the_native_fence(epoch1_workspace: Path) -> None:
    """The fixture tree really is epoch 1, and the fence names the code."""
    refusal = _fence_refusal(epoch1_workspace)
    assert refusal.code == DomainErrorCode.NATIVE_AUTHORITY_REQUIRED.value
    assert DomainErrorCode.NATIVE_AUTHORITY_REQUIRED.value in str(refusal)


@pytest.mark.parametrize(("method", "argv"), _NATIVE_INVOCATIONS)
def test_native_verb_refuses_on_an_epoch1_root_with_zero_writes(
    epoch1_workspace: Path,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    argv: list[str],
) -> None:
    """Every native verb answers the epoch-1 refusal and moves nothing."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", lambda *a, **k: 0)
    refusal = _fence_refusal(epoch1_workspace)
    monkeypatch.setattr(
        domain_cmd,
        "DaemonClient",
        lambda *a, **k: _FakeClient(
            error=DaemonRpcError(-32002, str(refusal), {"kind": "NativeAuthorityRefused"})
        ),
    )
    before = _tree_digest(epoch1_workspace / ".ea")

    result = runner.invoke(
        app,
        [
            "--workspace",
            str(epoch1_workspace),
            "--json",
            *argv,
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )

    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT, result.output
    envelope = orjson.loads(result.stdout)
    assert envelope["status"] == "error"
    assert envelope["operation"] == method
    assert envelope["errors"][0]["code"] == DomainErrorCode.NATIVE_AUTHORITY_REQUIRED.value
    assert envelope["revision_before"] is None
    assert envelope["revision_after"] is None
    assert _tree_digest(epoch1_workspace / ".ea") == before


def test_native_verb_under_daemonless_is_rejected_with_zero_writes(
    epoch1_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Error path: the carve-out cannot carry a native mutation either."""
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setattr(
        domain_cmd,
        "DaemonClient",
        lambda *a, **k: pytest.fail("a daemonless mutation must not reach the wire"),
    )
    before = _tree_digest(epoch1_workspace / ".ea")
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(epoch1_workspace),
            "--daemonless",
            "milestone",
            "activate",
            f"{_ROOT}/milestone/MLS-0001",
            "--expected-milestone-revision",
            "1",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "--daemonless rejected: milestone activate is a mutating verb" in result.output
    assert _tree_digest(epoch1_workspace / ".ea") == before
