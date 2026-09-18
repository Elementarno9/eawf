"""The three lifecycle skills carry a complete contract and no stub body.

Each of ``/dispatch``, ``/integrate`` and ``/verify`` must state four
things and honour them: the invocation grammar it accepts, the effects
boundary it may cross, the typed output schema it emits, and the closed
set of terminal outcomes it may end on. This module is the oracle for
all four, plus the registration surfaces that make the skills reachable
-- the canonical skill list, the body-model map and the rendered skill
registry the plugin trees are built from.

"No stub body" is checked by running each skill and validating what came
back, not by reading the source: a body that returned a placeholder
would fail the registered body model or carry an outcome outside the
declared set.
"""

from __future__ import annotations

from typing import Any, Final, get_args

import pytest
from pydantic import BaseModel, ValidationError

from eawf.surfaces.render.envelope import CANONICAL_SKILL_NAMES
from eawf.surfaces.render.skills import SKILL_REGISTRY
from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.bodies import SKILL_BODY_MODELS, body_model_for
from eawf.workflow.skills.bodies.dispatch import DispatchBody
from eawf.workflow.skills.bodies.integrate import IntegrateBody
from eawf.workflow.skills.bodies.verify import VerifyBody
from eawf.workflow.skills.engine import SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import (
    UNTYPED_REFUSAL_CODE,
    RpcCaller,
    refusal_code,
    row_keys,
    status_for,
)
from eawf.workflow.skills.registry import lookup

#: The three skill modules under test, with the body model each emits.
_LIFECYCLE: Final = (
    ("/dispatch", dispatch_skill, DispatchBody),
    ("/integrate", integrate_skill, IntegrateBody),
    ("/verify", verify_skill, VerifyBody),
)

#: A projection answer with no rows, which every read double falls back to.
_EMPTY_READ: Final[dict[str, Any]] = {"header": {"source_cursor": 11}, "rows": []}


class RecordingCaller:
    """A transport double that records what reached it and answers canned results.

    Attributes:
        calls: Every ``(method, params)`` pair the skill sent, in order.
    """

    def __init__(self, answers: dict[str, dict[str, Any]] | None = None) -> None:
        """Bind the canned answers this double replies with.

        Args:
            answers: Per-method result objects. A method with no entry
                answers with an empty projection.
        """
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._answers = answers or {}

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Record one call and answer it.

        Args:
            method: The dotted method name the skill addressed.
            params: The request params.

        Returns:
            The canned answer, or an empty projection.
        """
        self.calls.append((method, params))
        return self._answers.get(method, dict(_EMPTY_READ))

    def methods(self) -> list[str]:
        """Return just the method names that reached the transport."""
        return [method for method, _params in self.calls]


def _run(module: Any, args: dict[str, Any], caller: RpcCaller) -> SkillResult:
    """Run one lifecycle skill's action with an injected transport."""
    skill_cls = lookup(module.MANIFEST.name)
    assert skill_cls is not None, f"{module.MANIFEST.name} is not registered"
    skill = skill_cls(caller=caller)  # type: ignore[call-arg]
    return skill.action(SkillContext(scope="scope", session="session", args=args))


def _body(result: SkillResult, model: type[BaseModel]) -> BaseModel:
    """Validate a skill result's body against its registered model."""
    assert isinstance(result.body, dict), "a lifecycle body is always typed, never markdown"
    return model.model_validate(result.body)


# ---- the four declared halves of the contract --------------------------------


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_lifecycle_skill_declares_its_invocation_grammar(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """The grammar names the skill and every long flag the args model declares."""
    grammar = module.INVOCATION_GRAMMAR
    assert grammar.startswith(f"{name} "), f"{name} grammar must open with its own name"
    args_model = next(
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value.__name__.endswith("Args")
    )
    positional = {"batch_ref", "subject_ref", "action"}
    for field in args_model.model_fields:
        if field in positional or field == "repo_root":
            continue
        flag = f"--{field.replace('_', '-')}"
        assert flag in grammar, f"{name} declares {field!r} but its grammar omits {flag}"


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_lifecycle_skill_declares_its_effects_boundary(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """The effects sentence is present and the allowlist behind it is non-empty."""
    assert module.EFFECTS.strip(), f"{name} states no effects boundary"
    assert module.RPC_SCOPE.skill == name
    assert module.RPC_SCOPE.methods, f"{name} declares an empty RPC allowlist"


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_lifecycle_skill_declares_one_output_schema(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """The manifest, the module constant and the body discriminator agree."""
    assert module.MANIFEST.output_envelope_kind == module.OUTPUT_SCHEMA
    assert get_args(model.model_fields["kind"].annotation) == (module.OUTPUT_SCHEMA,)
    assert body_model_for(name) is model


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_lifecycle_skill_declares_closed_terminal_outcomes(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """Every declared outcome is a body-model outcome and projects onto a status."""
    declared = module.TERMINAL_OUTCOMES
    assert declared, f"{name} declares no terminal outcome"
    assert set(declared) == set(get_args(model.model_fields["outcome"].annotation))
    for outcome in declared:
        assert status_for(outcome) in {"ok", "needs_user", "blocked", "failed", "partial"}


# ---- registration surfaces ---------------------------------------------------


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_canonical_skill_list_names_the_lifecycle_skill(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """The canonical list, the body map and the render registry all carry the name."""
    assert name in CANONICAL_SKILL_NAMES
    assert name in SKILL_BODY_MODELS
    assert lookup(name) is not None
    rendered = {spec.skill_name for spec in SKILL_REGISTRY}
    assert name.removeprefix("/") in rendered


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_rendered_skill_body_states_grammar_effects_and_outcomes(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """The shipped skill body carries all four halves of the contract."""
    spec = next(s for s in SKILL_REGISTRY if s.skill_name == name.removeprefix("/"))
    assert "## Invocation" in spec.body
    assert "## Effects boundary" in spec.body
    assert "## Output contract" in spec.body
    assert module.OUTPUT_SCHEMA in spec.body
    for outcome in module.TERMINAL_OUTCOMES:
        assert f"`{outcome}`" in spec.body, f"{name} body omits terminal outcome {outcome!r}"


# ---- no stub body: run each skill and validate what came back ----------------


def test_dispatch_reports_an_empty_frontier_as_a_drained_batch() -> None:
    """A Batch with no PLANNED Task reads as frontier_empty, not as an error."""
    caller = RecordingCaller()
    result = _run(dispatch_skill, {"batch_ref": "batch-1"}, caller)

    body = _body(result, DispatchBody)
    assert isinstance(body, DispatchBody)
    assert body.outcome == "frontier_empty"
    assert body.source_cursor == 11
    assert body.reason
    assert result.status == "ok"


def test_dispatch_stops_for_an_operator_when_the_frontier_is_underivable() -> None:
    """A PLANNED Task with no readable dependency edge stops the pass, with a question."""
    caller = RecordingCaller(
        {
            dispatch_skill.TASK_READ_METHOD: {
                "header": {"source_cursor": 4},
                "rows": [
                    {"key": "task-a", "status": {"state": "known", "value": "PLANNED"}},
                    {"key": "task-b", "status": {"state": "known", "value": "RUNNING"}},
                ],
            }
        }
    )
    result = _run(dispatch_skill, {"batch_ref": "batch-1", "max_parallel": 1}, caller)

    body = _body(result, DispatchBody)
    assert isinstance(body, DispatchBody)
    assert body.outcome == "needs_operator"
    assert body.frontier == ["task-a"]
    assert body.plan.parallel == ["task-a"]
    assert body.stopped_on == ["dependency_proof_unreadable", "run_request_uncompilable"]
    assert body.user_question is not None
    assert result.status == "needs_user"


def test_dispatch_resume_reaches_the_run_retry_verb() -> None:
    """``--resume`` completes: the retry request needs the Run reference and nothing else."""
    caller = RecordingCaller({dispatch_skill.RUN_RETRY_METHOD: {"run_ref": "run-9"}})
    result = _run(dispatch_skill, {"batch_ref": "batch-1", "resume": "run-9"}, caller)

    body = _body(result, DispatchBody)
    assert isinstance(body, DispatchBody)
    assert dispatch_skill.RUN_RETRY_METHOD in caller.methods()
    assert [row.method for row in body.dispatched] == [dispatch_skill.RUN_RETRY_METHOD]
    assert body.dispatched[0].outcome == "retried"


def test_integrate_show_renders_batch_truth_without_mutating() -> None:
    """``show`` reads the Batch and its conflicts and addresses no mutating verb."""
    caller = RecordingCaller(
        {
            integrate_skill.BATCH_READ_METHOD: {
                "header": {"source_cursor": 2},
                "rows": [{"key": "gen-1", "status": {"state": "known", "value": "selected"}}],
            }
        }
    )
    result = _run(integrate_skill, {"action": "show", "subject_ref": "batch-1"}, caller)

    body = _body(result, IntegrateBody)
    assert isinstance(body, IntegrateBody)
    assert body.outcome == "shown"
    assert body.generation_ids == ["gen-1"]
    assert caller.methods() == [
        integrate_skill.BATCH_READ_METHOD,
        integrate_skill.CONFLICT_READ_METHOD,
    ]
    assert integrate_skill.DELIVERY_INTEGRATE_METHOD not in caller.methods()


@pytest.mark.parametrize(
    ("action", "code"),
    [
        ("select", "candidate_set_unreadable"),
        ("seal", "candidate_report_unbound"),
        ("apply", "integration_request_unnamed"),
        ("retry", "integration_request_unnamed"),
    ],
)
def test_integrate_names_the_request_fields_it_will_not_invent(action: str, code: str) -> None:
    """A branch whose request cannot be assembled stops and says which fields are missing."""
    caller = RecordingCaller()
    result = _run(integrate_skill, {"action": action, "subject_ref": "batch-1"}, caller)

    body = _body(result, IntegrateBody)
    assert isinstance(body, IntegrateBody)
    assert body.outcome == "blocked"
    assert body.refusal_code == code
    assert caller.calls == [], "a branch that cannot assemble its request sends nothing"
    if action != "select":
        assert body.unresolved_request_fields


def test_verify_walks_the_batch_cycle_and_derives_the_aggregate() -> None:
    """A cleared Batch reads as passed, and the rows the verdict rests on are carried."""
    caller = RecordingCaller(
        {
            verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {
                "head_generation": 3,
                "stage": "CLEARED",
                "blocking_criterion_ids": [],
                "settled_criterion_ids": ["CR-01"],
                "merge_ready": True,
                "reason": "every required criterion cleared on generation 3",
            }
        }
    )
    result = _run(verify_skill, {"subject_ref": "batch-1", "mode": "audit"}, caller)

    body = _body(result, VerifyBody)
    assert isinstance(body, VerifyBody)
    assert body.outcome == "passed"
    assert body.head_generation == 3
    assert [row.criterion_id for row in body.rows] == ["CR-01"]
    assert verify_skill.DELIVERY_VERIFY_BATCH_METHOD in caller.methods()
    assert result.status == "ok"


def test_verify_reports_a_blocking_criterion_as_unverified() -> None:
    """An open required criterion blocks, and the row carries no falsifier."""
    caller = RecordingCaller(
        {
            verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {
                "head_generation": 1,
                "stage": "CHECKING",
                "blocking_criterion_ids": ["CR-02"],
                "merge_ready": False,
                "reason": "one required criterion is open",
            }
        }
    )
    result = _run(verify_skill, {"subject_ref": "batch-1", "mode": "all"}, caller)

    body = _body(result, VerifyBody)
    assert isinstance(body, VerifyBody)
    assert body.outcome == "unverified"
    assert body.rows[0].verdict == "unverified"
    assert body.rows[0].falsifier == ""
    assert result.status == "blocked"


@pytest.mark.parametrize(
    ("mode", "code"),
    [("gates", "proof_receipts_unpresented"), ("security", "security_verification_unbound")],
)
def test_verify_mode_that_reaches_no_verb_reports_unverified(mode: str, code: str) -> None:
    """Absence of evidence reads as unverified, which blocks, and sends no request."""
    caller = RecordingCaller()
    result = _run(verify_skill, {"subject_ref": "batch-1", "mode": mode}, caller)

    body = _body(result, VerifyBody)
    assert isinstance(body, VerifyBody)
    assert body.outcome == "unverified"
    assert body.refusal_code == code
    assert caller.calls == []


# ---- boundary and error paths ------------------------------------------------


@pytest.mark.parametrize(
    ("module", "args"),
    [
        (dispatch_skill, {"batch_ref": ""}),
        (dispatch_skill, {"batch_ref": "b", "undeclared": True}),
        (dispatch_skill, {"batch_ref": "b", "max_parallel": 0}),
        (dispatch_skill, {"batch_ref": "b", "max_parallel": 65}),
        (dispatch_skill, {}),
        (integrate_skill, {"action": "show", "subject_ref": ""}),
        (integrate_skill, {"action": "fold", "subject_ref": "b"}),
        (integrate_skill, {"action": "show", "subject_ref": "b", "undeclared": 1}),
        (verify_skill, {"subject_ref": ""}),
        (verify_skill, {"subject_ref": "b", "mode": "vibes"}),
        (verify_skill, {"subject_ref": "b", "agents": 0}),
        (verify_skill, {"subject_ref": "b", "agents": 9}),
    ],
)
def test_lifecycle_skill_refuses_an_undeclared_invocation(
    module: Any, args: dict[str, Any]
) -> None:
    """An invocation the grammar does not declare is refused before any call."""
    caller = RecordingCaller()
    result = _run(module, args, caller)

    assert isinstance(result.body, dict)
    assert result.body["outcome"] == "blocked"
    assert result.body["reason"].startswith("the invocation does not parse")
    assert caller.calls == []
    assert result.repair_commands == [module.INVOCATION_GRAMMAR]


def test_row_keys_reads_an_empty_a_single_and_a_filtered_projection() -> None:
    """Row extraction holds at empty, at one row, and across a status filter."""
    assert row_keys({}) == []
    assert row_keys({"rows": []}) == []
    single = {"rows": [{"key": "a", "status": {"state": "known", "value": "PLANNED"}}]}
    assert row_keys(single) == ["a"]
    assert row_keys(single, status="PLANNED") == ["a"]
    assert row_keys(single, status="RUNNING") == []
    unknown = {"rows": [{"key": "b", "status": {"state": "unavailable", "value": None}}]}
    assert row_keys(unknown) == ["b"]
    assert row_keys(unknown, status="PLANNED") == []
    assert row_keys({"rows": [{"status": {"state": "known", "value": "PLANNED"}}]}) == []


def test_status_for_rejects_an_outcome_no_lifecycle_skill_declares() -> None:
    """An invented terminal outcome is a bug, not a sixth envelope status."""
    with pytest.raises(KeyError, match="unknown lifecycle terminal outcome"):
        status_for("mostly_fine")


def test_refusal_code_reads_a_typed_refusal_and_degrades_on_anything_else() -> None:
    """A typed refusal yields its code; a fault yields the untyped sentinel."""
    assert refusal_code("validation_failed: integration_workspace_absent: no tree") == (
        "integration_workspace_absent"
    )
    assert refusal_code("validation_failed: ") == UNTYPED_REFUSAL_CODE
    assert refusal_code("validation_failed: only_a_code") == UNTYPED_REFUSAL_CODE
    assert refusal_code("") == UNTYPED_REFUSAL_CODE
    assert refusal_code("internal error") == UNTYPED_REFUSAL_CODE


def test_body_model_for_rejects_a_name_no_skill_owns() -> None:
    """The body map is closed; an unknown name raises rather than returning a default."""
    with pytest.raises(KeyError, match="unknown skill"):
        body_model_for("/integrate-but-faster")


@pytest.mark.parametrize(("name", "module", "model"), _LIFECYCLE)
def test_lifecycle_body_forbids_an_unmodeled_key(
    name: str, module: Any, model: type[BaseModel]
) -> None:
    """A drifted body key fails validation rather than riding the envelope."""
    minimal: dict[str, Any] = {"outcome": module.TERMINAL_OUTCOMES[-1], "reason": "r"}
    if model is DispatchBody:
        minimal["batch_ref"] = "b"
    else:
        minimal["subject_ref"] = "b"
    if model is IntegrateBody:
        minimal["action"] = "show"
    if model is VerifyBody:
        minimal["mode"] = "all"
    model.model_validate(minimal)
    with pytest.raises(ValidationError):
        model.model_validate({**minimal, "surprise": 1})
