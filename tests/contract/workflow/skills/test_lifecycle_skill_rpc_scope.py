"""Each lifecycle skill calls inside its declared RPC allowlist and nowhere else.

The allowlist is the skill's effects boundary: the closed set of daemon
verbs its catalog row grants it. Three properties have to hold together,
and each one alone is insufficient.

First, the allowlist must equal what the catalog row states. The expected
tuples below are the row transcribed, so widening a skill's scope in the
source without changing its row reds here.

Second, every allowlisted method must exist in the live daemon method
table. A row naming a verb nobody registered would otherwise read as a
grant and fail only at the first call, in production.

Third, a call outside the allowlist must be *refused*, not merely
unusual -- and refused before the transport is touched, so the request
never leaves the process. The recorder doubles below assert exactly that:
after a violation their call log is still empty.
"""

from __future__ import annotations

from typing import Any, Final

import pytest

import eawf.runtime.daemon.server  # noqa: F401  -- registers every daemon method
from eawf.runtime.daemon.methods import registered_methods
from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.engine import SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import (
    RpcRefusedError,
    RpcScope,
    RpcScopeViolationError,
)
from eawf.workflow.skills.registry import lookup

#: The coordinator row: coordinator read models plus the Run-dispatch verbs.
_DISPATCH_ROW: Final[tuple[str, ...]] = (
    "projection.batch.detail.read",
    "projection.task.detail.read",
    "projection.run.detail.read",
    "runtime.run.dispatch",
    "runtime.run.retry",
)

#: The integration row: candidate and integration-generation verbs, plus their reads.
_INTEGRATE_ROW: Final[tuple[str, ...]] = (
    "projection.batch.detail.read",
    "projection.merge.conflict.read",
    "runtime.candidate.report.bind",
    "runtime.delivery.assemble",
    "runtime.delivery.integrate",
)

#: The verification row: read and check effects plus the receipt-filing verbs.
_VERIFY_ROW: Final[tuple[str, ...]] = (
    "projection.batch.detail.read",
    "projection.evidence.read",
    "runtime.delivery.verify_batch",
    "runtime.delivery.assess_completion",
    "runtime.delivery.open_acceptance_approval",
)

#: Each skill module beside the allowlist its catalog row grants it.
_ROWS: Final = (
    ("/dispatch", dispatch_skill, _DISPATCH_ROW),
    ("/integrate", integrate_skill, _INTEGRATE_ROW),
    ("/verify", verify_skill, _VERIFY_ROW),
)

#: Every invocation shape that drives one branch of one skill to a terminal result.
_INVOCATIONS: Final[tuple[tuple[Any, dict[str, Any]], ...]] = (
    (dispatch_skill, {"batch_ref": "batch-1"}),
    (dispatch_skill, {"batch_ref": "batch-1", "resume": "run-1"}),
    (dispatch_skill, {"batch_ref": "batch-1", "task": ["task-a"], "dry_run": True}),
    (
        dispatch_skill,
        {
            "batch_ref": "batch-1",
            "task": ["task-a"],
            "run": "run-1",
            "run_request": {"base": "main"},
        },
    ),
    (integrate_skill, {"action": "show", "subject_ref": "batch-1"}),
    (integrate_skill, {"action": "seal", "subject_ref": "cand-1"}),
    (integrate_skill, {"action": "select", "subject_ref": "batch-1"}),
    (integrate_skill, {"action": "apply", "subject_ref": "batch-1"}),
    (integrate_skill, {"action": "retry", "subject_ref": "batch-1"}),
    (
        integrate_skill,
        {
            "action": "apply",
            "subject_ref": "batch-1",
            "base": {"head_sha": "a" * 40},
            "exit": {"repair_task": "task-9"},
            "diagnostic": "evidence-1",
        },
    ),
    (verify_skill, {"subject_ref": "batch-1", "mode": "audit"}),
    (verify_skill, {"subject_ref": "batch-1", "mode": "review"}),
    (verify_skill, {"subject_ref": "batch-1", "mode": "all"}),
    (verify_skill, {"subject_ref": "batch-1", "mode": "gates"}),
    (verify_skill, {"subject_ref": "batch-1", "mode": "security"}),
    (
        verify_skill,
        {
            "subject_ref": "batch-1",
            "mode": "all",
            "milestone": "milestone-1",
            "journey": [{"step_id": "AS-01"}],
            "accepted_binding": {"head_sha": "a" * 40},
            "requested_by": {"principal_kind": "human", "principal_id": "OP-0001"},
        },
    ),
)


class Recorder:
    """A transport double that records every call and answers emptily.

    Attributes:
        calls: The method names that reached the transport, in order.
    """

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[str] = []

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record one call and answer with an empty projection."""
        self.calls.append(method)
        return {"header": {"source_cursor": 0}, "rows": []}


class ExplodingCaller:
    """A transport double that must never be reached."""

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Fail the test if a call ever gets this far.

        Raises:
            AssertionError: Always. Reaching the transport means the
                allowlist was consulted after dispatch, not before.
        """
        raise AssertionError(f"{method} reached the transport past the allowlist")


def _run(module: Any, args: dict[str, Any], caller: Any) -> SkillResult:
    """Run one lifecycle skill's action with an injected transport."""
    skill_cls = lookup(module.MANIFEST.name)
    assert skill_cls is not None
    skill = skill_cls(caller=caller)  # type: ignore[call-arg]
    return skill.action(SkillContext(scope="scope", session="session", args=args))


# ---- the allowlist equals the catalog row ------------------------------------


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_rpc_allowlist_equals_the_catalog_row(name: str, module: Any, row: tuple[str, ...]) -> None:
    """The shipped allowlist is exactly the set the skill's catalog row grants."""
    assert module.RPC_SCOPE.skill == name
    assert module.RPC_SCOPE.methods == row


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_every_allowlisted_method_exists_in_the_live_method_table(
    name: str, module: Any, row: tuple[str, ...]
) -> None:
    """A granted verb nobody registered is a broken grant, not a future one."""
    live = set(registered_methods())
    missing = sorted(method for method in row if method not in live)
    assert not missing, f"{name} grants {missing}, which the daemon registers nowhere"


# ---- a call outside the allowlist is refused ---------------------------------


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_a_call_outside_the_allowlist_is_refused_before_the_transport(
    name: str, module: Any, row: tuple[str, ...]
) -> None:
    """The guard refuses first: an ungranted method never reaches the caller."""
    recorder = Recorder()

    with pytest.raises(RpcScopeViolationError) as caught:
        module.RPC_SCOPE.call(recorder, "state.mutate", {})

    assert recorder.calls == [], "the refusal must precede dispatch, not follow it"
    assert caught.value.skill == name
    assert caught.value.method == "state.mutate"
    assert caught.value.granted == row
    assert "state.mutate" in str(caught.value)


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_a_skill_refuses_every_method_granted_only_to_a_sibling(
    name: str, module: Any, row: tuple[str, ...]
) -> None:
    """The copy-paste defect: a sibling's grant is not this skill's grant."""
    siblings = {
        method
        for _other_name, _other_module, other_row in _ROWS
        for method in other_row
        if _other_name != name
    }
    foreign = sorted(siblings - set(row))
    assert foreign, "the three rows would otherwise be indistinguishable"
    for method in foreign:
        assert not module.RPC_SCOPE.grants(method)
        with pytest.raises(RpcScopeViolationError):
            module.RPC_SCOPE.call(ExplodingCaller(), method, {})


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_the_guard_reds_when_a_granted_method_is_dropped_from_the_scope(
    name: str, module: Any, row: tuple[str, ...]
) -> None:
    """Fire proof: the same call that passes on the shipped scope reds on a narrowed one."""
    granted = row[0]
    recorder = Recorder()
    module.RPC_SCOPE.call(recorder, granted, {})
    assert recorder.calls == [granted]

    narrowed = RpcScope(skill=name, methods=tuple(m for m in row if m != granted))
    with pytest.raises(RpcScopeViolationError):
        narrowed.call(ExplodingCaller(), granted, {})


def test_an_empty_allowlist_grants_nothing_and_a_single_entry_grants_only_itself() -> None:
    """Boundary: zero grants and exactly one grant both behave as declared."""
    empty = RpcScope(skill="/nothing", methods=())
    assert not empty.grants("daemon.ping")
    with pytest.raises(RpcScopeViolationError, match=r"grants \(none\)"):
        empty.call(ExplodingCaller(), "daemon.ping", {})

    single = RpcScope(skill="/one", methods=("daemon.ping",))
    assert single.grants("daemon.ping")
    assert not single.grants("daemon.pings")
    recorder = Recorder()
    single.call(recorder, "daemon.ping", {})
    assert recorder.calls == ["daemon.ping"]


# ---- what the skills actually call stays inside the allowlist -----------------


@pytest.mark.parametrize(("module", "args"), _INVOCATIONS)
def test_every_branch_calls_only_inside_its_allowlist(module: Any, args: dict[str, Any]) -> None:
    """Driving every branch never produces a call the row does not grant."""
    recorder = Recorder()
    result = _run(module, args, recorder)

    assert isinstance(result.body, dict)
    assert result.body["outcome"] in module.TERMINAL_OUTCOMES
    outside = sorted(set(recorder.calls) - set(module.RPC_SCOPE.methods))
    assert not outside, f"{module.MANIFEST.name} called {outside} outside its allowlist"


@pytest.mark.parametrize(("name", "module", "row"), _ROWS)
def test_a_daemon_refusal_surfaces_as_a_terminal_outcome_with_its_code(
    name: str, module: Any, row: tuple[str, ...]
) -> None:
    """A refused call ends the invocation on a declared outcome, never on a traceback."""

    def refusing(method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise RpcRefusedError(
            method=method, code="native_authority_required", detail="no native tree here"
        )

    args: dict[str, Any] = (
        {"batch_ref": "batch-1"}
        if module is dispatch_skill
        else {"action": "show", "subject_ref": "batch-1"}
        if module is integrate_skill
        else {"subject_ref": "batch-1", "mode": "all"}
    )
    result = _run(module, args, refusing)

    assert isinstance(result.body, dict)
    assert result.body["outcome"] in module.TERMINAL_OUTCOMES
    assert "native_authority_required" in str(result.body)
