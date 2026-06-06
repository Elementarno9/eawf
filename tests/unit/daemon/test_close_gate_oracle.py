"""Tests: the daemon close gate runs the ordered oracle (FS06).

Exercises the FS06 rewrite of :func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate`
and the companion model-validate-boundary check
:func:`eawf.kernel.spec.common.validate_criterion_gate_refs`:

* boundary -- a wave close with NO enforcing profile is byte-unchanged:
  the gate returns early at the ``verify.enforce`` guard before the
  per-criterion ``run_oracle`` loop, so the wave closes exactly as it
  does today (the safety invariant the sibling enforce-flip wave relies
  on);
* boundary -- with an enforcing profile and ``run_oracle`` stubbed to
  PASS every required criterion, the close proceeds and the wave lands
  CLOSED (CR-3 positive);
* error-path CR-1 -- a wave whose criterion ``gate_ids`` names a
  non-existent gate is rejected at the close-mutation boundary BEFORE
  any apply (the gate loader returns ``[]`` today, so any non-empty
  ``gate_ids`` is an orphan);
* error-path CR-2 -- a wave whose criterion carries an author-set
  ``oracle_tier`` is rejected at the same boundary;
* error-path CR-3 -- with an enforcing profile and ``run_oracle``
  stubbed to FAIL a required criterion, the close is blocked with a
  structured ``LifecycleError`` (surfaced as ``DaemonValidationError``)
  whose detail names the criterion id + tier + status, and the wave
  stays CLAIMED.

``run_oracle`` is monkeypatched on the daemon ``state`` module so the
pass / fail branches are driven deterministically with NO live jury, NO
auditor spawn, and NO subprocess.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf import __version__
from eawf.kernel.spec.common import CriterionSpec, OracleTier, grandfather_criterion
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.workflow.verify.oracle import OracleResult

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 6, 12, 0, 0, tzinfo=UTC)

_WAVE = "P29-I11-W03"


def _now() -> datetime:
    return _T0


def _criterion(
    *,
    cid: str = "CR-01",
    gate_ids: list[str] | None = None,
    oracle_tier: OracleTier | None = None,
    required: bool = True,
) -> dict[str, Any]:
    """Return the JSON shape of a typed criterion for the state payload."""
    spec = CriterionSpec(
        id=cid,
        text="ship the run_oracle close gate and prove it blocks correctly",
        kind="contract",
        acceptance_style="binary",
        evidence_kind="attested",
        gate_ids=gate_ids or [],
        required=required,
        quality_dimension="functional_suitability",  # type: ignore[arg-type]
        measurable_signal="a measurable signal of at least twenty characters long",
        oracle_tier=oracle_tier,
    )
    # model_dump exposes the same dict the daemon validates on read; the
    # author-set-tier / orphan-ref checks reject AT that boundary, so we
    # build the on-disk dict directly rather than through the model so a
    # forbidden tier value can still be planted on the wire.
    payload = spec.model_dump(mode="json")
    if oracle_tier is not None:
        payload["oracle_tier"] = int(oracle_tier)
    return payload


def _legacy_criterion(*, cid_index: int, required: bool = True) -> dict[str, Any]:
    """Return a grandfathered (legacy-kind) criterion JSON shape.

    A grandfathered criterion renders ``source="legacy"`` /
    ``status="pass"`` in the always-on close-readiness projection, so it
    never blocks readiness -- isolating the FS06 oracle loop as the sole
    enforce-path gate under test. The id is overridable so a wave can
    carry more than one legacy criterion.
    """
    spec = grandfather_criterion(
        "ship the run_oracle close gate and prove it blocks correctly",
        index=cid_index,
    ).model_copy(update={"required": required})
    return spec.model_dump(mode="json")


def _state_payload(*, criteria: list[dict[str, Any]]) -> dict[str, Any]:
    """A minimal valid State with one CLAIMED wave carrying *criteria*."""
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
                "wave_ids": [_WAVE],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": _now().isoformat(),
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE: {
                "id": _WAVE,
                "iter_id": "P29-I11",
                "title": "wire run_oracle into the close gate",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "success_criteria": criteria,
                "effort_bucket": "L",
                "agent_role": "executor",
                "opened_at": _now().isoformat(),
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
    """Enable an enforcing profile whose floor check passes.

    With a passing floor check the readiness projection clears, so the
    only gate that can refuse close is the FS06 ordered-oracle loop --
    isolating it. The floor check is ``git status`` (a read-only verb
    that exits 0 inside the git repo :func:`_init_git_repo` set up).

    ``cross_vendor_jury: true`` is set so the high-risk (``"always"``,
    e.g. the L-effort wave these tests use) close routes through the
    ``run_oracle`` loop -- the gate these FS06 tests stub. Without it the
    risk-weighted enforce-flip (FS13) routes a high-risk jury-off wave
    through the dedicated single-auditor producer instead, which is
    covered by ``test_verdict_enforced.py``.
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
                "  cross_vendor_jury: true",
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


def _close_mutation() -> Mutation:
    return Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _WAVE, "outcome": "ok"},
    )


def _run(body: Callable[[], Coroutine[Any, Any, None]]) -> None:
    asyncio.run(body())


# --------------------------------------------------------------------------- #
# Boundary: an advisory (enforce=False) close is byte-unchanged.
# --------------------------------------------------------------------------- #


def test_close_enforce_false_is_byte_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With NO enforcing profile the close skips the oracle loop and lands CLOSED.

    No active profile contributes a verify block, so
    :func:`_enforce_wave_close_gate` returns at the early ``verify.enforce``
    guard -- the per-criterion ``run_oracle`` loop never runs. The wave
    closes exactly as it does today (the safety invariant the sibling
    enforce-flip wave relies on). The forbidding stub proves the advisory
    path never reaches ``run_oracle``: if the loop ran, the close would
    raise instead of landing CLOSED.
    """
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_state(state_path, _state_payload(criteria=[_criterion()]))
    ctx = _build_ctx(tmp_path, state_path)

    async def _forbidden(*_a: Any, **_k: Any) -> OracleResult:
        raise AssertionError("run_oracle must not run on the advisory (enforce=False) path")

    monkeypatch.setattr("eawf.workflow.verify.oracle.run_oracle", _forbidden)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"

    _run(body)


# --------------------------------------------------------------------------- #
# Boundary: enforce=True + run_oracle PASS -> close proceeds (CR-3 positive).
# --------------------------------------------------------------------------- #


def test_close_enforce_oracle_pass_proceeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enforcing close whose every required criterion PASSes the oracle closes."""
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(criteria=[_legacy_criterion(cid_index=1)]))
    ctx = _build_ctx(tmp_path, state_path)

    async def _pass(criterion: CriterionSpec, *_a: Any, **_k: Any) -> OracleResult:
        return OracleResult(tier=OracleTier.T1_STATIC, status="pass", criterion_id=criterion.id)

    monkeypatch.setattr("eawf.workflow.verify.oracle.run_oracle", _pass)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"

    _run(body)


def test_close_enforce_skips_non_required_criterion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-required criterion is skipped: only required criteria gate close."""
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(
            criteria=[
                _legacy_criterion(cid_index=1, required=True),
                _legacy_criterion(cid_index=2, required=False),
            ]
        ),
    )
    ctx = _build_ctx(tmp_path, state_path)

    seen: list[str] = []

    async def _track(criterion: CriterionSpec, *_a: Any, **_k: Any) -> OracleResult:
        seen.append(criterion.id)
        return OracleResult(tier=OracleTier.T1_STATIC, status="pass", criterion_id=criterion.id)

    monkeypatch.setattr("eawf.workflow.verify.oracle.run_oracle", _track)

    async def body() -> None:
        await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "closed"
        # Only the required criterion was scored.
        assert seen == ["CR-01"]

    _run(body)


# --------------------------------------------------------------------------- #
# Error-path CR-1: an orphan gate_id is rejected at the mutate boundary.
# --------------------------------------------------------------------------- #


def test_close_orphan_gate_id_rejected(tmp_path: Path) -> None:
    """A criterion gate_ids entry naming a non-existent gate refuses close.

    The gate loader returns ``[]`` today, so any non-empty ``gate_ids`` is
    an orphan; the validate boundary rejects it BEFORE any apply,
    regardless of enforce. No enforcing profile is needed -- the check is
    structural.
    """
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_state(state_path, _state_payload(criteria=[_criterion(gate_ids=["GATE-NOPE"])]))
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="references unknown gate id"):
            await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        # The wave stayed CLAIMED -- the close never persisted.
        assert payload["waves"][_WAVE]["status"] == "claimed"

    _run(body)


# --------------------------------------------------------------------------- #
# Error-path CR-2: an author-set oracle_tier is rejected at the mutate boundary.
# --------------------------------------------------------------------------- #


def test_close_author_set_oracle_tier_rejected(tmp_path: Path) -> None:
    """A criterion carrying an author-set oracle_tier refuses close.

    The tier is computed server-side, never authored on input, so a
    non-None value on a close mutation is a malformed spec the validate
    boundary rejects before any apply.
    """
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    _write_state(
        state_path,
        _state_payload(criteria=[_criterion(oracle_tier=OracleTier.T7_JURY)]),
    )
    ctx = _build_ctx(tmp_path, state_path)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="oracle_tier must not be author-set"):
            await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "claimed"

    _run(body)


# --------------------------------------------------------------------------- #
# Error-path CR-3: a failing required criterion blocks close with structured detail.
# --------------------------------------------------------------------------- #


def test_close_oracle_fail_blocks_with_structured_detail(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An enforcing close whose oracle FAILs a required criterion is refused.

    The block surfaces as a ``DaemonValidationError`` (the daemon's
    -32002 mapping for wave-close lifecycle rejections) whose message
    carries the structured detail: the criterion id, the producing tier,
    and the closed status. The wave stays CLAIMED.
    """
    _write_enforcing_profile(tmp_path)
    _init_git_repo(tmp_path)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(criteria=[_criterion(cid="CR-01")]))
    ctx = _build_ctx(tmp_path, state_path)

    async def _fail(criterion: CriterionSpec, *_a: Any, **_k: Any) -> OracleResult:
        return OracleResult(
            tier=OracleTier.T7_JURY,
            status="fail",
            criterion_id=criterion.id,
            detail="jury vetoed the close",
        )

    monkeypatch.setattr("eawf.workflow.verify.oracle.run_oracle", _fail)

    async def body() -> None:
        with pytest.raises(DaemonValidationError) as excinfo:
            await mutate(ctx, {"mutation": _close_mutation().model_dump(mode="json")})
        message = str(excinfo.value)
        assert "oracle blocked close" in message
        assert "criterion='CR-01'" in message
        assert "tier=7" in message
        assert "status=fail" in message
        assert "jury vetoed the close" in message
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_WAVE]["status"] == "claimed"

    _run(body)
