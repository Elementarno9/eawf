"""Tests: the durable close auditor is spawned through the forced JSON schema.

The daemon's high-risk verdict producer resolves its single auditor spawn from
``_jury_spawn_factory``. On a DURABLE close that spawn now carries
``--json-schema`` with the exact schema
:func:`~eawf.workflow.dispatch.verdict.durable_auditor_json_schema` renders, so
the claude CLI constrains the answer instead of the prompt asking for it. Every
other lane is untouched: a non-durable produce and any non-claude juror runtime
spawn with a byte-unchanged argv.

The spawn never reaches a subprocess -- ``select_adapter`` is replaced with a
recording adapter that replays a canned ``stream-json`` transcript, which also
pins that the forced result text still binds through ``assist_with_schema``.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import orjson
import pytest

from eawf.kernel.state.enums import AgentSessionRole, StoreKind
from eawf.kernel.state.models import State, Wave
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon.methods import state as daemon_state
from eawf.runtime.daemon.methods.state_jury import durable_auditor_extra_args, jury_spawn_factory
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.workflow.agent_report.rollup import iter_agent_reports
from eawf.workflow.dispatch.verdict import (
    DurableAuditContext,
    DurableAuditCriterion,
    durable_auditor_json_schema,
)

pytestmark = pytest.mark.integration

_T0 = datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC)
_WAVE_ID = "P41-I01-W07"
_RECEIPT_URN = f"urn:eawf:v1:store:{_WAVE_ID}/gate_receipt/GR-01"
_TEXTS = ("ship the forced auditor schema", "prove the forced auditor schema")


class _RecordingAdapter:
    """Adapter double that records its spawn kwargs and replays a transcript."""

    def __init__(self, transcript: str) -> None:
        self.transcript = transcript
        self.calls: list[dict[str, Any]] = []

    async def spawn_session(self, prompt: str, **kwargs: Any) -> SpawnResult:
        self.calls.append(kwargs)
        return SpawnResult(
            session_id="sess-auditor",
            runtime="claude-code",
            model=str(kwargs.get("model", "opus")),
            subprocess_pid=4242,
            exit_status=0,
            text=self.transcript,
            started_at=_T0,
            ended_at=_T0,
        )


def _durable_context(*texts: str) -> DurableAuditContext:
    """Return a frozen close context with one deterministic and one judged row."""
    first, second = texts or _TEXTS
    return DurableAuditContext(
        wave_id=_WAVE_ID,
        close_attempt_id="CA-01",
        integration_id="WI-01",
        integrated_sha="a" * 40,
        tree_sha="b" * 40,
        spec_digest="c" * 64,
        criteria_digest="d" * 64,
        gate_manifest_digest="e" * 64,
        policy_digest="f" * 64,
        runner_digest="1" * 64,
        dependency_binding_digest="2" * 64,
        criteria=(
            DurableAuditCriterion(
                criterion_id="CR-01",
                text=first,
                deterministic=True,
                gate_receipt_urns=(_RECEIPT_URN,),
            ),
            DurableAuditCriterion(criterion_id="CR-02", text=second, deterministic=False),
        ),
    )


def _forced_body() -> dict[str, Any]:
    """Return the auditor body the forced schema admits for that context."""
    return {
        "role": "auditor",
        "verdict": "pass",
        "confidence": "high",
        "summary": "re-read the frozen close inputs against both criteria",
        "target_id": _WAVE_ID,
        "criteria": [
            {
                "criterion": _TEXTS[0],
                "passed": True,
                "evidence_refs": [{"kind": "store_record", "ref": _RECEIPT_URN}],
            },
            {
                "criterion": _TEXTS[1],
                "passed": True,
                "evidence_refs": [
                    {"kind": "artifact", "ref": "tests/integration/runtime/daemon/x.py"}
                ],
            },
        ],
        "refutations": [],
    }


def _stream_json_transcript(body: dict[str, Any]) -> str:
    """Return a claude stream-json transcript whose result text is *body*."""
    lines = [
        orjson.dumps({"type": "system", "subtype": "init"}).decode("utf-8"),
        orjson.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": orjson.dumps(body).decode("utf-8"),
            }
        ).decode("utf-8"),
    ]
    return "\n".join(lines)


def _criterion(criterion_id: str, text: str) -> dict[str, Any]:
    """Return one grandfathered success-criterion row carrying *text*."""
    return {
        "id": criterion_id,
        "text": text,
        "kind": "legacy",
        "acceptance_style": "binary",
        "evidence_kind": "attested",
        "quality_dimension": "functional_suitability",
        "measurable_signal": "grandfathered legacy criterion",
    }


def _state_payload() -> dict[str, Any]:
    """Return a minimal valid State carrying the one CLAIMED wave under audit."""
    stamp = _T0.isoformat()
    return {
        "schema_version": "1.0",
        "scope_kind": "repo",
        "urn": "urn:eawf:v1:state:ABC",
        "updated_at": stamp,
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
            "P41": {
                "id": "P41",
                "scope_id": "ABC",
                "track_id": None,
                "title": "P41",
                "status": "active",
                "iter_ids": ["P41-I01"],
                "outcome_ids": [],
                "opened_at": stamp,
                "closed_at": None,
                "audit_id": None,
            }
        },
        "iters": {
            "P41-I01": {
                "id": "P41-I01",
                "phase_id": "P41",
                "title": "I01",
                "status": "active",
                "wave_ids": [_WAVE_ID],
                "estimate_id": None,
                "audit_id": None,
                "opened_at": stamp,
                "closed_at": None,
            }
        },
        "waves": {
            _WAVE_ID: {
                "id": _WAVE_ID,
                "iter_id": "P41-I01",
                "title": "force the durable auditor report",
                "status": "claimed",
                "claim_session_id": "session-abc",
                "success_criteria": [
                    _criterion("CR-01", _TEXTS[0]),
                    _criterion("CR-02", _TEXTS[1]),
                ],
                "effort_bucket": "L",
                "agent_role": "executor",
                "opened_at": stamp,
                "sessions": {},
            }
        },
        "artifacts": {},
        "agent_sessions": {},
        "plugins": {},
        "indexes": {},
    }


def _live_tree(tmp_path: Path) -> tuple[State, Path]:
    """Init a git repo with the state file the producer reads and writes."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t.t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t.t",
    }
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "--allow-empty", "-m", "init"], cwd=tmp_path, check=True, env=env
    )
    state_path = tmp_path / ".ea" / "state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    store_path(state_path, StoreKind.EVENT).parent.mkdir(parents=True, exist_ok=True)
    state = State.model_validate(_state_payload())
    state_path.write_text(state.model_dump_json(), encoding="utf-8")
    return state, state_path


def _produce(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    durable_context: DurableAuditContext | None,
) -> tuple[_RecordingAdapter, Path]:
    """Run the daemon's high-risk producer against a recording adapter."""
    state, state_path = _live_tree(tmp_path)
    adapter = _RecordingAdapter(_stream_json_transcript(_forced_body()))
    monkeypatch.setattr("eawf.runtime.runtimes.selector.select_adapter", lambda runtime: adapter)
    asyncio.run(
        daemon_state._produce_high_risk_verdict(
            state,
            state.waves[_WAVE_ID],
            state_path=state_path,
            repo_root=tmp_path,
            wall_clock_seconds=42.0,
            reuse_existing=False,
            durable_context=durable_context,
        )
    )
    return adapter, state_path


# --------------------------------------------------------------------------- #
# The durable produce forces the schema; the answer still binds.
# --------------------------------------------------------------------------- #


def test_durable_produce_spawns_the_claude_auditor_with_the_forced_schema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The claude auditor argv carries ``--json-schema`` with the rendered schema."""
    context = _durable_context()
    adapter, _state_path = _produce(tmp_path, monkeypatch, durable_context=context)

    assert len(adapter.calls) == 1
    extra_args = tuple(adapter.calls[0]["extra_args"])
    assert extra_args[0] == "--json-schema"
    assert orjson.loads(extra_args[1]) == durable_auditor_json_schema(context)


def test_durable_produce_binds_the_stream_json_result_text(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The forced object arrives inside the stream-json result and binds first try.

    The schema flag does not change how the answer comes back, so the durable
    verdict must still land in the auditor report store from the transcript's
    ``result`` text via ``assist_with_schema``.
    """
    _adapter, state_path = _produce(tmp_path, monkeypatch, durable_context=_durable_context())

    rows = iter_agent_reports(state_path, role=AgentSessionRole.AUDITOR, base_id=_WAVE_ID)

    assert len(rows) == 1
    body = rows[-1].payload.body
    assert body.verdict.value == "pass"
    assert [row.criterion for row in body.criteria] == list(_TEXTS)


def test_non_durable_produce_keeps_the_auditor_argv_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without a durable context the single-auditor spawn adds no flag."""
    adapter, _state_path = _produce(tmp_path, monkeypatch, durable_context=None)

    assert len(adapter.calls) == 1
    assert tuple(adapter.calls[0]["extra_args"]) == ()


# --------------------------------------------------------------------------- #
# The flag stays on the claude lane only.
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("runtime", ["codex", "opencode"])
def test_jury_spawn_factory_leaves_other_runtimes_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, runtime: str
) -> None:
    """A juror that is not the claude lane spawns with an empty argv tail."""
    state, _state_path = _live_tree(tmp_path)
    adapter = _RecordingAdapter(_stream_json_transcript(_forced_body()))
    monkeypatch.setattr("eawf.runtime.runtimes.selector.select_adapter", lambda _runtime: adapter)
    wave: Wave = state.waves[_WAVE_ID]
    factory = jury_spawn_factory(
        state,
        wave,
        repo_root=tmp_path,
        extra_args_by_runtime=durable_auditor_extra_args(_durable_context()),
    )

    asyncio.run(factory(runtime)("prompt"))

    assert tuple(adapter.calls[0]["extra_args"]) == ()


def test_jury_spawn_factory_without_extra_args_spawns_unchanged(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default factory (no map at all) still spawns with an empty argv tail."""
    state, _state_path = _live_tree(tmp_path)
    adapter = _RecordingAdapter(_stream_json_transcript(_forced_body()))
    monkeypatch.setattr("eawf.runtime.runtimes.selector.select_adapter", lambda _runtime: adapter)

    factory = jury_spawn_factory(state, state.waves[_WAVE_ID], repo_root=tmp_path)
    asyncio.run(factory("claude-code")("prompt"))

    assert tuple(adapter.calls[0]["extra_args"]) == ()


# --------------------------------------------------------------------------- #
# durable_auditor_extra_args: boundaries and degrade paths.
# --------------------------------------------------------------------------- #


def test_durable_auditor_extra_args_without_a_context_is_empty() -> None:
    """A non-durable spawn maps no runtime, so no argv changes."""
    assert durable_auditor_extra_args(None) == {}


def test_durable_auditor_extra_args_maps_only_the_claude_lane() -> None:
    """Only ``claude-code`` is mapped; no other vendor CLI takes the flag."""
    mapped = durable_auditor_extra_args(_durable_context())

    assert list(mapped) == ["claude-code"]
    assert mapped["claude-code"][0] == "--json-schema"


def test_durable_auditor_extra_args_declines_unpinnable_criteria() -> None:
    """Criteria no schema can pin exactly degrade to an unforced spawn."""
    assert durable_auditor_extra_args(_durable_context(_TEXTS[0], _TEXTS[0])) == {}
