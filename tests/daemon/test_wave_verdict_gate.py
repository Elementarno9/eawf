"""Tests: the daemon close path enforces the fresh-auditor verdict gate.

Exercises the single additive daemon-side hook P29-I04-W07 wires into
:func:`eawf.runtime.daemon.methods.state._compute_wave_close_readiness`: on
the enforcing close path a high-risk (``always``) wave whose freshest
auditor verdict is absent or FAIL / BLOCKED is refused before any write,
while a clean PASS verdict lets close proceed and a ``skip`` mechanical
wave never blocks.

The verify profile here declares a *passing* floor check so the verdict
gate is the SOLE blocker -- the existing readiness-enforcement test in
``test_state_methods.py`` already covers the failing-floor path. Auditor
verdicts are written to disk through the dispatch-layer producer
(:func:`eawf.workflow.dispatch.verdict.produce_wave_verdict`) driven by a
recording spawn stub -- no real subprocess.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.verdict import produce_wave_verdict, verdict_requirement

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _now() -> datetime:
    return _T0


def _state_payload(*, wave_id: str, effort_bucket: str, title: str) -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave under P29-I04."""
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": _now().isoformat(),
        "project": {
            "code": "ABC",
            "slug": "abc",
            "title": "ABC",
            "description": None,
            "domains": ["x"],
            "default_branch": "main",
            "status": "active",
            "repo_urn": "urn:eawf:v1:repo:ABC",
        },
        "current": {"project_code": "ABC"},
        "workspace": None,
        "phases": {
            "P29": {
                "id": "P29",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P29",
                "status": "active",
                "iter_ids": ["P29-I04"],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I04": {
                "id": "P29-I04",
                "phase_id": "P29",
                "title": "I04",
                "status": "active",
                "wave_ids": [wave_id],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            wave_id: {
                "id": wave_id,
                "iter_id": "P29-I04",
                "title": title,
                "status": "claimed",
                "claim_session_id": "session-abc",
                "success_criteria": [
                    {
                        "id": "CR-01",
                        "text": "ship the producer",
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": "grandfathered legacy criterion",
                    }
                ],
                "effort_bucket": effort_bucket,
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
                "sessions": {},
                "runtime_baseline": {
                    "api_duration_ms": 5000,
                    "total_duration_ms": 7000,
                    "captured_at": _now().isoformat(),
                },
                "runtime_latest": {
                    "api_duration_ms": 17000,
                    "total_duration_ms": 23000,
                    "captured_at": (_now() + timedelta(minutes=5)).isoformat(),
                },
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _init_git_repo(root: Path) -> None:
    """Init a git repo with one empty commit so a read-only floor check passes.

    The enforcing profile's floor check runs ``git status`` with *root* as
    cwd; ``git status`` exits 0 only inside a real repo, so the readiness
    enforcement clears and the W07 verdict gate is the SOLE blocker.
    """
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"],
        cwd=root,
        check=True,
        env=env,
    )


def _write_passing_verify_profile(root: Path) -> None:
    """Enable an enforcing profile whose floor check passes.

    With a passing floor check the readiness enforcement clears, so the only
    gate that can refuse close is the W07 verdict gate -- isolating it. The
    floor check is ``git status`` (a read-only allow-set verb that exits 0 in
    the git repo :func:`_init_git_repo` set up).
    """
    profile_dir = root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n",
        encoding="utf-8",
    )
    profile_dir.joinpath("enforcing.yaml").write_text(
        "\n".join(
            [
                "name: enforcing",
                "verify:",
                "  enforce: true",
                "  argv_allowlist:",
                "    - git",
                "  floor_checks:",
                "    - name: pass-floor",
                '      cmd: ["git", "status"]',
                "      scope: all",
                "      cadence: every-wave",
                "      policy: warn",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _write_state(state_path: Path, payload: dict[str, Any]) -> State:
    state = State.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-06-01T00:00:00+00:00",
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


def _auditor_body_json(*, verdict: str, wave_id: str) -> str:
    return orjson.dumps(
        {
            "role": "auditor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": wave_id,
            "criteria": [{"criterion": "ship the producer", "passed": verdict != "fail"}],
            "refutations": [],
        }
    ).decode("utf-8")


class _RecordingSpawn:
    def __init__(self, answer: str) -> None:
        self._answer = answer
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.calls += 1
        return SpawnResult(
            session_id="sess-auditor",
            runtime="claude-code",
            model="opus",
            subprocess_pid=8888,
            exit_status=0,
            text=self._answer,
            started_at=_T0,
            ended_at=_T0,
        )


def _patch_producer_spawn(
    monkeypatch: pytest.MonkeyPatch, *, verdict: str, wave_id: str
) -> _RecordingSpawn:
    """Stub the close gate's single-auditor producer spawn factory.

    The unified close gate (W03) routes an ``always`` wave under a
    jury-OFF profile to the single-auditor producer, which now SPAWNS a
    fresh auditor (via ``_jury_spawn_factory(...)('claude-code')``) before
    reading the verdict gate -- so the daemon must never reach a real
    subprocess under test. The returned recording stub replays one canned
    auditor body so the producer persists a deterministic verdict.
    """
    stub = _RecordingSpawn(_auditor_body_json(verdict=verdict, wave_id=wave_id))

    def _fake_factory(
        state: Any,
        wave: Any,
        *,
        repo_root: Path,
        timeout_seconds: float = 600.0,
        events_path: Any = None,
    ) -> Callable[[str], Any]:
        def _factory(runtime: str) -> _RecordingSpawn:
            return stub

        return _factory

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _fake_factory,
    )
    return stub


def _seed_auditor_verdict(*, state_path: Path, wave_id: str, verdict: str, tmp_path: Path) -> None:
    """Write a fresh auditor verdict to disk via the dispatch-layer producer."""
    state = State.model_validate(orjson.loads(state_path.read_bytes()))
    events_path = store_path(state_path, StoreKind.EVENT)
    wave = state.waves[wave_id]
    asyncio.run(
        produce_wave_verdict(
            state=state,
            state_path=state_path,
            events_path=events_path,
            wave=wave,
            spawn=_RecordingSpawn(_auditor_body_json(verdict=verdict, wave_id=wave_id)),
            repo_root=tmp_path,
        )
    )
    # Persist the auditor session the producer registered so the close path
    # reads a consistent state (the producer mutates state in place).
    state_path.write_text(state.model_dump_json(), encoding="utf-8")


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


_HIGH_RISK_WAVE = "P29-I04-W07"


# --------------------------------------------------------------------------- #
# A high-risk wave with NO fresh auditor verdict blocks close.
# --------------------------------------------------------------------------- #


def test_close_high_risk_no_prior_verdict_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An always-wave with no prior verdict whose produced verdict is FAIL blocks.

    Re-pinned to the unified close gate (W03): a jury-OFF ``always`` wave
    routes to the single-auditor producer, which now SPAWNS a fresh auditor
    before the verdict gate reads it. With no PRIOR auditor verdict the
    producer spawns (stubbed to a FAIL body); the freshly-produced verdict is
    not close-ready, so the gate refuses close with ``verdict gate blocked``
    and the wave stays CLAIMED.
    """
    stub = _patch_producer_spawn(monkeypatch, verdict="fail", wave_id=_HIGH_RISK_WAVE)
    _write_passing_verify_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(wave_id=_HIGH_RISK_WAVE, effort_bucket="L", title="live verdict producer"),
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_HIGH_RISK_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _HIGH_RISK_WAVE, "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        # The wave stayed CLAIMED -- the close never persisted.
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)
    # The single-auditor producer spawned exactly one fresh auditor.
    assert stub.calls == 1


def test_close_high_risk_fail_verdict_is_blocked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An always-wave whose auditor verdict stays FAIL is refused.

    Re-pinned to the unified close gate (W03): a FAIL verdict is seeded, so
    the single-auditor producer's idempotency check sees a non-close-ready
    verdict and RE-SPAWNS (stubbed to a FAIL body again); the gate then reads
    a still-FAIL verdict and refuses close with ``verdict gate blocked``.
    """
    stub = _patch_producer_spawn(monkeypatch, verdict="fail", wave_id=_HIGH_RISK_WAVE)
    _write_passing_verify_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(wave_id=_HIGH_RISK_WAVE, effort_bucket="L", title="live verdict producer"),
    )
    _seed_auditor_verdict(
        state_path=state_path, wave_id=_HIGH_RISK_WAVE, verdict="fail", tmp_path=tmp_path
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_HIGH_RISK_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _HIGH_RISK_WAVE, "outcome": "ok"},
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)
    # The producer re-spawned because the seeded FAIL was not close-ready.
    assert stub.calls == 1


def test_close_high_risk_pass_verdict_proceeds(tmp_path: Path) -> None:
    """An always-wave with a clean PASS auditor verdict closes."""
    _write_passing_verify_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(wave_id=_HIGH_RISK_WAVE, effort_bucket="L", title="live verdict producer"),
    )
    _seed_auditor_verdict(
        state_path=state_path, wave_id=_HIGH_RISK_WAVE, verdict="pass", tmp_path=tmp_path
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_HIGH_RISK_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _HIGH_RISK_WAVE, "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"

    _run(body)


# --------------------------------------------------------------------------- #
# A mechanical (skip) wave never blocks on a missing verdict.
# --------------------------------------------------------------------------- #


def test_close_skip_mechanical_wave_not_blocked(tmp_path: Path) -> None:
    """A mechanical wave the sampler skipped closes despite no verdict."""
    # Pick a small mechanical wave id whose deterministic sampler skips it.
    skip_wave_id: str | None = None
    for suffix in range(1, 60):
        candidate = f"P29-I04-W{suffix:02d}"
        probe = State.model_validate(
            _state_payload(wave_id=candidate, effort_bucket="S", title="mechanical edit")
        ).waves[candidate]
        if verdict_requirement(probe) == "skip":
            skip_wave_id = candidate
            break
    assert skip_wave_id is not None, "expected a skipped mechanical wave id"

    _write_passing_verify_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(wave_id=skip_wave_id, effort_bucket="S", title="mechanical edit"),
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=skip_wave_id,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": skip_wave_id, "outcome": "ok"},
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][skip_wave_id]["status"] == "closed"

    _run(body)
