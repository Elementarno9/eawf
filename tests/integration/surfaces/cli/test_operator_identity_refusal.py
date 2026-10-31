"""An agent session cannot record an operator act; the operator's terminal can.

A real ``eawfd`` serves an epoch-2 canary and the real ``eawf`` entry point
runs in subprocesses, so the session marker is read from the environment
the CLI actually runs in and the refusal is decided by the daemon across
the socket. An admitted acceptance runs against a seeded Milestone in
review and its sealed approval, so it is shown to commit; an admitted plan
approval names no seeded plan revision, so it is shown to reach the verb's
own lookup, past this check.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.operator_identity import (
    AGENT_SESSION_ENV_MARKERS,
    AGENT_SESSION_PARAM,
    OPERATOR_IDENTITY_REFUSED,
    OperatorIdentityRefusedError,
    admit_operator_identity,
    agent_session_runtime,
    names_acting_principal,
)
from eawf.surfaces.cli import exit_codes
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    APPROVAL_URN,
    MILESTONE_URN,
    acceptance_bundle_row,
    approval_rows,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

_EMPTY_REPO_STATE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)
_CANARY_CODE: Final = "OIR"
_OPERATOR: Final = "OP-0001"
_SERVICE: Final = "EAWF-PLANNER"
_CLAUDE: Final = {"CLAUDECODE": "1"}
_CODEX: Final = {"CODEX_THREAD_ID": "0199a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"}
_READY_TIMEOUT_S: Final = 15.0
_CLI_TIMEOUT_S: Final = 60.0
_TERM_GRACE_S: Final = 5.0


@dataclass(frozen=True)
class _Daemon:
    """A provisioned canary with a live daemon serving it."""

    canary: CanaryProvision
    env: Mapping[str, str]

    def urn(self, kind: str, key: str) -> str:
        container = str(self.canary.ref.repository).rsplit("/repository/", 1)[0]
        return f"{container}/{kind}/{key}"

    def eawf(self, *args: str, session: Mapping[str, str]) -> subprocess.CompletedProcess[str]:
        """Run the real ``eawf`` from an operator terminal plus the *session* markers."""
        return subprocess.run(
            [sys.executable, "-m", "eawf", "--workspace", str(self.canary.root), *args],
            cwd=str(self.canary.root),
            env={**self.env, **session},
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_S,
            check=False,
        )


def _socket_accepts(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            probe.connect(str(path))
    except OSError:
        return False
    return True


def _reap(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is not None:
        return
    with contextlib.suppress(ProcessLookupError, OSError):
        proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=_TERM_GRACE_S)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, OSError):
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.wait(timeout=_TERM_GRACE_S)


@pytest.fixture(scope="module")
def daemon() -> Iterator[_Daemon]:
    """Yield a canary served by a real daemon, with every agent marker stripped.

    The scratch tree sits directly under the system temp directory: a
    ``tmp_path`` on macOS pushes the socket address past the AF_UNIX cap.
    """
    if os.name == "nt":
        pytest.skip("the daemon listens on an AF_UNIX socket")
    scratch = Path(tempfile.mkdtemp(prefix="eawf-oir-"))
    runtime_dir = scratch / "rt"
    runtime_dir.mkdir()
    state_path = scratch / "state" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    shutil.copy(_EMPTY_REPO_STATE, state_path)
    canary = provision_canary(
        repo_root=scratch / "repo", ref=canary_ref(_CANARY_CODE), provisioned_at=datetime.now(UTC)
    )
    # One Milestone in review with the sealed approval it is accepted on, so
    # an admitted acceptance can be shown to commit, not only to pass this check.
    seed(
        canary,
        {
            "milestone": {"MLS-0030": seed_row("milestone", "ACCEPTANCE_REVIEW")},
            **approval_rows(),
        },
    )
    markers = {variable for variable, _runtime in AGENT_SESSION_ENV_MARKERS}
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in markers and key not in {"EAWF_DAEMONLESS", "EAWF_VERBOSE"}
    }
    env["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    env["EA_STATE"] = str(state_path)
    env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
    proc = subprocess.Popen(
        [sys.executable, "-m", "eawf.runtime.daemon.main"],
        cwd=str(canary.root),
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + _READY_TIMEOUT_S
        while not _socket_accepts(runtime_dir / "eawfd.sock"):
            if time.monotonic() > deadline or proc.poll() is not None:
                log = runtime_dir / "eawfd.log"
                tail = log.read_text(errors="replace")[-4000:] if log.exists() else "(no log)"
                pytest.fail(f"daemon never accepted a connection\n{tail}")
            time.sleep(0.05)
        yield _Daemon(canary=canary, env=env)
    finally:
        _reap(proc)
        shutil.rmtree(scratch, ignore_errors=True)


def _accept(
    daemon: _Daemon, *, actor: str, session: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return daemon.eawf(
        "--json",
        "milestone",
        "accept",
        daemon.urn("milestone", "MLS-0404"),
        "--expected-revision",
        "1",
        "--idempotency-key",
        "accept-0001",
        "--actor",
        actor,
        "--yes",
        session=session,
    )


def _approve(
    daemon: _Daemon, *, actor: str, approved_by: str, session: Mapping[str, str]
) -> subprocess.CompletedProcess[str]:
    return daemon.eawf(
        "--json",
        "plan",
        "approve",
        "PRV-0404",
        "--expected-revision",
        "1",
        "--action-ref",
        daemon.urn("pending-action", "ACT-0404"),
        "--approved-by",
        approved_by,
        "--idempotency-key",
        "approve-0001",
        "--actor",
        actor,
        session=session,
    )


def _refused(result: subprocess.CompletedProcess[str]) -> bool:
    return OPERATOR_IDENTITY_REFUSED in result.stdout + result.stderr


@pytest.mark.parametrize("session", [_CLAUDE, _CODEX], ids=["claude", "codex"])
def test_milestone_accept_refuses_operator_actor_from_agent_session(
    daemon: _Daemon, session: Mapping[str, str]
) -> None:
    result = _accept(daemon, actor=_OPERATOR, session=session)

    assert result.returncode == exit_codes.VALIDATION_ERROR, result.stdout + result.stderr
    assert _refused(result), result.stdout + result.stderr
    assert _OPERATOR in result.stdout + result.stderr


def test_milestone_accept_admits_operator_actor_from_operator_terminal(
    daemon: _Daemon, tmp_path: Path
) -> None:
    """The operator's own terminal accepts a reviewed Milestone on its sealed approval."""
    spec = tmp_path / "accept.json"
    spec.write_text(
        json.dumps(
            {
                "updates": {
                    "accepted_binding": seed_row("milestone", "COMPLETED")["accepted_binding"]
                }
            }
        ),
        encoding="utf-8",
    )
    bundle = tmp_path / "bundle.json"
    bundle.write_text(json.dumps(acceptance_bundle_row()), encoding="utf-8")

    result = daemon.eawf(
        "--json",
        "milestone",
        "accept",
        MILESTONE_URN,
        "--expected-revision",
        "1",
        "--idempotency-key",
        "req-accept-0001",
        "--actor",
        _OPERATOR,
        "--approval-receipt-ref",
        APPROVAL_URN,
        "--acceptance-bundle",
        str(bundle),
        "--from-spec",
        str(spec),
        "--yes",
        session={},
    )

    assert result.returncode == exit_codes.OK, result.stdout + result.stderr
    answer = json.loads(result.stdout)
    assert answer["status"] == "ok", answer
    assert answer["result"]["event_name"] == "domain.milestone.completed"


def test_milestone_accept_admits_service_actor_from_agent_session(daemon: _Daemon) -> None:
    result = _accept(daemon, actor=_SERVICE, session=_CLAUDE)

    assert result.returncode != exit_codes.OK, "an absent Milestone cannot be accepted"
    assert not _refused(result), result.stdout + result.stderr


def test_plan_approve_refuses_operator_actor_from_agent_session(daemon: _Daemon) -> None:
    result = _approve(daemon, actor=_OPERATOR, approved_by=_OPERATOR, session=_CLAUDE)

    assert result.returncode == exit_codes.VALIDATION_ERROR, result.stdout + result.stderr
    assert _refused(result), result.stdout + result.stderr


def test_plan_approve_refuses_operator_approver_behind_service_actor(daemon: _Daemon) -> None:
    """A service actor cannot carry an operator's approval in from an agent session."""
    result = _approve(daemon, actor=_SERVICE, approved_by=_OPERATOR, session=_CODEX)

    assert result.returncode == exit_codes.VALIDATION_ERROR, result.stdout + result.stderr
    assert _refused(result), result.stdout + result.stderr


def test_plan_approve_admits_operator_actor_from_operator_terminal(daemon: _Daemon) -> None:
    """The admitted approval reaches the verb, which then looks the plan revision up."""
    result = _approve(daemon, actor=_OPERATOR, approved_by=_OPERATOR, session={})

    assert not _refused(result), result.stdout + result.stderr
    codes = [row["code"] for row in json.loads(result.stdout)["errors"]]
    assert codes == ["identity_not_found"], result.stdout + result.stderr


# ---- The library check the daemon runs -------------------------------------


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({}, None),
        ({"CLAUDECODE": ""}, None),
        ({"CODEX_HOME": "/codex"}, None),
        (_CLAUDE, "claude"),
        (_CODEX, "codex"),
        ({**_CODEX, **_CLAUDE}, "claude"),
    ],
    ids=["empty", "blank-marker", "unrelated-codex-var", "claude", "codex", "both"],
)
def test_agent_session_runtime_reads_only_set_markers(
    env: dict[str, str], expected: str | None
) -> None:
    assert agent_session_runtime(env) == expected


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({}, False),
        ({"drain": True, "timeout_seconds": 30}, False),
        ({"actor": _SERVICE}, True),
        ({"actor": None}, True),
        ({"approved_by": {"principal_id": _OPERATOR}}, True),
    ],
    ids=["empty", "daemon-shutdown", "actor", "null-actor", "approver"],
)
def test_names_acting_principal_reads_the_actor_and_approver_keys(
    params: dict[str, Any], expected: bool
) -> None:
    assert names_acting_principal(params) is expected


def test_admit_without_marker_admits_operator_unchanged() -> None:
    params = {"actor": _OPERATOR, "urn": "eawf://x"}

    assert admit_operator_identity("domain.milestone.accept", params) == params


def test_admit_strips_marker_for_service_actor() -> None:
    params = {"actor": _SERVICE, AGENT_SESSION_PARAM: "claude"}

    admitted = admit_operator_identity("domain.milestone.accept", params)

    assert admitted == {"actor": _SERVICE}
    assert AGENT_SESSION_PARAM in params


def test_admit_strips_marker_when_no_actor_is_named() -> None:
    assert admit_operator_identity("daemon.ping", {AGENT_SESSION_PARAM: "codex"}) == {}


@pytest.mark.parametrize(
    "params",
    [
        {"actor": _OPERATOR},
        {"actor": _SERVICE, "approved_by": {"principal_kind": "operator", "principal_id": "OP-1"}},
    ],
    ids=["actor", "approver"],
)
def test_admit_refuses_operator_from_agent_session(params: dict[str, Any]) -> None:
    with pytest.raises(OperatorIdentityRefusedError, match=OPERATOR_IDENTITY_REFUSED):
        admit_operator_identity("planning.plan_revision.approve", {**params, "agent_session": "x"})


@pytest.mark.parametrize(
    "params",
    [
        {"actor": 7},
        {"actor": "OPERATOR"},
        {"actor": _SERVICE, "approved_by": "OP-0001"},
        {"actor": _SERVICE, "approved_by": {"principal_kind": "service"}},
    ],
    ids=["non-string-actor", "op-without-dash", "approver-not-mapping", "approver-without-id"],
)
def test_admit_ignores_what_names_no_operator_key(params: dict[str, Any]) -> None:
    admitted = admit_operator_identity("m", {**params, AGENT_SESSION_PARAM: "claude"})

    assert admitted == params


@pytest.mark.parametrize("marker", ["", None, 1, ["claude"]], ids=["empty", "null", "int", "list"])
def test_admit_refuses_malformed_marker(marker: object) -> None:
    with pytest.raises(DaemonValidationError, match=AGENT_SESSION_PARAM) as caught:
        admit_operator_identity("m", {"actor": _SERVICE, AGENT_SESSION_PARAM: marker})

    assert not isinstance(caught.value, OperatorIdentityRefusedError)
