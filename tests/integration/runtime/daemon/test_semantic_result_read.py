"""An answer outlives the connection that asked for it.

``semantic.result.read`` serves the receipt of a completed call by its
call identity, and it reads it from the ledger rather than from anything
a live process remembers, so the suite asserts it against a context built
after the call was made.

A denied receipt has to be actionable without being dangerous. It names
the check that refused, a code from the closed set, and the retry class
that code implies -- and it offers no shell command as a remedy, because
a remedy the worker can paste into a shell routes around the broker,
which is the one thing the gateway exists to prevent. The shell shapes
are asserted directly against the rendered message rather than trusted to
the model that rejects them, since it is the rendered message a worker
reads.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Final

import pytest

from eawf.kernel.runtime.semantic import RETRY_CLASS_BY_CODE, RetryClass, SemanticToolErrorCode
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.semantic import SEMANTIC_RESULT_READ_METHOD
from eawf.runtime.daemon.semantic_gateway import PreHandlerCheck
from tests.integration.runtime.daemon.test_semantic_gateway_guards import (
    RUN_URN,
    WALL_CEILING,
    bind,
    budget_payload,
    call_verb,
    candidate_payload,
    invoke,
    make_canary,
    method_ctx,
    seal_call,
    seal_capsule,
)

pytestmark = pytest.mark.integration


#: Shapes that would make a remedy a shell command rather than a reason.
SHELL_SHAPES: Final[tuple[str, ...]] = ("`", "$(", "&&", "||", "sudo ", "; ", "| ")


@pytest.fixture
def runtime_root(tmp_path: Path) -> Path:
    """The daemon runtime directory the native WAL is namespaced under."""
    return tmp_path / "runtime"


@pytest.fixture
def canary(tmp_path: Path) -> CanaryProvision:
    """A canary holding one running Run that started five seconds ago."""
    return make_canary(tmp_path / "repo")


@pytest.fixture
def ctx(runtime_root: Path) -> MethodContext:
    """A daemon context with a WAL directory of its own."""
    return method_ctx(runtime_root)


def read_result(ctx: MethodContext, canary: CanaryProvision, call_id: str) -> dict[str, Any]:
    """Read one receipt back through the registered verb."""
    return call_verb(
        SEMANTIC_RESULT_READ_METHOD,
        ctx,
        repo_root=str(canary.root),
        run_ref=RUN_URN,
        call_id=call_id,
    )


def test_a_completed_call_is_served_by_its_call_id(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    served = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    read = read_result(ctx, canary, served["call_id"])

    assert read == served
    assert read["result"]["status"] == "succeeded"
    assert read["result"]["bounded_output"]["quality"] == "measured"
    assert read["result"]["bounded_output"]["remaining"]["wall_seconds"] <= WALL_CEILING


def test_a_receipt_outlives_the_context_that_wrote_it(
    canary: CanaryProvision, ctx: MethodContext, tmp_path: Path
) -> None:
    """The answer is a ledger line, so a fresh daemon context still serves it."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    served = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    later = method_ctx(tmp_path / "another-runtime")

    assert read_result(later, canary, served["call_id"]) == served


def test_a_denied_receipt_names_the_check_the_code_and_the_remedy_class(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    denied = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="submit_candidate",
            payload=candidate_payload(changed_paths=("tests/unit/escape.py",)),
        ),
        capsule,
    )

    read = read_result(ctx, canary, denied["call_id"])

    assert read["refused_check"] == PreHandlerCheck.SCOPE.value
    assert read["result"]["error"]["code"] == SemanticToolErrorCode.SCOPE_DENIED.value
    assert read["result"]["error"]["retry_class"] == RetryClass.NEVER.value
    assert read["result"]["bounded_output"] is None


def test_a_denied_receipt_offers_no_shell_command_as_a_remedy(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Three different checks refuse, and not one names a shell."""
    denied_capsule = seal_capsule(tool_denials=("budget_status",))
    bind(ctx, canary, denied_capsule)
    cases = (
        ("budget_status", budget_payload(), "denied-1"),
        ("submit_candidate", candidate_payload(changed_paths=("tests/escape.py",)), "denied-2"),
        (
            "workspace_apply_patch",
            {
                "tool_id": "workspace_apply_patch",
                "lease_id": "lease-0123456789abcdef",
                "expected_workspace_generation": 1,
                "patch_ref": "artifact://patch/one",
                "patch_digest": f"sha256:{'2' * 64}",
            },
            "denied-3",
        ),
    )
    refusing: list[str] = []

    for ordinal, (tool_id, payload, key) in enumerate(cases, start=1):
        answer = invoke(
            ctx,
            canary,
            seal_call(
                capsule=denied_capsule,
                tool_id=tool_id,
                payload=payload,
                key=key,
                ordinal=ordinal,
            ),
            denied_capsule,
        )
        read = read_result(ctx, canary, answer["call_id"])
        message = read["result"]["error"]["message"]
        assert read["result"]["status"] == "denied"
        refusing.append(read["refused_check"])
        for shape in SHELL_SHAPES:
            assert shape not in message, f"{tool_id} remedy carries {shape!r}"

    assert refusing == [
        PreHandlerCheck.DENIAL.value,
        PreHandlerCheck.SCOPE.value,
        PreHandlerCheck.LEASE.value,
    ]


def test_every_refusal_code_the_gateway_uses_carries_its_declared_retry_class(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The class travels on the wire, recomputed from the code every time."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="workspace_apply_patch",
            payload={
                "tool_id": "workspace_apply_patch",
                "lease_id": "lease-0123456789abcdef",
                "expected_workspace_generation": 1,
                "patch_ref": "artifact://patch/one",
                "patch_digest": f"sha256:{'2' * 64}",
            },
        ),
        capsule,
    )

    read = read_result(ctx, canary, answer["call_id"])
    code = SemanticToolErrorCode(read["result"]["error"]["code"])

    assert read["result"]["error"]["retry_class"] == RETRY_CLASS_BY_CODE[code].value


def test_a_call_this_run_never_made_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        read_result(ctx, canary, f"call-{0:016x}")


def test_a_receipt_of_another_run_is_not_served_by_this_one(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """Receipts are read under the Run that made the call and no other."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    served = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload()),
        capsule,
    )

    with pytest.raises(DaemonValidationError, match="identity_not_found"):
        call_verb(
            SEMANTIC_RESULT_READ_METHOD,
            ctx,
            repo_root=str(canary.root),
            run_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000099",
            call_id=served["call_id"],
        )


def test_a_call_id_outside_the_grammar_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="call_id"):
        read_result(ctx, canary, "call-not-hex")


def test_an_unknown_parameter_is_refused(canary: CanaryProvision, ctx: MethodContext) -> None:
    with pytest.raises(DaemonValidationError, match="schema_validation_failed"):
        call_verb(
            SEMANTIC_RESULT_READ_METHOD,
            ctx,
            repo_root=str(canary.root),
            run_ref=RUN_URN,
            call_id=f"call-{1:016x}",
            elevated=True,
        )


def test_a_run_reference_that_is_not_a_run_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    with pytest.raises(DaemonValidationError, match="run_ref"):
        call_verb(
            SEMANTIC_RESULT_READ_METHOD,
            ctx,
            repo_root=str(canary.root),
            run_ref="eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/task/EAWF-0042",
            call_id=f"call-{1:016x}",
        )
