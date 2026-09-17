"""The probe, canary and certify stage machine over the daemon's journal.

A certification exists only at the end of a walk the store can show: the
three ``conformance.*`` verbs append one stage record each under the
runtime tuple's digest, and the record the certify verb returns cites
exactly the probe and canary rows that preceded it.

Certify refuses rather than writes in the two cases that would certify
something nobody observed: while a canary for the same tuple is in
flight, and when any capability evidence it would cite is past its
``expires_at``. Both refusals come back as typed stage results with their
reason code, and neither leaves a certification behind. Walking the
machine out of order -- a canary with no passed probe, a certify with no
passed canary, a second certify -- raises instead, because that is a
caller error rather than a verdict about the tuple.
"""

from __future__ import annotations

import ast
import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from eawf.kernel.runtime.certification import (
    STAGE_ORDER,
    CapabilityCertification,
    CertificationFailureCode,
    CertifiedRuntimeFacts,
    DriverCertification,
)
from eawf.kernel.runtime.compiled import (
    UNSEALED_DIGEST,
    CompiledCapability,
    CompiledRunSpec,
    ResolvedFieldSource,
)
from eawf.kernel.runtime.provider import AgentProviderProfile, AuthorityGrant
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
    runner_for,
)
from eawf.runtime.runtimes.conformance import (
    CanaryRequest,
    CertificationRequest,
    ConformanceSequenceError,
    ProbeRequest,
    ProtocolFacts,
    RuntimeTuple,
    certifiable_stages,
)
from eawf.runtime.runtimes.containment import (
    CONTAINMENT_ATTEMPTS,
    ContainmentAttempt,
    ContainmentCallOutcome,
    DenialReason,
)
from tests import _provider_helpers as fx

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 17, 12, 0, tzinfo=UTC)
CANARY_RUN_URN = "eawf://WSP-MAIN/PRJ-EAWF/REP-EAWF/run/RUN-00000011"
EVIDENCE_REF = "artifact://conformance/claude-2026-09-17"
BUNDLE_REF = "artifact://conformance/bundle-2026-09-17"

#: Flags a live claude-code binary advertises that satisfy the matrix
#: rules for ``tool_use`` and ``streaming`` while showing no resume
#: surface, which is what the matrix declares for ``session_resume``.
OBSERVED_FLAGS = ("--allowedTools", "--output-format")

#: The three matrix rows that carry a probe rule, so a verdict about them
#: can rest on evidence instead of on the declaration under test.
EVIDENCED_CAPABILITIES = ("tool_use", "streaming", "session_resume")


def runtime_tuple(**overrides: Any) -> RuntimeTuple:
    """Return the fixture runtime tuple with *overrides* applied."""
    document: dict[str, Any] = {
        "manifest_ref": "driver://claude-sdk/v1",
        "manifest_digest": fx.digest("a"),
        "distribution_version": "2.1.0",
        "sdk_or_server_version": "1.9.0",
        "auth_kind": "subscription",
        "model_family": "claude-opus",
        "os_class": "macos",
        "architecture": "aarch64",
        "managed_profile_digest": fx.digest("e"),
        "conformance_suite_version": "1.0.0",
    }
    document.update(overrides)
    return RuntimeTuple.model_validate(document)


def spec(**overrides: Any) -> CompiledRunSpec:
    """Return a sealed compiled spec with *overrides* applied."""
    profile = AgentProviderProfile.model_validate(fx.profile_document())
    fields: dict[str, Any] = {
        "run_ref": fx.RUN_URN,
        "run_scope": fx.task_request().run_scope,
        "purpose": "implement",
        "agent_role": "executor",
        "route_policy_ref": "route://mutating-task",
        "route_policy_revision": 3,
        "profile_ref": "profile://fixture_codex",
        "profile_revision": 1,
        "driver_manifest_ref": fx.DRIVER_REF,
        "driver_manifest_digest": fx.digest("a"),
        "certification_ref": fx.CERTIFICATION_REF,
        "certification_digest": fx.digest("c"),
        "auth_profile_ref": fx.AUTH_REF,
        "auth_kind": "subscription",
        "model_id": fx.CERTIFIED_MODEL,
        "capabilities": (
            CompiledCapability.model_validate(
                {
                    **fx.observation("semantic_tools").model_dump(),
                    "requested": "required",
                    "requested_level": True,
                }
            ),
        ),
        "limits": profile.limits,
        "sandbox": profile.sandbox,
        "environment": profile.environment,
        "session": profile.session,
        "stream": profile.stream,
        "authority": AuthorityGrant.model_validate(profile.authority_ceiling.model_dump()),
        "tool_policy": profile.tool_policy,
        "context_policy": profile.context_policy,
        "provider_options": profile.provider_options,
        "source_map": (
            ResolvedFieldSource(
                field_path="/model_id",
                effective_value_digest=UNSEALED_DIGEST,
                source_layer="workspace",
                source_ref=fx.WORKSPACE_SOURCE,
                merge_operation="replace",
            ),
        ),
        "compiled_at": fx.COMPILED_AT,
        "compiler_version": "1.0.0",
    }
    fields.update(overrides)
    return CompiledRunSpec.seal(fields)


def contained() -> tuple[ContainmentCallOutcome, ...]:
    """Return a denied outcome for every containment attempt."""
    return tuple(
        ContainmentCallOutcome(
            attempt=attempt, denied=True, denial_reason=DenialReason.SCOPE_ESCAPE
        )
        for attempt in CONTAINMENT_ATTEMPTS
    )


def capability(**overrides: Any) -> CapabilityCertification:
    """Return a verified, unexpired capability certification row."""
    document: dict[str, Any] = {
        "capability_id": "tool_use",
        "level": True,
        "status": "verified",
        "basis": "native",
        "evidence_ref": EVIDENCE_REF,
        "verified_at": NOW,
        "expires_at": NOW + timedelta(days=3650),
    }
    document.update(overrides)
    return CapabilityCertification.model_validate(document)


def runtime_facts() -> CertifiedRuntimeFacts:
    """Return measured caps of the certified runtime."""
    return CertifiedRuntimeFacts(
        context_window_tokens=200_000,
        auto_compaction_threshold_tokens=150_000,
        project_document_cap_bytes=32_768,
        tool_output_cap_tokens=25_000,
        measured_at=NOW,
        measurement_method="observed",
    )


def probe_request(**overrides: Any) -> ProbeRequest:
    """Return a probe request whose live facts clear the matrix."""
    document: dict[str, Any] = {
        "runtime_tuple": runtime_tuple(),
        "runtime_id": "claude-code",
        "installed": True,
        "observed_flags": OBSERVED_FLAGS,
        "required_capabilities": EVIDENCED_CAPABILITIES,
        "evidence_ref": EVIDENCE_REF,
    }
    document.update(overrides)
    return ProbeRequest.model_validate(document)


def canary_request(**overrides: Any) -> CanaryRequest:
    """Return a canary request at parity with the spec it stands for."""
    document: dict[str, Any] = {
        "runtime_tuple": runtime_tuple(),
        "served_spec": spec(),
        "canary_spec": spec(run_ref=CANARY_RUN_URN),
        "outcomes": contained(),
        "evidence_ref": EVIDENCE_REF,
    }
    document.update(overrides)
    return CanaryRequest.model_validate(document)


def certification_request(**overrides: Any) -> CertificationRequest:
    """Return a certification request over unexpired evidence."""
    document: dict[str, Any] = {
        "runtime_tuple": runtime_tuple(),
        "protocol": ProtocolFacts(
            worker_protocol_version="2.1.0",
            semantic_protocol_version="1.0.0",
            event_codec_version="1.0.0",
        ),
        "certification_id": "claude-sdk-2026-09",
        "capabilities": (capability(),),
        "runtime_facts": runtime_facts(),
        "install_trust": "managed",
        "evidence_bundle_ref": BUNDLE_REF,
        "expires_at": NOW + timedelta(days=3650),
    }
    document.update(overrides)
    return CertificationRequest.model_validate(document)


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


def call(handler: Any, ctx: MethodContext, request: Any) -> dict[str, Any]:
    """Invoke one stage verb over the wire shape and return its result."""
    params = StageParams(request=request.model_dump(mode="json")).model_dump(mode="json")
    result: dict[str, Any] = asyncio.run(handler(ctx, params))
    return result


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


def walk_to_canary(ctx: MethodContext) -> None:
    """Drive the tuple through a passed probe and a passed canary."""
    assert call(probe, ctx, probe_request())["record"]["outcome"] == "passed"
    assert call(canary, ctx, canary_request())["record"]["outcome"] == "passed"


# ---- the closed sequence ----------------------------------------------------


def test_the_three_stages_append_contiguously_for_one_tuple(
    ctx: MethodContext, state_path: Path
) -> None:
    """Probe, canary and certify land as three rows under the tuple digest."""
    walk_to_canary(ctx)

    result = call(certify, ctx, certification_request())

    rows = journal_rows(state_path)
    assert [row.payload["stage"] for row in rows] == ["probe", "canary", "certify"]
    assert {row.scope_id for row in rows} == {runtime_tuple().tuple_digest}
    assert {row.kind for row in rows} == {StoreKind.CONFORMANCE_STAGE}
    assert [row.payload["outcome"] for row in rows] == ["passed"] * 3
    assert result["record"]["outcome"] == "passed"


def test_certify_cites_exactly_the_stages_that_earned_it(ctx: MethodContext) -> None:
    """The written record's history is the walk, in stage order."""
    walk_to_canary(ctx)

    result = call(certify, ctx, certification_request())

    record = DriverCertification.model_validate(result["certification"])
    assert tuple(row.stage for row in record.stage_history) == STAGE_ORDER[:3]
    assert record.overall_status == "verified"
    assert record.certification_id == "claude-sdk-2026-09"


def test_stage_records_are_append_only(ctx: MethodContext, state_path: Path) -> None:
    """A later stage adds bytes and rewrites none of the earlier ones."""
    walk_to_canary(ctx)
    path = store_path(state_path, StoreKind.CONFORMANCE_STAGE)
    before = path.read_bytes()

    call(certify, ctx, certification_request())

    after = path.read_bytes()
    assert after.startswith(before)
    assert len(after) > len(before)


def test_each_tuple_keeps_its_own_history(ctx: MethodContext) -> None:
    """A second tuple is a second certification, never an edit of the first."""
    other = runtime_tuple(architecture="x86_64")
    walk_to_canary(ctx)
    call(probe, ctx, probe_request(runtime_tuple=other))

    runner = runner_for(ctx.state_path)

    assert [row.stage for row in runner.stage_history(runtime_tuple())] == ["probe", "canary"]
    assert [row.stage for row in runner.stage_history(other)] == ["probe"]


# ---- the two refusals -------------------------------------------------------


def test_certify_is_refused_while_a_canary_for_the_tuple_runs(
    ctx: MethodContext, state_path: Path
) -> None:
    """A tuple under canary cannot certify from the canary before it."""
    walk_to_canary(ctx)
    runner = runner_for(ctx.state_path)

    with runner.canary_lease(runtime_tuple()):
        result = call(certify, ctx, certification_request())

    assert result["certification"] is None
    assert result["record"]["outcome"] == "refused"
    assert result["record"]["reason_code"] == CertificationFailureCode.CANARY_IN_PROGRESS.value
    assert [row.payload["stage"] for row in journal_rows(state_path)][-1] == "certify"


def test_certify_is_refused_when_cited_evidence_has_expired(ctx: MethodContext) -> None:
    """Evidence past its expiry certifies nothing, and names itself."""
    walk_to_canary(ctx)
    stale = capability(
        capability_id="streaming",
        verified_at=datetime(2020, 1, 1, tzinfo=UTC),
        expires_at=datetime(2021, 1, 1, tzinfo=UTC),
    )

    result = call(certify, ctx, certification_request(capabilities=(capability(), stale)))

    assert result["certification"] is None
    assert result["record"]["reason_code"] == CertificationFailureCode.EVIDENCE_EXPIRED.value
    assert result["expired"] == ["streaming"]


def test_a_refused_certify_admits_a_retry(ctx: MethodContext) -> None:
    """A refusal means nothing ran, so the passed canary still stands."""
    walk_to_canary(ctx)
    runner = runner_for(ctx.state_path)
    with runner.canary_lease(runtime_tuple()):
        call(certify, ctx, certification_request())

    result = call(certify, ctx, certification_request())

    assert result["record"]["outcome"] == "passed"
    assert result["certification"] is not None


def test_certify_refuses_before_it_writes_anything(ctx: MethodContext) -> None:
    """The refusal path never leaves a half-written certification."""
    walk_to_canary(ctx)
    runner = runner_for(ctx.state_path)

    with runner.canary_lease(runtime_tuple()):
        result = call(certify, ctx, certification_request())

    assert result["certification"] is None
    assert not any(
        row.stage == "certify" and row.outcome == "passed"
        for row in runner.stage_history(runtime_tuple())
    )


# ---- probe verdicts ---------------------------------------------------------


def test_probe_fails_when_the_runtime_is_not_installed(ctx: MethodContext) -> None:
    """An absent binary is graded ahead of every verdict about it."""
    result = call(probe, ctx, probe_request(installed=False, observed_flags=()))

    assert result["record"]["outcome"] == "failed"
    assert result["record"]["reason_code"] == CertificationFailureCode.TUPLE_NOT_INSTALLABLE.value


def test_probe_fails_when_a_declared_capability_drifted(ctx: MethodContext) -> None:
    """A capability the binary no longer advertises is not observed."""
    result = call(probe, ctx, probe_request(observed_flags=("--output-format",)))

    assert result["record"]["reason_code"] == CertificationFailureCode.CAPABILITY_NOT_OBSERVED.value
    assert result["drifted"] == ["tool_use"]


def test_probe_fails_when_a_required_capability_has_no_probe_rule(ctx: MethodContext) -> None:
    """A declaration is not its own evidence, so the probe refuses it."""
    result = call(probe, ctx, probe_request(required_capabilities=("skills", "tool_use")))

    expected = CertificationFailureCode.CAPABILITY_EVIDENCE_UNCOVERED.value
    assert result["record"]["reason_code"] == expected
    assert result["uncovered"] == ["skills"]


def test_probe_raises_for_a_capability_the_matrix_has_no_row_for(ctx: MethodContext) -> None:
    """No row means no verdict could exist, so nothing is recorded."""
    with pytest.raises(ValueError, match="no row for"):
        call(probe, ctx, probe_request(required_capabilities=("telepathy",)))


def test_probe_raises_for_an_unknown_runtime(ctx: MethodContext) -> None:
    """A runtime outside the matrix cannot be probed against it."""
    with pytest.raises(ValueError, match="unknown runtime"):
        call(probe, ctx, probe_request(runtime_id="gemini"))


# ---- sequence errors --------------------------------------------------------


def test_canary_raises_without_a_passed_probe(ctx: MethodContext) -> None:
    """No canary may start from a tuple the probe stage never cleared."""
    with pytest.raises(ConformanceSequenceError, match="passed probe"):
        call(canary, ctx, canary_request())


def test_canary_raises_after_a_failed_probe(ctx: MethodContext) -> None:
    """A failed probe stops the tuple; the canary does not run anyway."""
    call(probe, ctx, probe_request(installed=False, observed_flags=()))

    with pytest.raises(ConformanceSequenceError, match="passed probe"):
        call(canary, ctx, canary_request())


def test_certify_raises_without_a_passed_canary(ctx: MethodContext) -> None:
    """A probe alone certifies nothing about what the tuple does."""
    call(probe, ctx, probe_request())

    with pytest.raises(ConformanceSequenceError, match="passed canary"):
        call(certify, ctx, certification_request())


def test_certify_raises_after_a_failed_canary(ctx: MethodContext) -> None:
    """An observed escape is not evidence a certification may cite."""
    call(probe, ctx, probe_request())
    escaped = (
        ContainmentCallOutcome(attempt=ContainmentAttempt.CANONICAL_GIT, denied=False),
        *(row for row in contained() if row.attempt is not ContainmentAttempt.CANONICAL_GIT),
    )
    result = call(canary, ctx, canary_request(outcomes=escaped))
    assert result["record"]["outcome"] == "failed"
    assert result["quarantine_trigger"] == "canary_failure"

    with pytest.raises(ConformanceSequenceError, match="passed canary"):
        call(certify, ctx, certification_request())


def test_a_second_certify_raises(ctx: MethodContext) -> None:
    """One walk certifies once; a re-certification starts from probe."""
    walk_to_canary(ctx)
    call(certify, ctx, certification_request())

    with pytest.raises(ConformanceSequenceError, match="passed canary"):
        call(certify, ctx, certification_request())


def test_a_second_canary_for_a_tuple_raises(ctx: MethodContext) -> None:
    """Two canaries would each certify the other's evidence."""
    walk_to_canary(ctx)

    with pytest.raises(ConformanceSequenceError, match="passed probe"):
        call(canary, ctx, canary_request())


# ---- the contiguity rule, on its own ----------------------------------------


def test_certifiable_stages_reports_nothing_for_an_empty_history() -> None:
    """A tuple nobody probed has no stages to cite."""
    assert certifiable_stages(()) is None


def test_certifiable_stages_reports_nothing_for_a_probe_alone() -> None:
    """One passed probe is half the pair a certification needs."""
    runner = _memory_runner()
    runner.run_probe(probe_request())

    assert certifiable_stages(runner.stage_history(runtime_tuple())) is None


def test_certifiable_stages_returns_the_probe_and_canary_pair() -> None:
    """The pair is returned in stage order, oldest first."""
    runner = _memory_runner()
    runner.run_probe(probe_request())
    runner.run_canary(canary_request())

    pair = certifiable_stages(runner.stage_history(runtime_tuple()))

    assert pair is not None
    assert tuple(row.stage for row in pair) == ("probe", "canary")


# ---- one writer -------------------------------------------------------------


def _builds_a_certification(tree: ast.AST) -> bool:
    """Report whether *tree* calls the certification record or a method of it."""
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id == "DriverCertification":
            return True
        if (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id == "DriverCertification"
        ):
            return True
    return False


def test_the_conformance_runner_is_the_only_writer_of_certifications() -> None:
    """Nothing else in the tree constructs a certification record."""
    root = Path(__file__).resolve().parents[4] / "src" / "eawf"
    writers = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*.py")
        if _builds_a_certification(ast.parse(path.read_text(encoding="utf-8")))
    )

    assert writers == ["runtime/runtimes/conformance.py"]


# ---- wire strictness --------------------------------------------------------


def test_the_stage_verbs_refuse_an_unknown_param(ctx: MethodContext) -> None:
    """A stage verb takes the request and nothing beside it."""
    params = {"request": probe_request().model_dump(mode="json"), "force": True}

    with pytest.raises(ValidationError):
        asyncio.run(probe(ctx, params))


def test_runner_for_returns_one_runner_per_tree(state_path: Path) -> None:
    """The lease a canary holds is visible to the certify that follows."""
    assert runner_for(state_path) is runner_for(state_path)


def _memory_runner() -> Any:
    """Return a runner over a journal that lives only in this process."""
    from eawf.runtime.runtimes.conformance import ConformanceRunner

    class _Journal:
        def __init__(self) -> None:
            self.rows: dict[str, list[Any]] = {}

        def append(self, *, tuple_digest: str, record: Any) -> None:
            self.rows.setdefault(tuple_digest, []).append(record)

        def records(self, *, tuple_digest: str) -> tuple[Any, ...]:
            return tuple(self.rows.get(tuple_digest, ()))

    return ConformanceRunner(journal=_Journal())
