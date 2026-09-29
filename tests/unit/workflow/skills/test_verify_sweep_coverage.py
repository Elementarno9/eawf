"""SURF-092: a verification sweep reports what it covered and what it did not.

"Recheck deeply" is answered by the report's own rows rather than by a
second pass: every criterion the walk reached is a row, a criterion the
walk left open reads ``unverified`` instead of disappearing, and a mode
that reaches no verb names the request fields it could not resolve.
"""

from __future__ import annotations

from typing import Any

import pytest

from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.bodies.verify import VerifyBody
from eawf.workflow.skills.engine import SkillContext

pytestmark = pytest.mark.unit


def _run(args: dict[str, Any], answers: dict[str, dict[str, Any]]) -> VerifyBody:
    skill = verify_skill.VerifySkill(caller=lambda method, _params: answers.get(method, {}))
    result = skill.action(SkillContext(scope="s", session="s", args=args))
    return VerifyBody.model_validate(result.body)


def test_surf_092_sweep_rows_cover_both_the_settled_and_the_open_criteria() -> None:
    body = _run(
        {"subject_ref": "BAT-0001"},
        {
            verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {
                "blocking_criterion_ids": ["CR-001"],
                "settled_criterion_ids": ["CR-002"],
                "merge_ready": False,
            }
        },
    )
    verdicts = {row.criterion_id: row.verdict for row in body.rows}
    assert verdicts == {"CR-001": "unverified", "CR-002": "passed"}
    assert body.blocking_criterion_ids == ["CR-001"]
    assert body.outcome == "unverified"


def test_surf_092_empty_walk_reports_no_row_rather_than_a_pass() -> None:
    body = _run(
        {"subject_ref": "BAT-0001"},
        {verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {"merge_ready": False}},
    )
    assert body.rows == []
    assert body.outcome == "unverified"


def test_surf_092_mode_reaching_no_verb_names_what_it_did_not_cover() -> None:
    body = _run({"subject_ref": "BAT-0001", "mode": "gates"}, {})
    assert body.unresolved_request_fields
    assert body.outcome == "unverified"
