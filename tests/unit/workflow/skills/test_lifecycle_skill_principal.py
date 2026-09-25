"""Gate-fire proof: the lifecycle skills send a principal the daemon accepts.

``/dispatch`` and ``/verify`` each attribute one daemon-mutating call to an
actor. The daemon's ``PrincipalKey`` grammar is uppercase-anchored and
admits no ``/``, so a skill's own canonical name -- ``self.name``, e.g.
``/dispatch`` -- never validates. This module proves each skill's
attributed call carries a principal the validator accepts, and that the
failure mode this wave fixes -- sending ``self.name`` -- is a real red
against that same validator rather than a tautology.

``/integrate`` sends no daemon-mutating call today: every write branch
(``select``, ``seal``, ``apply``, ``retry``) stops before the transport is
touched because the request it would send cannot be assembled from the
grammar (see ``test_integrate_names_the_request_fields_it_will_not_invent``
in ``test_lifecycle_skill_contracts.py``). There is therefore no principal
for ``/integrate`` to get wrong yet; this module asserts that absence
holds rather than fabricating a call the skill does not make.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

from eawf.kernel.state.epoch2.base import PrincipalKey
from eawf.workflow.skills import dispatch as dispatch_skill
from eawf.workflow.skills import integrate as integrate_skill
from eawf.workflow.skills import verify as verify_skill
from eawf.workflow.skills.engine import SkillContext, SkillResult
from eawf.workflow.skills.lifecycle_rpc import RpcCaller
from eawf.workflow.skills.registry import lookup


class _PrincipalProbe(BaseModel):
    """The one field the daemon's principal validator actually checks."""

    model_config = ConfigDict(extra="forbid")

    actor: PrincipalKey


class RecordingCaller:
    """A transport double that records the params of every call it answers.

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
        """Record one call and answer it."""
        self.calls.append((method, dict(params)))
        return self._answers.get(method, {"header": {"source_cursor": 0}, "rows": []})

    def params_for(self, method: str) -> dict[str, Any]:
        """Return the params of the one call made to *method*.

        Raises:
            AssertionError: *method* was called zero or more than once.
        """
        matches = [params for called, params in self.calls if called == method]
        assert len(matches) == 1, f"expected exactly one call to {method}, got {len(matches)}"
        return matches[0]


def _run(module: Any, args: dict[str, Any], caller: RpcCaller) -> SkillResult:
    """Run one lifecycle skill's action with an injected transport."""
    skill_cls = lookup(module.MANIFEST.name)
    assert skill_cls is not None, f"{module.MANIFEST.name} is not registered"
    skill = skill_cls(caller=caller)  # type: ignore[call-arg]
    return skill.action(SkillContext(scope="scope", session="session", args=args))


# ---- the two skills that reach a daemon-mutating verb today -------------------


def test_dispatch_resume_sends_an_actor_the_principal_validator_accepts() -> None:
    """The retry request's ``actor`` field validates as a ``PrincipalKey``."""
    caller = RecordingCaller({dispatch_skill.RUN_RETRY_METHOD: {"run_ref": "run-9"}})
    _run(dispatch_skill, {"batch_ref": "batch-1", "resume": "run-9"}, caller)

    actor = caller.params_for(dispatch_skill.RUN_RETRY_METHOD)["actor"]
    _PrincipalProbe.model_validate({"actor": actor})


def test_verify_cycle_sends_an_actor_the_principal_validator_accepts() -> None:
    """The verify-batch request's ``actor`` field validates as a ``PrincipalKey``."""
    caller = RecordingCaller(
        {
            verify_skill.DELIVERY_VERIFY_BATCH_METHOD: {
                "head_generation": 1,
                "stage": "CHECKING",
                "blocking_criterion_ids": [],
                "merge_ready": True,
                "reason": "cleared",
            }
        }
    )
    _run(verify_skill, {"subject_ref": "batch-1", "mode": "all"}, caller)

    actor = caller.params_for(verify_skill.DELIVERY_VERIFY_BATCH_METHOD)["actor"]
    _PrincipalProbe.model_validate({"actor": actor})


def test_integrate_reaches_no_verb_that_would_need_a_principal() -> None:
    """``/integrate`` sends no daemon-mutating call today, so no principal to break.

    If a future wave wires ``apply``/``retry``/``seal`` to their verbs,
    this red is the signal that those branches now need an accepted
    ``actor`` too.
    """
    caller = RecordingCaller()
    _run(integrate_skill, {"action": "apply", "subject_ref": "batch-1"}, caller)

    assert caller.calls == []


# ---- gate-fire proof: the pre-wave shape is a real red, not a tautology -------


@pytest.mark.parametrize(
    ("module", "verb_attr"),
    [
        (dispatch_skill, "RUN_RETRY_METHOD"),
        (verify_skill, "DELIVERY_VERIFY_BATCH_METHOD"),
    ],
)
def test_the_skills_own_name_reds_the_principal_probe(module: Any, verb_attr: str) -> None:
    """``self.name`` -- what each skill sent before this wave -- fails validation.

    It is lowercase and carries a leading ``/``, which the daemon's
    uppercase-anchored ``PrincipalKey`` pattern refuses on both counts.
    """
    skill_cls = lookup(module.MANIFEST.name)
    assert skill_cls is not None
    with pytest.raises(ValidationError):
        _PrincipalProbe.model_validate({"actor": skill_cls.name})


# ---- boundary and error paths of the validator this proof relies on -----------


@pytest.mark.parametrize(
    "invalid",
    [
        "",  # empty: below the pattern's minimum length
        "dispatch",  # lowercase: fails the leading-uppercase anchor
        "/dispatch",  # exactly what self.name sent before this wave
        "S",  # single character: below the pattern's minimum length
        "S" * 33,  # 33 characters: past the pattern's maximum length
        "SKILL DISPATCH",  # space: outside the admitted alphabet
    ],
)
def test_principal_probe_rejects_every_shape_the_daemon_refuses(invalid: str) -> None:
    """Error path: the validator this proof leans on actually rejects the bad shapes."""
    with pytest.raises(ValidationError):
        _PrincipalProbe.model_validate({"actor": invalid})


@pytest.mark.parametrize(
    "valid",
    [
        "SK",  # boundary: the shortest accepted key, two characters
        "SKILL-DISPATCH",
        "SKILL-VERIFY",
        "A" + "9" * 31,  # boundary: the longest accepted key, 32 characters
    ],
)
def test_principal_probe_accepts_every_shape_the_daemon_accepts(valid: str) -> None:
    """Boundary: the validator accepts the daemon's true grammar at both length ends."""
    _PrincipalProbe.model_validate({"actor": valid})
