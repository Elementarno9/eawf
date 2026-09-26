"""The native create and lifecycle verbs, driven end to end over a real daemon.

The unit-level CLI suite answers every call from a stand-in client, so it
proves the command forwards the right frame but not that the daemon takes
it, nor which exit status an operator's shell sees when the daemon refuses.
Here a real ``eawfd`` is spawned against a freshly provisioned epoch-2
canary and the real ``eawf`` entry point is run in subprocesses, so the
create, the activation and the refusal all cross the socket and the exit
statuses are the ones a script would branch on.

A refusal exits ``STATE_CONFLICT`` (3). The literal is asserted beside the
named constant so a change to the refusal exit reds here even if the
constant moves with it.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import orjson
import pytest

from eawf.platform.install.canary import CanaryProvision, canary_ref, provision_canary
from eawf.surfaces.cli import exit_codes

pytestmark = pytest.mark.integration

#: The committed empty-repo state, so the daemon's epoch-1 resolver and its
#: session sweep load a schema-valid ledger instead of the operator's own.
_EMPTY_REPO_STATE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

_CANARY_CODE: Final = "DCA"
_OPERATOR: Final = "OP-0001"
_READY_TIMEOUT_S: Final = 15.0
_CLI_TIMEOUT_S: Final = 60.0
_TERM_GRACE_S: Final = 5.0


@dataclass(frozen=True)
class _Sandbox:
    """A provisioned canary with a live daemon serving it."""

    canary: CanaryProvision
    env: dict[str, str]

    @property
    def container(self) -> str:
        return str(self.canary.ref.repository).rsplit("/repository/", 1)[0]

    def urn(self, kind: str, key: str) -> str:
        return f"{self.container}/{kind}/{key}"

    def eawf(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the real ``eawf`` entry point against the canary."""
        return subprocess.run(
            [sys.executable, "-m", "eawf", "--workspace", str(self.canary.root), *args],
            cwd=str(self.canary.root),
            env=self.env,
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


@pytest.fixture
def sandbox() -> Iterator[_Sandbox]:
    """Yield a canary served by a real daemon; reap the daemon and the dirs.

    Everything sits directly under the system temp directory rather than
    ``tmp_path``: the daemon binds ``<runtime_dir>/eawfd.sock``, and a
    ``tmp_path`` on macOS already pushes that address past the 104-byte
    AF_UNIX cap.
    """
    if os.name == "nt":
        pytest.skip("the daemon listens on an AF_UNIX socket")
    scratch = Path(tempfile.mkdtemp(prefix="eawf-dca-"))
    runtime_dir = scratch / "rt"
    runtime_dir.mkdir()
    state_path = scratch / "state" / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True)
    shutil.copy(_EMPTY_REPO_STATE, state_path)
    canary = provision_canary(
        repo_root=scratch / "repo", ref=canary_ref(_CANARY_CODE), provisioned_at=datetime.now(UTC)
    )
    env = {key: value for key, value in os.environ.items() if key != "EAWF_DAEMONLESS"}
    env.pop("EAWF_VERBOSE", None)
    env["EAWF_RUNTIME_DIR"] = str(runtime_dir)
    env["EA_STATE"] = str(state_path)
    env["EAWF_DAEMON_IDLE_TIMEOUT"] = "120"
    daemon = subprocess.Popen(
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
            if time.monotonic() > deadline or daemon.poll() is not None:
                log = runtime_dir / "eawfd.log"
                tail = log.read_text(errors="replace")[-4000:] if log.exists() else "(no log)"
                pytest.fail(f"daemon never accepted a connection\n{tail}")
            time.sleep(0.05)
        yield _Sandbox(canary=canary, env=env)
    finally:
        _reap(daemon)
        shutil.rmtree(scratch, ignore_errors=True)


def _json(result: subprocess.CompletedProcess[str]) -> dict[str, Any]:
    payload = orjson.loads(result.stdout)
    assert isinstance(payload, dict), result.stdout
    return payload


def _create(
    sandbox: _Sandbox, kind: str, urn: str, *, cursor: int, document: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    spec = sandbox.canary.root.parent / f"{kind}-create.json"
    spec.write_bytes(orjson.dumps(document))
    return sandbox.eawf(
        "--json",
        kind,
        "create",
        urn,
        "--expected-tree-revision",
        str(cursor),
        "--idempotency-key",
        f"create-{kind}",
        "--actor",
        _OPERATOR,
        "--from-spec",
        str(spec),
    )


def _track_document(sandbox: _Sandbox) -> dict[str, Any]:
    owner = {"principal_kind": "operator", "principal_id": _OPERATOR}
    return {
        "key": "TRK-DCA",
        "title": "Exercise the native verbs over a real daemon",
        "charter": "Admit and activate one Milestone through the operator surface.",
        "scope": {"scope_kind": "repository", "repository_ref": str(sandbox.canary.ref.repository)},
        "owner": owner,
        "policy": {
            "revision": 1,
            "wip": {"active_milestones": 1, "active_batches_per_repo": 1},
            "ownership_principal": owner,
            "permitted_milestone_kinds": ["product"],
            "campaign_templates": [],
            "outcome_metrics": [],
            "promotion_rules": [],
            "integration_priority": 50,
            "presentation": {"default_view": "roadmap", "color_token": "accent_blue"},
        },
    }


def _milestone_document(track_urn: str) -> dict[str, Any]:
    return {
        "key": "MLS-0001",
        "primary_track_ref": track_urn,
        "title": "Activate one Milestone through the CLI",
        "outcome": "The Milestone reaches ACTIVE through the operator surface.",
        "appetite": "S",
        "exclusions": ["any provider process"],
        "acceptance_journey": [
            {
                "step_id": "AS-01",
                "actor": "operator",
                "action": "read the Milestone status",
                "expected_observation": "the Milestone is ACTIVE",
                "evidence_kinds": ["artifact"],
            }
        ],
    }


def _activate(
    sandbox: _Sandbox, urn: str, *, revision: int, key: str
) -> subprocess.CompletedProcess[str]:
    return sandbox.eawf(
        "--json",
        "milestone",
        "activate",
        urn,
        "--expected-milestone-revision",
        str(revision),
        "--idempotency-key",
        key,
        "--actor",
        _OPERATOR,
    )


def _create_milestone(sandbox: _Sandbox) -> str:
    track_urn = sandbox.urn("track", "TRK-DCA")
    track = _create(sandbox, "track", track_urn, cursor=0, document=_track_document(sandbox))
    assert track.returncode == exit_codes.OK, track.stdout + track.stderr
    milestone_urn = sandbox.urn("milestone", "MLS-0001")
    milestone = _create(
        sandbox, "milestone", milestone_urn, cursor=1, document=_milestone_document(track_urn)
    )
    assert milestone.returncode == exit_codes.OK, milestone.stdout + milestone.stderr
    payload = _json(milestone)
    assert payload["status"] == "ok"
    assert payload["result"]["entity_ref"] == milestone_urn
    assert payload["revision_after"] == 1
    return milestone_urn


def test_milestone_create_then_activate_exits_zero(sandbox: _Sandbox) -> None:
    milestone_urn = _create_milestone(sandbox)

    result = _activate(sandbox, milestone_urn, revision=1, key="activate-0001")

    assert result.returncode == exit_codes.OK, result.stdout + result.stderr
    payload = _json(result)
    assert payload["status"] == "ok"
    assert payload["operation"] == "domain.milestone.activate"
    assert payload["revision_before"] == 1
    assert payload["revision_after"] == 2


def test_refused_activate_exits_state_conflict(sandbox: _Sandbox) -> None:
    """Error path: a stale revision is refused by the daemon and exits 3."""
    milestone_urn = _create_milestone(sandbox)
    first = _activate(sandbox, milestone_urn, revision=1, key="activate-0001")
    assert first.returncode == exit_codes.OK, first.stdout + first.stderr

    refused = _activate(sandbox, milestone_urn, revision=1, key="activate-0002")

    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert refused.returncode == exit_codes.STATE_CONFLICT
    payload = _json(refused)
    assert payload["status"] == "error"
    assert payload["errors"], payload
    assert all(row["entity_ref"] == milestone_urn for row in payload["errors"])


def test_activate_of_an_absent_milestone_exits_state_conflict(sandbox: _Sandbox) -> None:
    """Error path: a verb addressed at no record is refused, not crashed."""
    absent = sandbox.urn("milestone", "MLS-0404")

    refused = _activate(sandbox, absent, revision=1, key="activate-absent")

    assert refused.returncode == 3, refused.stdout + refused.stderr
    assert _json(refused)["status"] == "error"
