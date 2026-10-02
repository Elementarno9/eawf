"""Each Run records the runtime it ran on, and its controls are gated on that runtime.

The producers: ``eawf run start`` inside a Claude Code session presents the host's
harness and provider and the daemon's start edge reads the version and model off the
session transcript; a subagent the host spawned is adopted on its own session and read
the same way; a Campaign round records the runtime and session its agent spawned. The
gate (UI-014): ``runtime.run.control.request`` admits a control only on a runtime the
repository's committed canary export certifies, refuses an uncertified or expired one
with a typed code naming both, and keeps today's behaviour, with a note, for a Run that
records no runtime.

Every call runs the real Typer commands or daemon verbs against a provisioned canary.
"""

from __future__ import annotations

import asyncio
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.state.enums import EffortBucket
from eawf.kernel.state.epoch2.measurement import VendorSessionRef
from eawf.kernel.state.epoch2.run import Run, RunRuntimeTuple
from eawf.kernel.state.types import UtcDatetime
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.campaign_scheduler import StepAssignment, StepReport
from eawf.runtime.daemon.methods import DaemonValidationError, campaign_run
from eawf.runtime.daemon.methods.campaign_run import CAMPAIGN_START_METHOD, HostResearchAgent
from eawf.runtime.daemon.methods.host_subagent import HOST_SUBAGENT_START_METHOD
from eawf.runtime.daemon.methods.run import RUN_CONTROL_REQUEST_METHOD
from eawf.runtime.runtimes.adapter import SpawnResult
from eawf.runtime.session.host_session import claude_code_provider, with_host_session
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from eawf.workflow.evidence.provider_certification import canary_evidence_path
from tests.conftest import REPO_ROOT
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    method_context,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    _runs,
    _settled,
    _start,
    canary,
    served,
)
from tests.integration.runtime.daemon.test_host_subagent_adoption import (
    AGENT_ID,
    repository_row,
    run_record,
)
from tests.integration.runtime.daemon.test_root_run_host_session import (
    MODEL,
    SESSION,
    _head,
    _hosted,
    _queued,
    _stored_run,
    _transcript,
    cli,
)
from tests.integration.runtime.daemon.test_root_run_host_session import (
    _start as _cli_start,
)

pytestmark = pytest.mark.integration

#: Re-exported so pytest collects the fixtures the borrowed helpers lean on; ``cli``
#: is autouse and keeps every test's home, temp dir and transcripts under tmp.
__all__ = ["canary", "cli"]

VERSION: Final = "2.1.274"
RC1: Final = "REL-0.7.0rc1"
RUN_KEY: Final = "RUN-00000010"
RUN_URN: Final = f"eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/{RUN_KEY}"


def _versioned(rows: list[dict[str, Any]], version: str = VERSION) -> list[dict[str, Any]]:
    """Stamp each transcript row with the Claude Code version that wrote it."""
    return [{**row, "version": version} for row in rows]


# ---------- producer: eawf run start inside a host session ----------


def test_run_start_records_the_host_runtime_its_version_and_its_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    _transcript(cli, _versioned(_head()))
    canary_run = _queued(tmp_path)

    _cli_start(canary_run, tmp_path)

    run = _stored_run(canary_run)
    assert run.runtime_tuple == RunRuntimeTuple(
        harness="claude-code", harness_version=VERSION, provider="anthropic", model=MODEL
    )


def test_run_start_keeps_an_operator_stated_tuple_and_fills_only_its_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cli: Path
) -> None:
    _hosted(monkeypatch)
    _transcript(cli, _versioned(_head()))
    canary_run = _queued(tmp_path)

    _cli_start(
        canary_run, tmp_path, runtime_tuple={"harness": "claude-code", "provider": "google-vertex"}
    )

    assert _stored_run(canary_run).runtime_tuple == RunRuntimeTuple(
        harness="claude-code", harness_version=VERSION, provider="google-vertex", model=MODEL
    )


def test_run_start_outside_a_host_session_records_no_runtime(tmp_path: Path) -> None:
    canary_run = _queued(tmp_path)

    _cli_start(canary_run, tmp_path)

    assert _stored_run(canary_run).runtime_tuple is None


def test_run_start_with_no_transcript_yet_leaves_version_and_model_unrecorded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hosted(monkeypatch)
    canary_run = _queued(tmp_path)

    _cli_start(canary_run, tmp_path)

    runtime = _stored_run(canary_run).runtime_tuple
    assert runtime is not None
    assert (runtime.harness, runtime.harness_version, runtime.model) == ("claude-code", None, None)


@pytest.mark.parametrize(
    ("environ", "provider"),
    [
        ({}, "anthropic"),
        ({"CLAUDE_CODE_USE_BEDROCK": "1"}, "amazon-bedrock"),
        ({"CLAUDE_CODE_USE_VERTEX": "true"}, "google-vertex"),
        ({"CLAUDE_CODE_USE_BEDROCK": "0"}, "anthropic"),
        ({"ANTHROPIC_BASE_URL": "http://gateway.invalid"}, None),
    ],
)
def test_the_host_environment_names_who_serves_its_model(
    environ: dict[str, str], provider: str | None
) -> None:
    assert claude_code_provider(environ) == provider
    hosted = with_host_session({}, {"CLAUDE_CODE_SESSION_ID": SESSION, **environ})
    assert hosted["runtime_tuple"] == (
        {"harness": "claude-code", "provider": provider} if provider else {"harness": "claude-code"}
    )


# ---------- producer: subagent adoption ----------


def test_an_adopted_subagent_records_its_harness_version_and_model(
    tmp_path: Path, cli: Path
) -> None:
    subagents = cli / "-repo" / SESSION / "subagents"
    subagents.mkdir(parents=True)
    (subagents / f"agent-{AGENT_ID}.jsonl").write_text(
        "\n".join(json.dumps(row) for row in _versioned(_head(), "2.1.275")) + "\n",
        encoding="utf-8",
    )
    adopted = provision(tmp_path / "repo", code="HOST")
    seed(adopted, {"repository": {"REP-EAWF": repository_row()}})
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()

    answer = asyncio.run(
        methods.dispatch(
            HOST_SUBAGENT_START_METHOD,
            ctx,
            {"repo_root": str(adopted.root), "harness": "claude-code", "agent_id": AGENT_ID},
        )
    )

    run = run_record(adopted, answer["run_ref"].rsplit("/", 1)[1])
    assert run.runtime_tuple == RunRuntimeTuple(
        harness="claude-code", harness_version="2.1.275", model=MODEL
    )


# ---------- producer: a Campaign round ----------


class _RanOnAgent:
    """Works every round and reports the runtime and session it ran on."""

    async def work(self, assignment: StepAssignment) -> StepReport:
        return StepReport(
            report=f"# {assignment.title}\n",
            outcome="held",
            tokens=10,
            runtime=RunRuntimeTuple(harness="codex", model="gpt-5-codex"),
            session=VendorSessionRef(harness="codex", session_digest=f"round-{assignment.ordinal}"),
        )


def test_a_campaign_round_records_the_runtime_and_session_it_ran_on(
    canary: CanaryProvision, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: _RanOnAgent())

    async def body() -> Any:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow", agents=1)
            )
            return await _settled(client, canary.root, started["record"]["key"])

    view = asyncio.run(body())

    runs = _runs(canary)
    for step in view.steps:
        run = Run.model_validate(runs[step.step.run_refs[0].entity_key])
        assert run.runtime_tuple == RunRuntimeTuple(harness="codex", model="gpt-5-codex")
        assert run.vendor_session == VendorSessionRef(
            harness="codex", session_digest=f"round-{step.step.ordinal}"
        )
        assert step.runner_runtime == run.runtime_tuple
        assert step.runner_session == run.vendor_session


class _Adapter:
    """A runtime adapter answering one spawn with a fixed result."""

    def __init__(self, result: SpawnResult) -> None:
        self.result = result

    async def spawn_session(self, _prompt: str, *, model: str, cwd: str) -> SpawnResult:
        assert model and cwd
        return self.result


def _spawned(runtime: str, *, resolved: str | None) -> SpawnResult:
    at: UtcDatetime = datetime(2026, 10, 2, tzinfo=UTC)
    return SpawnResult(
        session_id="round-session",
        runtime=runtime,
        model="sonnet",
        resolved_model=resolved,
        subprocess_pid=4242,
        exit_status=0,
        text='```json\n{"report": "# R", "outcome": "held"}\n```',
        started_at=at,
        ended_at=at,
    )


@pytest.mark.parametrize(
    ("runtime", "resolved", "expected"),
    [
        (
            "claude-code",
            "claude-sonnet-4-5",
            RunRuntimeTuple(harness="claude-code", provider="anthropic", model="claude-sonnet-4-5"),
        ),
        (
            "claude-code",
            None,
            RunRuntimeTuple(harness="claude-code", provider="anthropic", model="sonnet"),
        ),
        ("codex", "bad model id", RunRuntimeTuple(harness="codex")),
    ],
)
def test_the_host_research_agent_reports_what_its_spawn_ran_on(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    runtime: str,
    resolved: str | None,
    expected: RunRuntimeTuple,
) -> None:
    for name in ("ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX"):
        monkeypatch.delenv(name, raising=False)
    adapter = _Adapter(_spawned(runtime, resolved=resolved))
    monkeypatch.setattr(campaign_run, "select_adapter", lambda _runtime: adapter)
    agent = HostResearchAgent(runtime, tmp_path, EffortBucket.S)
    assignment = StepAssignment.model_validate(
        {
            "campaign_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001",
            "campaign_title": "t",
            "ordinal": 1,
            "title": "s",
            "method": "survey",
            "question": "q",
            "run_ref": RUN_URN,
            "round_number": 1,
        }
    )

    report = asyncio.run(agent.work(assignment))

    assert report.runtime == expected
    assert report.session == VendorSessionRef(
        harness=runtime, session_digest=hash_vendor_session_id("round-session")
    )


# ---------- the gate on runtime.run.control.request ----------


def _export(repo: Path, *, expires: str) -> None:
    """Commit the rc1 canary export into *repo*, its expiry moved to *expires*."""
    source = canary_evidence_path(REPO_ROOT, RC1)
    target = canary_evidence_path(repo, RC1)
    target.parent.mkdir(parents=True)
    shutil.copy(source, target)
    export = json.loads(target.read_text(encoding="utf-8"))
    certification = export["certifications"][0]["certification"]
    certification["expires_at"] = expires
    for capability in certification["capabilities"]:
        capability["expires_at"] = expires
    target.write_text(json.dumps(export), encoding="utf-8")


def _controlled(tmp_path: Path, runtime: dict[str, Any] | None) -> CanaryProvision:
    controlled = provision(tmp_path / "ctl", code="CTLG")
    row = seed_row("run", "RUNNING")
    if runtime is not None:
        row["runtime_tuple"] = runtime
    seed(controlled, {"run": {RUN_KEY: row}})
    return controlled


def _ask(canary_run: CanaryProvision, tmp_path: Path, control: str = "cancel") -> dict[str, Any]:
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(
        methods.dispatch(
            RUN_CONTROL_REQUEST_METHOD,
            ctx,
            {
                "repo_root": str(canary_run.root),
                "urn": RUN_URN,
                "control_request_ref": "CTL-000000000001",
                "control": control,
                "actor": "OP-0001",
            },
        )
    )


CERTIFIED: Final = {"harness": "claude-code", "harness_version": VERSION}


def test_a_control_on_a_certified_runtime_is_recorded_with_no_note(tmp_path: Path) -> None:
    controlled = _controlled(tmp_path, CERTIFIED)
    _export(controlled.root, expires="2999-01-01T00:00:00Z")

    answer = _ask(controlled, tmp_path)

    assert answer["disposition"] == "requesting"
    assert answer["warnings"] == []


def test_a_control_on_an_expired_certification_is_refused_naming_both(
    tmp_path: Path,
) -> None:
    controlled = _controlled(tmp_path, CERTIFIED)
    _export(controlled.root, expires="2026-09-19T00:00:00Z")

    with pytest.raises(DaemonValidationError) as refused:
        _ask(controlled, tmp_path)

    message = str(refused.value)
    assert message.startswith("validation_failed: runtime_certification_expired: ")
    assert "claude-code 2.1.274" in message and "certification://claude-code/2026-09-18" in message


def test_a_control_on_an_uncertified_runtime_is_refused(tmp_path: Path) -> None:
    controlled = _controlled(tmp_path, {"harness": "claude-code", "harness_version": "9.9.9"})
    _export(controlled.root, expires="2999-01-01T00:00:00Z")

    with pytest.raises(DaemonValidationError, match=r"runtime_uncertified: claude-code 9\.9\.9"):
        _ask(controlled, tmp_path)


def test_a_control_needing_an_unsupported_capability_is_refused(tmp_path: Path) -> None:
    controlled = _controlled(tmp_path, CERTIFIED)
    _export(controlled.root, expires="2999-01-01T00:00:00Z")

    with pytest.raises(DaemonValidationError, match="runtime_capability_uncertified"):
        _ask(controlled, tmp_path, control="resume")


def test_a_run_recording_no_runtime_keeps_its_control_with_a_note(tmp_path: Path) -> None:
    controlled = _controlled(tmp_path, None)

    answer = _ask(controlled, tmp_path)

    assert answer["disposition"] == "requesting"
    assert answer["warnings"] == [
        "this Run records no runtime, so the control was not gated on a certification"
    ]
