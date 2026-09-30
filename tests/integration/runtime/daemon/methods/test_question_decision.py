"""The operator-decision verb files a question the host can show, and the console can read it.

Driven through the real ``runtime.question.open_decision`` and
``projection.question.decisions`` handlers against a provisioned canary, with the
repository configuration written into the canary's own ``.ea/config.yaml`` and the
machine-wide layer pointed at an empty home. Row ids name the packet requirement each
test proves: SURF-030 and SURF-073 (bound one-to-one and answered only by option id),
SURF-032 (a timeout default only where the persisted policy permits it), SURF-033
(durable before displayed, and an unpresentable question never filed), SURF-038
(the resolved configuration outranks the recommendation), SURF-039 (a question whose
answers change nothing is not asked), and SURF-091 and SURF-111 (the verb the skills
call produces a question that passes every presentation rule). The Codex path --
a numbered text prompt bound to the same pending action, answerable in the session or
in the console with the first answer winning -- is proved under SURF-073 too.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Any, Final

import pytest
import yaml

from eawf.kernel.store.compaction import read_document
from eawf.kernel.store.ledger import LedgerRecord, append_ledger_record
from eawf.kernel.store.tiers import Epoch2Collection
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon import methods
from eawf.runtime.daemon.epoch2_transaction import TransactionRefusedError
from eawf.runtime.daemon.methods import DaemonValidationError
from eawf.runtime.daemon.methods.delivery_approval import DELIVERY_SEAL_APPROVAL_METHOD
from eawf.runtime.daemon.methods.question_decision import (
    QUESTION_ANSWER_NUMBERED_METHOD,
    QUESTION_OPEN_DECISION_METHOD,
)
from eawf.workflow.decision_question import QUESTION_DECISIONS_METHOD
from eawf.workflow.skills.bodies.user_question import UserQuestion
from tests.integration.runtime.daemon._epoch2_transaction_fixtures import (
    AT,
    MILESTONE_URN,
    document_path,
    method_context,
    provision,
    root_context,
)

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0001"
RECEIPT: Final = f"{CONTAINER}/evidence/EVD-0001"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
OPERATOR: Final[dict[str, Any]] = {"principal_kind": "human", "principal_id": "OP-0001"}
AGENT: Final[dict[str, Any]] = {
    "principal_kind": "agent",
    "principal_id": "AG-0001",
    "run_ref": RUN,
}
AXIS: Final[dict[str, Any]] = {
    "key": "audit.default_level",
    "values": {"ask": "quick", "auto": "deep"},
}


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep canary runtimes under tmp, and the operator's own configuration out of reach."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("EAWF_PREFERENCES__AUTO_CHOOSE", raising=False)
    monkeypatch.delenv("EAWF_AUDIT__DEFAULT_LEVEL", raising=False)


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one evidence row an answer can be recorded under."""
    provisioned = provision(tmp_path / "ask", code="ASK")
    context = root_context(provisioned, tmp_path / "runtime")
    with context.session([MILESTONE_URN]) as session:
        append_ledger_record(
            session.ledger_path(Epoch2Collection.EVIDENCE),
            LedgerRecord(
                collection=Epoch2Collection.EVIDENCE,
                record_key="EVD-0001",
                status="recorded",
                recorded_at=AT,
                payload={
                    "id": "EVD-0001",
                    "kind": "decision",
                    "summary": "the operator chose",
                    "recorded_at": AT.isoformat(),
                },
            ),
        )
    return provisioned


def configure(canary: CanaryProvision, **sections: dict[str, Any]) -> None:
    """Merge *sections* into the canary repository's own configuration layer."""
    path = canary.root / ".ea" / "config.yaml"
    current = yaml.safe_load(path.read_text(encoding="utf-8")) if path.exists() else None
    merged = dict(current or {})
    for name, values in sections.items():
        merged[name] = {**dict(merged.get(name) or {}), **values}
    path.write_text(yaml.safe_dump(merged), encoding="utf-8")


def call(canary: CanaryProvision, tmp_path: Path, method: str, **params: Any) -> dict[str, Any]:
    """Dispatch one verb through the real handler against *canary*."""
    ctx = method_context(tmp_path / "runtime")
    return asyncio.run(methods.dispatch(method, ctx, {"repo_root": str(canary.root), **params}))


def _options() -> list[dict[str, Any]]:
    return [
        {
            "option_id": "ask",
            "label": "Ask before each commit",
            "effect": "decline",
            "consequence": "Nothing is committed until you say so.",
            "cost": "Every step waits for you to answer.",
            "preview": "edit -> ask -> commit",
        },
        {
            "option_id": "auto",
            "label": "Commit on its own",
            "effect": "approve",
            "consequence": "Each finished step is committed at once.",
            "cost": "A step you would have stopped lands first.",
            "preview": "edit -> commit",
        },
    ]


def open_decision(canary: CanaryProvision, tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    """File the commit-policy decision an agent raises; *overrides* replace params."""
    params: dict[str, Any] = {
        "urn": MILESTONE_URN,
        "idempotency_key": "req-commit-policy-1",
        "actor": "AG-0001",
        "requested_by": dict(AGENT),
        "question": "Should finished steps be committed without asking?",
        "options": _options(),
        "recommended_option_id": "auto",
        "recommendation_rationale": "Small commits keep every step easy to undo.",
    }
    params.update(overrides)
    return call(canary, tmp_path, QUESTION_OPEN_DECISION_METHOD, **params)


def filed(canary: CanaryProvision) -> dict[str, Any]:
    """Return every pending-action row the tree holds."""
    return dict(read_document(document_path(canary)).get(Epoch2Collection.PENDING_ACTION.value, {}))


# ---------- SURF-030 / SURF-033 / SURF-111: filed waiting, presented bound ----------


def test_surf_030_the_decision_is_filed_waiting_and_presented_bound(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The verb writes one waiting operator decision and answers with its bound question."""
    opened = open_decision(canary, tmp_path)

    rows = filed(canary)
    assert sorted(rows) == ["ACT-0001"]
    assert rows["ACT-0001"]["kind"] == "operator_decision"
    assert rows["ACT-0001"]["status"] == "WAITING"
    question = UserQuestion.model_validate(opened["host_question"])
    assert opened["created"] is True
    assert question.action_ref == opened["action_ref"] == ACTION
    assert question.action_revision == rows["ACT-0001"]["revision"]
    assert [option.option_id for option in question.options] == ["ask", "auto"]


@pytest.mark.parametrize(
    ("override", "code"),
    [
        (
            {"recommended_option_id": None, "recommendation_rationale": None},
            "recommendation_unstated",
        ),
        ({"question": "Should CI commit finished steps?"}, "term_unexpanded"),
        (
            {"options": [{**_options()[0], "preview": None}, _options()[1]]},
            "option_not_shown",
        ),
    ],
)
def test_surf_033_an_unpresentable_decision_is_refused_with_nothing_written(
    canary: CanaryProvision, tmp_path: Path, override: dict[str, Any], code: str
) -> None:
    """Error path: a question an operator could not answer as shown is never filed."""
    with pytest.raises(DaemonValidationError, match=code):
        open_decision(canary, tmp_path, **override)
    assert filed(canary) == {}


def test_surf_039_a_decision_whose_answers_change_nothing_is_not_asked(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Error path: two options producing the same thing spend attention for nothing."""
    same = [_options()[0], {**_options()[1], "consequence": _options()[0]["consequence"]}]

    with pytest.raises(DaemonValidationError, match="question_without_consequence"):
        open_decision(canary, tmp_path, options=same)
    assert filed(canary) == {}


def test_surf_111_asking_again_under_the_same_key_finds_the_filed_question(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The same question under the same key is returned, with nothing written twice."""
    first = open_decision(canary, tmp_path)
    again = open_decision(canary, tmp_path)

    assert again["created"] is False
    assert again["action_ref"] == first["action_ref"]
    assert again["host_question"] == first["host_question"]
    assert sorted(filed(canary)) == ["ACT-0001"]


def test_surf_111_the_same_key_naming_another_question_is_refused(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Error path: one key cannot name two different questions."""
    open_decision(canary, tmp_path)

    with pytest.raises(TransactionRefusedError, match="idempotency_conflict"):
        open_decision(canary, tmp_path, question="Should failed steps be committed too?")
    assert sorted(filed(canary)) == ["ACT-0001"]


def test_surf_111_the_verb_refuses_an_unknown_parameter(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Error path: the request is a closed model at the daemon boundary."""
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        open_decision(canary, tmp_path, deadline="tomorrow")
    assert filed(canary) == {}


# ---------- SURF-032: a timeout default only where the persisted policy permits ----------


def test_surf_032_the_default_policy_files_no_timeout_default(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """With ``preferences.auto_choose`` unset (``off``), a requested window files no default."""
    open_decision(canary, tmp_path, override_window_minutes=30)

    row = filed(canary)["ACT-0001"]
    assert row.get("default_on_timeout") is None
    assert row.get("override_until") is None


def test_surf_032_a_permitting_policy_files_and_shows_the_window(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The repository permits a default, so it is filed with its window and shown."""
    configure(canary, preferences={"auto_choose": "recommended"})
    opened = open_decision(canary, tmp_path, override_window_minutes=30)

    row = filed(canary)["ACT-0001"]
    assert row["default_on_timeout"] == "auto"
    assert row["override_until"] is not None
    assert row["default_policy"] == "preferences.auto_choose=recommended"
    assert "If nobody answers by" in opened["host_question"]["question"]


# ---------- SURF-038: configuration outranks the recommendation ----------


def test_surf_038_the_repository_configuration_picks_the_recommendation(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The asker recommends ``auto``; the repository's value is ``ask``'s, so it is recommended."""
    configure(canary, audit={"default_level": "quick"})
    opened = open_decision(canary, tmp_path, config_axis=AXIS)

    row = filed(canary)["ACT-0001"]
    assert row["recommended_option_id"] == "ask"
    assert "audit.default_level to quick" in row["recommendation_rationale"]
    first = opened["host_question"]["options"][0]
    assert first["option_id"] == "ask"
    assert first["description"].startswith("Recommended.")


def test_surf_038_a_configured_value_no_option_honours_is_refused(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Error path: the configuration says ``standard``, which no option stands for."""
    configure(canary, audit={"default_level": "standard"})

    with pytest.raises(DaemonValidationError, match="config_value_unhonoured"):
        open_decision(canary, tmp_path, config_axis=AXIS)
    assert filed(canary) == {}


# ---------- SURF-073 / SURF-091: answered by option id; the console reads the waiting ones ----


def test_surf_073_the_decision_is_answered_by_option_id_and_leaves_the_read(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """The seal verb seals a decision by option id, and a sealed one no longer waits."""
    open_decision(canary, tmp_path)
    waiting = call(canary, tmp_path, QUESTION_DECISIONS_METHOD)
    assert [item["id"] for item in waiting["decisions"]] == ["ACT-0001"]
    assert [item["option_id"] for item in waiting["decisions"][0]["options"]] == ["ask", "auto"]

    sealed = call(
        canary,
        tmp_path,
        DELIVERY_SEAL_APPROVAL_METHOD,
        urn=ACTION,
        expected_revision=1,
        idempotency_key="req-seal-1",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        option_id="ask",
        receipt_ref=RECEIPT,
    )

    assert sealed["outcome"] == "sealed"
    assert filed(canary)["ACT-0001"]["selected_option_id"] == "ask"
    assert call(canary, tmp_path, QUESTION_DECISIONS_METHOD) == {"decisions": []}


def test_surf_073_free_text_is_not_an_answer(canary: CanaryProvision, tmp_path: Path) -> None:
    """Error path: an answer that is not one of the filed option ids seals nothing."""
    open_decision(canary, tmp_path)

    with pytest.raises((DaemonValidationError, TransactionRefusedError)):
        call(
            canary,
            tmp_path,
            DELIVERY_SEAL_APPROVAL_METHOD,
            urn=ACTION,
            expected_revision=1,
            idempotency_key="req-seal-1",
            actor="OP-0001",
            resolver=dict(OPERATOR),
            option_id="yes",
            receipt_ref=RECEIPT,
        )
    assert filed(canary)["ACT-0001"]["status"] == "WAITING"


def test_surf_091_the_read_holds_only_waiting_decisions(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    """Boundary: an empty tree reads empty, and an unknown parameter is refused."""
    assert call(canary, tmp_path, QUESTION_DECISIONS_METHOD) == {"decisions": []}
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call(canary, tmp_path, QUESTION_DECISIONS_METHOD, route="attention")


# ---------- SURF-073 on Codex: a numbered prompt bound to the pending action ----------


def _answer_numbered(
    canary: CanaryProvision, tmp_path: Path, reply: str, *, key: str = "req-codex-1"
) -> dict[str, Any]:
    return call(
        canary,
        tmp_path,
        QUESTION_ANSWER_NUMBERED_METHOD,
        urn=ACTION,
        expected_revision=1,
        idempotency_key=key,
        actor="OP-0001",
        resolver=dict(OPERATOR),
        reply=reply,
        receipt_ref=RECEIPT,
    )


def test_surf_073_codex_prompt_numbers_the_filed_options_and_names_its_binding(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    opened = open_decision(canary, tmp_path)

    prompt = opened["numbered_prompt"]
    assert prompt.index("1) Ask before each commit") < prompt.index("2) Commit on its own")
    assert "   | edit -> commit" in prompt
    assert f"sealed on {ACTION} at revision 1" in prompt
    assert "Reply with one number from 1 to 2." in prompt


def test_surf_073_codex_reply_seals_the_numbered_option(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    open_decision(canary, tmp_path)

    sealed = _answer_numbered(canary, tmp_path, " 2\n")

    assert sealed["outcome"] == "sealed"
    assert filed(canary)["ACT-0001"]["selected_option_id"] == "auto"


@pytest.mark.parametrize("reply", ["yes", "Commit on its own", "3", "0", "", "2 please", "\u0662"])
def test_surf_073_codex_free_text_reply_is_not_consent(
    canary: CanaryProvision, tmp_path: Path, reply: str
) -> None:
    open_decision(canary, tmp_path)

    with pytest.raises(DaemonValidationError, match="reply_not_an_option"):
        _answer_numbered(canary, tmp_path, reply)
    assert filed(canary)["ACT-0001"]["status"] == "WAITING"


def test_surf_073_the_console_answer_first_wins_over_the_codex_reply(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    open_decision(canary, tmp_path)
    call(
        canary,
        tmp_path,
        DELIVERY_SEAL_APPROVAL_METHOD,
        urn=ACTION,
        expected_revision=1,
        idempotency_key="req-console-1",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        option_id="ask",
        receipt_ref=RECEIPT,
    )

    late = _answer_numbered(canary, tmp_path, "2")

    assert late["outcome"] == "superseded"
    assert filed(canary)["ACT-0001"]["selected_option_id"] == "ask"


def test_surf_073_the_codex_reply_first_wins_over_the_console(
    canary: CanaryProvision, tmp_path: Path
) -> None:
    open_decision(canary, tmp_path)
    assert _answer_numbered(canary, tmp_path, "2")["outcome"] == "sealed"

    late = call(
        canary,
        tmp_path,
        DELIVERY_SEAL_APPROVAL_METHOD,
        urn=ACTION,
        expected_revision=1,
        idempotency_key="req-console-1",
        actor="OP-0001",
        resolver=dict(OPERATOR),
        option_id="ask",
        receipt_ref=RECEIPT,
    )

    assert late["outcome"] == "superseded"
    assert filed(canary)["ACT-0001"]["selected_option_id"] == "auto"
