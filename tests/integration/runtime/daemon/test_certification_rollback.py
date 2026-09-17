"""Rolling a profile back to the last tuple that actually passed certify.

The subject is how far a rollback reaches. A quarantine takes the profile
out of service at once; the rollback that follows walks it back to the
newest pin whose tuple is still good, and what that changes is the next
Run the compiler seals. A Run compiled before the rollback keeps the
certification it was compiled against, because re-pointing a sealed spec
would run it under authority nobody compiled.

The refusals are the other half. A profile with no pin, and a profile
whose only pin is the tuple that just tripped, both come back refused and
stay disabled: the compiler then has no eligible profile at all, which is
the shape "never select an uncertified tuple" takes at the seam where a
Run would otherwise be compiled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.compiled import CompiledRunSpec, canonical_digest
from eawf.kernel.state.enums import StoreKind
from eawf.kernel.store.envelope import Envelope
from eawf.kernel.store.paths import store_path
from eawf.runtime.daemon import PROTOCOL_VERSION
from eawf.runtime.daemon.methods import MethodContext
from eawf.runtime.daemon.methods.conformance import (
    StageParams,
    canary,
    certify,
    probe,
    quarantine,
    rollback,
)
from eawf.runtime.runtimes.conformance import RuntimeTuple
from eawf.runtime.runtimes.containment import CONTAINMENT_ATTEMPTS, DenialReason
from eawf.workflow.runtime.compile import CompileRejection, RunCompileError, compile_run_spec
from tests import _provider_helpers as fx

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
FAR_FUTURE = NOW + timedelta(days=3650)
CANARY_RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
EVIDENCE_REF = "artifact://conformance/codex-2026-09-17"
BUNDLE_REF = "artifact://conformance/bundle-2026-09-17"
GOOD_REF = "certification://codex-app-server/2026-08"
TRIPPED_REF = "certification://codex-app-server/2026-09"
PROFILE_ID = "fixture_codex"

#: Tokens a live codex binary advertises that satisfy the matrix rules for
#: ``tool_use`` and ``streaming`` while showing no resume surface, which is
#: what the matrix declares for ``session_resume``.
OBSERVED_FLAGS = ("mcp", "exec")

#: The matrix rows that carry a codex probe rule, so a verdict about them
#: rests on evidence rather than on the declaration under test.
EVIDENCED_CAPABILITIES = ("tool_use", "streaming", "session_resume")


def runtime_tuple(*, distribution_version: str) -> RuntimeTuple:
    """Return the fixture tuple at one installed distribution version."""
    return RuntimeTuple.model_validate(
        {
            "manifest_ref": fx.DRIVER_REF,
            "manifest_digest": fx.digest("a"),
            "distribution_version": distribution_version,
            "sdk_or_server_version": "1.9.0",
            "auth_kind": "subscription",
            "model_family": "gpt-5",
            "os_class": "macos",
            "architecture": "aarch64",
            "managed_profile_digest": fx.digest("e"),
            "conformance_suite_version": "1.0.0",
        }
    )


GOOD_TUPLE = runtime_tuple(distribution_version="0.44.0")
TRIPPED_TUPLE = runtime_tuple(distribution_version="0.45.0")


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    """Return the state path of a throwaway tree."""
    return tmp_path / ".ea" / "state.json"


@pytest.fixture
def ctx(state_path: Path) -> MethodContext:
    """Return a daemon context bound to the throwaway tree."""
    return MethodContext(
        started_at=NOW.isoformat(),
        pid=4242,
        protocol_version=PROTOCOL_VERSION,
        version="test",
        state_path=state_path,
    )


def contained() -> list[dict[str, Any]]:
    """Return a denied outcome for every attempt the probe set owes."""
    return [
        {"attempt": attempt.value, "denied": True, "denial_reason": DenialReason.SCOPE_ESCAPE.value}
        for attempt in CONTAINMENT_ATTEMPTS
    ]


def call(handler: Any, ctx: MethodContext, request: dict[str, Any]) -> dict[str, Any]:
    """Invoke one conformance verb over the wire shape and return its result."""
    params = StageParams(request=request).model_dump(mode="json")
    result: dict[str, Any] = asyncio.run(handler(ctx, params))
    return result


def compiled(
    *,
    certification_ref: str = fx.CERTIFICATION_REF,
    certification_digest: str = fx.digest("c"),
    profiles: list[dict[str, Any]] | None = None,
    run_ref: str = fx.RUN_URN,
) -> CompiledRunSpec:
    """Compile one Run against *profiles* and the named certification."""
    return compile_run_spec(
        fx.task_request(run_ref=run_ref),
        configuration=fx.configuration(profiles=profiles),
        bindings=[
            fx.binding(
                certification_ref=certification_ref, certification_digest=certification_digest
            )
        ],
        compiled_at=fx.COMPILED_AT,
    )


def certify_tuple(
    ctx: MethodContext,
    *,
    covered: RuntimeTuple,
    certification_id: str,
    profile_ids: tuple[str, ...] = (PROFILE_ID,),
) -> dict[str, Any]:
    """Walk *covered* through probe, canary and certify, and pin the result."""
    served = compiled()
    assert (
        call(
            probe,
            ctx,
            {
                "runtime_tuple": covered.model_dump(mode="json"),
                "runtime_id": "codex",
                "installed": True,
                "observed_flags": list(OBSERVED_FLAGS),
                "required_capabilities": list(EVIDENCED_CAPABILITIES),
                "evidence_ref": EVIDENCE_REF,
            },
        )["record"]["outcome"]
        == "passed"
    )
    assert (
        call(
            canary,
            ctx,
            {
                "runtime_tuple": covered.model_dump(mode="json"),
                "served_spec": served.model_dump(mode="json"),
                "canary_spec": compiled(run_ref=CANARY_RUN_URN).model_dump(mode="json"),
                "outcomes": contained(),
                "evidence_ref": EVIDENCE_REF,
            },
        )["record"]["outcome"]
        == "passed"
    )
    return call(
        certify,
        ctx,
        {
            "runtime_tuple": covered.model_dump(mode="json"),
            "protocol": {
                "worker_protocol_version": "2.1.0",
                "semantic_protocol_version": "1.0.0",
                "event_codec_version": "1.0.0",
            },
            "certification_id": certification_id,
            "capabilities": [
                {
                    "capability_id": "semantic_tools",
                    "level": True,
                    "status": "verified",
                    "basis": "native",
                    "evidence_ref": EVIDENCE_REF,
                    "verified_at": NOW.isoformat(),
                    "expires_at": FAR_FUTURE.isoformat(),
                }
            ],
            "runtime_facts": {
                "context_window_tokens": 200_000,
                "auto_compaction_threshold_tokens": 150_000,
                "project_document_cap_bytes": 32_768,
                "tool_output_cap_tokens": 25_000,
                "measured_at": NOW.isoformat(),
                "measurement_method": "observed",
            },
            "install_trust": "managed",
            "evidence_bundle_ref": BUNDLE_REF,
            "expires_at": FAR_FUTURE.isoformat(),
            "profile_ids": list(profile_ids),
        },
    )


def quarantine_tuple(
    ctx: MethodContext, *, covered: RuntimeTuple, certification: dict[str, Any]
) -> dict[str, Any]:
    """Trip *covered* on a canary failure and take its dependents down."""
    return call(
        quarantine,
        ctx,
        {
            "runtime_tuple": covered.model_dump(mode="json"),
            "trigger": "canary_failure",
            "certification": certification,
            "profiles": [fx.profile_document()],
            "routes": [fx.task_route_document()],
            "evidence_ref": EVIDENCE_REF,
        },
    )


def roll_profile_back(ctx: MethodContext, profile: dict[str, Any]) -> dict[str, Any]:
    """Ask the daemon to walk *profile* back to its last-known-good pin."""
    return call(rollback, ctx, {"profile": profile, "evidence_ref": EVIDENCE_REF})


def journal_rows(state_path: Path) -> list[Envelope]:
    """Return every envelope in the tree's conformance journal."""
    path = store_path(state_path, StoreKind.CONFORMANCE_STAGE)
    if not path.exists():
        return []
    return [
        Envelope.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def two_certified_tuples(ctx: MethodContext) -> tuple[dict[str, Any], dict[str, Any]]:
    """Certify the good tuple and then the one that later trips."""
    good = certify_tuple(ctx, covered=GOOD_TUPLE, certification_id="codex-app-server-2026-08")
    tripped = certify_tuple(ctx, covered=TRIPPED_TUPLE, certification_id="codex-app-server-2026-09")
    return good["certification"], tripped["certification"]


# ---- the rollback reaches the next compiled Run and no further ---------------


def test_rollback_binds_the_previous_certification_for_the_next_run(
    ctx: MethodContext,
) -> None:
    """The next compiled Run names the pinned certification, not the tripped one."""
    good, tripped = two_certified_tuples(ctx)
    in_flight = compiled(
        certification_ref=TRIPPED_REF, certification_digest=canonical_digest(tripped)
    )
    disabled = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)

    result = roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    pin = result["selection"]["pin"]
    next_run = compiled(
        certification_ref=GOOD_REF,
        certification_digest=canonical_digest(pin["certification"]),
        profiles=[result["profile"]],
    )
    assert pin["certification"]["certification_id"] == good["certification_id"]
    assert next_run.certification_digest == canonical_digest(good)
    assert next_run.certification_digest != in_flight.certification_digest


def test_rollback_leaves_the_already_compiled_run_untouched(ctx: MethodContext) -> None:
    """A Run sealed before the rollback keeps the contract it was compiled under."""
    _, tripped = two_certified_tuples(ctx)
    in_flight = compiled(
        certification_ref=TRIPPED_REF, certification_digest=canonical_digest(tripped)
    )
    before = in_flight.model_dump_json()
    disabled = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)

    result = roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    assert result["applies_to"] == "next_compiled_run"
    assert in_flight.model_dump_json() == before
    assert CompiledRunSpec.model_validate_json(before) == in_flight
    assert in_flight.certification_ref == TRIPPED_REF


def test_rollback_re_enables_the_profile_at_the_next_revision(ctx: MethodContext) -> None:
    """The profile comes back enabled and distinguishable from the disabled copy."""
    _, tripped = two_certified_tuples(ctx)
    disabled = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)
    off = disabled["disabled"]["profiles"][0]

    result = roll_profile_back(ctx, off)

    assert off["disabled"] is True
    assert result["profile"]["disabled"] is False
    assert result["profile"]["revision"] == off["revision"] + 1


def test_rollback_selects_only_a_certification_still_verified(ctx: MethodContext) -> None:
    """The bound pin is a verified certification of a tuple out of quarantine."""
    _, tripped = two_certified_tuples(ctx)
    disabled = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)

    result = roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    pinned = result["selection"]["pin"]["certification"]
    assert pinned["overall_status"] == "verified"
    assert pinned["install_trust"] == "managed"
    assert result["selection"]["rejected"] == [TRIPPED_TUPLE.tuple_digest]


def test_rollback_records_its_stage_under_the_restored_tuple(
    ctx: MethodContext, state_path: Path
) -> None:
    """The quarantine files under the tripped tuple, the rollback under the good one."""
    _, tripped = two_certified_tuples(ctx)
    disabled = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)

    roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    rows = [row for row in journal_rows(state_path) if row.payload["stage"] == "rollback"]
    assert [(row.scope_id, row.payload["outcome"]) for row in rows] == [
        (TRIPPED_TUPLE.tuple_digest, "failed"),
        (GOOD_TUPLE.tuple_digest, "passed"),
    ]


# ---- a profile with no admissible pin stays disabled -------------------------


def test_profile_with_no_pin_is_refused_and_stays_disabled(ctx: MethodContext) -> None:
    """Boundary: no pin at all refuses rather than binding something."""
    _, tripped = two_certified_tuples(ctx)
    quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)
    stranger = fx.profile_document(profile_id="fixture_claude", disabled=True)

    result = roll_profile_back(ctx, stranger)

    assert result["selection"]["pin"] is None
    assert result["selection"]["refusal"] == "no_last_known_good_pin"
    assert result["profile"] is None
    assert result["record"] is None


def test_profile_whose_only_pin_is_quarantined_is_refused(ctx: MethodContext) -> None:
    """Boundary: a single pin, and the quarantine took it, leaves nothing good."""
    certified = certify_tuple(
        ctx, covered=TRIPPED_TUPLE, certification_id="codex-app-server-2026-09"
    )
    disabled = quarantine_tuple(
        ctx, covered=TRIPPED_TUPLE, certification=certified["certification"]
    )

    result = roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    assert result["selection"]["refusal"] == "no_last_known_good_pin"
    assert result["selection"]["rejected"] == [TRIPPED_TUPLE.tuple_digest]
    assert result["profile"] is None


def test_a_refused_rollback_leaves_the_compiler_with_no_eligible_profile(
    ctx: MethodContext,
) -> None:
    """The disabled profile is what the compiler sees, so the next Run refuses."""
    certified = certify_tuple(
        ctx, covered=TRIPPED_TUPLE, certification_id="codex-app-server-2026-09"
    )
    disabled = quarantine_tuple(
        ctx, covered=TRIPPED_TUPLE, certification=certified["certification"]
    )
    off = disabled["disabled"]["profiles"][0]

    refused = roll_profile_back(ctx, off)

    assert refused["profile"] is None
    with pytest.raises(RunCompileError) as caught:
        compiled(profiles=[off])
    assert caught.value.code is CompileRejection.NO_ELIGIBLE_PROFILE


def test_a_certify_that_names_no_profile_pins_nothing(ctx: MethodContext) -> None:
    """Boundary: an empty profile list leaves the profile with no pin."""
    certified = certify_tuple(
        ctx,
        covered=GOOD_TUPLE,
        certification_id="codex-app-server-2026-08",
        profile_ids=(),
    )
    disabled = quarantine_tuple(ctx, covered=GOOD_TUPLE, certification=certified["certification"])

    result = roll_profile_back(ctx, disabled["disabled"]["profiles"][0])

    assert result["selection"]["refusal"] == "no_last_known_good_pin"
    assert result["selection"]["rejected"] == []


# ---- what the quarantine verb takes down ------------------------------------


def test_quarantine_disables_the_profile_and_the_route_that_needs_it(
    ctx: MethodContext,
) -> None:
    """Dispatch goes down with the tuple, on the profile and on the route."""
    _, tripped = two_certified_tuples(ctx)

    result = quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=tripped)

    assert [row["profile_id"] for row in result["disabled"]["profiles"]] == [PROFILE_ID]
    assert [row["route_id"] for row in result["disabled"]["routes"]] == ["mutating-task"]
    assert result["certification"]["install_trust"] == "quarantined"


def test_quarantine_refuses_a_certification_of_another_tuple(ctx: MethodContext) -> None:
    """A certification that covers another driver cannot quarantine this one."""
    good, _ = two_certified_tuples(ctx)
    foreign = {**good, "manifest_ref": fx.OTHER_DRIVER_REF}

    with pytest.raises(ValidationError, match="certification covers"):
        quarantine_tuple(ctx, covered=TRIPPED_TUPLE, certification=foreign)


def test_rollback_refuses_a_request_that_is_not_a_profile(ctx: MethodContext) -> None:
    """Error path: the wire shape is validated before the runner is reached."""
    with pytest.raises(ValidationError):
        roll_profile_back(ctx, {"profile_id": PROFILE_ID})


def test_rollback_refuses_an_unknown_request_key(ctx: MethodContext) -> None:
    """Error path: an unknown key is refused rather than ignored."""
    with pytest.raises(ValidationError):
        call(
            rollback,
            ctx,
            {
                "profile": fx.profile_document(),
                "evidence_ref": EVIDENCE_REF,
                "force": True,
            },
        )
