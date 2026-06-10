"""Tests: the daemon close path convenes the cross-vendor jury (P29-I04-W15).

Exercises the opt-in jury upgrade of the enforcing wave-close verdict gate
wired into :func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate`:
when an enabled profile sets ``verify.cross_vendor_jury: true`` AND the host's
cross-vendor CLI lanes resolve, a high-risk (``always``) wave's close is gated
on a three-vendor disjoint-family jury reduced through the TRUST-3 reducer. A
minority-veto FAIL is held ADVISORY (W10) -- logged, never blocking -- until I07
TRUST-4 supplies the earned-authority computation; a split / sub-quorum
NEEDS_USER still blocks close. The path degrades to the single fresh-auditor
gate when the flag is OFF or the lanes are unavailable.

The juror spawn is ALWAYS stubbed: the test monkeypatches
``_jury_spawn_factory`` to return per-runtime recording stubs that replay canned
auditor bodies, and ``_cross_vendor_lanes_ready`` to a constant, so NO real
``claude`` / ``codex`` / ``opencode`` subprocess, network, or auth runs. The
real :func:`convene_cross_vendor_jury` + reducer run over the stubbed spawns, so
the gate -> reducer -> lifecycle-mapping wiring is exercised end to end.
"""

from __future__ import annotations

import asyncio
import json
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
from eawf.observability.eval.cross_vendor_jury import JURY_RUNTIME_FAMILIES
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.llm_assist import SpawnFn

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
_HIGH_RISK_WAVE = "P29-I04-W15"
_CRITERION = "convene the cross-vendor jury at the close gate"


def _now() -> datetime:
    return _T0


# --------------------------------------------------------------------------- #
# Per-runtime recording stubs (mirrors tests/eval/jury/test_cross_vendor_jury).
# --------------------------------------------------------------------------- #


def _auditor_body_json(*, verdict: str) -> str:
    return json.dumps(
        {
            "role": "auditor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": _HIGH_RISK_WAVE,
            "criteria": [
                {"criterion": _CRITERION, "passed": verdict in {"pass", "pass-with-followups"}}
            ],
            "refutations": [],
        }
    )


class _RecordingSpawn:
    """Replays one canned auditor body per call for a single runtime."""

    def __init__(self, runtime: str, answers: list[str]) -> None:
        self.runtime = runtime
        self._answers = list(answers)
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        if self.calls >= len(self._answers):
            raise AssertionError(f"spawn for {self.runtime!r} over-called")
        text = self._answers[self.calls]
        self.calls += 1
        return SpawnResult(
            session_id=f"sess-{self.runtime}-{self.calls}",
            runtime=self.runtime,
            model="model-x",
            subprocess_pid=4242,
            exit_status=0,
            text=text,
            started_at=_T0,
            ended_at=_T0,
        )


class _RaisingSpawn:
    """Models an unavailable juror lane: every spawn raises (-> abstention)."""

    def __init__(self, runtime: str) -> None:
        self.runtime = runtime
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.calls += 1
        raise RuntimeError(f"{self.runtime}: runtime unavailable")


def _patch_jury(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdicts: dict[str, str] | None = None,
    raising: set[str] | None = None,
    lanes_ready: bool = True,
) -> dict[str, object]:
    """Patch the lane pre-check + spawn factory; return the per-runtime stubs.

    Each juror runtime gets a recording stub replaying its canned verdict, or a
    raising stub when named in *raising*. Returns the stub map so a test can
    assert which lanes were spawned.
    """
    verdicts = verdicts or {}
    raising = raising or set()
    stubs: dict[str, object] = {}
    for runtime in JURY_RUNTIME_FAMILIES:
        if runtime in raising:
            stubs[runtime] = _RaisingSpawn(runtime)
        else:
            stubs[runtime] = _RecordingSpawn(
                runtime, [_auditor_body_json(verdict=verdicts.get(runtime, "pass"))]
            )

    def _fake_factory(state: Any, wave: Any, *, repo_root: Path) -> Callable[[str], SpawnFn]:
        def _factory(runtime: str) -> SpawnFn:
            return stubs[runtime]  # type: ignore[return-value]

        return _factory

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._cross_vendor_lanes_ready",
        lambda *, quorum: lanes_ready,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _fake_factory,
    )
    return stubs


# --------------------------------------------------------------------------- #
# State + enforcing-profile fixtures.
# --------------------------------------------------------------------------- #


def _state_payload(*, wave_id: str, effort_bucket: str, title: str) -> dict[str, Any]:
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
                "subproject_id": None,
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
                        "text": _CRITERION,
                        "kind": "legacy",
                        "acceptance_style": "binary",
                        "evidence_kind": "attested",
                        "quality_dimension": "functional_suitability",
                        "measurable_signal": _CRITERION,
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
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=root, check=True, env=env
    )


def _write_enforcing_profile(root: Path, *, cross_vendor_jury: bool) -> None:
    """Enable an enforcing profile whose floor check passes.

    The floor check is a passing ``git status`` so readiness enforcement clears
    and the verdict gate (single-auditor or jury) is the SOLE blocker. The
    ``cross_vendor_jury`` leaf toggles the gate flavour.
    """
    profile_dir = root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n", encoding="utf-8"
    )
    lines = [
        "name: enforcing",
        "verify:",
        "  enforce: true",
        f"  cross_vendor_jury: {'true' if cross_vendor_jury else 'false'}",
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
    profile_dir.joinpath("enforcing.yaml").write_text("\n".join(lines), encoding="utf-8")


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


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


def _setup(
    tmp_path: Path, *, cross_vendor_jury: bool, effort_bucket: str = "L"
) -> tuple[Path, MethodContext, Mutation]:
    _write_enforcing_profile(tmp_path, cross_vendor_jury=cross_vendor_jury)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            wave_id=_HIGH_RISK_WAVE, effort_bucket=effort_bucket, title="cross-vendor jury gate"
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_HIGH_RISK_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _HIGH_RISK_WAVE, "outcome": "ok"},
    )
    return state_path, ctx, mutation


# --------------------------------------------------------------------------- #
# (a) flag-on + unanimous PASS jury -> close proceeds.
# --------------------------------------------------------------------------- #


def test_jury_unanimous_pass_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Three PASS ballots reduce to PASS and the wave closes."""
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"

    _run(body)
    # Every disjoint vendor lane was spawned exactly once.
    for runtime in JURY_RUNTIME_FAMILIES:
        stub = stubs[runtime]
        assert isinstance(stub, _RecordingSpawn)
        assert stub.calls == 1


# --------------------------------------------------------------------------- #
# (b) flag-on + split (no-veto) jury -> NEEDS_USER -> blocks close.
# --------------------------------------------------------------------------- #


def test_jury_split_no_veto_blocks_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A pass / pass-with-followups split surfaces NEEDS_USER and blocks close.

    Re-pinned to the unified oracle: run_oracle routes the always-wave's
    single un-gated criterion to the jury tier, whose NEEDS_USER reduction
    surfaces as ``oracle blocked close (... status=needs_user): cross-vendor
    jury outcome=needs_user``.
    """
    _patch_jury(
        monkeypatch,
        verdicts={
            "claude-code": "pass",
            "codex": "pass-with-followups",
            "opencode": "pass",
        },
    )
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="oracle blocked close"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)


# --------------------------------------------------------------------------- #
# (c) flag-on + a juror failing -> per the reducer (advisory FAIL / sub-quorum).
# --------------------------------------------------------------------------- #


def test_jury_one_fail_veto_held_advisory_close_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A single FAIL veto is held advisory (W10): the close proceeds, not blocks.

    The cross-vendor jury is uncalibrated on eawf's own distribution, so a
    minority-veto FAIL is held advisory by
    :func:`~eawf.workflow.verify.oracle.jury_block_authority` until I07
    TRUST-4: run_oracle's jury tier logs the veto at WARNING and returns
    ``status="pass"``, so the high-risk wave CLOSES rather than staying CLAIMED.
    """
    _patch_jury(
        monkeypatch,
        verdicts={"claude-code": "pass", "codex": "fail", "opencode": "pass"},
    )
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        with caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"

    _run(body)
    assert any(
        r.levelname == "WARNING" and "jury_veto_advisory" in r.getMessage() for r in caplog.records
    )


def test_jury_sub_quorum_abstentions_block(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Two juror lanes failing drops below quorum -> NEEDS_USER -> blocks close.

    Re-pinned to the unified oracle: two abstaining lanes leave the jury
    below quorum, so run_oracle's jury tier returns NEEDS_USER and the close
    surfaces as ``oracle blocked close (... status=needs_user)``.
    """
    _patch_jury(
        monkeypatch,
        verdicts={"claude-code": "pass"},
        raising={"codex", "opencode"},
    )
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="oracle blocked close"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)


def test_jury_one_lane_abstains_quorum_pass_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One lane down + two PASS ballots stays at quorum -> PASS -> closes.

    Boundary: the jury survives a single vendor outage (quorum 2 of 3) and a
    transient lane failure does not block an otherwise-clean close.
    """
    _patch_jury(
        monkeypatch,
        verdicts={"claude-code": "pass", "codex": "pass"},
        raising={"opencode"},
    )
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"

    _run(body)


# --------------------------------------------------------------------------- #
# (d) flag-OFF -> the single-auditor gate path, jury never convened.
# --------------------------------------------------------------------------- #


def test_flag_off_routes_always_wave_to_single_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag OFF an always-wave routes to the single-auditor producer.

    Under the unified close gate (W03), an ``always`` wave whose profile has
    ``cross_vendor_jury: false`` takes the single-auditor branch BEFORE the
    run_oracle loop: it spawns one ``claude-code`` auditor (via the same
    spawn factory the jury uses) and reads its verdict. The cross-vendor jury
    is never convened, so only the ``claude-code`` lane is spawned -- the
    ``codex`` / ``opencode`` lanes stay untouched -- and a clean PASS verdict
    lets the wave close.
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=False)

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"

    _run(body)
    # Only the single-auditor (claude-code) lane spawned; no jury convened.
    claude_stub = stubs["claude-code"]
    assert isinstance(claude_stub, _RecordingSpawn)
    assert claude_stub.calls == 1
    for runtime in ("codex", "opencode"):
        stub = stubs[runtime]
        assert isinstance(stub, _RecordingSpawn)
        assert stub.calls == 0


# --------------------------------------------------------------------------- #
# Flag-on: run_oracle convenes the jury with no separate lane pre-check.
# --------------------------------------------------------------------------- #


def test_flag_on_lanes_unavailable_block_via_jury_abstention(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flag-on with all lanes raising -> jury abstains, sub-quorum -> block.

    The unified close gate (W03) dropped the separate ``_cross_vendor_lanes_
    ready`` pre-check: an ``always`` wave under ``cross_vendor_jury: true``
    always falls through to run_oracle's jury tier. When every juror lane
    raises (the host lacks the vendor CLIs), each juror abstains, the vote
    falls below quorum, and the jury returns NEEDS_USER -- so the close is
    blocked (it does NOT gracefully degrade to a single-auditor PASS). Every
    lane is spawned (attempted) exactly once.
    """
    stubs = _patch_jury(
        monkeypatch,
        raising=set(JURY_RUNTIME_FAMILIES),
        lanes_ready=False,
    )
    state_path, ctx, mutation = _setup(tmp_path, cross_vendor_jury=True)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="oracle blocked close"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)
    for runtime in JURY_RUNTIME_FAMILIES:
        stub = stubs[runtime]
        assert isinstance(stub, _RaisingSpawn)
        assert stub.calls == 1
