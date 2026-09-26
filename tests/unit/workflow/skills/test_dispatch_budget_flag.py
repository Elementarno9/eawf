"""``/dispatch --budget`` wires the Run token ceiling; the P34 leftovers close.

``--budget`` was declared on :class:`DispatchArgs` but never read: the pass
always forwarded the presented ``--run-request`` capsule untouched, so an
operator naming a token ceiling on the invocation line watched it silently
dropped. This suite pins the fix -- a named budget overrides the presented
capsule's ``token_budget``, an omitted one leaves it exactly as compiled --
and confirms the idle-surface census no longer lists the three P34
surfaces this wave wires or removes.

No daemon, no socket, no filesystem beyond reading the repo's own source
tree for the census check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.workflow.skills.dispatch import RUN_DISPATCH_METHOD, DispatchArgs, DispatchSkill
from eawf.workflow.skills.engine import SkillContext
from tools.idle_surface_report import find_idle_functions

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SOURCE_ROOT = _REPO_ROOT / "src" / "eawf"
_CALLER_ROOTS = [_REPO_ROOT / "tools"]

#: The test-only / orphaned P34 surfaces this wave wires to a caller or
#: removes outright; none of them may read as idle once this wave lands.
_P34_LEFTOVER_NAMES = frozenset(
    {"assemble_verify_request", "assemble_completion_request", "route_claim_to_rung"}
)

_BATCH_REF = "BAT-0001"
_RUN_REF = "RUN-0001"
_TASK_REF = "TASK-0001"

#: The capsule's own compiled ceiling, present before any ``--budget`` override.
_CONFIGURED_DEFAULT = 999


class _RecordingCaller:
    """A minimal RpcCaller stand-in: answers every call with an empty row-set.

    Records every ``(method, params)`` pair it was handed so a test can
    inspect exactly what reached the transport, without a daemon socket.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((method, dict(params)))
        return {}


def _args(*, budget: int | None) -> dict[str, Any]:
    """Return a ``/dispatch`` invocation naming one Task, Run and compiled request."""
    return {
        "batch_ref": _BATCH_REF,
        "task": (_TASK_REF,),
        "run": _RUN_REF,
        "run_request": {"capsule": {"token_budget": _CONFIGURED_DEFAULT}},
        "budget": budget,
    }


def _dispatched_capsule(caller: _RecordingCaller) -> dict[str, Any]:
    """Return the capsule the recorder captured on the one dispatch call."""
    matches = [
        params["capsule"] for method, params in caller.calls if method == RUN_DISPATCH_METHOD
    ]
    assert len(matches) == 1, "expected exactly one runtime.run.dispatch call"
    return matches[0]


def test_dispatch_budget_flag_overrides_the_capsule_token_budget() -> None:
    """Gate-fire proof: ``--budget 5000`` sets the dispatched Run's token_budget to 5000."""
    caller = _RecordingCaller()

    result = DispatchSkill(caller=caller).action(
        SkillContext(scope="scope", session="session", args=_args(budget=5000))
    )

    assert isinstance(result.body, dict)
    assert _dispatched_capsule(caller)["token_budget"] == 5000


def test_dispatch_without_budget_leaves_the_configured_default() -> None:
    """Omitting ``--budget`` leaves the presented request's own compiled ceiling."""
    caller = _RecordingCaller()

    DispatchSkill(caller=caller).action(
        SkillContext(scope="scope", session="session", args=_args(budget=None))
    )

    assert _dispatched_capsule(caller)["token_budget"] == _CONFIGURED_DEFAULT


def test_dispatch_budget_of_zero_is_rejected() -> None:
    """Error path: a non-positive budget fails validation before any call is sent."""
    with pytest.raises(ValidationError):
        DispatchArgs.model_validate(_args(budget=0))


def test_dispatch_negative_budget_is_rejected() -> None:
    """Boundary: a negative budget is rejected the same as zero."""
    with pytest.raises(ValidationError):
        DispatchArgs.model_validate(_args(budget=-1))


def test_p34_leftover_surfaces_no_longer_read_as_idle() -> None:
    """The idle-surface census no longer lists the three names this wave closes."""
    idle = {name for name, _ in find_idle_functions(_SOURCE_ROOT, caller_roots=_CALLER_ROOTS)}

    assert not idle & _P34_LEFTOVER_NAMES
