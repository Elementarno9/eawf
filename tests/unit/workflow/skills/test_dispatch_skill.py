"""``/dispatch`` sends each ready Task to the daemon's Task dispatch verb.

With no ``--run-request`` the coordinator no longer stops for a compiled request
it cannot build: every Task on the first stage of the derived plan is sent to
``runtime.task.dispatch`` by key, and the report lists each in ``dispatched``.
A presented ``--run-request`` stays the explicit override over
``runtime.run.dispatch``. Every case drives the skill through a fake RPC caller,
so what reached the transport is read back exactly; no daemon runs.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills.bodies.dispatch import DispatchBody
from eawf.workflow.skills.engine import SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import RpcRefusedError

BATCH: Final = "BAT-0001"

#: The refusal code the fake daemon answers a refused Task with.
SETTLED: Final = "dispatch_task_settled"

#: The stop code the pass used to raise when it could not compile a request.
OLD_STOP: Final = "run_request_uncompilable"


def task_row(key: str, status: str = "PLANNED", **facts: str) -> dict[str, Any]:
    """Return one Task read-model row carrying its graph facts."""
    return {"key": key, "status": {"state": "known", "value": status}, "facts": facts}


class FakeCaller:
    """A transport double: answers reads, dispatches Tasks, refuses the ones named.

    Attributes:
        calls: Every ``(method, params)`` pair the skill sent, in order.
    """

    def __init__(self, rows: list[dict[str, Any]], *, refuse: tuple[str, ...] = ()) -> None:
        """Serve *rows* as the Task read model and refuse dispatching each Task in *refuse*."""
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._rows = rows
        self._refuse = refuse

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        if method == dispatch_skill.TASK_READ_METHOD:
            return {"header": {"source_cursor": 7}, "rows": self._rows}
        if method == dispatch_skill.TASK_DISPATCH_METHOD:
            if params["task"] in self._refuse:
                raise RpcRefusedError(method=method, code=SETTLED, detail="settled")
            return {"run_ref": f"run-of-{params['task']}", "stage": "announced"}
        return {"header": {"source_cursor": 7}, "rows": []}

    def sent(self, method: str) -> list[dict[str, Any]]:
        """Return the params of every call to *method*."""
        return [params for name, params in self.calls if name == method]


def run(caller: FakeCaller, **args: Any) -> tuple[SkillResult, DispatchBody]:
    """Run one pass over the Batch and return its result and validated body."""
    result = dispatch_skill.DispatchSkill(caller=caller).action(
        SkillContext(scope="s", session="s", args={"batch_ref": BATCH, **args})
    )
    return result, DispatchBody.model_validate(result.body)


# ---- the frontier goes to the Task dispatch verb ---------------------------------


def test_each_first_stage_task_is_sent_to_the_task_dispatch_verb() -> None:
    caller = FakeCaller(
        [
            task_row("EAWF-0001"),
            task_row("EAWF-0002"),
            task_row("EAWF-0003", depends_on="EAWF-0001"),
            task_row("EAWF-0004", "RUNNING"),
        ]
    )

    result, body = run(caller)

    sent = caller.sent(dispatch_skill.TASK_DISPATCH_METHOD)
    assert [params["task"] for params in sent] == ["EAWF-0001", "EAWF-0002"]
    assert {params["actor"] for params in sent} == {"SKILL-DISPATCH"}
    assert caller.sent(dispatch_skill.RUN_DISPATCH_METHOD) == []
    assert [(row.task_ref, row.run_ref, row.outcome) for row in body.dispatched] == [
        ("EAWF-0001", "run-of-EAWF-0001", "dispatched"),
        ("EAWF-0002", "run-of-EAWF-0002", "dispatched"),
    ]
    assert {row.method for row in body.dispatched} == {dispatch_skill.TASK_DISPATCH_METHOD}
    assert body.outcome == "frontier_empty"
    assert body.frontier == []
    assert OLD_STOP not in body.stopped_on
    assert "2 dispatched" in body.reason
    assert "1 wait on earlier stages" in body.reason
    assert result.status == "ok"


def test_a_single_ready_task_is_sent_alone() -> None:
    caller = FakeCaller([task_row("EAWF-0001")])

    _, body = run(caller)

    assert [row.task_ref for row in body.dispatched] == ["EAWF-0001"]


def test_provider_and_budget_reach_each_task_dispatch() -> None:
    caller = FakeCaller([task_row("EAWF-0001"), task_row("EAWF-0002")])

    run(caller, provider="codex", budget=5000, repo_root="/tree")

    for params in caller.sent(dispatch_skill.TASK_DISPATCH_METHOD):
        assert params["provider"] == "codex"
        assert params["budget"] == 5000
        assert params["repo_root"] == "/tree"


def test_omitted_provider_and_budget_are_left_to_the_daemon() -> None:
    caller = FakeCaller([task_row("EAWF-0001")])

    run(caller)

    (params,) = caller.sent(dispatch_skill.TASK_DISPATCH_METHOD)
    assert "provider" not in params
    assert "budget" not in params


def test_task_narrows_the_frontier_that_is_sent() -> None:
    caller = FakeCaller([task_row("EAWF-0001"), task_row("EAWF-0002")])

    _, body = run(caller, task=["EAWF-0002"])

    assert [p["task"] for p in caller.sent(dispatch_skill.TASK_DISPATCH_METHOD)] == ["EAWF-0002"]
    assert body.outcome == "frontier_empty"


def test_an_empty_frontier_sends_nothing() -> None:
    caller = FakeCaller([task_row("EAWF-0001", "COMPLETED")])

    _, body = run(caller)

    assert caller.sent(dispatch_skill.TASK_DISPATCH_METHOD) == []
    assert body.dispatched == []
    assert body.outcome == "frontier_empty"


# ---- one refusal does not stop the stage --------------------------------------------


def test_a_refused_task_is_reported_and_the_rest_still_go_out() -> None:
    caller = FakeCaller([task_row("EAWF-0001"), task_row("EAWF-0002")], refuse=("EAWF-0001",))

    result, body = run(caller)

    assert [p["task"] for p in caller.sent(dispatch_skill.TASK_DISPATCH_METHOD)] == [
        "EAWF-0001",
        "EAWF-0002",
    ]
    refused, dispatched = body.dispatched
    assert (refused.task_ref, refused.outcome, refused.refusal_code) == (
        "EAWF-0001",
        "refused",
        SETTLED,
    )
    assert (dispatched.task_ref, dispatched.outcome) == ("EAWF-0002", "dispatched")
    assert body.outcome == "blocked"
    assert body.stopped_on == [SETTLED]
    assert body.frontier == ["EAWF-0001"]
    assert result.status == "blocked"


# ---- the explicit override and the dry run ------------------------------------------


def test_a_presented_run_request_overrides_the_task_dispatch() -> None:
    caller = FakeCaller([task_row("EAWF-0001"), task_row("EAWF-0002")])

    _, body = run(caller, task=["EAWF-0001"], run="RUN-0001", run_request={"capsule": {}})

    assert caller.sent(dispatch_skill.TASK_DISPATCH_METHOD) == []
    (params,) = caller.sent(dispatch_skill.RUN_DISPATCH_METHOD)
    assert params["urn"] == "RUN-0001"
    assert [(row.task_ref, row.method) for row in body.dispatched] == [
        ("EAWF-0001", dispatch_skill.RUN_DISPATCH_METHOD)
    ]
    assert body.outcome == "frontier_empty"


def test_a_run_request_beside_no_single_task_sends_nothing() -> None:
    caller = FakeCaller([task_row("EAWF-0001"), task_row("EAWF-0002")])

    result, body = run(caller, run="RUN-0001", run_request={"capsule": {}})

    assert caller.sent(dispatch_skill.TASK_DISPATCH_METHOD) == []
    assert caller.sent(dispatch_skill.RUN_DISPATCH_METHOD) == []
    assert body.outcome == "needs_operator"
    assert body.stopped_on == ["run_request_unaddressed"]
    assert body.user_question is not None
    assert result.status == "needs_user"


def test_a_dry_run_sends_nothing_and_stops_on_the_frontier() -> None:
    caller = FakeCaller([task_row("EAWF-0001")])

    _, body = run(caller, dry_run=True)

    assert caller.sent(dispatch_skill.TASK_DISPATCH_METHOD) == []
    assert body.outcome == "needs_operator"
    assert body.frontier == ["EAWF-0001"]
    assert body.stopped_on == ["dry_run"]


def test_a_resume_dispatches_no_frontier() -> None:
    caller = FakeCaller([task_row("EAWF-0001")])

    run(caller, resume="RUN-0009")

    assert caller.sent(dispatch_skill.TASK_DISPATCH_METHOD) == []
    assert len(caller.sent(dispatch_skill.RUN_RETRY_METHOD)) == 1


# ---- the grammar and the allowlist ---------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        pytest.param({"provider": "opencode"}, id="undriven-provider"),
        pytest.param({"budget": 0}, id="zero-budget"),
        pytest.param({"parallel": 2}, id="undeclared-option"),
    ],
)
def test_an_undeclared_invocation_sends_nothing(args: dict[str, Any]) -> None:
    caller = FakeCaller([task_row("EAWF-0001")])

    _, body = run(caller, **args)

    assert caller.calls == []
    assert body.outcome == "blocked"
    assert body.stopped_on == ["invocation_undeclared"]


def test_the_allowlist_grants_the_task_dispatch_verb() -> None:
    assert dispatch_skill.TASK_DISPATCH_METHOD in dispatch_skill.RPC_SCOPE.methods
