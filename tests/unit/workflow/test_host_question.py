"""A pending action becomes a host question only when an operator can answer it as shown.

Row ids name the packet requirement each test proves: SURF-030/031/073 the
one-to-one binding and the exact persisted options, SURF-032 the timeout
rule, SURF-033 durable before displayed, SURF-034 to SURF-037 and SURF-039
the presentation rules, and SURF-111 the rules taken together.
"""

from __future__ import annotations

from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.state.epoch2.pending_action import PendingAction
from eawf.workflow.host_question import (
    MAX_SENTENCE_WORDS,
    PresentationRefusal,
    QuestionPresentationError,
    present_pending_action,
)
from eawf.workflow.skills.bodies.user_question import UserQuestion

ACTION_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/pending-action/ACT-0001"
SUBJECT_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/milestone/MLS-0030"
RUN_URN: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000010"
AT: Final = "2026-09-18T12:00:00+00:00"


def _options() -> list[dict[str, Any]]:
    """Return three presentable options, the recommended one last."""
    return [
        {
            "option_id": "keep",
            "label": "Keep the current layout",
            "effect": "decline",
            "consequence": "Nothing moves. The panel stays on the left.",
            "preview": "[panel] [main]",
        },
        {
            "option_id": "split",
            "label": "Split the panel in two",
            "effect": "request_repair",
            "consequence": "The panel is split. Each half scrolls on its own.",
            "preview": "[top]\n[bottom] [main]",
        },
        {
            "option_id": "move",
            "label": "Move the panel right",
            "effect": "approve",
            "consequence": "The panel moves to the right edge. The main view widens.",
            "preview": "[main] [panel]",
        },
    ]


def _row(**overrides: Any) -> dict[str, Any]:
    """Return a waiting, presentable operator decision; *overrides* replace fields."""
    row: dict[str, Any] = {
        "id": "ACT-0001",
        "urn": ACTION_URN,
        "kind": "operator_decision",
        "subject_ref": SUBJECT_URN,
        "question": "Where should the side panel go?",
        "options": _options(),
        "recommended_option_id": "move",
        "recommendation_rationale": "A right panel keeps reading order intact as the view grows.",
        "idempotency_key": "req-layout-0001",
        "status": "WAITING",
        "revision": 3,
        "requested_by": {"principal_kind": "agent", "principal_id": "AG-0001", "run_ref": RUN_URN},
        "created_at": AT,
        "updated_at": AT,
    }
    row.update(overrides)
    return row


def _action(**overrides: Any) -> PendingAction:
    """Return a validated pending action built from :func:`_row`."""
    return PendingAction.model_validate(_row(**overrides))


def _refusal(action: PendingAction) -> PresentationRefusal:
    """Return the code presenting *action* refuses with."""
    with pytest.raises(QuestionPresentationError) as caught:
        present_pending_action(action)
    return caught.value.code


def _option_with(index: int, **fields: Any) -> list[dict[str, Any]]:
    """Return the options with option *index* changed by *fields*."""
    options = _options()
    options[index] = {**options[index], **fields}
    return options


# ---------- SURF-030 / SURF-073: bound one-to-one, free text is not consent ----------


def test_surf_030_the_question_is_bound_to_the_action_and_the_revision_it_was_read_at() -> None:
    """The host question names the durable action and its compare-and-swap revision."""
    question = present_pending_action(_action())

    assert question.action_ref == ACTION_URN
    assert question.action_revision == 3
    assert [item.option_id for item in question.options] == ["keep", "split", "move"]


def test_surf_073_a_bound_question_needs_an_id_on_every_option() -> None:
    """Error path: an option a host answer could not be sealed with does not validate."""
    payload = present_pending_action(_action()).model_dump()
    payload["options"][1]["option_id"] = None

    with pytest.raises(ValidationError, match="names its own option_id"):
        UserQuestion.model_validate(payload)


def test_surf_073_a_bound_question_refuses_a_repeated_option_id() -> None:
    """Error path: two options sealing the same id would make the answer ambiguous."""
    payload = present_pending_action(_action()).model_dump()
    payload["options"][1]["option_id"] = "keep"

    with pytest.raises(ValidationError, match="names its own option_id"):
        UserQuestion.model_validate(payload)


@pytest.mark.parametrize(("ref", "revision"), [(ACTION_URN, None), (None, 3)])
def test_surf_030_the_action_and_its_revision_are_given_together(
    ref: str | None, revision: int | None
) -> None:
    """Boundary: half a binding binds to nothing."""
    payload = present_pending_action(_action()).model_dump()
    payload.update(action_ref=ref, action_revision=revision)

    with pytest.raises(ValidationError, match="given together"):
        UserQuestion.model_validate(payload)


def test_surf_030_an_unbound_question_still_validates() -> None:
    """Boundary: a question no pending action backs keeps its old shape."""
    question = UserQuestion.model_validate(
        {"question": "Pick one", "options": [{"label": "a"}, {"label": "b"}]}
    )

    assert question.action_ref is None


def test_surf_030_revision_zero_is_not_a_revision() -> None:
    """Error path: a revision is positive."""
    payload = present_pending_action(_action()).model_dump()
    payload["action_revision"] = 0

    with pytest.raises(ValidationError):
        UserQuestion.model_validate(payload)


# ---------- SURF-031: exactly the persisted options ----------


def test_surf_031_options_are_the_persisted_options_in_order_under_their_labels() -> None:
    """The recommended option is last in the record and stays last on the surface."""
    action = _action()

    question = present_pending_action(action)

    assert [(o.option_id, o.label) for o in question.options] == [
        (o.option_id, o.label) for o in action.options
    ]


def test_surf_031_the_recommendation_is_stated_without_relabelling() -> None:
    """The recommended label is the persisted label; the recommendation rides the description."""
    question = present_pending_action(_action())

    move = question.options[-1]
    assert move.label == "Move the panel right"
    assert move.description is not None
    assert move.description.startswith("Recommended. A right panel keeps reading order")
    assert all("Recommended" not in (o.description or "") for o in question.options[:-1])


@pytest.mark.parametrize("count", [2, 4])
def test_surf_031_every_offered_count_presents_whole(count: int) -> None:
    """Boundary: the fewest and the most options an action offers present one-to-one."""
    options = [
        {
            "option_id": f"opt_{index}",
            "label": f"Choice {index}",
            "effect": "approve" if index == 0 else "decline",
            "consequence": f"Outcome number {index} happens.",
            "preview": f"state {index}",
        }
        for index in range(count)
    ]
    action = _action(options=options, recommended_option_id="opt_0")

    question = present_pending_action(action)

    assert len(question.options) == count


# ---------- SURF-032: no timeout default the surface cannot show ----------


def test_surf_032_a_protected_approval_never_carries_a_timeout_default() -> None:
    """Error path: the record itself refuses a protected approval that could lapse into a yes."""
    with pytest.raises(ValidationError, match="cannot carry default_on_timeout"):
        _action(
            kind="protected_approval",
            bundle_digest="sha256:" + "c" * 64,
            default_on_timeout="keep",
        )


def test_surf_032_a_protected_approval_presents_with_no_timeout() -> None:
    """A protected approval is shown with nothing that answers for the operator."""
    question = present_pending_action(
        _action(kind="protected_approval", bundle_digest="sha256:" + "c" * 64)
    )

    assert "If nobody answers" not in question.question


def test_surf_032_a_reversible_default_without_an_override_window_is_not_shown() -> None:
    """A default the surface cannot pair with its override window is refused, not hidden."""
    assert _refusal(_action(default_on_timeout="keep")) is PresentationRefusal.TIMEOUT_UNSHOWN


# ---------- SURF-033: durable before displayed ----------


@pytest.mark.parametrize("status", ["CREATED", "SEALED"])
def test_surf_033_only_a_filed_waiting_question_is_presented(status: str) -> None:
    """A question not yet asked, or already answered, is not put in front of anyone."""
    sealed = {
        "resolution_actor": {"principal_kind": "human", "principal_id": "OP-0001"},
        "selected_option_id": "move",
        "receipt_ref": "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/evidence/EVD-0001",
    }
    action = _action(status=status, **(sealed if status == "SEALED" else {}))

    assert _refusal(action) is PresentationRefusal.NOT_WAITING


def test_surf_033_presenting_the_same_record_again_shows_the_same_question() -> None:
    """A surface lost and reopened on the filed record shows exactly what it showed."""
    action = _action()

    assert present_pending_action(action) == present_pending_action(action)


# ---------- SURF-034: show, do not only tell ----------


def test_surf_034_every_option_carries_its_rendering_for_side_by_side_comparison() -> None:
    """Each option's persisted sketch is the host preview, the sketch kept byte for byte."""
    question = present_pending_action(_action())

    assert [o.preview for o in question.options] == [o["preview"] for o in _options()]


def test_surf_034_an_option_told_only_in_prose_is_incomplete() -> None:
    """Error path: an option without a rendering refuses the whole question."""
    action = _action(options=_option_with(1, preview=None))

    assert _refusal(action) is PresentationRefusal.OPTION_UNSHOWN


# ---------- SURF-035: expand every term ----------


@pytest.mark.parametrize("term", ["MLS-0030", "CI", "P31", "ACCEPTANCE_REVIEW", "SURF-035"])
def test_surf_035_an_unexpanded_term_is_refused(term: str) -> None:
    """Error path: an identifier, abbreviation or screaming-case key needs its expansion."""
    action = _action(question=f"Where should the side panel go for {term}?")

    with pytest.raises(QuestionPresentationError, match=term) as caught:
        present_pending_action(action)
    assert caught.value.code is PresentationRefusal.TERM_UNEXPANDED


def test_surf_035_a_term_in_a_preview_needs_its_expansion_too() -> None:
    """The preview is part of the view the reader has to understand."""
    action = _action(options=_option_with(0, preview="[panel] [main]  CI"))

    assert _refusal(action) is PresentationRefusal.TERM_UNEXPANDED


def test_surf_035_the_expansion_is_shown_in_the_same_view() -> None:
    """An expanded term presents, and its expansion rides the question itself."""
    action = _action(
        question="Where should the side panel go for MLS-0030?",
        terms=[{"term": "MLS-0030", "expansion": "the Milestone this layout ships in"}],
    )

    question = present_pending_action(action)

    assert question.question.endswith("Terms: MLS-0030 is the Milestone this layout ships in.")


def test_surf_035_ordinary_capitalised_words_are_not_terms() -> None:
    """Boundary: a sentence-initial word or a lone capital is prose, not a code."""
    action = _action(question="I ask: Where should the Milestone panel go?")

    assert present_pending_action(action).question.startswith("I ask:")


# ---------- SURF-036: plain prose, consequence in the option ----------


def test_surf_036_the_consequence_is_stated_in_the_option() -> None:
    """Each option's description carries its consequence, not a footnote."""
    question = present_pending_action(_action())

    assert question.options[0].description == "Nothing moves. The panel stays on the left."


def test_surf_036_an_option_without_its_consequence_is_refused() -> None:
    """Error path: the reader must not have to guess what a choice does."""
    action = _action(options=_option_with(2, consequence=None))

    assert _refusal(action) is PresentationRefusal.OPTION_UNSHOWN


def _sentence(words: int) -> str:
    """Return one sentence of exactly *words* words."""
    return " ".join(["word"] * (words - 1) + ["end."])


def test_surf_036_a_sentence_at_the_limit_presents() -> None:
    """Boundary: exactly the maximum sentence length is plain enough."""
    action = _action(question=_sentence(MAX_SENTENCE_WORDS))

    assert present_pending_action(action).question == _sentence(MAX_SENTENCE_WORDS)


def test_surf_036_a_sentence_one_word_over_the_limit_is_refused() -> None:
    """Boundary: one word past the maximum is refused."""
    action = _action(options=_option_with(0, consequence=_sentence(MAX_SENTENCE_WORDS + 1)))

    assert _refusal(action) is PresentationRefusal.PROSE_NOT_PLAIN


# ---------- SURF-037: one recommendation, one sentence ----------


def test_surf_037_exactly_one_option_is_recommended() -> None:
    """One option, and only one, carries the recommendation."""
    question = present_pending_action(_action())

    assert sum((o.description or "").startswith("Recommended.") for o in question.options) == 1


def test_surf_037_a_question_recommending_nothing_is_refused() -> None:
    """Error path: the operator is owed the durable choice, stated."""
    action = _action(recommended_option_id=None, recommendation_rationale=None)

    assert _refusal(action) is PresentationRefusal.RECOMMENDATION_UNSTATED


def test_surf_037_a_rationale_of_two_sentences_is_refused() -> None:
    """Error path: the rationale is one sentence."""
    action = _action(recommendation_rationale="It lasts. It also reads well.")

    assert _refusal(action) is PresentationRefusal.RECOMMENDATION_UNSTATED


def test_surf_037_a_rationale_without_a_recommendation_does_not_validate() -> None:
    """Error path: the record files a recommendation and its reason together."""
    with pytest.raises(ValidationError, match="filed together"):
        _action(recommended_option_id=None)


def test_surf_037_a_recommendation_of_an_option_not_offered_does_not_validate() -> None:
    """Error path: a recommendation names one of the offered options."""
    with pytest.raises(ValidationError, match="recommended_option_id 'elsewhere'"):
        _action(recommended_option_id="elsewhere")


# ---------- SURF-039: no question without a consequence ----------


@pytest.mark.parametrize("field", ["consequence", "preview"])
def test_surf_039_options_that_lead_to_the_same_place_are_not_asked(field: str) -> None:
    """A choice whose answers change nothing spends attention for nothing."""
    options = _options()
    options[1] = {**options[1], field: options[0][field]}

    assert _refusal(_action(options=options)) is PresentationRefusal.NO_CONSEQUENCE


# ---------- SURF-111: every rule at once ----------


def test_surf_111_a_legacy_record_filed_without_its_presentation_is_not_shown() -> None:
    """A row filed before the presentation fields existed reads back, and is refused."""
    bare = [
        {k: v for k, v in option.items() if k in {"option_id", "label", "effect"}}
        for option in _options()
    ]
    action = _action(options=bare, recommended_option_id=None, recommendation_rationale=None)

    assert _refusal(action) is PresentationRefusal.OPTION_UNSHOWN


def test_surf_111_the_presented_question_carries_visuals_terms_and_a_recommendation() -> None:
    """The whole presentation: renderings, the expansion, and the one recommendation."""
    action = _action(
        question="Where should the side panel go for MLS-0030?",
        terms=[{"term": "MLS-0030", "expansion": "the Milestone this layout ships in"}],
    )

    question = present_pending_action(action)

    assert all(o.preview for o in question.options)
    assert "MLS-0030 is the Milestone" in question.question
    assert [o.option_id for o in question.options if "Recommended." in (o.description or "")] == [
        "move"
    ]
