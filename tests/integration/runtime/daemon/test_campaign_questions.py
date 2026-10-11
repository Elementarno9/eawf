"""Campaign questions carry their own context and brief, and the campaign verbs are anchored.

``eawf campaign new --questions-file`` reads a YAML list of questions through a closed
model before anything reaches the daemon; ``runtime.campaign.start`` reads each brief once,
keeps its text and digest on the Campaign, and every round of the question sees both in its
prompt. A start under an idempotency key the tree already holds is answered with the first
Campaign, and ``runtime.campaign.run`` refuses a Campaign that moved past the caller's
revision. The daemon runs in process over an isolated socket against a tmp canary, with a
stub research agent: no model is called and no host binary starts.
"""

from __future__ import annotations

import asyncio
import hashlib
import tempfile
import uuid
from pathlib import Path
from typing import Any, Final

import orjson
import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from eawf.kernel.state.epoch2.campaign import (
    BRIEF_MAX_CHARS,
    Campaign,
    CampaignQuestionContext,
    CampaignQuestionSpec,
    QuestionBrief,
)
from eawf.kernel.store.compaction import document_rows, read_document
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.campaign_scheduler import StepAssignment
from eawf.runtime.daemon.methods import DaemonValidationError, campaign_run
from eawf.runtime.daemon.methods.campaign_run import (
    CAMPAIGN_RUN_METHOD,
    CAMPAIGN_START_METHOD,
    read_brief,
    round_prompt,
)
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    document_path,
    provision,
    seed,
    seed_row,
)
from tests.integration.runtime.daemon.methods.test_campaign_run import (
    QUESTION,
    TRACK,
    StubAgent,
    _Client,
    _settled,
    _start,
    served,
)

pytestmark = pytest.mark.integration

CONTEXT: Final = "Replay runs after an unclean daemon stop; the WAL is the only source."
BRIEF_TEXT: Final = "# Brief\n\nRead src/eawf/kernel/store/ledger.py first.\n"
BRIEF_PATH: Final = ".ea/local/briefs/replay.md"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000001"
CAMPAIGN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/campaign/CAM-0001"
QUESTION_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/_/question/QST-0001"


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, and the operator's home out of reach."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    provisioned = provision(tmp_path / "cam", code="CAM")
    seed(provisioned, {"track": {"TRK-RUNTIME": seed_row("track", "ACTIVE")}})
    brief = provisioned.root / BRIEF_PATH
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(BRIEF_TEXT, encoding="utf-8")
    return provisioned


@pytest.fixture
def agent(monkeypatch: pytest.MonkeyPatch) -> StubAgent:
    stub = StubAgent()
    monkeypatch.setattr(campaign_run, "research_agent_for", lambda *_args: stub)
    return stub


async def _raw(client: _Client, method: str, **params: Any) -> dict[str, Any]:
    """Send one call and return the whole reply, an error reply included."""
    frame = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": method, "params": params}
    client._writer.write(orjson.dumps(frame) + b"\n")
    await client._writer.drain()
    return dict(orjson.loads(await client._reader.readline()))


def _framed() -> dict[str, Any]:
    return {"question": QUESTION, "context": CONTEXT, "brief": BRIEF_PATH}


def _campaigns(canary: CanaryProvision) -> dict[str, Any]:
    return dict(document_rows(read_document(document_path(canary)), Epoch2Collection.CAMPAIGN))


# ---------- the questions file, refused or sent by the CLI ----------


@pytest.fixture
def sent(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def answer(method: str, params: dict[str, Any], **_options: Any) -> dict[str, Any]:
        calls.append((method, params))
        return {"record": {"key": "CAM-0001", "plan_steps": [{}]}, "driving": False}

    monkeypatch.setattr(domain, "_native_answer", answer)
    return calls


def _new(tmp_path: Path, *args: str) -> Any:
    return CliRunner().invoke(
        app,
        [
            "--json", "-w", str(tmp_path), "campaign", "new", "Establish replay order",
            "--actor", "OP-0001", "--track", TRACK, *args,
        ],
    )  # fmt: skip


def _file(tmp_path: Path, text: str) -> str:
    path = tmp_path / "questions.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_a_questions_file_sends_each_question_with_its_context_and_brief(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    path = _file(
        tmp_path,
        f"- question: {QUESTION}\n  context: {CONTEXT}\n  brief: {BRIEF_PATH}\n"
        "- question: Which restarts reorder?\n",
    )
    result = _new(tmp_path, "--question", "Is the WAL fsynced?", "--questions-file", path)
    assert result.exit_code == exit_codes.OK, result.output
    [(method, params)] = sent
    assert method == CAMPAIGN_START_METHOD
    assert params["questions"] == [
        "Is the WAL fsynced?",
        {"question": QUESTION, "context": CONTEXT, "brief": BRIEF_PATH},
        {"question": "Which restarts reorder?", "context": "", "brief": None},
    ]
    assert params["idempotency_key"].startswith("campaign-new-")


@pytest.mark.parametrize(
    "text",
    [
        f"- question: {QUESTION}\n  focus: replay\n",
        f"- question: {'q' * 501}\n",
        f"- question: {QUESTION}\n  context: {'c' * 2001}\n",
        f"- question: {QUESTION}\n  brief: /etc/passwd\n",
        f"- question: {QUESTION}\n  brief: ../outside.md\n",
        "- question: ''\n",
        "- 7\n",
        f"question: {QUESTION}\n",
        "[]\n",
        "- question: [unclosed\n",
    ],
    ids=[
        "unknown-key", "question-501", "context-2001", "absolute-brief", "climbing-brief",
        "empty-question", "not-a-mapping", "not-a-list", "empty-list", "not-yaml",
    ],
)  # fmt: skip
def test_a_bad_questions_file_is_refused_before_any_daemon_call(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]], text: str
) -> None:
    result = _new(tmp_path, "--questions-file", _file(tmp_path, text))
    assert result.exit_code == exit_codes.USER_ERROR, result.output
    assert sent == []


def test_a_missing_questions_file_and_no_question_are_refused_before_any_daemon_call(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    assert _new(tmp_path, "--questions-file", str(tmp_path / "absent.yaml")).exit_code == (
        exit_codes.USER_ERROR
    )
    assert _new(tmp_path).exit_code == exit_codes.USER_ERROR
    assert sent == []


def test_a_bare_question_still_works_and_a_retried_start_keeps_its_key(
    tmp_path: Path, sent: list[tuple[str, dict[str, Any]]]
) -> None:
    assert _new(tmp_path, "--question", QUESTION).exit_code == exit_codes.OK
    assert _new(tmp_path, "--question", QUESTION).exit_code == exit_codes.OK
    assert _new(tmp_path, "--question", QUESTION, "--idempotency-key", "k-1").exit_code == 0
    assert [params["questions"] for _method, params in sent] == [[QUESTION]] * 3
    keys = [params["idempotency_key"] for _method, params in sent]
    assert keys[0] == keys[1]
    assert keys[2] == "k-1"


def test_the_question_model_bounds_are_inclusive() -> None:
    longest = CampaignQuestionSpec(question="q" * 500, context="c" * 2000, brief="a.md")
    assert (len(longest.question), len(longest.context)) == (500, 2000)
    assert CampaignQuestionSpec(question="q").context == ""
    with pytest.raises(ValidationError):
        CampaignQuestionSpec.model_validate({"question": 7})
    with pytest.raises(ValidationError):
        CampaignQuestionSpec.model_validate({"context": "no question"})


# ---------- the brief, read once at plan time ----------


def test_a_brief_is_read_with_the_digest_of_its_bytes(tmp_path: Path) -> None:
    (tmp_path / "b.md").write_text(BRIEF_TEXT, encoding="utf-8")
    brief = read_brief(tmp_path, "b.md")
    assert brief.text == BRIEF_TEXT
    assert brief.sha256 == f"sha256:{hashlib.sha256(BRIEF_TEXT.encode()).hexdigest()}"


def test_a_brief_is_bounded_at_its_character_limit(tmp_path: Path) -> None:
    (tmp_path / "max.md").write_text("é" * BRIEF_MAX_CHARS, encoding="utf-8")
    assert len(read_brief(tmp_path, "max.md").text) == BRIEF_MAX_CHARS
    (tmp_path / "over.md").write_text("x" * (BRIEF_MAX_CHARS + 1), encoding="utf-8")
    with pytest.raises(DaemonValidationError, match="16384"):
        read_brief(tmp_path, "over.md")


@pytest.mark.parametrize("name", ["absent.md", "blank.md", "binary.md", "dir", "escape.md"])
def test_a_brief_that_cannot_be_read_is_refused(tmp_path: Path, name: str) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "blank.md").write_text("  \n", encoding="utf-8")
    (repo / "binary.md").write_bytes(b"\xff\xfe\x00")
    (repo / "dir").mkdir()
    (tmp_path / "outside.md").write_text("secret", encoding="utf-8")
    (repo / "escape.md").symlink_to(tmp_path / "outside.md")
    with pytest.raises(DaemonValidationError, match="brief"):
        read_brief(repo, name)


# ---------- the round prompt ----------


def _assignment(**framing: Any) -> StepAssignment:
    return StepAssignment.model_validate(
        {
            "campaign_ref": CAMPAIGN_URN,
            "campaign_title": "Establish replay order",
            "ordinal": 2,
            "title": "Adversarial question QST-0001",
            "method": "adversarial",
            "question": QUESTION,
            "prior_outcomes": ("40 events, 0 inversions",),
            "run_ref": RUN_URN,
            "round_number": 1,
            "actor": "OP-0001",
            **framing,
        }
    )


def test_the_round_prompt_places_context_and_brief_between_question_and_prior() -> None:
    brief = QuestionBrief(path=BRIEF_PATH, sha256=f"sha256:{'0' * 64}", text=BRIEF_TEXT)
    prompt = round_prompt(_assignment(context=CONTEXT, brief=brief))
    marks = [
        f"Question: {QUESTION}",
        f"Context:\n{CONTEXT}",
        f"Brief ({BRIEF_PATH}):\n{BRIEF_TEXT}",
        "What earlier steps showed:\n- 40 events, 0 inversions",
    ]
    places = [prompt.index(mark) for mark in marks]
    assert places == sorted(places)


def test_a_bare_question_prompt_carries_no_context_or_brief_section() -> None:
    prompt = round_prompt(_assignment())
    assert "Context:" not in prompt
    assert "Brief (" not in prompt
    assert prompt.index("Question:") < prompt.index("What earlier steps showed:")
    only_context = round_prompt(_assignment(context=CONTEXT))
    assert "Context:" in only_context
    assert "Brief (" not in only_context


# ---------- the persisted question context ----------


def test_a_campaign_refuses_context_for_a_question_no_step_works() -> None:
    row = _campaign_row()
    assert Campaign.model_validate(row).context_of(QUESTION_URN) is not None
    stray = {**row, "question_context": [{"question_ref": QUESTION_URN.replace("1", "9")}]}
    with pytest.raises(ValidationError, match="no step works"):
        Campaign.model_validate(stray)
    twice = {**row, "question_context": [{"question_ref": QUESTION_URN}] * 2}
    with pytest.raises(ValidationError, match="twice"):
        Campaign.model_validate(twice)
    with pytest.raises(ValidationError):
        CampaignQuestionContext.model_validate({"question_ref": QUESTION_URN, "focus": "x"})


def _campaign_row() -> dict[str, Any]:
    step = {
        "ordinal": 1,
        "title": "Survey question QST-0001",
        "method": "survey",
        "question_ref": QUESTION_URN,
        "bound": {"axes": [{"axis_kind": "rounds", "limit": 1, "unit": "rounds"}]},
    }
    return {
        "uid": str(uuid.uuid4()),
        "key": "CAM-0001",
        "urn": CAMPAIGN_URN,
        "origin": {"kind": "native", "mapping_basis": "native", "confidence": "exact"},
        "revision": 1,
        "created_at": "2026-10-06T00:00:00Z",
        "updated_at": "2026-10-06T00:00:00Z",
        "track_ref": TRACK,
        "title": "Establish replay order",
        "evidence_budget": {"axes": [{"axis_kind": "rounds", "limit": 1, "unit": "rounds"}]},
        "approved_plan_digest": f"sha256:{'0' * 64}",
        "plan_steps": [step],
        "question_context": [{"question_ref": QUESTION_URN, "context": CONTEXT}],
    }


# ---------- end to end on the daemon ----------


def test_each_round_of_a_question_is_worked_from_its_context_and_brief(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    async def body() -> dict[str, Any]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD,
                **_start(canary.root, depth="medium", questions=[_framed(), "Which restarts?"]),
                idempotency_key="start-1",
            )
            await _settled(client, canary.root, started["record"]["key"])
            return started

    started = asyncio.run(body())

    record = started["record"]
    [framed] = record["question_context"]
    digest = hashlib.sha256((canary.root / BRIEF_PATH).read_bytes()).hexdigest()
    assert framed["context"] == CONTEXT
    assert framed["brief"] == {"path": BRIEF_PATH, "sha256": f"sha256:{digest}", "text": BRIEF_TEXT}
    assert record["idempotency_key"] == "start-1"
    by_title = {a.title: a for a in agent.assignments}
    first = by_title["Survey question QST-0001"]
    assert (first.context, first.brief and first.brief.text) == (CONTEXT, BRIEF_TEXT)
    second = by_title["Survey question QST-0002"]
    assert (second.context, second.brief) == ("", None)
    # the synthesis works the Campaign's own question, so it is framed the same way
    assert by_title["Synthesize the findings"].context == CONTEXT


def test_the_brief_is_read_once_at_plan_time(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    async def body() -> None:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD,
                **_start(canary.root, depth="shallow", questions=[_framed()], drive=False),
            )
            (canary.root / BRIEF_PATH).write_text("rewritten after planning", encoding="utf-8")
            key = started["record"]["key"]
            await client.call(
                CAMPAIGN_RUN_METHOD,
                repo_root=str(canary.root),
                actor="OP-0001",
                campaign_key=key,
                expected_revision=1,
            )
            await _settled(client, canary.root, key)

    asyncio.run(body())

    assert {a.brief.text for a in agent.assignments if a.brief is not None} == {BRIEF_TEXT}


def test_an_unreadable_brief_refuses_the_start_with_nothing_written(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    missing = {**_framed(), "brief": ".ea/local/briefs/absent.md"}

    async def body() -> dict[str, Any]:
        async with served(canary, tmp_path) as client:
            return await _raw(
                client, CAMPAIGN_START_METHOD, **_start(canary.root, questions=[missing])
            )

    reply = asyncio.run(body())

    assert "brief" in reply["error"]["message"]
    assert _campaigns(canary) == {}


def test_a_replayed_start_returns_the_first_campaign(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    brief = _start(canary.root, depth="shallow", drive=False, idempotency_key="start-7")

    async def body() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        async with served(canary, tmp_path) as client:
            first = await client.call(CAMPAIGN_START_METHOD, **brief)
            again = await client.call(CAMPAIGN_START_METHOD, **brief)
            other = await _raw(
                client, CAMPAIGN_START_METHOD, **{**brief, "title": "Another campaign"}
            )
            return first, again, other

    first, again, other = asyncio.run(body())

    assert (first["committed"], again["committed"]) == (True, False)
    assert again["record"]["key"] == first["record"]["key"] == "CAM-0001"
    assert list(_campaigns(canary)) == ["CAM-0001"]
    assert "idempotency_conflict" in orjson.dumps(other["error"]).decode()


def test_a_drive_decided_at_a_stale_revision_is_refused(
    canary: CanaryProvision, tmp_path: Path, agent: StubAgent
) -> None:
    async def body() -> tuple[dict[str, Any], dict[str, Any]]:
        async with served(canary, tmp_path) as client:
            started = await client.call(
                CAMPAIGN_START_METHOD, **_start(canary.root, depth="shallow", drive=False)
            )
            key = started["record"]["key"]
            run = {"repo_root": str(canary.root), "actor": "OP-0001", "campaign_key": key}
            stale = await _raw(client, CAMPAIGN_RUN_METHOD, **run, expected_revision=2)
            driven = await client.call(CAMPAIGN_RUN_METHOD, **run, expected_revision=1)
            await _settled(client, canary.root, key)
            return stale, driven

    stale, driven = asyncio.run(body())

    assert "revision_conflict" in orjson.dumps(stale["error"]).decode()
    assert driven["driving"] is True
    assert len(agent.assignments) == 2
