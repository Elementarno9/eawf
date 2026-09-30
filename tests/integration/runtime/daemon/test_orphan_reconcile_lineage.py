"""The daemon-boot orphan sweep only flips its own lineage's sessions.

A daemon's runtime directory is its lineage: every daemon that boots on
the same directory is a restart of the one before, and the first boot
record in the churn ledger marks when that lineage was born. The release
pipeline proves gates with a daemon on a fresh ``EAWF_RUNTIME_DIR`` inside
a release checkout whose committed ``state.json`` carries ACTIVE sessions
some other daemon opened. That boot must leave ``state.json`` byte for
byte, while a restart on the same directory still flips every session its
lineage opened.

Each test runs the daemon's real synchronous boot prologue (the event loop
is stubbed out, so no socket is bound) against a tmp repository.
"""

from __future__ import annotations

import hashlib
import shutil
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Final

import orjson
import pytest
from typer.testing import CliRunner

from eawf.kernel.state.enums import AgentSessionStatus
from eawf.kernel.state.models import State
from eawf.runtime.daemon import main as daemon_main
from eawf.runtime.daemon.churn import (
    CHURN_LEDGER_NAME,
    CHURN_LEDGER_ROTATED_NAME,
    ChurnOp,
    ChurnRecord,
    lineage_born_at,
)
from tests._session_helpers import seed_active_session

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(sys.platform == "win32", reason="boot path needs the POSIX runtime"),
]

runner = CliRunner()

#: A committed epoch-1 state holding one project and no sessions.
EMPTY_REPO_STATE: Final = (
    Path(__file__).resolve().parents[3] / "fixtures" / "states" / "valid" / "01-empty-repo.json"
)

#: When the pre-existing lineage in the restart tests first booted.
LINEAGE_BIRTH: Final = datetime.now(UTC) - timedelta(hours=2)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An epoch-1 repository with no sessions yet, laid down from a committed state.

    The flag day retired ``project init``, so the tree is copied into place
    rather than built through the CLI.
    """
    root = tmp_path / "repo"
    (root / ".ea").mkdir(parents=True)
    shutil.copyfile(EMPTY_REPO_STATE, root / ".ea" / "state.json")
    monkeypatch.setenv("EA_STATE", str(root / ".ea" / "state.json"))
    monkeypatch.setenv("EAWF_REGISTRY_PATH", str(tmp_path / "registry.json"))
    return root


def _state_path(repo: Path) -> Path:
    return repo / ".ea" / "state.json"


def _seed_sessions(repo: Path, opened: dict[str, datetime]) -> None:
    """Commit one ACTIVE session per id, each opened at its given instant."""
    state_path = _state_path(repo)
    state = State.model_validate(orjson.loads(state_path.read_bytes()))
    for session_id, started_at in opened.items():
        session = seed_active_session(state, session_id=session_id, scope_id=session_id)
        session.started_at = started_at
    state_path.write_bytes(
        orjson.dumps(
            state.model_dump(mode="json"),
            option=orjson.OPT_INDENT_2 | orjson.OPT_SORT_KEYS,
        )
    )


def _plant_boot(rt_dir: Path, at: datetime, *, ledger: str = CHURN_LEDGER_NAME) -> None:
    """Record a prior daemon boot on *rt_dir*, as an earlier lifetime left it."""
    rt_dir.mkdir(parents=True, exist_ok=True)
    record = ChurnRecord(
        pid=1,
        op=ChurnOp.BOOT,
        at_ns=int(at.timestamp() * 1_000_000_000),
        suite_session=None,
        touched=("eawfd.pid",),
    )
    with (rt_dir / ledger).open("a", encoding="utf-8") as fh:
        fh.write(f"{record.model_dump_json()}\n")


def _boot(rt_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Run the daemon's synchronous boot prologue on *rt_dir* without binding."""
    rt_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(daemon_main, "ensure_runtime_dir", lambda: rt_dir)
    monkeypatch.setattr(daemon_main, "pid_path", lambda: rt_dir / "eawfd.pid")
    monkeypatch.setattr(daemon_main, "log_path", lambda: rt_dir / "eawfd.log")
    monkeypatch.setattr(daemon_main, "socket_path", lambda: rt_dir / "eawfd.sock")

    def _close(coro: object) -> None:
        if hasattr(coro, "close"):
            coro.close()

    monkeypatch.setattr(daemon_main.asyncio, "run", _close)
    assert daemon_main.run(foreground=True) == 0


def _statuses(repo: Path) -> dict[str, AgentSessionStatus]:
    state = State.model_validate(orjson.loads(_state_path(repo).read_bytes()))
    return {sid: row.status for sid, row in state.agent_sessions.items()}


def _digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def _committed_session_repo(repo: Path) -> None:
    """Seed the sessions a release checkout carries from the operator's daemon."""
    now = datetime.now(UTC)
    _seed_sessions(repo, {"SA": now - timedelta(days=3), "SB": now - timedelta(minutes=1)})


# ---- foreign lineage: the gate-proof daemon --------------------------------


def test_fresh_runtime_dir_boot_leaves_state_bytes_unchanged(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daemon born on a fresh runtime dir owns none of the committed sessions."""
    _committed_session_repo(repo)
    before = _digest(_state_path(repo))
    _boot(tmp_path / "rt", monkeypatch)
    assert _digest(_state_path(repo)) == before
    assert set(_statuses(repo).values()) == {AgentSessionStatus.ACTIVE}


def test_planted_blanket_sweep_rewrites_the_committed_state(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate fires: a sweep that ignores lineage dirties the checkout."""
    _committed_session_repo(repo)
    before = _digest(_state_path(repo))
    monkeypatch.setattr(daemon_main, "lineage_born_at", lambda _rt_dir: None)
    _boot(tmp_path / "rt", monkeypatch)
    assert _digest(_state_path(repo)) != before
    assert set(_statuses(repo).values()) == {AgentSessionStatus.STALE}


# ---- same lineage: a restart ------------------------------------------------


def test_same_lineage_restart_flips_each_orphan(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every session opened since the lineage's first boot goes STALE."""
    rt_dir = tmp_path / "rt"
    _plant_boot(rt_dir, LINEAGE_BIRTH)
    _seed_sessions(
        repo,
        {
            "S1": LINEAGE_BIRTH + timedelta(minutes=5),
            "S2": LINEAGE_BIRTH + timedelta(minutes=50),
            "S3": LINEAGE_BIRTH,
        },
    )
    _boot(rt_dir, monkeypatch)
    assert _statuses(repo) == {
        "S1": AgentSessionStatus.STALE,
        "S2": AgentSessionStatus.STALE,
        "S3": AgentSessionStatus.STALE,
    }


def test_same_lineage_restart_spares_sessions_older_than_the_lineage(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session opened before the lineage first booted is another daemon's."""
    rt_dir = tmp_path / "rt"
    _plant_boot(rt_dir, LINEAGE_BIRTH)
    _seed_sessions(
        repo,
        {
            "OLD": LINEAGE_BIRTH - timedelta(microseconds=1),
            "OURS": LINEAGE_BIRTH + timedelta(minutes=1),
        },
    )
    _boot(rt_dir, monkeypatch)
    assert _statuses(repo) == {
        "OLD": AgentSessionStatus.ACTIVE,
        "OURS": AgentSessionStatus.STALE,
    }


def test_rotated_ledger_lineage_flips_every_active_session(
    tmp_path: Path, repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lineage older than its retained boot records may own any session."""
    rt_dir = tmp_path / "rt"
    _plant_boot(rt_dir, LINEAGE_BIRTH, ledger=CHURN_LEDGER_ROTATED_NAME)
    _plant_boot(rt_dir, LINEAGE_BIRTH + timedelta(hours=1))
    _seed_sessions(repo, {"ANCIENT": LINEAGE_BIRTH - timedelta(days=30)})
    _boot(rt_dir, monkeypatch)
    assert _statuses(repo) == {"ANCIENT": AgentSessionStatus.STALE}


# ---- lineage_born_at --------------------------------------------------------


def test_lineage_born_at_empty_dir_is_now(tmp_path: Path) -> None:
    """No boot record at all: the lineage is born at the call."""
    floor = time.time_ns()
    born = lineage_born_at(tmp_path)
    assert born is not None
    assert born.timestamp() == pytest.approx(floor / 1_000_000_000, abs=5.0)


def test_lineage_born_at_single_boot(tmp_path: Path) -> None:
    """One boot record is the birth."""
    _plant_boot(tmp_path, LINEAGE_BIRTH)
    born = lineage_born_at(tmp_path)
    assert born is not None
    assert born.timestamp() == pytest.approx(LINEAGE_BIRTH.timestamp(), abs=1e-3)


def test_lineage_born_at_takes_the_earliest_boot_and_ignores_exits(tmp_path: Path) -> None:
    """Later boots and exit records never move the birth."""
    _plant_boot(tmp_path, LINEAGE_BIRTH + timedelta(hours=1))
    _plant_boot(tmp_path, LINEAGE_BIRTH)
    exit_record = ChurnRecord(
        pid=1, op=ChurnOp.EXIT, at_ns=0, suite_session=None, touched=("eawfd.pid",)
    )
    with (tmp_path / CHURN_LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write(f"{exit_record.model_dump_json()}\n")
    born = lineage_born_at(tmp_path)
    assert born is not None
    assert born.timestamp() == pytest.approx(LINEAGE_BIRTH.timestamp(), abs=1e-3)


def test_lineage_born_at_rotated_ledger_is_unbounded(tmp_path: Path) -> None:
    """A rotated ledger means the birth is past the retained records."""
    _plant_boot(tmp_path, LINEAGE_BIRTH, ledger=CHURN_LEDGER_ROTATED_NAME)
    assert lineage_born_at(tmp_path) is None


def test_lineage_born_at_skips_a_torn_line(tmp_path: Path) -> None:
    """A torn ledger tail does not hide the valid boot before it."""
    _plant_boot(tmp_path, LINEAGE_BIRTH)
    with (tmp_path / CHURN_LEDGER_NAME).open("a", encoding="utf-8") as fh:
        fh.write('{"pid": 1, "op": "bo')
    born = lineage_born_at(tmp_path)
    assert born is not None
    assert born.timestamp() == pytest.approx(LINEAGE_BIRTH.timestamp(), abs=1e-3)
