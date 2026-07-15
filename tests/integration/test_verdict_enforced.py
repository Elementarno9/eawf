"""Tests: the daemon close gate enforces the verdict for the high-risk band (FS13).

Exercises the risk-weighted enforce-flip that
:func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate` wires on
top of the FS06 ``run_oracle`` loop: with an enforcing profile, a HIGH-RISK
(``"always"``) wave cannot close until a fresh-context auditor verdict is
written + read, while a LOW-RISK (``"skip"``) mechanical wave closes exactly
as it does on the advisory path -- no verdict required, no auditor spawned.

The risk band is decided by
:func:`eawf.workflow.dispatch.verdict.verdict_requirement` purely on the
wave's typed fields. The fixtures pin the band DETERMINISTICALLY (a
``"sandbox"`` security keyword forces ``"always"``; a mechanical wave id the
1-in-N sampler skips forces ``"skip"``) so neither test relies on a sampling
outcome.

The auditor producer is driven by a recording spawn stub (or
monkeypatched to fail), so no real subprocess is ever spawned:

* CR-1 boundary -- a high-risk (security ``"sandbox"``) wave with NO fresh
  auditor verdict is refused before any write; the wave stays CLAIMED;
* CR-1 positive -- the same high-risk wave, once the close path produces a
  PASS auditor verdict (via a recording spawn), closes;
* CR-1 error-path -- the no-verdict block surfaces a structured reason
  string naming the verdict gate;
* CR-2 determinism -- ``verdict_requirement`` returns ``"always"`` for the
  fixed high-risk wave AND ``"skip"`` for the fixed low-risk wave, asserted
  on the branch VALUE, not a sampling outcome;
* positive-control -- a low-risk mechanical wave closes under enforce=True
  WITHOUT a verdict and WITHOUT spawning (``produce_wave_verdict`` is
  monkeypatched to fail the test if the low-risk close ever reaches it).
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
from eawf.kernel.spec.common import grandfather_criterion
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.dispatch.verdict import verdict_requirement

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)

# A high-risk wave: the ``"sandbox"`` keyword in the title forces
# ``verdict_requirement == "always"`` regardless of the small effort bucket,
# so the band is deterministic (not effort- or sampler-driven).
_HIGH_RISK_WAVE = "P29-I11-W06"
_HIGH_RISK_TITLE = "enforce the sandbox deny-list at the close gate"

# A low-risk mechanical wave: a small executor wave with no security keyword,
# whose id the 1-in-N sampler deterministically skips (verified in
# ``test_verdict_requirement_returns_fixed_band_values``).
_LOW_RISK_WAVE = "P29-I11-W02"
_LOW_RISK_TITLE = "rename a local variable in the renderer"


def _now() -> datetime:
    return _T0


def _criteria(*texts: str) -> list[dict[str, Any]]:
    """Return grandfathered (legacy-kind) criteria JSON shapes.

    A grandfathered criterion renders ``source="legacy"`` / ``status="pass"``
    in the always-on close-readiness projection, so it never blocks
    readiness -- isolating the FS13 verdict gate as the sole enforce-path
    blocker under test.
    """
    return [
        grandfather_criterion(text, index=idx).model_dump(mode="json")
        for idx, text in enumerate(texts, start=1)
    ]


def _state_payload(
    *,
    wave_id: str,
    title: str,
    criteria: list[dict[str, Any]],
    effort_bucket: str = "S",
) -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave under P29-I11."""
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
                "iter_ids": ["P29-I11"],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I11": {
                "id": "P29-I11",
                "phase_id": "P29",
                "title": "I11",
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
                "iter_id": "P29-I11",
                "title": title,
                "status": "claimed",
                "claim_session_id": "session-abc",
                "success_criteria": criteria,
                "effort_bucket": effort_bucket,
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
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
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _init_git_repo(root: Path) -> None:
    """Init a git repo with one empty commit so a read-only floor check passes."""
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


def _write_enforcing_profile(root: Path) -> None:
    """Enable a whole-fleet enforcing profile whose floor check passes.

    ``enforce: true`` with NO ``uiux_bands`` is the whole-fleet enforce shape
    the FS13 close path narrows to the high-risk band: a mechanical wave is
    left advisory, a high-risk wave is gated. ``cross_vendor_jury`` is left
    ``false`` (the default) so the high-risk gate is the single fresh-context
    auditor, not the three-vendor jury. The floor check is ``git status`` (a
    read-only verb that exits 0 inside the git repo :func:`_init_git_repo`
    set up) so the readiness projection clears and the verdict gate is the
    sole blocker.
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
        started_at="2026-06-06T00:00:00+00:00",
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


def _close_mutation(wave_id: str) -> Mutation:
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=wave_id,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": wave_id, "outcome": "ok"},
    )


def _auditor_body_json(*, verdict: str, wave_id: str) -> str:
    return orjson.dumps(
        {
            "role": "auditor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": wave_id,
            "criteria": [{"criterion": "ship the gate", "passed": verdict != "fail"}],
            "refutations": [],
        }
    ).decode("utf-8")


class _RecordingSpawn:
    """A spawn stub returning a canned auditor body; counts its calls."""

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


def _patch_spawn_factory(monkeypatch: pytest.MonkeyPatch, spawn: _RecordingSpawn) -> None:
    """Make the daemon close path bind *spawn* instead of a real adapter.

    The high-risk producer obtains its single auditor spawn via
    ``_jury_spawn_factory(...)(runtime)``; replacing the factory builder with
    one that returns *spawn* for any runtime lets the REAL
    ``produce_wave_verdict`` run end-to-end with no subprocess.
    """

    def _factory_builder(*_a: Any, **_k: Any) -> Callable[[str], _RecordingSpawn]:
        return lambda _runtime: spawn

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _factory_builder,
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# CR-2 determinism: verdict_requirement returns the fixed band VALUE.
# --------------------------------------------------------------------------- #


def test_verdict_requirement_returns_fixed_band_values() -> None:
    """The risk band is the asserted VALUE for each fixed wave, not a sample.

    The high-risk wave's ``"sandbox"`` security keyword forces ``"always"``;
    the low-risk mechanical wave's id is one the deterministic sampler skips,
    forcing ``"skip"``. Both are exact-value assertions so neither relies on
    a 1-in-N sampling outcome.
    """
    high_risk = State.model_validate(
        _state_payload(
            wave_id=_HIGH_RISK_WAVE,
            title=_HIGH_RISK_TITLE,
            criteria=_criteria("enforce the deny-list before the close gate writes state"),
        )
    ).waves[_HIGH_RISK_WAVE]
    low_risk = State.model_validate(
        _state_payload(
            wave_id=_LOW_RISK_WAVE,
            title=_LOW_RISK_TITLE,
            criteria=_criteria("rename the renderer variable and update its call sites"),
        )
    ).waves[_LOW_RISK_WAVE]

    assert verdict_requirement(high_risk) == "always"
    assert verdict_requirement(low_risk) == "skip"


# --------------------------------------------------------------------------- #
# CR-1 error-path: a high-risk wave whose verdict never lands is refused.
# --------------------------------------------------------------------------- #


def test_high_risk_close_blocked_without_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-risk wave whose produce writes no verdict cannot close.

    With ``produce_wave_verdict`` stubbed to a no-op, no auditor verdict
    reaches disk, so the read-side gate has nothing close-ready to read and
    the close is refused before any write. The block surfaces as a
    ``DaemonValidationError`` whose message names the verdict gate, and the
    wave stays CLAIMED. This isolates the "missing verdict" rejection from
    the "FAIL verdict" rejection covered below.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            wave_id=_HIGH_RISK_WAVE,
            title=_HIGH_RISK_TITLE,
            criteria=_criteria("enforce the deny-list before the close gate writes state"),
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)
    produced = {"calls": 0}

    async def _noop_produce(*_a: Any, **_k: Any) -> None:
        produced["calls"] += 1

    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.produce_wave_verdict",
        _noop_produce,
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as excinfo:
            await mutate(
                ctx, {"mutation": _close_mutation(_HIGH_RISK_WAVE).model_dump(mode="json")}
            )
        message = str(excinfo.value)
        assert "verdict gate blocked" in message
        # The producer was reached (high-risk subset) but wrote nothing.
        assert produced["calls"] == 1
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)


# --------------------------------------------------------------------------- #
# CR-1 error-path: a high-risk wave whose produced verdict is FAIL is refused.
# --------------------------------------------------------------------------- #


def test_high_risk_close_blocked_on_fail_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-risk wave whose fresh auditor verdict is FAIL cannot close.

    The close path produces a real (recording-stub) verdict, but a FAIL is
    not close-ready, so the read-side gate refuses close with a structured
    reason naming the auditor verdict. The wave stays CLAIMED and the auditor
    spawned exactly once.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            wave_id=_HIGH_RISK_WAVE,
            title=_HIGH_RISK_TITLE,
            criteria=_criteria("enforce the deny-list before the close gate writes state"),
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)
    spawn = _RecordingSpawn(_auditor_body_json(verdict="fail", wave_id=_HIGH_RISK_WAVE))
    _patch_spawn_factory(monkeypatch, spawn)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as excinfo:
            await mutate(
                ctx, {"mutation": _close_mutation(_HIGH_RISK_WAVE).model_dump(mode="json")}
            )
        message = str(excinfo.value)
        assert "verdict gate blocked" in message
        assert "fail" in message.lower()
        assert spawn.calls == 1
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "claimed"

    _run(body)


# --------------------------------------------------------------------------- #
# CR-1 positive: a high-risk wave with a produced PASS verdict closes.
# --------------------------------------------------------------------------- #


def test_high_risk_close_proceeds_with_pass_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A high-risk wave whose close path produces a PASS auditor verdict closes.

    The close path spawns a single fresh-context auditor (recording stub),
    writes its PASS verdict, then the read-side gate clears -- so the wave
    lands CLOSED. The spawn ran exactly once (single-auditor, not a jury).
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            wave_id=_HIGH_RISK_WAVE,
            title=_HIGH_RISK_TITLE,
            criteria=_criteria("enforce the deny-list before the close gate writes state"),
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)
    spawn = _RecordingSpawn(_auditor_body_json(verdict="pass", wave_id=_HIGH_RISK_WAVE))
    _patch_spawn_factory(monkeypatch, spawn)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation(_HIGH_RISK_WAVE).model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_HIGH_RISK_WAVE]["status"] == "closed"
        # Single-auditor gate: exactly one auditor spawned, never a jury.
        assert spawn.calls == 1

    _run(body)


# --------------------------------------------------------------------------- #
# Positive-control: a low-risk wave closes under enforce=True without spawning.
# --------------------------------------------------------------------------- #


def test_low_risk_close_proceeds_without_verdict_or_spawn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A low-risk mechanical wave closes under enforce=True with no spawn.

    The safety invariant the enforce-flip rests on: turning the verifier ON
    must not change a non-high-risk close. A ``"skip"`` mechanical wave
    closes with NO verdict required and NO auditor spawned. ``produce_wave_verdict``
    is monkeypatched to fail the test if the low-risk close path ever reaches
    it.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            wave_id=_LOW_RISK_WAVE,
            title=_LOW_RISK_TITLE,
            criteria=_criteria("rename the renderer variable and update its call sites"),
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)

    async def _must_not_produce(*_a: Any, **_k: Any) -> None:
        pytest.fail("produce_wave_verdict must not run for a low-risk (skip) close")

    monkeypatch.setattr(
        "eawf.workflow.dispatch.verdict.produce_wave_verdict",
        _must_not_produce,
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation(_LOW_RISK_WAVE).model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_LOW_RISK_WAVE]["status"] == "closed"

    _run(body)
