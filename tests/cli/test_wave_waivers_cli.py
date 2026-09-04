"""Counting surface for the daemonless close-waiver bypass lane.

The ``EAWF_DAEMONLESS`` bypass door already REJECTS a gate-bearing close
without ``--no-runtime`` and stamps an auditable waiver event on the allowed
bypass -- but nothing could *count* those events. A checkpoint close therefore
had no way to answer "how many gate-bearing waves were force-closed daemonless
since the last checkpoint, and why", which is exactly the question that decides
whether the bypass lane is being used as an escape hatch.

``eawf wave waivers [--since <commit-ish>]`` closes that gap: it returns the
number of daemonless-waiver events recorded after a given commit, naming each
waived scope and the operator's reason. These tests drive the REAL CLI.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.spec.common import convert_legacy_criterion
from eawf.kernel.state.models import State
from eawf.surfaces.cli.app import app
from tests._session_helpers import seed_active_session_on_disk
from tests.conftest import make_claim_criterion

pytestmark = pytest.mark.unit

runner = CliRunner()

_WAVE_ID = "P01-I01-W01"

#: Fixed commit dates bracket the waiver instant from both sides so the
#: "after this commit" filter is decided by the fixture, never by how long the
#: close took. Git stores committer time at one-second granularity, so a commit
#: created *now* could land in the same second as the waiver and flip the
#: comparison.
_ANCIENT = "2020-01-01T00:00:00+0000"
_FUTURE = "2099-01-01T00:00:00+0000"


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Temp git workspace whose state mutations run daemonless."""
    _git_init(tmp_path)
    monkeypatch.setenv("EA_STATE", str(tmp_path / ".ea" / "state.json"))
    monkeypatch.setenv("EAWF_DAEMONLESS", "1")
    monkeypatch.setenv("EAWF_EVIDENCE_DIRECT_WRITE", "1")
    yield tmp_path


def _git(root: Path, *args: str, env: dict[str, str] | None = None) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, env=env)


def _git_init(root: Path) -> None:
    """Initialise *root* as a git repo whose seed commit predates every waiver."""
    import os

    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "README.md").write_text("seed\n", encoding="utf-8")
    _git(root, "add", ".")
    env = {**os.environ, "GIT_AUTHOR_DATE": _ANCIENT, "GIT_COMMITTER_DATE": _ANCIENT}
    _git(root, "commit", "-q", "-m", "seed", env=env)


def _commit_dated_future(root: Path, message: str) -> None:
    """Add an empty commit whose committer time is after every waiver."""
    import os

    env = {**os.environ, "GIT_AUTHOR_DATE": _FUTURE, "GIT_COMMITTER_DATE": _FUTURE}
    _git(root, "commit", "-q", "--allow-empty", "-m", message, env=env)


def _state_path(workspace: Path) -> Path:
    return workspace / ".ea" / "state.json"


def _bootstrap_claimed_wave(workspace: Path) -> None:
    """Bring state up to one CLAIMED wave under an ACTIVE P01-I01."""
    assert (
        runner.invoke(
            app, ["project", "init", "QR", "--title", "Quant", "--domains", "quant"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "P1"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                _WAVE_ID,
                "--title",
                "w",
                "--files",
                "src/",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    state.waves[_WAVE_ID].success_criteria = [make_claim_criterion()]
    _state_path(workspace).write_bytes(orjson.dumps(state.model_dump(mode="json")))
    seed_active_session_on_disk(_state_path(workspace), session_id="S-1")
    assert runner.invoke(app, ["wave", "claim", _WAVE_ID, "--session", "S-1"]).exit_code == 0


def _attach_gate(workspace: Path) -> None:
    """Attach a paired criterion + gate so the wave is gate-bearing."""
    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    wave = state.waves[_WAVE_ID]
    criterion, gate = convert_legacy_criterion(
        "the bypass lane is countable", index=1, file_scopes=["src/"]
    )
    wave.success_criteria = [criterion]
    wave.gates = [gate]
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _attach_operator_session(workspace: Path) -> None:
    """Seed an ACTIVE operator session so the ``--no-runtime`` waiver can land."""
    from datetime import UTC, datetime

    from eawf.kernel.state.enums import AgentSessionRole, AgentSessionStatus
    from eawf.kernel.state.models import AgentSession

    state = State.model_validate(orjson.loads(_state_path(workspace).read_bytes()))
    session = AgentSession(
        id="OP-1",
        role=AgentSessionRole.OPERATOR,
        runtime="cli",
        scope_id="QR",
        status=AgentSessionStatus.ACTIVE,
        started_at=datetime(2026, 6, 11, tzinfo=UTC),
    )
    state.agent_sessions[session.id] = session
    if session.id not in state.current.active_session_ids:
        state.current.active_session_ids.insert(0, session.id)
    _state_path(workspace).write_text(state.model_dump_json(), encoding="utf-8")


def _force_close_gate_bearing_wave(workspace: Path) -> None:
    """Drive one daemonless gate-bearing close through the waiver door."""
    _bootstrap_claimed_wave(workspace)
    _attach_gate(workspace)
    _attach_operator_session(workspace)
    res = runner.invoke(app, ["wave", "close", _WAVE_ID, "--outcome", "done", "--no-runtime"])
    assert res.exit_code == 0, res.stdout


def _waivers_json(*args: str) -> tuple[int, dict[str, Any]]:
    """Invoke ``wave waivers`` in JSON mode; return ``(exit_code, payload)``."""
    res = runner.invoke(app, ["--json", "wave", "waivers", *args])
    return res.exit_code, orjson.loads(res.stdout)


# --- Counting the bypass lane ----------------------------------------------


def test_wave_waivers_counts_waiver_recorded_after_commit(workspace: Path) -> None:
    """A daemonless waiver recorded after the seed commit is counted, and the
    row names the waived scope and the operator's reason.
    """
    _force_close_gate_bearing_wave(workspace)

    exit_code, payload = _waivers_json("--since", "HEAD")

    assert exit_code == 0
    assert payload["count"] == 1
    assert payload["since"] == "HEAD"
    row = payload["waivers"][0]
    assert row["scope"] == _WAVE_ID
    assert row["reason"]  # the operator's recorded reason, verbatim


def test_wave_waivers_since_later_commit_excludes_earlier_waiver(workspace: Path) -> None:
    """Off-by-one boundary: a waiver recorded BEFORE the ``--since`` commit is
    excluded, so a checkpoint never re-counts a waiver a prior one answered for.
    """
    _force_close_gate_bearing_wave(workspace)
    _commit_dated_future(workspace, "checkpoint")

    exit_code, payload = _waivers_json("--since", "HEAD")

    assert exit_code == 0
    assert payload["count"] == 0
    assert payload["waivers"] == []


def test_wave_waivers_without_since_counts_every_waiver(workspace: Path) -> None:
    """With no ``--since`` the verb counts every waiver ever recorded."""
    _force_close_gate_bearing_wave(workspace)
    _commit_dated_future(workspace, "checkpoint")

    exit_code, payload = _waivers_json()

    assert exit_code == 0
    assert payload["count"] == 1
    assert payload["since"] is None


def test_wave_waivers_empty_store_reports_zero(workspace: Path) -> None:
    """Empty boundary: a workspace that never bypassed the daemon reports 0
    rather than erroring on the absent / waiver-free event store.
    """
    _bootstrap_claimed_wave(workspace)

    exit_code, payload = _waivers_json("--since", "HEAD")

    assert exit_code == 0
    assert payload["count"] == 0
    assert payload["waivers"] == []


def test_wave_waivers_text_output_names_scope_and_reason(workspace: Path) -> None:
    """The default (non-JSON) rendering carries the count plus one line per
    waiver naming its scope -- the shape an operator reads at checkpoint close.
    """
    _force_close_gate_bearing_wave(workspace)

    res = runner.invoke(app, ["wave", "waivers", "--since", "HEAD"])

    assert res.exit_code == 0, res.stdout
    assert "daemonless close waivers since HEAD: 1" in res.stdout
    assert _WAVE_ID in res.stdout


# --- Error paths ------------------------------------------------------------


def test_wave_waivers_rejects_unresolvable_since_ref(workspace: Path) -> None:
    """Error path: a ``--since`` ref that names no commit is refused with the
    typed ``InvalidInput`` envelope instead of silently counting everything.
    """
    _force_close_gate_bearing_wave(workspace)

    exit_code, payload = _waivers_json("--since", "no-such-ref-wave-waivers")

    assert exit_code != 0
    assert payload["error"] == "UserError"
    assert payload["data"]["kind"] == "InvalidInput"


def test_wave_waivers_rejects_empty_since_ref(workspace: Path) -> None:
    """Boundary: an empty ``--since`` value is a ref that resolves to nothing,
    so it is refused rather than treated as "no filter".
    """
    _force_close_gate_bearing_wave(workspace)

    exit_code, _payload = _waivers_json("--since", "")

    assert exit_code != 0
