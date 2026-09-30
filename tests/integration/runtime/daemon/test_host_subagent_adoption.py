"""A subagent the host harness spawned is adopted as a Run, and its transcript is bridged.

SURF-054 and SURF-104: the subagent start and stop hooks call the adoption verbs, which
admit the subagent as a repository-scoped Run -- typed, and not a Task's -- start it on
the subagent's own vendor session, bridge its transcript into the Run's stream when it
stops, and complete it.

SURF-075: the adopted Run's transcript is reachable through the transcript route's read
model, which renders the Run's own words as blocks and a subagent it spawned as work
elsewhere.

SURF-097: the adopted Run lands in the same Run register as a daemon-dispatched Run, so
the Activity grouping counts harness-side and daemon-side fan-out in one view.
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.projection.activity import ActivityExceptionBucket, group_runs
from eawf.kernel.projection.compute import RouteProjection
from eawf.kernel.projection.registers import build_register_view
from eawf.kernel.projection.transcript import build_transcript_blocks
from eawf.kernel.runtime.events import RunEventKind
from eawf.kernel.state.epoch2.run import Run, RunStatus
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.ledger import read_ledger_records
from eawf.kernel.store.paths import ledger_path
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.bus import EventBus
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.host_subagent import (
    HOST_SUBAGENT_START_METHOD,
    HOST_SUBAGENT_STOP_METHOD,
)
from eawf.runtime.daemon.run_events import run_events_of
from eawf.runtime.runtimes.host_transcript import SUMMARY_LIMIT, WITHHELD_TEXT
from eawf.runtime.session.vendor_id import hash_vendor_session_id
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    document_path,
    method_context,
    provision,
    seed,
    seed_row,
)

pytestmark = pytest.mark.integration

REPOSITORY_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/repository/REP-EAWF"
DAEMON_RUN_KEY: Final = "RUN-00000010"
AGENT_ID: Final = "a0a45f67519cb14dd"
#: A message carrying a home-path shape, which the bridge must withhold.
LEAKY_TEXT: Final = "read /Users/someone/.ssh/config first"  # pragma: allowlist secret
HOST_SESSION: Final = "30f683b4-388a-4c04-89f4-612b7fe60362"


def repository_row() -> dict[str, Any]:
    """Return the one Repository row a single-repository tree admits."""
    return {
        "key": "REP-EAWF",
        "urn": REPOSITORY_URN,
        "revision": 1,
        "head_sha": "a" * 40,
        "created_at": AT.isoformat(),
        "updated_at": AT.isoformat(),
    }


@pytest.fixture(autouse=True)
def hermetic_runtime(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtime dirs and Claude transcript discovery under this test's tmp dir."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("EAWF_CLAUDE_PROJECTS_DIR", str(tmp_path / "projects"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary admitting one repository and holding one daemon-side Run."""
    provisioned = provision(tmp_path / "repo", code="HOST")
    seed(
        provisioned,
        {
            Epoch2Collection.REPOSITORY.value: {"REP-EAWF": repository_row()},
            Epoch2Collection.RUN.value: {DAEMON_RUN_KEY: seed_row("run", "RUNNING")},
        },
    )
    return provisioned


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb against the canary, as the hook's daemon client would."""
    ctx = method_context(tmp_path / "runtime")
    ctx.bus = EventBus()
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def start(canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    return call(
        canary,
        tmp_path,
        HOST_SUBAGENT_START_METHOD,
        harness="claude-code",
        **{"agent_id": AGENT_ID, **params},
    )


def stop(canary: CanaryProvision, tmp_path: Path, **params: Any) -> dict[str, Any]:
    return call(
        canary,
        tmp_path,
        HOST_SUBAGENT_STOP_METHOD,
        harness="claude-code",
        **{"agent_id": AGENT_ID, **params},
    )


def runs(canary: CanaryProvision) -> dict[str, dict[str, Any]]:
    return document_rows(read_document(document_path(canary)), Epoch2Collection.RUN)


def run_record(canary: CanaryProvision, key: str) -> Run:
    """Return one Run, from the document or, once compacted, from the run ledger."""
    row = runs(canary).get(key)
    if row is not None:
        return Run.model_validate(row)
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    latest = [r.payload for r in records if r.record_key == key and "payload_kind" not in r.payload]
    return Run.model_validate(latest[-1])


def events(canary: CanaryProvision, run: Run) -> tuple[Any, ...]:
    records = read_ledger_records(ledger_path(document_path(canary), Epoch2Collection.RUN))
    return run_events_of(records, run.urn)


def claude_transcript(path: Path, lines: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(line) + "\n" for line in lines), encoding="utf-8")
    return path


def _assistant(*blocks: dict[str, Any]) -> dict[str, Any]:
    return {"type": "assistant", "message": {"role": "assistant", "content": list(blocks)}}


SUBAGENT_LINES: Final[list[dict[str, Any]]] = [
    {"type": "user", "message": {"role": "user", "content": "Count the modules in the tree."}},
    _assistant({"type": "thinking", "thinking": "hidden reasoning is never bridged"}),
    _assistant({"type": "text", "text": "I will list the package first."}),
    _assistant({"type": "tool_use", "id": "toolu_01Bash", "name": "Bash", "input": {}}),
    {
        "type": "user",
        "message": {"role": "user", "content": [{"type": "tool_result", "content": "42"}]},
    },
    _assistant({"type": "tool_use", "id": "toolu_01Spawn", "name": "Agent", "input": {}}),
    "not an object",  # type: ignore[list-item]
    _assistant({"type": "text", "text": "There are forty-two modules."}),
]


# ---- SURF-054 / SURF-104: adoption --------------------------------------------------


def test_surf_054_subagent_start_adopts_the_subagent_as_a_running_run(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = start(canary, tmp_path)

    assert answer["run_ref"].endswith("/run/RUN-00000011")
    assert answer["status"] == RunStatus.RUNNING.value
    run = run_record(canary, "RUN-00000011")
    assert run.status is RunStatus.RUNNING
    assert run.started_at is not None
    assert run.vendor_session is not None
    assert run.vendor_session.harness == "claude-code"
    assert run.vendor_session.session_digest == hash_vendor_session_id(AGENT_ID)


def test_surf_104_adopted_run_has_a_typed_non_task_scope(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    start(canary, tmp_path)

    scope = run_record(canary, "RUN-00000011").scope
    assert scope.scope_kind == "repository"
    assert str(scope.repository_ref) == REPOSITORY_URN
    assert scope.purpose.value == "observe"
    assert scope.write_set == ()


def test_surf_054_redelivered_start_adopts_once(canary: CanaryProvision, tmp_path: Path) -> None:
    first = start(canary, tmp_path)
    again = start(canary, tmp_path)

    assert again["run_ref"] == first["run_ref"]
    assert sorted(runs(canary)) == [DAEMON_RUN_KEY, "RUN-00000011"]


def test_surf_054_two_subagents_become_two_runs(canary: CanaryProvision, tmp_path: Path) -> None:
    start(canary, tmp_path)
    start(canary, tmp_path, agent_id="b1b2")

    assert sorted(runs(canary)) == [DAEMON_RUN_KEY, "RUN-00000011", "RUN-00000012"]


def test_surf_054_stop_bridges_the_transcript_and_completes_the_run(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    start(canary, tmp_path)
    transcript = claude_transcript(tmp_path / "agent.jsonl", SUBAGENT_LINES)

    answer = stop(canary, tmp_path, transcript_path=str(transcript))

    assert answer["status"] == RunStatus.COMPLETED.value
    assert answer["bridged_events"] == 4
    run = run_record(canary, "RUN-00000011")
    assert run.status is RunStatus.COMPLETED
    assert run.ended_at is not None
    lines = events(canary, run)
    assert [line.event_kind for line in lines] == [
        RunEventKind.MESSAGE_SUMMARIZED,
        RunEventKind.MESSAGE_SUMMARIZED,
        RunEventKind.CHILD_RUN_REQUESTED,
        RunEventKind.MESSAGE_SUMMARIZED,
    ]
    assert [line.run_sequence for line in lines] == [1, 2, 3, 4]
    assert all(line.provenance == "provider_native" for line in lines)


def test_surf_054_redelivered_stop_writes_nothing_twice(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    transcript = claude_transcript(tmp_path / "agent.jsonl", SUBAGENT_LINES)
    stop(canary, tmp_path, transcript_path=str(transcript))

    again = stop(canary, tmp_path, transcript_path=str(transcript))

    assert again["bridged_events"] == 0
    assert "already stopped" in again["reason"]
    assert len(events(canary, run_record(canary, "RUN-00000011"))) == 4


def test_surf_054_stop_without_a_start_adopts_then_completes(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = stop(canary, tmp_path)

    assert answer["status"] == RunStatus.COMPLETED.value
    assert answer["bridged_events"] == 0
    run = run_record(canary, "RUN-00000011")
    assert run.started_at is not None
    assert run.ended_at is not None


def test_surf_054_codex_rollout_is_bridged(canary: CanaryProvision, tmp_path: Path) -> None:
    rollout = claude_transcript(
        tmp_path / "rollout.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "codex-agent"}},
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "instructions"}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Done."}],
                },
            },
        ],
    )

    answer = call(
        canary,
        tmp_path,
        HOST_SUBAGENT_STOP_METHOD,
        harness="codex",
        agent_id="codex-agent",
        transcript_path=str(rollout),
    )

    assert answer["bridged_events"] == 1
    run = run_record(canary, "RUN-00000011")
    assert run.vendor_session is not None
    assert run.vendor_session.harness == "codex"


def test_surf_054_long_and_leaky_messages_are_cut_or_withheld(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    long_text = "word " * 200
    transcript = claude_transcript(
        tmp_path / "agent.jsonl",
        [
            _assistant({"type": "text", "text": long_text}),
            _assistant({"type": "text", "text": LEAKY_TEXT}),
        ],
    )

    stop(canary, tmp_path, transcript_path=str(transcript))

    first, second = events(canary, run_record(canary, "RUN-00000011"))
    assert len(first.payload.summary) <= SUMMARY_LIMIT
    assert first.payload.truncated is True
    assert second.payload.summary == WITHHELD_TEXT
    assert second.payload.truncated is True


def test_surf_054_missing_transcript_file_completes_with_nothing_bridged(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = stop(canary, tmp_path, transcript_path=str(tmp_path / "absent.jsonl"))

    assert answer["status"] == RunStatus.COMPLETED.value
    assert answer["bridged_events"] == 0


# ---- lineage: the producer of child Runs --------------------------------------------


def test_surf_054_spawning_session_run_becomes_the_parent(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    parent = seed_row("run", "RUNNING")
    parent["vendor_session"] = {
        "harness": "claude-code",
        "session_digest": hash_vendor_session_id(HOST_SESSION),
    }
    seed(canary, {Epoch2Collection.RUN.value: {DAEMON_RUN_KEY: parent}})

    answer = start(canary, tmp_path, host_session_id=HOST_SESSION)

    assert answer["parent_run_ref"] == parent["urn"]
    run = run_record(canary, "RUN-00000011")
    assert run.parent_run_ref is not None
    assert run.parent_run_ref.entity_key == DAEMON_RUN_KEY


def _seed_parent(canary: CanaryProvision) -> dict[str, Any]:
    """Seed the daemon Run as the live Run whose vendor session spawns the subagent."""
    parent = seed_row("run", "RUNNING")
    parent["vendor_session"] = {
        "harness": "claude-code",
        "session_digest": hash_vendor_session_id(HOST_SESSION),
    }
    seed(canary, {Epoch2Collection.RUN.value: {DAEMON_RUN_KEY: parent}})
    return parent


def test_prx_065_start_states_child_run_started_on_the_parent_stream(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """PRX-065, RUN-062: a subagent start is a typed ``child_run_started`` on its parent."""
    _seed_parent(canary)
    answer = start(canary, tmp_path, host_session_id=HOST_SESSION)
    start(canary, tmp_path, host_session_id=HOST_SESSION)

    (line,) = events(canary, run_record(canary, DAEMON_RUN_KEY))
    assert line.event_kind is RunEventKind.CHILD_RUN_STARTED
    assert line.run_sequence == 1
    assert str(line.payload.child_run_ref) == answer["run_ref"]
    assert line.payload.delegation_request_ref.startswith("delegation://claude-code/")


def test_prx_065_stop_states_child_run_terminal_after_the_start_and_bridges_at_the_tail(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """PRX-065: the stop ends the delegation on the parent, and a redelivery adds nothing."""
    _seed_parent(canary)
    transcript = claude_transcript(tmp_path / "agent.jsonl", SUBAGENT_LINES)
    start(canary, tmp_path, host_session_id=HOST_SESSION)
    stop(canary, tmp_path, host_session_id=HOST_SESSION, transcript_path=str(transcript))
    stop(canary, tmp_path, host_session_id=HOST_SESSION, transcript_path=str(transcript))

    lines = events(canary, run_record(canary, DAEMON_RUN_KEY))
    assert [line.event_kind for line in lines] == [
        RunEventKind.CHILD_RUN_STARTED,
        RunEventKind.CHILD_RUN_TERMINAL,
    ]
    assert [line.run_sequence for line in lines] == [1, 2]
    assert lines[1].payload.terminal_status is RunStatus.COMPLETED
    assert len(events(canary, run_record(canary, "RUN-00000011"))) == 4


def test_prx_065_no_parent_states_no_delegation(canary: CanaryProvision, tmp_path: Path) -> None:
    """A subagent with no resolved parent writes nothing on any other Run's stream."""
    start(canary, tmp_path, host_session_id="interactive-session")

    assert events(canary, run_record(canary, DAEMON_RUN_KEY)) == ()


def test_surf_054_unknown_spawning_session_records_no_parent(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    answer = start(canary, tmp_path, host_session_id="interactive-session")

    assert answer["parent_run_ref"] is None


def test_surf_054_session_shared_by_two_runs_records_no_parent(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    session = {"harness": "claude-code", "session_digest": hash_vendor_session_id(HOST_SESSION)}
    first = seed_row("run", "RUNNING") | {"vendor_session": session}
    second = seed_row("run", "RUNNING") | {
        "key": "RUN-00000020",
        "urn": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000020",
        "vendor_session": session,
    }
    seed(canary, {Epoch2Collection.RUN.value: {DAEMON_RUN_KEY: first, "RUN-00000020": second}})

    answer = start(canary, tmp_path, host_session_id=HOST_SESSION)

    assert answer["parent_run_ref"] is None
    assert answer["run_ref"].endswith("/run/RUN-00000021")


# ---- SURF-075: the transcript route -------------------------------------------------


def test_surf_075_transcript_renders_the_runs_words_and_a_spawn_as_work_elsewhere(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    transcript = claude_transcript(tmp_path / "agent.jsonl", SUBAGENT_LINES)
    stop(canary, tmp_path, transcript_path=str(transcript))

    blocks, purged = build_transcript_blocks(events(canary, run_record(canary, "RUN-00000011")))

    assert purged == ()
    assert [block.lane for block in blocks] == ["message", "message", "subagent", "message"]
    assert [block.text.value for block in blocks] == [
        "Count the modules in the tree.",
        "I will list the package first.",
        "a subagent · requested · working elsewhere",
        "There are forty-two modules.",
    ]


# ---- SURF-097: one Activity view ----------------------------------------------------


def test_surf_097_harness_and_daemon_runs_share_one_activity_view(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    start(canary, tmp_path)

    served = call(canary, tmp_path, "projection.activity.read")
    grouping = group_runs(build_register_view(RouteProjection.model_validate(served)))

    assert {row["key"] for row in served["rows"]} == {DAEMON_RUN_KEY, "RUN-00000011"}
    assert grouping.total == 2
    assert grouping.unbucketed == 0
    counts = {count.bucket: count.count.value for count in grouping.top_level()}
    assert counts[ActivityExceptionBucket.RUNNING] == "2"


# ---- refusals ------------------------------------------------------------------------


def test_surf_054_tree_without_one_repository_is_refused(tmp_path: Path) -> None:
    bare = provision(tmp_path / "bare", code="BARE")

    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call(bare, tmp_path, HOST_SUBAGENT_START_METHOD, harness="claude-code", agent_id="x")


def test_surf_054_epoch_one_tree_is_refused_with_a_typed_code(tmp_path: Path) -> None:
    plain = tmp_path / "plain"
    (plain / ".ea").mkdir(parents=True)
    ctx = method_context(tmp_path / "runtime")

    with pytest.raises(DaemonValidationError, match="native_authority_required"):
        asyncio.run(
            methods.dispatch(
                HOST_SUBAGENT_START_METHOD,
                ctx,
                {"repo_root": str(plain), "harness": "claude-code", "agent_id": "x"},
            )
        )


@pytest.mark.parametrize(
    "params",
    [
        pytest.param({"harness": "claude-code", "agent_id": ""}, id="empty-agent-id"),
        pytest.param({"harness": "claude-code", "agent_id": "x" * 257}, id="oversized-agent-id"),
        pytest.param({"harness": "opencode", "agent_id": "x"}, id="unknown-harness"),
        pytest.param({"agent_id": "x"}, id="missing-harness"),
        pytest.param({"harness": "claude-code", "agent_id": "x", "extra": 1}, id="unknown-key"),
        pytest.param({"harness": "claude-code", "agent_id": 7}, id="agent-id-not-a-string"),
    ],
)
def test_surf_054_malformed_start_is_refused(
    canary: CanaryProvision, tmp_path: Path, params: dict[str, Any]
) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(canary, tmp_path, HOST_SUBAGENT_START_METHOD, **params)
    assert sorted(runs(canary)) == [DAEMON_RUN_KEY]


def test_surf_054_start_takes_no_transcript_path(canary: CanaryProvision, tmp_path: Path) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        start(canary, tmp_path, transcript_path="agent.jsonl")
