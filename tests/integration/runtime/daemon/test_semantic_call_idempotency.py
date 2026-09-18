"""RUN-012: one key answers one request, and a replay adds no effect.

The suite drives the ``duplicate-call`` conformance fixture through the
registered ``semantic.call`` verb. The fixture is three calls sharing one
idempotency key: the request, its exact replay, and the same key carrying
a different payload. What the gateway must do with each is stated in the
fixture rather than only in the assertions, so the same three-call shape
can be replayed against any certified runtime later.

"One effect" is counted rather than assumed. The receipt ledger is read
before and after every call, so a replay that quietly filed a second
receipt fails here even though its answer would look identical.

The refusal is the interesting half. A reused key carrying another
payload is not a duplicate: it is a second request wearing the first
one's name, and answering it from the first receipt would report an
effect that never happened for input nobody ever ran.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from eawf.kernel.runtime.semantic import RETRY_CLASS_BY_CODE, SemanticToolErrorCode
from eawf.platform.install.canary import CanaryProvision
from eawf.runtime.daemon.methods import DaemonValidationError, MethodContext
from eawf.runtime.daemon.methods.semantic import SEMANTIC_CALL_METHOD
from eawf.runtime.daemon.semantic_gateway import PreHandlerCheck
from tests.integration.runtime.daemon.test_semantic_gateway_guards import (
    bind,
    budget_payload,
    call_verb,
    invoke,
    load_fixture,
    make_canary,
    method_ctx,
    receipt_lines,
    seal_call,
    seal_capsule,
)

pytestmark = pytest.mark.integration


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


def test_the_duplicate_call_fixture_states_three_calls_and_three_answers() -> None:
    fixture = load_fixture("duplicate-call")

    assert fixture.fixture_id == "duplicate-call"
    assert len(fixture.calls) == 3
    assert {call.idempotency_key for call in fixture.calls} == {"duplicate-call-01"}
    assert [answer.verdict for answer in fixture.expected] == ["served", "replayed", "refused"]


def test_the_duplicate_call_fixture_replays_to_one_effect(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The fixture walked end to end through the wired verb."""
    fixture = load_fixture("duplicate-call")
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    answers = []
    counts = []
    for ordinal, call in enumerate(fixture.calls, start=1):
        answers.append(
            invoke(
                ctx,
                canary,
                seal_call(
                    capsule=capsule,
                    tool_id=call.tool_id.value,
                    payload=call.payload,
                    key=call.idempotency_key,
                    ordinal=ordinal,
                ),
                capsule,
            )
        )
        counts.append(receipt_lines(canary, runtime_root))

    served, replayed, refused = answers
    assert served["result"]["status"] == "succeeded"
    assert replayed == served
    assert refused["result"]["status"] == "denied"
    assert refused["refused_check"] == PreHandlerCheck.IDEMPOTENCY.value
    assert counts == [1, 1, 2]


def test_a_replay_returns_the_original_call_and_receipt_identity(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    """The second call carries its own identity and is answered by the first."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    first = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="one"),
        capsule,
    )

    second = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(),
            key="one",
            ordinal=2,
        ),
        capsule,
    )

    assert second["call_id"] == first["call_id"]
    assert second["receipt_id"] == first["receipt_id"]
    assert second["result"]["completed_at"] == first["result"]["completed_at"]


def test_a_reused_key_with_another_payload_fails_closed(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="one"),
        capsule,
    )

    answer = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(include_children=True),
            key="one",
            ordinal=2,
        ),
        capsule,
    )

    code = SemanticToolErrorCode.IDEMPOTENCY_PAYLOAD_MISMATCH
    assert answer["result"]["error"]["code"] == code.value
    assert answer["result"]["error"]["retry_class"] == RETRY_CLASS_BY_CODE[code].value
    assert answer["result"]["error"]["field_path"] == "/payload_digest"
    assert receipt_lines(canary, runtime_root) == 2


def test_two_keys_over_one_payload_are_two_requests(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The key is the identity, so the same payload twice is two effects."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    first = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="one"),
        capsule,
    )

    second = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(),
            key="two",
            ordinal=2,
        ),
        capsule,
    )

    assert second["call_id"] != first["call_id"]
    assert receipt_lines(canary, runtime_root) == 2


def test_a_refusal_ahead_of_the_key_check_is_decided_again(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The replay exit sits eighth, so an earlier refusal never reaches it.

    Repeating a call the grant check already refused is refused again on
    its own merits and files its own denied receipt. That is the honest
    reading of the declared order and it costs nothing: a refusal has no
    effect to repeat, so two denials are two records of one answer rather
    than two effects.
    """
    capsule = seal_capsule(tool_grants=())
    bind(ctx, canary, capsule)
    first = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="one"),
        capsule,
    )

    second = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(),
            key="one",
            ordinal=2,
        ),
        capsule,
    )

    assert first["refused_check"] == PreHandlerCheck.GRANT.value
    assert second["refused_check"] == first["refused_check"]
    assert second["result"]["error"] == first["result"]["error"]
    assert second["call_id"] != first["call_id"]
    assert receipt_lines(canary, runtime_root) == 2


def test_an_earlier_check_answers_before_the_key_is_consulted(
    tmp_path: Path, ctx: MethodContext, runtime_root: Path
) -> None:
    """Idempotency sits eighth, so a budget-exhausted replay is not a replay."""
    provisioned = make_canary(tmp_path / "repo", started_delta=timedelta(seconds=5))
    capsule = seal_capsule()
    bind(ctx, provisioned, capsule)
    invoke(
        ctx,
        provisioned,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key="one"),
        capsule,
    )
    exhausted = make_canary(tmp_path / "later", started_delta=timedelta(hours=2))
    bind(ctx, exhausted, capsule)

    answer = invoke(
        ctx,
        exhausted,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(),
            key="one",
            ordinal=2,
        ),
        capsule,
    )

    assert answer["refused_check"] == PreHandlerCheck.BUDGET.value
    assert receipt_lines(provisioned, runtime_root) == 1


def test_an_empty_idempotency_key_never_reaches_the_gateway(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The grammar refuses an unnamed request at the envelope boundary."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    envelope = seal_call(
        capsule=capsule, tool_id="budget_status", payload=budget_payload()
    ).model_dump(mode="json")
    envelope["idempotency_key"] = ""

    with pytest.raises(DaemonValidationError, match="idempotency_key"):
        call_verb(
            SEMANTIC_CALL_METHOD,
            ctx,
            repo_root=str(canary.root),
            call=envelope,
            capsule=capsule.model_dump(mode="json"),
        )
    assert receipt_lines(canary, runtime_root) == 0


def test_the_longest_admitted_key_still_replays(
    canary: CanaryProvision, ctx: MethodContext, runtime_root: Path
) -> None:
    """The boundary of the key grammar: 128 characters, replayed once."""
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    key = "k" * 128
    first = invoke(
        ctx,
        canary,
        seal_call(capsule=capsule, tool_id="budget_status", payload=budget_payload(), key=key),
        capsule,
    )

    second = invoke(
        ctx,
        canary,
        seal_call(
            capsule=capsule,
            tool_id="budget_status",
            payload=budget_payload(),
            key=key,
            ordinal=2,
        ),
        capsule,
    )

    assert second == first
    assert receipt_lines(canary, runtime_root) == 1


def test_a_key_one_character_past_the_grammar_is_refused(
    canary: CanaryProvision, ctx: MethodContext
) -> None:
    capsule = seal_capsule()
    bind(ctx, canary, capsule)
    envelope = seal_call(
        capsule=capsule, tool_id="budget_status", payload=budget_payload()
    ).model_dump(mode="json")
    envelope["idempotency_key"] = "k" * 129

    with pytest.raises(DaemonValidationError, match="idempotency_key"):
        call_verb(
            SEMANTIC_CALL_METHOD,
            ctx,
            repo_root=str(canary.root),
            call=envelope,
            capsule=capsule.model_dump(mode="json"),
        )
