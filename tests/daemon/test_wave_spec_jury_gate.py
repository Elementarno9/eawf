"""Tests: the daemon close path routes UI/UX-band waves through the oracle.

Exercises the enforcing wave-close gate for a UI/UX-banded wave after the W03
oracle unification. When an enabled profile sets ``verify.enforce: true`` AND
lists a ``verify.uiux_bands`` token that matches the closing wave, the wave-aware
resolver (:func:`eawf.workflow.verify.readiness.resolve_wave_verify_block`) keeps
``enforce`` + ``cross_vendor_jury`` ON for the band wave, so the close routes
through :func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate`'s
per-criterion :func:`eawf.workflow.verify.oracle.run_oracle` loop. For an
``always`` (high-risk) band wave each un-gated criterion escalates to the jury
tier, which convenes the three disjoint-family cross-vendor jurors and reduces
their ballots: a unanimous PASS closes; a minority-veto FAIL is held ADVISORY
(W10) -- logged, never blocking -- until I07 TRUST-4 calibrates the jury; a
sub-quorum NEEDS_USER still blocks close with the unified ``oracle blocked
close`` reason.

The W03 unification REPLACED the separate uiux-band spec-jury branch (the
pre-W03 ``_enforce_spec_jury_gate`` / ``_spec_jury_ballot_fn`` close path) with
the same ``run_oracle`` path the cross-vendor jury rides; the close gate no
longer reads a WaveSpec rubric or writes one per-item AUDITOR report. These
tests therefore drive the jury through the per-runtime spawn factory rather than
an injected per-item ballot fn, mirroring
``tests/daemon/test_wave_cross_vendor_jury_gate.py``.

The juror spawn is ALWAYS stubbed: the test monkeypatches ``_jury_spawn_factory``
to return per-runtime recording stubs that replay canned auditor bodies, so NO
real ``claude`` / ``codex`` / ``opencode`` subprocess, network, or auth runs.

Back-compat: a non-band wave under a band-scoped profile resolves to
``enforce=False`` and closes advisory-only -- the gate never fires.
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
from eawf.kernel.state.enums import AgentSessionRole, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import JURY_RUNTIME_FAMILIES
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.dispatch.llm_assist import SpawnFn

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _blocking_jury_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grant BLOCKING jury authority so the jury tier stays reachable.

    P30-I23-W07 closed the OR-fold bypass: a verdict-always wave reaches
    the jury tier only when the jury has EARNED blocking authority; under
    ADVISORY the blocking single-auditor fires instead (covered by
    test_close_gate_auditor_jury_routing.py). These band-jury routing
    suites exercise the jury tier itself, so the resolver is stubbed.
    """
    from eawf.observability.eval.jury_validation import BlockAuthority

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._resolve_jury_block_authority",
        lambda state, *, state_path, verify_block: BlockAuthority.BLOCKING,
    )


_T0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
_BAND_WAVE = "P29-I08-W05"
_BAND_TOKEN = "tui"
_CRITERION = "route band waves through the unified oracle close gate"


def _now() -> datetime:
    return _T0


# --------------------------------------------------------------------------- #
# Per-runtime recording stubs (mirrors test_wave_cross_vendor_jury_gate).
# --------------------------------------------------------------------------- #


def _auditor_body_json(*, verdict: str) -> str:
    return json.dumps(
        {
            "role": "auditor",
            "verdict": verdict,
            "confidence": "high",
            "summary": "re-read the diff against the criteria",
            "target_id": _BAND_WAVE,
            "criteria": [
                {"criterion": _CRITERION, "passed": verdict in {"pass", "pass-with-followups"}}
            ],
            "refutations": [],
        }
    )


class _RecordingSpawn:
    """Replays one canned auditor body per call for a single runtime."""

    def __init__(self, runtime: str, answer: str) -> None:
        self.runtime = runtime
        self._answer = answer
        self.calls = 0

    async def __call__(self, prompt: str) -> SpawnResult:
        self.calls += 1
        return SpawnResult(
            session_id=f"sess-{self.runtime}-{self.calls}",
            runtime=self.runtime,
            model="model-x",
            subprocess_pid=4242,
            exit_status=0,
            text=self._answer,
            started_at=_T0,
            ended_at=_T0,
        )


def _patch_jury(
    monkeypatch: pytest.MonkeyPatch,
    *,
    verdicts: dict[str, str] | None = None,
) -> dict[str, _RecordingSpawn]:
    """Patch the per-runtime jury spawn factory; return the per-runtime stubs.

    Each juror runtime gets a recording stub replaying its canned verdict
    (defaulting to ``pass``). Returns the stub map so a test can assert which
    lanes were spawned. The lane pre-check is patched to a no-op constant so a
    host without the vendor CLIs does not perturb the (now pre-check-free)
    routing.
    """
    verdicts = verdicts or {}
    stubs: dict[str, _RecordingSpawn] = {
        runtime: _RecordingSpawn(runtime, _auditor_body_json(verdict=verdicts.get(runtime, "pass")))
        for runtime in JURY_RUNTIME_FAMILIES
    }

    def _fake_factory(
        state: Any,
        wave: Any,
        *,
        repo_root: Path,
        timeout_seconds: float = 600.0,
        events_path: Any = None,
    ) -> Callable[[str], SpawnFn]:
        def _factory(runtime: str) -> SpawnFn:
            return stubs[runtime]  # type: ignore[return-value]

        return _factory

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._cross_vendor_lanes_ready",
        lambda *, quorum: True,
    )
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._jury_spawn_factory",
        _fake_factory,
    )
    return stubs


# --------------------------------------------------------------------------- #
# State + enforcing-profile + WaveSpec fixtures.
# --------------------------------------------------------------------------- #


def _state_payload(*, wave_id: str, title: str, effort_bucket: str = "L") -> dict[str, Any]:
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
                "iter_ids": ["P29-I08"],
                "outcome_ids": [],
                "opened_at": _now().isoformat(),
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P29-I08": {
                "id": "P29-I08",
                "phase_id": "P29",
                "title": "I08",
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
                "iter_id": "P29-I08",
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


def _write_enforcing_profile(
    root: Path, *, uiux_bands: list[str], cross_vendor_jury: bool = False
) -> None:
    """Enable an enforcing profile whose floor check passes + lists band tokens.

    The floor check is a passing ``git status`` so readiness enforcement clears
    and the unified oracle gate is the SOLE blocker. The ``uiux_bands`` list
    band-scopes enforcement (the wave-aware resolver keeps ``enforce`` ON only
    for a matching wave); ``cross_vendor_jury`` selects the jury tier of the
    oracle for an ``always`` band wave (vs the single-auditor branch when off).
    """
    profile_dir = root / ".ea" / "profiles"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (root / ".ea" / "config.yaml").write_text(
        "profiles:\n  enabled:\n    - enforcing\n", encoding="utf-8"
    )
    # Omit the uiux_bands key entirely when empty: a bare ``uiux_bands:`` with
    # no children parses as YAML null, which fails the list-typed field.
    band_block: list[str] = []
    if uiux_bands:
        band_block = ["  uiux_bands:", *[f"    - {token}" for token in uiux_bands]]
    lines = [
        "name: enforcing",
        "verify:",
        "  enforce: true",
        f"  cross_vendor_jury: {'true' if cross_vendor_jury else 'false'}",
        *band_block,
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


def _write_wave_spec(root: Path, *, wave_id: str) -> None:
    """Write a WaveSpec markdown file with two jury-scorable behaviours.

    Lands at ``.ea/specs/P29/P29-I08/<wave>.md`` so the daemon's
    ``_load_wave_spec`` (via ``spec_file_path``) resolves it.
    """
    spec_dir = root / ".ea" / "specs" / "P29" / "P29-I08"
    spec_dir.mkdir(parents=True, exist_ok=True)
    frontmatter = "\n".join(
        [
            "---",
            "schema_version: '1.0'",
            "kind: WaveSpec",
            f"id: {wave_id}",
            "iter_id: P29-I08",
            "phase_id: P29",
            "title: route band waves through the spec-jury producer",
            "agent_role: executor",
            "effort_bucket: L",
            "file_scopes:",
            "  - src/eawf/workflow/dispatch/spec_jury.py",
            "implements:",
            "  - verdict_id: D17",
            "    brief: .ea/local/research/2026-06-03-p29-drift-audit.md",
            "behaviors:",
            "  - id: B1",
            "    text: the close gate routes a banded wave through the spec jury",
            "    jury_scorable: true",
            "    quality_dimension: operability",
            "  - id: B2",
            "    text: the producer is idle until a per-item ballot fn is injected",
            "    jury_scorable: true",
            "    quality_dimension: interaction_capability",
            "failure_modes:",
            "  - idle producer silently passes a banded close",
            "---",
            "",
            "# Spec body",
            "",
            "Route band waves through the spec-jury producer at daemon close.",
            "",
        ]
    )
    spec_dir.joinpath(f"{wave_id}.md").write_text(frontmatter, encoding="utf-8")


def _write_state(state_path: Path, payload: dict[str, Any]) -> State:
    state = State.model_validate(payload)
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state


def _build_ctx(tmp_path: Path, state_path: Path) -> MethodContext:
    event_path = store_path(state_path, StoreKind.EVENT)
    wal_dir = tmp_path / "wal"
    wal_dir.mkdir(parents=True, exist_ok=True)
    return MethodContext(
        started_at="2026-06-03T00:00:00+00:00",
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
    tmp_path: Path,
    *,
    uiux_bands: list[str],
    wave_title: str = "native tui modes chassis",
    write_spec: bool = True,
    cross_vendor_jury: bool = True,
    effort_bucket: str = "L",
) -> tuple[Path, MethodContext, Mutation]:
    _write_enforcing_profile(tmp_path, uiux_bands=uiux_bands, cross_vendor_jury=cross_vendor_jury)
    _init_git_repo(tmp_path)
    if write_spec:
        _write_wave_spec(tmp_path, wave_id=_BAND_WAVE)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(
        state_path,
        _state_payload(wave_id=_BAND_WAVE, title=wave_title, effort_bucket=effort_bucket),
    )
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_BAND_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _BAND_WAVE, "outcome": "ok"},
    )
    return state_path, ctx, mutation


# --------------------------------------------------------------------------- #
# (a) band wave + unanimous PASS jury -> per-juror reports written, close ok.
# --------------------------------------------------------------------------- #


def test_band_wave_unanimous_pass_writes_reports_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded close with a unanimous PASS jury writes per-juror reports and closes.

    Re-pinned to the unified oracle: a banded ``always`` wave routes
    through run_oracle's jury tier (not the removed spec-jury producer). The
    three disjoint-family jurors each spawn once and vote PASS; the reduction
    is PASS so the wave closes, and each juror's fresh-auditor verdict lands as
    its OWN AUDITOR report at ``base_id=wave_id`` (one report per juror, not a
    single per-item report).
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    # Every disjoint vendor lane was spawned exactly once.
    for runtime in JURY_RUNTIME_FAMILIES:
        assert stubs[runtime].calls == 1
    # One fresh-auditor report per juror landed at base_id=wave_id, all PASS.
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE)
    assert len(rows) == len(JURY_RUNTIME_FAMILIES)
    for row in rows:
        body_obj = row.payload.body
        assert body_obj.target_id == _BAND_WAVE
        assert all(c.passed for c in body_obj.criteria)


# --------------------------------------------------------------------------- #
# (b) band wave + a refuted item -> FAIL -> held ADVISORY, close proceeds.
# --------------------------------------------------------------------------- #


def test_band_wave_advisory_authority_routes_to_auditor_not_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """ADVISORY authority: the single-auditor fires and NO jury convenes.

    Post P30-I23-W07 an ADVISORY jury never sees a verdict-always wave at
    the mutate path — the blocking single-auditor fires instead and the
    juror lanes stay cold. (The advisory-veto-logs-and-close-proceeds
    contract still holds at the run_oracle surface, covered by
    tests/unit/verify/test_run_oracle.py, which other callers such as the
    fleet clean-close path drive directly.) The verdict producer + gate
    are stubbed so no real auditor spawns.
    """
    from eawf.observability.eval.jury_validation import BlockAuthority

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._resolve_jury_block_authority",
        lambda state, *, state_path, verify_block: BlockAuthority.ADVISORY,
    )

    async def _produce(
        state: Any,
        wave: Any,
        *,
        state_path: Path,
        repo_root: Path,
        wall_clock_seconds: float,
        reuse_existing: bool = True,
    ) -> None:
        return None

    monkeypatch.setattr("eawf.runtime.daemon.methods.state._produce_high_risk_verdict", _produce)
    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._enforce_wave_verdict_gate",
        lambda wave, *, state_path: None,
    )
    stubs = _patch_jury(
        monkeypatch,
        verdicts={"claude-code": "pass", "codex": "fail", "opencode": "pass"},
    )
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        with caplog.at_level("WARNING", logger="eawf.workflow.verify.oracle"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    # No juror lane spawned and no juror report written: ADVISORY routes to
    # the (stubbed) single-auditor, not the jury.
    assert all(stub.calls == 0 for stub in stubs.values())
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE)
    assert rows == []


# --------------------------------------------------------------------------- #
# (c) non-band wave close -> no jury, behaviour identical to advisory-only.
# --------------------------------------------------------------------------- #


def test_non_band_wave_does_not_convene_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-band wave under a band-scoped enforcing profile closes advisory-only.

    Enforcement is band-conditional: the band-scoped profile
    declares ``enforce: true`` at the fleet level, but
    :func:`~eawf.workflow.verify.readiness.resolve_wave_verify_block` narrows
    it to OFF for a non-band wave (no UI file_scopes, no matching token). So
    the close runs no oracle gate at all -- it proceeds exactly as a close in a
    repo with no enforcing profile, and no juror lane is spawned.
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    # The wave title matches no band token (band list lists 'tui', title is plain)
    # and the fixture wave carries no UI file_scopes, so the structural arm
    # does not band it either.
    # A genuinely mechanical wave: the S bucket keeps verdict_requirement
    # below "always", so the W12 verdict-always preservation arm does not
    # fire and the band narrowing stays in effect for this non-band wave.
    state_path, ctx, mutation = _setup(
        tmp_path,
        uiux_bands=[_BAND_TOKEN],
        wave_title="backend telemetry rollup",
        effort_bucket="S",
    )

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        # Band-conditional enforce-off -> the close lands (no gate fired).
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    for runtime in JURY_RUNTIME_FAMILIES:
        assert stubs[runtime].calls == 0
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE) == []


# --------------------------------------------------------------------------- #
# (d) band wave routes through the jury tier (3 lanes), not a single auditor.
# --------------------------------------------------------------------------- #


def test_band_wave_routes_through_jury_tier_not_single_auditor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded ``always`` wave convenes the full three-lane jury, not one auditor.

    Re-pinned from the removed spec-jury idle-producer path (W03 unification):
    the close gate no longer has an injectable per-item ballot fn that can idle
    and degrade to a single-auditor gate. Because the band-scoped profile keeps
    ``cross_vendor_jury`` ON for the band wave, the close routes through
    run_oracle's jury tier, which convenes ALL THREE disjoint-family jurors --
    each lane is spawned exactly once -- and on a unanimous PASS the wave
    closes. This discriminates the jury tier from the single-auditor path
    (which would spawn only the ``claude-code`` lane).
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    # All three disjoint vendor lanes spawned -- the jury convened, not one auditor.
    for runtime in JURY_RUNTIME_FAMILIES:
        assert stubs[runtime].calls == 1


# --------------------------------------------------------------------------- #
# (e) band wave with NO WaveSpec on disk still routes through the jury.
# --------------------------------------------------------------------------- #


def test_band_wave_missing_spec_still_routes_through_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded wave with no WaveSpec on disk still convenes the jury and closes.

    Re-pinned from the removed spec-jury safe-skip path (W03 unification): the
    unified close gate scores the wave's ``success_criteria`` + diff through
    run_oracle's jury tier and no longer reads a WaveSpec rubric, so a missing
    spec file does NOT change routing. The jury convenes over the three lanes,
    votes a unanimous PASS, and the wave closes.
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "pass"))
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN], write_spec=False)

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    # The jury still convened despite the absent spec file.
    for runtime in JURY_RUNTIME_FAMILIES:
        assert stubs[runtime].calls == 1


# --------------------------------------------------------------------------- #
# (f) no band tokens configured -> whole-fleet single-auditor gate, no jury.
# --------------------------------------------------------------------------- #


def test_empty_band_list_routes_to_single_auditor_not_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty uiux_bands profile is whole-fleet enforce -> single-auditor, no jury.

    A profile that declares no ``uiux_bands`` is a whole-fleet enforce profile
    (not band-scoped) and does not opt into ``cross_vendor_jury``, so the
    unified close gate takes the single-auditor branch for the
    ``always`` wave: it spawns one ``claude-code`` auditor (stubbed to FAIL),
    the verdict gate reads a non-close-ready verdict, and the close is refused
    with ``verdict gate blocked``. The other two jury lanes are never spawned.
    """
    stubs = _patch_jury(monkeypatch, verdicts=dict.fromkeys(JURY_RUNTIME_FAMILIES, "fail"))
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[], cross_vendor_jury=False)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "claimed"

    _run(body)
    # Single-auditor branch: only the claude-code lane spawned, no jury.
    assert stubs["claude-code"].calls == 1
    for runtime in ("codex", "opencode"):
        assert stubs[runtime].calls == 0
