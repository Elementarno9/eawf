"""An operator decision is built from what the asker files and what the configuration says.

Row ids name the packet requirement each test proves: SURF-032 (a timeout default only
where the persisted policy permits it, filed with its override window and shown), and
SURF-038 (a configured value outranks the asker's recommendation, and a configured value
no option can honour is refused rather than asked about).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, Final

import pytest
from pydantic import ValidationError

from eawf.kernel.config.schema import AutoChoose
from eawf.kernel.identity import parse_qualified_urn
from eawf.kernel.state.epoch2.pending_action import (
    PendingAction,
    PendingActionKind,
    PendingActionStatus,
)
from eawf.workflow.decision_question import (
    AUTO_CHOOSE_KEY,
    MAX_OVERRIDE_WINDOW_MINUTES,
    DecisionRefusal,
    DecisionRefusedError,
    DecisionRequest,
    decision_question,
)
from eawf.workflow.host_question import (
    PresentationRefusal,
    QuestionPresentationError,
    present_pending_action,
)

CONTAINER: Final = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF"
SUBJECT: Final = f"{CONTAINER}/milestone/MLS-0030"
ACTION: Final = f"{CONTAINER}/pending-action/ACT-0003"
RUN: Final = f"{CONTAINER}/run/RUN-00000010"
AT: Final = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)


def _options() -> list[dict[str, Any]]:
    """Return two presentable options for a commit-policy question."""
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


def _request(**overrides: Any) -> DecisionRequest:
    """Return a presentable request recommending ``auto``; *overrides* replace fields."""
    payload: dict[str, Any] = {
        "urn": SUBJECT,
        "idempotency_key": "req-commit-policy-1",
        "requested_by": {"principal_kind": "agent", "principal_id": "AG-0001", "run_ref": RUN},
        "question": "Should finished steps be committed without asking?",
        "options": _options(),
        "recommended_option_id": "auto",
        "recommendation_rationale": "Small commits keep every step easy to undo.",
    }
    payload.update(overrides)
    return DecisionRequest.model_validate(payload)


def _decide(
    request: DecisionRequest,
    *,
    configured: str | None = None,
    auto_choose: AutoChoose = AutoChoose.OFF,
) -> PendingAction:
    return decision_question(
        request,
        key="ACT-0003",
        urn=parse_qualified_urn(ACTION),
        configured=configured,
        auto_choose=auto_choose,
        at=AT,
    )


# ---------- the decision itself ----------


def test_surf_032_a_decision_is_a_waiting_reversible_choice() -> None:
    """The record is an operator decision, born waiting, carrying exactly the filed options."""
    action = _decide(_request())

    assert action.kind is PendingActionKind.OPERATOR_DECISION
    assert action.status is PendingActionStatus.WAITING
    assert action.option_ids == ("ask", "auto")
    assert action.recommended_option_id == "auto"
    assert action.default_on_timeout is None


@pytest.mark.parametrize("count", [1, 5])
def test_surf_032_a_request_offers_two_to_four_options(count: int) -> None:
    """Boundary: one option is not a question, five cannot be shown side by side."""
    options = [
        {**_options()[0], "option_id": f"opt_{index}", "label": f"Option {index}"}
        for index in range(count)
    ]
    with pytest.raises(ValidationError):
        _request(options=options)


def test_surf_032_a_request_refuses_an_unknown_field() -> None:
    """Error path: the request model is closed."""
    with pytest.raises(ValidationError, match="extra"):
        _request(deadline="tomorrow")


# ---------- SURF-032: a timeout default only where policy permits ----------


def test_surf_032_policy_off_files_no_default_even_with_a_window() -> None:
    """Under the default ``off`` policy nothing answers for the operator."""
    action = _decide(_request(override_window_minutes=30), auto_choose=AutoChoose.OFF)

    assert action.default_on_timeout is None
    assert action.override_until is None
    assert action.default_policy is None


@pytest.mark.parametrize("policy", [AutoChoose.RECOMMENDED, AutoChoose.ALWAYS])
def test_surf_032_a_permitting_policy_files_the_default_with_its_window(
    policy: AutoChoose,
) -> None:
    """The default is the recommendation, with the window and the policy that allowed it."""
    action = _decide(_request(override_window_minutes=30), auto_choose=policy)

    assert action.default_on_timeout == "auto"
    assert action.override_until == AT + timedelta(minutes=30)
    assert action.default_policy == f"{AUTO_CHOOSE_KEY}={policy.value}"


def test_surf_032_a_permitting_policy_without_a_window_files_no_default() -> None:
    """A default with no window the operator could see is never filed."""
    action = _decide(_request(), auto_choose=AutoChoose.ALWAYS)

    assert action.default_on_timeout is None


@pytest.mark.parametrize("minutes", [1, MAX_OVERRIDE_WINDOW_MINUTES])
def test_surf_032_the_window_admits_one_minute_to_a_week(minutes: int) -> None:
    """Boundary: the shortest and the longest window are both admitted."""
    action = _decide(_request(override_window_minutes=minutes), auto_choose=AutoChoose.ALWAYS)

    assert action.override_until == AT + timedelta(minutes=minutes)


@pytest.mark.parametrize("minutes", [0, -5, MAX_OVERRIDE_WINDOW_MINUTES + 1, "30"])
def test_surf_032_the_window_refuses_nothing_negative_too_long_or_text(minutes: object) -> None:
    """Error path: an empty, negative, over-long or textual window does not validate."""
    with pytest.raises(ValidationError):
        _request(override_window_minutes=minutes)


def test_surf_032_the_host_shows_the_default_and_its_override_window() -> None:
    """The presented question names the default, when it stands and the policy behind it."""
    action = _decide(_request(override_window_minutes=90), auto_choose=AutoChoose.RECOMMENDED)
    presented = present_pending_action(action)

    assert "If nobody answers by 2026-09-18 13:30 universal time" in presented.question
    assert "'Commit on its own' stands" in presented.question
    assert f"{AUTO_CHOOSE_KEY}=recommended" in presented.question
    assert "Until then an answer overrides it." in presented.question


def test_surf_032_a_window_without_a_default_does_not_validate() -> None:
    """Error path: an override window means nothing without the default it overrides."""
    action = _decide(_request())
    payload = action.model_dump(mode="json") | {"override_until": AT.isoformat()}

    with pytest.raises(ValidationError, match="belong to a default_on_timeout"):
        PendingAction.model_validate(payload)


def test_surf_032_a_default_filed_without_its_window_is_not_presented() -> None:
    """A row carrying a default but no window is refused by the presenter, not hidden."""
    action = _decide(_request())
    unshown = PendingAction.model_validate(
        action.model_dump(mode="json") | {"default_on_timeout": "auto"}
    )

    with pytest.raises(QuestionPresentationError) as caught:
        present_pending_action(unshown)
    assert caught.value.code is PresentationRefusal.TIMEOUT_UNSHOWN


# ---------- SURF-038: configuration outranks the recommendation ----------


def test_surf_038_the_configured_value_picks_the_recommendation_and_says_so() -> None:
    """The asker recommends ``auto``; the configuration says ``ask``, so ``ask`` is recommended."""
    request = _request(
        config_axis={"key": "vcs.auto_commit", "values": {"ask": "ask", "auto": "auto"}}
    )
    action = _decide(request, configured="ask")

    assert action.recommended_option_id == "ask"
    assert action.recommendation_rationale is not None
    assert "vcs.auto_commit to ask" in action.recommendation_rationale
    assert present_pending_action(action).options[0].description is not None
    assert present_pending_action(action).options[0].description.startswith("Recommended.")


def test_surf_038_the_configured_default_follows_the_configured_value() -> None:
    """A permitted timeout default is the configured option, not the asker's."""
    request = _request(
        config_axis={"key": "vcs.auto_commit", "values": {"ask": "ask", "auto": "auto"}},
        override_window_minutes=15,
    )
    action = _decide(request, configured="ask", auto_choose=AutoChoose.RECOMMENDED)

    assert action.default_on_timeout == "ask"


def test_surf_038_a_configured_value_no_option_honours_is_refused() -> None:
    """Error path: recommending against the configuration would be a surface defect."""
    request = _request(
        config_axis={"key": "vcs.auto_commit", "values": {"ask": "ask", "auto": "auto"}}
    )

    with pytest.raises(DecisionRefusedError) as caught:
        _decide(request, configured="never")
    assert caught.value.code is DecisionRefusal.CONFIG_UNHONOURED


def test_surf_038_an_axis_nothing_sets_is_refused() -> None:
    """Error path: a leaf no layer sets is a preference nothing on the path would honour."""
    request = _request(config_axis={"key": "vcs.no_such_leaf", "values": {"ask": "ask"}})

    with pytest.raises(DecisionRefusedError) as caught:
        _decide(request, configured=None)
    assert caught.value.code is DecisionRefusal.AXIS_UNRESOLVED


def test_surf_038_an_axis_mapping_an_unoffered_option_is_refused() -> None:
    """Error path: the axis names an option the question does not offer."""
    request = _request(config_axis={"key": "vcs.auto_commit", "values": {"never": "never"}})

    with pytest.raises(DecisionRefusedError) as caught:
        _decide(request, configured="never")
    assert caught.value.code is DecisionRefusal.AXIS_OPTION_UNOFFERED


@pytest.mark.parametrize(
    "axis",
    [
        {"key": "auto_commit", "values": {"ask": "ask"}},
        {"key": "Vcs.Auto", "values": {"ask": "ask"}},
        {"key": "vcs.auto_commit", "values": {}},
    ],
)
def test_surf_038_an_axis_must_name_a_dotted_leaf_and_map_an_option(axis: dict[str, Any]) -> None:
    """Error path: an undotted or capitalised key, or an empty mapping, does not validate."""
    with pytest.raises(ValidationError):
        _request(config_axis=axis)
