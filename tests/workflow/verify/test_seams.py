"""Wire tests for the three wave-close seams attaching readiness compute.

Per the W06 success criteria:

* sc #3 — the three close seams call :func:`readiness.compute` AFTER
  the state mutation lands; at least one seam asserts ordering
  explicitly. The CLI seam (``_close_and_pin``) is the canonical
  one — :func:`test_close_and_pin_calls_compute_after_close_wave`
  pins the ordering by monkeypatching both ``close_wave`` and
  ``compute`` to record the call sequence.
* sc #5 — the close envelope grows a ``readiness_warnings_count``
  field; additive so existing callers stay green. Verified for the
  daemon seam directly against the on-disk envelope.
* sc #6 — ``wave_land`` runs readiness before auto-close and leaves
  the wave open when the projection is non-ready.
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import (
    ProjectStatus,
    ScopeKind,
    StoreKind,
    WaveStatus,
)
from eawf.kernel.state.models import (
    CurrentPointers,
    Project,
    State,
)
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.workflow.lifecycle.transitions import (
    claim_wave,
    open_iter,
    open_phase,
    plan_wave,
)
from eawf.workflow.verify import readiness as readiness_mod
from eawf.workflow.verify.models import CloseReadiness
from tests._criteria_helpers import legacy_criteria

WAVE_ID = "P01-I01-W01"


# ---- Shared helpers ---------------------------------------------------------


def _empty_state() -> State:
    return State.model_validate(
        {
            "schema_version": "1.0",
            "scope_kind": ScopeKind.REPO.value,
            "urn": "urn:eawf:v1:state:VFY",
            "updated_at": datetime.now(UTC).isoformat(),
            "project": Project(
                code="VFY",
                slug="vfy",
                title="VFY",
                description=None,
                domains=["x"],
                default_branch="main",
                status=ProjectStatus.ACTIVE,
                repo_urn="urn:eawf:v1:repo:VFY",
            ).model_dump(mode="json"),
            "current": CurrentPointers(project_code="VFY").model_dump(mode="json"),
            "workspace": None,
            "phases": {},
            "iters": {},
            "waves": {},
            "artifacts": {},
            "agent_sessions": {},
            "plugins": {},
            "indexes": {},
        }
    )


def _seed_claimed_wave(state: State, *, criteria: list[str] | None = None) -> None:
    open_phase(state, phase_id="P01", title="phase")
    open_iter(state, iter_id="P01-I01", phase_id="P01", title="iter")
    plan_wave(
        state,
        wave_id=WAVE_ID,
        iter_id="P01-I01",
        title="wave",
        file_scopes=["src/"],
        success_criteria=legacy_criteria(*(criteria or [])),
        effort_bucket="M",
    )
    claim_wave(state, wave_id=WAVE_ID, session_id="SES-1")


# ---- Seam #1 — CLI _close_and_pin -------------------------------------------


def test_close_and_pin_calls_compute_after_close_wave(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_close_and_pin`` calls ``compute`` AFTER ``close_wave`` (sc #3).

    Drives ``eawf wave close`` via :class:`typer.testing.CliRunner`
    against a bootstrapped state.json. Patches the live call sites of
    ``close_wave`` (imported into ``lifecycle_wave``) and ``compute``
    (re-exported from ``eawf.workflow.verify`` and imported into
    ``lifecycle_wave``) to a recorder; the recorder asserts the
    canonical ``close_wave`` -> ``compute`` ordering.
    """
    from typer.testing import CliRunner

    from eawf.surfaces.cli.app import app

    # Bootstrap a project on disk so the CLI close has real
    # state.json + .ea/store to land against.
    state_path = tmp_path / ".ea" / "state.json"
    monkeypatch.setenv("EA_STATE", str(state_path))
    runner = CliRunner()
    assert (
        runner.invoke(app, ["project", "init", "QR", "--title", "Q", "--domains", "x"]).exit_code
        == 0
    )
    assert runner.invoke(app, ["phase", "open", "--auto", "--title", "x"]).exit_code == 0
    assert runner.invoke(app, ["iter", "open", "--phase", "P01", "--title", "I1"]).exit_code == 0
    assert (
        runner.invoke(
            app,
            [
                "wave",
                "plan",
                "P01-I01",
                "--id",
                WAVE_ID,
                "--title",
                "wave",
                "--files",
                "src/",
                "--success",
                "legacy a",
                "--effort-bucket",
                "M",
            ],
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["wave", "claim", WAVE_ID, "--session", "SES-1"]).exit_code == 0

    # Patch the two functions in the CLI handler module (their import
    # site, not the source module — lazy imports inside the function
    # body bind the names against the handler module's namespace).
    import eawf.workflow.lifecycle.transitions as transitions_mod
    import eawf.workflow.verify as verify_pkg

    call_log: list[str] = []
    original_close = transitions_mod.close_wave

    def fake_close_wave(
        state_arg: State,
        *,
        wave_id: str,
        outcome: str,
        tokens_consumed: int | None = None,
    ) -> Any:
        call_log.append(f"close_wave:{wave_id}")
        return original_close(
            state_arg,
            wave_id=wave_id,
            outcome=outcome,
            tokens_consumed=tokens_consumed,
        )

    def fake_compute(scope_id: str, **_kwargs: Any) -> CloseReadiness:
        call_log.append(f"compute:{scope_id}")
        return CloseReadiness(ready=True, criteria=[], warnings=[], waived_gate_ids=[])

    monkeypatch.setattr(transitions_mod, "close_wave", fake_close_wave)
    monkeypatch.setattr(verify_pkg, "compute", fake_compute)

    result = runner.invoke(app, ["wave", "close", WAVE_ID, "--outcome", "ok"])
    assert result.exit_code == 0, result.stdout

    # The handler MUST call close_wave then compute (in that order)
    # for the wave it just closed.
    assert call_log == [f"close_wave:{WAVE_ID}", f"compute:{WAVE_ID}"], (
        f"compute must run AFTER close_wave; got {call_log}"
    )


# ---- Seam #2 — Daemon _apply_wave_close (envelope extras) -------------------


def _daemon_state_layout(tmp_path: Path, state: State) -> tuple[Path, Path, Path]:
    """Write *state* under ``<tmp_path>/.ea/state.json`` and return paths.

    The daemon path joins ``<repo_root>/.ea/state.json`` (per
    :func:`eawf.runtime.daemon.methods.state._resolve_state_path`), so the
    test must mirror that layout — *not* drop ``state.json`` at the
    repo-root level.

    Returns:
        ``(state_path, event_path, wal_dir)``.
    """
    ea_dir = tmp_path / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = ea_dir / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return state_path, event_path, wal_dir


def test_daemon_mutate_wave_close_pins_readiness_warnings_count(
    tmp_path: Path,
) -> None:
    """Daemon WAVE_CLOSE envelope grows ``readiness_warnings_count`` (sc #5)."""
    import asyncio
    import os
    import uuid

    from eawf import __version__
    from eawf.runtime.daemon import PROTOCOL_VERSION
    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.methods import MethodContext
    from eawf.runtime.daemon.methods.state import mutate

    state = _empty_state()
    _seed_claimed_wave(state, criteria=["legacy x", "legacy y"])
    state_path, event_path, wal_dir = _daemon_state_layout(tmp_path, state)
    ctx = MethodContext(
        started_at="2026-05-26T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=WAVE_ID,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": WAVE_ID, "outcome": "test-close"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(
            ctx,
            {"mutation": mutation.model_dump(mode="json"), "repo_root": str(tmp_path)},
        )
        envelope_payload = result["event"]["payload"]
        # Two legacy criteria => two advisory warnings => count == 2.
        assert envelope_payload["extras"]["readiness_warnings_count"] == 2

    asyncio.run(body())


def test_daemon_mutate_non_wave_close_emits_empty_extras(tmp_path: Path) -> None:
    """Non-WAVE_CLOSE mutations carry an empty ``extras`` dict (additive)."""
    import asyncio
    import os
    import uuid

    from eawf import __version__
    from eawf.runtime.daemon import PROTOCOL_VERSION
    from eawf.runtime.daemon.bus import EventBus
    from eawf.runtime.daemon.methods import MethodContext
    from eawf.runtime.daemon.methods.state import mutate

    state = _empty_state()
    # Seed a claimed wave we will FAIL rather than close, so the
    # mutation kind is WAVE_FAIL (not WAVE_CLOSE).
    _seed_claimed_wave(state)
    state_path, event_path, wal_dir = _daemon_state_layout(tmp_path, state)
    ctx = MethodContext(
        started_at="2026-05-26T00:00:00+00:00",
        pid=os.getpid(),
        protocol_version=PROTOCOL_VERSION,
        version=__version__,
        shutdown_event=asyncio.Event(),
        bus=EventBus(),
        event_path=event_path,
        state_path=state_path,
        wal_dir=wal_dir,
        idempotency_cache={},
    )
    mutation = Mutation(
        kind=MutationKind.WAVE_FAIL,
        scope_id=WAVE_ID,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": WAVE_ID, "reason": "expected"},
    )

    async def body() -> None:
        result: dict[str, Any] = await mutate(
            ctx,
            {"mutation": mutation.model_dump(mode="json"), "repo_root": str(tmp_path)},
        )
        envelope_payload = result["event"]["payload"]
        # WAVE_FAIL is not a close kind, so the W06 extras are empty.
        assert envelope_payload["extras"] == {}

    asyncio.run(body())


# ---- Seam #3 — wave_land (non-blocking on non-ready) ------------------------


def _make_repo(repo_root: Path, *, feature_branch: str = "feature/eawf-v0.1") -> Path:
    """Init a tiny git repo on a feature branch (refuses ``main``).

    Mirrors :func:`tests.unit.test_worktree_create._make_repo` —
    :func:`eawf.runtime.worktree.create.create_worktree` refuses to branch
    from the default branch, so the repo must be on a feature branch
    when the test wires up the worktree.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(repo_root), "init", "-q", "-b", "main"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "config", "user.email", "test@example.com"],
        check=True,
    )
    subprocess.run(["git", "-C", str(repo_root), "config", "user.name", "test"], check=True)
    (repo_root / "README.md").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(repo_root), "add", "."], check=True)
    subprocess.run(["git", "-C", str(repo_root), "commit", "-q", "-m", "seed"], check=True)
    subprocess.run(
        ["git", "-C", str(repo_root), "checkout", "-q", "-b", feature_branch], check=True
    )
    return repo_root


@pytest.mark.skipif(shutil.which("git") is None, reason="git is required")
def test_wave_land_leaves_wave_open_on_failing_readiness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``wave_land`` lands commits but skips auto-close on non-ready readiness.

    We monkeypatch ``compute`` to return a failing readiness and assert
    ``close_wave`` does not run; merge-back evidence stays available for
    a later retry after evidence is fixed.
    """
    # Build a minimal repo + worktree with one commit on a feature branch.
    from eawf.runtime.worktree.create import create_worktree
    from eawf.runtime.worktree.wave_land import wave_land

    repo = _make_repo(tmp_path / "repo")
    # The wave-land path needs a state file (the seam resolves it via
    # ``resolve_state_path``) — write one alongside.
    ea_dir = repo / ".ea"
    ea_dir.mkdir(parents=True, exist_ok=True)
    state = _empty_state()
    _seed_claimed_wave(state)
    state_path = ea_dir / "state.json"
    state_path.write_bytes(orjson.dumps(state.model_dump(mode="json")))

    # Seed a worktree + a commit on it.
    record = create_worktree(state, repo_root=repo, wave_id=WAVE_ID)
    worktree_dir = repo / record.path
    (worktree_dir / "f.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(worktree_dir), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(worktree_dir), "commit", "-q", "-m", "[P01-W01] add f"],
        check=True,
    )

    # Patch compute to return a non-ready readiness.
    def fake_compute(scope_id: str, **_kwargs: Any) -> CloseReadiness:
        return CloseReadiness(
            ready=False,
            criteria=[],
            warnings=["synthetic failure for sc #6"],
            waived_gate_ids=[],
        )

    monkeypatch.setattr(readiness_mod, "compute", fake_compute)
    # The wave_land submodule imported ``compute`` as
    # ``compute_readiness`` at module load; rebind that local alias so
    # the live call site picks up the test double. The package
    # ``__init__`` shadows the submodule attribute with the function
    # of the same name, so go through ``sys.modules`` to grab the
    # module reference.
    import sys

    wave_land_module = sys.modules["eawf.runtime.worktree.wave_land"]
    monkeypatch.setattr(wave_land_module, "compute_readiness", fake_compute)

    result = wave_land(state, repo_root=repo, wave_id=WAVE_ID, keep_worktree=True)

    # The commit landed, but the wave remains open until readiness clears.
    assert state.waves[WAVE_ID].status == WaveStatus.CLAIMED
    assert result.closed is False
    assert result.commits  # cherry-pick landed.
