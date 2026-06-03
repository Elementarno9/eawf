"""Tests: the daemon close path routes band waves through the spec jury (P29-I08-W05).

Exercises the spec-jury flavour of the enforcing wave-close verdict gate wired
into :func:`eawf.runtime.daemon.methods.state._enforce_wave_close_gate`: when an
enabled profile sets ``verify.enforce: true`` AND lists a ``verify.uiux_bands``
token that matches the closing wave, the close routes through the per-rubric-item
spec-jury producer. The producer loads the wave's WaveSpec rubric, collects
per-item ballots (via an injected ballot fn), reduces them, and writes ONE
per-item AUDITOR report at ``base_id=wave_id``; the gate then consults the
verdict.

The ballot fn is ALWAYS stubbed: the test monkeypatches ``_spec_jury_ballot_fn``
to return a canned per-item ballot fn, so NO real model, spawn, or auth runs. The
real producer + reducer + report writer run over the canned ballots, so the
gate -> producer -> report-write -> lifecycle-mapping wiring is exercised end to
end.

Back-compat: a non-band wave close (or a band wave with no injected ballot fn)
behaves exactly as it does today -- the spec-jury path is dormant unless the band
membership AND the ballot fn are both present.
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
from eawf.kernel.state.enums import AgentSessionRole, StoreKind
from eawf.kernel.state.models import State
from eawf.kernel.state.mutations import Mutation, MutationKind
from eawf.kernel.store.paths import store_path
from eawf.observability.eval.cross_vendor_jury import PerItemJurorBallot, RubricItemVote
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.state import mutate
from eawf.workflow.agent_report.rollup import iter_agent_reports

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 6, 3, 12, 0, 0, tzinfo=UTC)
_BAND_WAVE = "P29-I08-W05"
_BAND_TOKEN = "tui"
_CRITERION = "route band waves through the spec-jury producer"


def _now() -> datetime:
    return _T0


# --------------------------------------------------------------------------- #
# Canned per-item ballot fn (no model, no spawn).
# --------------------------------------------------------------------------- #


def _patch_ballot_fn(
    monkeypatch: pytest.MonkeyPatch,
    *,
    votes_by_juror: dict[str, dict[str, tuple[bool, str | None]]] | None,
) -> dict[str, int]:
    """Patch ``_spec_jury_ballot_fn`` to return a canned per-item ballot fn.

    When *votes_by_juror* is ``None`` the patched builder returns ``None`` so
    the producer stays idle (modelling the v0.5 production default). Otherwise it
    returns a fn replaying the canned per-item votes. Returns a one-key call
    counter so a test can assert whether the ballot fn was convened.
    """
    counter = {"calls": 0}

    def _builder(state: Any, wave: Any, *, repo_root: Path) -> Any:
        if votes_by_juror is None:
            return None

        async def _fn(prompt: str) -> tuple[PerItemJurorBallot, ...]:
            counter["calls"] += 1
            ballots: list[PerItemJurorBallot] = []
            for juror, item_votes in votes_by_juror.items():
                votes = tuple(
                    RubricItemVote(item_id=item_id, passed=passed, refutation=refutation)
                    for item_id, (passed, refutation) in item_votes.items()
                )
                ballots.append(PerItemJurorBallot(juror=juror, votes=votes))
            return tuple(ballots)

        return _fn

    monkeypatch.setattr(
        "eawf.runtime.daemon.methods.state._spec_jury_ballot_fn",
        _builder,
    )
    return counter


# --------------------------------------------------------------------------- #
# State + enforcing-profile + WaveSpec fixtures.
# --------------------------------------------------------------------------- #


def _state_payload(*, wave_id: str, title: str) -> dict[str, Any]:
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
                "success_criteria": [_CRITERION],
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


def _write_enforcing_profile(root: Path, *, uiux_bands: list[str]) -> None:
    """Enable an enforcing profile whose floor check passes + lists band tokens.

    The floor check is a passing ``git status`` so readiness enforcement clears
    and the verdict gate (spec jury / single-auditor) is the SOLE blocker. The
    ``uiux_bands`` list toggles whether the closing wave routes through the spec
    jury.
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
) -> tuple[Path, MethodContext, Mutation]:
    _write_enforcing_profile(tmp_path, uiux_bands=uiux_bands)
    _init_git_repo(tmp_path)
    if write_spec:
        _write_wave_spec(tmp_path, wave_id=_BAND_WAVE)
    state_path = tmp_path / ".ea" / "state.json"
    _write_state(state_path, _state_payload(wave_id=_BAND_WAVE, title=wave_title))
    ctx = _build_ctx(tmp_path, state_path)
    mutation = Mutation(
        kind=MutationKind.WAVE_CLOSE,
        scope_id=_BAND_WAVE,
        mutation_id=uuid.uuid4().hex,
        params={"wave_id": _BAND_WAVE, "outcome": "ok"},
    )
    return state_path, ctx, mutation


# --------------------------------------------------------------------------- #
# (a) band wave + canned PASS ballots -> spec-jury report written, close ok.
# --------------------------------------------------------------------------- #


def test_band_wave_unanimous_pass_writes_report_and_closes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded close with unanimous per-item PASS writes >=1 AUDITOR report and closes."""
    counter = _patch_ballot_fn(
        monkeypatch,
        votes_by_juror={
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (True, None), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        },
    )
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "closed"

    _run(body)
    assert counter["calls"] == 1
    # A per-item AUDITOR report landed at base_id=wave_id with per-item verdicts.
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE)
    assert len(rows) == 1
    body_obj = rows[-1].payload.body
    assert body_obj.target_id == _BAND_WAVE
    assert {c.criterion for c in body_obj.criteria} == {"B1", "B2"}
    assert all(c.passed for c in body_obj.criteria)


# --------------------------------------------------------------------------- #
# (b) band wave + a refuted item -> FAIL -> blocks close.
# --------------------------------------------------------------------------- #


def test_band_wave_veto_blocks_close(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A single refuted rubric item vetoes -> FAIL verdict -> close blocked."""
    _patch_ballot_fn(
        monkeypatch,
        votes_by_juror={
            "claude-code": {"B1": (True, None), "B2": (True, None)},
            "codex": {"B1": (False, "B1 is not wired into the gate"), "B2": (True, None)},
            "opencode": {"B1": (True, None), "B2": (True, None)},
        },
    )
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="spec-jury blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "claimed"

    _run(body)
    # The failing verdict was still written before the close was rejected.
    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE)
    assert len(rows) == 1
    by_item = {c.criterion: c.passed for c in rows[-1].payload.body.criteria}
    assert by_item == {"B1": False, "B2": True}


# --------------------------------------------------------------------------- #
# (c) non-band wave close -> no spec-jury report, behaviour identical to today.
# --------------------------------------------------------------------------- #


def test_non_band_wave_does_not_convene_spec_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A wave whose title matches no band token never routes through the spec jury.

    With no band match the close falls to the single-auditor gate, which blocks
    an always-wave (effort L) with no fresh auditor verdict -- the pre-W05
    behaviour -- and the spec-jury ballot fn is never convened.
    """
    counter = _patch_ballot_fn(
        monkeypatch,
        votes_by_juror={"claude-code": {"B1": (True, None), "B2": (True, None)}},
    )
    # The wave title matches no band token (band list lists 'tui', title is plain).
    state_path, ctx, mutation = _setup(
        tmp_path, uiux_bands=[_BAND_TOKEN], wave_title="backend telemetry rollup"
    )

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "claimed"

    _run(body)
    assert counter["calls"] == 0
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE) == []


# --------------------------------------------------------------------------- #
# (d) band wave + idle producer (no ballot fn) -> degrades to default gate.
# --------------------------------------------------------------------------- #


def test_band_wave_idle_producer_degrades_to_default_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded wave with NO injected ballot fn is idle -> falls to default gate.

    The default single-auditor gate blocks an always-wave with no fresh auditor
    verdict (no crash, no spec-jury report). This is the v0.5 production default
    until the band-population wave binds the live ballot fn.
    """
    _patch_ballot_fn(monkeypatch, votes_by_juror=None)
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN])

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "claimed"

    _run(body)
    # The idle producer wrote no report.
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE) == []


# --------------------------------------------------------------------------- #
# (e) band wave + canned ballots but NO WaveSpec on disk -> safe-skip degrade.
# --------------------------------------------------------------------------- #


def test_band_wave_missing_spec_degrades_to_default_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A banded wave with a ballot fn but no WaveSpec on disk safe-skips.

    With nothing to score the producer skips (no crash, no report) and the close
    degrades to the default single-auditor gate (which blocks the missing verdict).
    """
    counter = _patch_ballot_fn(
        monkeypatch,
        votes_by_juror={"claude-code": {"B1": (True, None), "B2": (True, None)}},
    )
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[_BAND_TOKEN], write_spec=False)

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})
        payload = orjson.loads(state_path.read_bytes())
        assert payload["waves"][_BAND_WAVE]["status"] == "claimed"

    _run(body)
    # The producer short-circuited before convening (no spec to score).
    assert counter["calls"] == 0
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE) == []


# --------------------------------------------------------------------------- #
# (f) no band tokens configured -> spec jury never engaged (today's behaviour).
# --------------------------------------------------------------------------- #


def test_empty_band_list_never_engages_spec_jury(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty uiux_bands list bands no wave -> default gate, no spec jury."""
    counter = _patch_ballot_fn(
        monkeypatch,
        votes_by_juror={"claude-code": {"B1": (True, None), "B2": (True, None)}},
    )
    state_path, ctx, mutation = _setup(tmp_path, uiux_bands=[])

    async def body() -> None:
        with pytest.raises(DaemonValidationError, match="verdict gate blocked"):
            await mutate(ctx, {"mutation": mutation.model_dump(mode="json")})

    _run(body)
    assert counter["calls"] == 0
    assert iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_BAND_WAVE) == []
