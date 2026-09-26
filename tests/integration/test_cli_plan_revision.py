"""CLI tests for the plan-revision verbs, plus the cross-module native-verb census.

:mod:`eawf.surfaces.cli.commands.plan` adds three verbs beside the
read-only ``plan show``: ``submit``, ``approve`` and ``apply``, each
dispatch over one ``planning.plan_revision.*`` RPC. These tests drive the
real Typer app with a stand-in daemon client, mirroring the shape
``tests/integration/test_cli_domain_lifecycle.py`` already proved for the
lifecycle and create verbs.

This file also carries the CLI command census: every native create, plan,
seal and submit RPC this wave adds a command for, gathered from both
:mod:`eawf.surfaces.cli.commands.domain` and this module, so a command
dropped from either file reds a single assertion rather than going
unnoticed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon.methods.candidate import CandidateSubmitAnswer
from eawf.runtime.daemon.methods.delivery_approval import ApprovalAnswer
from eawf.runtime.daemon.methods.domain_envelope import (
    ENVELOPE_SCHEMA_VERSION,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
)
from eawf.runtime.daemon.methods.planning import PLANNING_METHODS
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd
from eawf.surfaces.cli.commands import plan as plan_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_TRACK_URN = f"{_ROOT}/track/TRK-CANARY"
_MILESTONE_URN = f"{_ROOT}/milestone/MLS-0001"
_BATCH_URN = f"{_ROOT}/batch/BAT-0001"
_TASK_URN = f"{_ROOT}/task/CANARY-0001"
_RUN_URN = f"{_ROOT}/run/RUN-00000001"
_REPOSITORY_URN = f"{_ROOT}/repository/REP-CANARY"
_ACTION_URN = f"{_ROOT}/pending-action/ACT-0001"
_EVIDENCE_URN = f"{_ROOT}/evidence/EVD-0001"

_SHA = "a" * 40
_DIGEST = "sha256:" + "b" * 64


def _proposal_document() -> dict[str, Any]:
    """Return a ``PlanRevisionProposal`` document valid enough to submit.

    ``plan submit`` validates its ``--from-spec`` file locally (unlike the
    native create commands, which forward their document unread), so this
    must be a genuinely legal proposal rather than a placeholder.
    """
    return {
        "key": "PRV-0001",
        "author": {"principal_kind": "operator", "principal_id": "OP-0001"},
        "body": {
            "milestone_urn": _MILESTONE_URN,
            "milestone": {
                "key": "MLS-0001",
                "primary_track_ref": _TRACK_URN,
                "title": "Canary milestone",
                "outcome": "The canary tree accepts a native plan revision.",
                "appetite": "M",
                "exclusions": ["nothing else changes"],
                "acceptance_journey": [
                    {
                        "step_id": "AS-01",
                        "actor": "operator",
                        "action": "run the canary suite",
                        "expected_observation": "the suite is green",
                        "evidence_kinds": ["artifact"],
                    }
                ],
                "required_batch_refs": [_BATCH_URN],
            },
            "batches": [{"urn": _BATCH_URN, "repository_ref": _REPOSITORY_URN}],
            "tasks": [
                {
                    "urn": _TASK_URN,
                    "batch_ref": _BATCH_URN,
                    "priority": "P1",
                    "intent": "prove the plan revision round trip",
                    "criteria": [
                        {
                            "id": "CR-01",
                            "text": "the canary suite is green",
                            "kind": "functional_suitability",
                            "acceptance_style": "binary",
                            "evidence_kind": "deterministic",
                            "quality_dimension": "functional_suitability",
                            "measurable_signal": "uv run pytest exits zero",
                        }
                    ],
                }
            ],
        },
    }


class _FakeClient:
    """A stand-in daemon client recording every call it is handed."""

    calls: ClassVar[list[tuple[str, dict[str, Any]]]] = []

    def __init__(self, *, result: dict[str, Any] | None = None, error: Exception | None = None):
        self._result = result
        self._error = error

    def __enter__(self) -> _FakeClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        type(self).calls.append((method, dict(params)))
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


def _no_escalate(verb: str, *, flags: object = None, runtime_dir: object = None) -> int:
    """Stand in for the mutating-verb escalation gate."""
    return 0


@pytest.fixture(autouse=True)
def _no_daemon_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the escalation gate from spawning a real daemon."""
    monkeypatch.delenv("EAWF_DAEMONLESS", raising=False)
    monkeypatch.setattr("eawf.surfaces.cli._dispatch.escalate_mutation", _no_escalate)
    _FakeClient.calls = []


def _install(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    """Point every command module's daemon client at a fake built with *kwargs*."""
    monkeypatch.setattr(plan_cmd, "DaemonClient", lambda *a, **k: _FakeClient(**kwargs))
    monkeypatch.setattr(domain_cmd, "DaemonClient", lambda *a, **k: _FakeClient(**kwargs))


# ---- the plan-revision verb table -------------------------------------------


def test_plan_revision_cli_methods_match_the_registered_rpcs() -> None:
    """Every CLI verb names a registered RPC, and none is left unexposed."""
    assert set(plan_cmd.PLAN_REVISION_CLI_METHODS) == set(PLANNING_METHODS)


# ---- CR-01: the cross-module native-verb census -----------------------------

#: Every native create, plan, seal and submit RPC this wave adds a command
#: for. Each entry is read off the constant its own ``@app.command``
#: decoration sends (see ``test_every_census_verb_forwards_its_own_rpc``),
#: so dropping a command's use of its constant, or dropping the constant
#: from this tuple, reds one of the two assertions below.
_NATIVE_VERB_CENSUS: tuple[str, ...] = (
    domain_cmd.TRACK_CREATE,
    domain_cmd.MILESTONE_CREATE,
    domain_cmd.BATCH_CREATE,
    domain_cmd.TASK_CREATE,
    plan_cmd.PLAN_SUBMIT_METHOD,
    plan_cmd.PLAN_APPROVE_METHOD,
    plan_cmd.PLAN_APPLY_METHOD,
    domain_cmd.DELIVERY_SEAL_APPROVAL,
    domain_cmd.CANDIDATE_SUBMIT,
)


def test_cli_command_census_covers_every_create_plan_seal_and_submit_rpc() -> None:
    """CR-01: one CLI command exists per native create, plan, seal and submit RPC.

    Gate-fire proof (verified by hand, not re-asserted here): commenting
    out any one of the nine ``@app.command`` decorations this census
    covers reds ``test_every_census_verb_forwards_its_own_rpc`` below,
    because that command's row can no longer be invoked.
    """
    assert len(_NATIVE_VERB_CENSUS) == len(set(_NATIVE_VERB_CENSUS)) == 9
    assert set(_NATIVE_VERB_CENSUS) == {
        "domain.track.create",
        "domain.milestone.create",
        "domain.batch.create",
        "domain.task.create",
        "planning.plan_revision.submit",
        "planning.plan_revision.approve",
        "planning.plan_revision.apply",
        "runtime.delivery.seal_acceptance_approval",
        "runtime.candidate.submit",
    }


def _create_receipt_envelope(method: str, urn: str) -> dict[str, Any]:
    """Return the daemon's own ok envelope for a create, as JSON."""
    from datetime import UTC, datetime

    from eawf.runtime.daemon.epoch2_transaction import MutationReceipt

    receipt = MutationReceipt(
        event_name=method,
        entity_ref=urn,
        revision_before=None,
        revision_after=1,
        canonical_sequence=1,
        event_id="evt-0001",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0001",
    )
    return accepted_envelope(receipt, operation=method).model_dump(mode="json")


def _plan_receipt_envelope(method: str, _subject: str) -> dict[str, Any]:
    """Return the daemon's own ok envelope for a plan-revision move, as JSON.

    The receipt's ``entity_ref`` is the plan's target Milestone URN, not
    the bare plan-revision key: a plan revision has no qualified URN of
    its own, so the daemon's own receipt names the record an apply would
    materialise (see ``eawf.workflow.planning.apply._commit``).
    """
    from datetime import UTC, datetime

    from eawf.runtime.daemon.epoch2_transaction import MutationReceipt

    receipt = MutationReceipt(
        event_name=method,
        entity_ref=_MILESTONE_URN,
        revision_before=1,
        revision_after=2,
        canonical_sequence=2,
        event_id="evt-0002",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0002",
    )
    return accepted_envelope(receipt, operation=method).model_dump(mode="json")


def _seal_answer() -> dict[str, Any]:
    """Return the daemon's own answer shape for a sealed approval, as JSON."""
    return ApprovalAnswer(
        action_ref=_ACTION_URN,
        status="sealed",
        revision=2,
        bundle_revision=1,
        bundle_digest=_DIGEST,
        acceptance_bundle=None,
        created=False,
        canonical_sequence=3,
        reason="every seal check holds",
    ).model_dump(mode="json")


def _submit_answer() -> dict[str, Any]:
    """Return the daemon's own answer shape for a candidate submission, as JSON."""
    return CandidateSubmitAnswer(
        candidate_ref="CND-" + "0" * 32,
        run_ref=_RUN_URN,
        replayed=False,
        report_binding="pending",
        submission={"candidate_ref": "CND-" + "0" * 32},
        reason="candidate recorded with its report binding pending",
    ).model_dump(mode="json")


@pytest.fixture
def _proposal_spec(tmp_path: Path) -> Path:
    """Write a legal plan-revision proposal file and return its path."""
    spec = tmp_path / "proposal.json"
    spec.write_bytes(orjson.dumps(_proposal_document()))
    return spec


def _census_rows(proposal_spec: Path) -> tuple[tuple[list[str], str, dict[str, Any]], ...]:
    """Return one CLI invocation per census entry, with its RPC and fake answer."""
    return (
        (
            [
                "track",
                "create",
                _TRACK_URN,
                "--expected-tree-revision",
                "0",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
                "--from-spec",
                str(proposal_spec.with_name("track.json")),
            ],
            domain_cmd.TRACK_CREATE,
            _create_receipt_envelope(domain_cmd.TRACK_CREATE, _TRACK_URN),
        ),
        (
            [
                "milestone",
                "create",
                _MILESTONE_URN,
                "--expected-tree-revision",
                "0",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
                "--from-spec",
                str(proposal_spec.with_name("milestone.json")),
            ],
            domain_cmd.MILESTONE_CREATE,
            _create_receipt_envelope(domain_cmd.MILESTONE_CREATE, _MILESTONE_URN),
        ),
        (
            [
                "batch",
                "create",
                _BATCH_URN,
                "--expected-tree-revision",
                "0",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
                "--from-spec",
                str(proposal_spec.with_name("batch.json")),
            ],
            domain_cmd.BATCH_CREATE,
            _create_receipt_envelope(domain_cmd.BATCH_CREATE, _BATCH_URN),
        ),
        (
            [
                "task",
                "create",
                _TASK_URN,
                "--expected-tree-revision",
                "0",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
                "--from-spec",
                str(proposal_spec.with_name("task.json")),
            ],
            domain_cmd.TASK_CREATE,
            _create_receipt_envelope(domain_cmd.TASK_CREATE, _TASK_URN),
        ),
        (
            [
                "plan",
                "submit",
                "--from-spec",
                str(proposal_spec),
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
            ],
            plan_cmd.PLAN_SUBMIT_METHOD,
            _plan_receipt_envelope(plan_cmd.PLAN_SUBMIT_METHOD, "PRV-0001"),
        ),
        (
            [
                "plan",
                "approve",
                "PRV-0001",
                "--expected-plan-revision",
                "1",
                "--action-ref",
                _ACTION_URN,
                "--approved-by",
                "OP-0001",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
            ],
            plan_cmd.PLAN_APPROVE_METHOD,
            _plan_receipt_envelope(plan_cmd.PLAN_APPROVE_METHOD, "PRV-0001"),
        ),
        (
            [
                "plan",
                "apply",
                "PRV-0001",
                "--expected-plan-revision",
                "2",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
            ],
            plan_cmd.PLAN_APPLY_METHOD,
            _plan_receipt_envelope(plan_cmd.PLAN_APPLY_METHOD, "PRV-0001"),
        ),
        (
            [
                "milestone",
                "seal-approval",
                _ACTION_URN,
                "--expected-approval-revision",
                "1",
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
                "--option-id",
                "accept",
                "--receipt-ref",
                _EVIDENCE_URN,
            ],
            domain_cmd.DELIVERY_SEAL_APPROVAL,
            _seal_answer(),
        ),
        (
            [
                "task",
                "submit",
                _RUN_URN,
                "--task-ref",
                _TASK_URN,
                "--submission-ref",
                f"artifact://git/commit/{_SHA}",
                "--changed-path",
                "src/eawf/example.py",
                "--resulting-tree-digest",
                _DIGEST,
                "--idempotency-key",
                "key-0001",
                "--actor",
                "OPERATOR",
            ],
            domain_cmd.CANDIDATE_SUBMIT,
            _submit_answer(),
        ),
    )


def test_every_census_verb_forwards_its_own_rpc(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _proposal_spec: Path
) -> None:
    """Each of the nine census verbs sends exactly its own RPC once.

    This is the behavioural half of the gate-fire proof: a command
    deleted, or renamed onto a different constant, either fails to invoke
    (``No such command``) or forwards the wrong method here.
    """
    for document_name in ("track.json", "milestone.json", "batch.json", "task.json"):
        (tmp_path / document_name).write_bytes(orjson.dumps({"key": "PLACEHOLDER"}))

    seen_methods: list[str] = []
    for args, method, result in _census_rows(_proposal_spec):
        _install(monkeypatch, result=result)
        _FakeClient.calls = []
        cli_result = runner.invoke(app, ["--workspace", str(tmp_path), *args])
        assert cli_result.exit_code == exit_codes.OK, (method, cli_result.output)
        assert len(_FakeClient.calls) == 1, (method, _FakeClient.calls)
        sent_method, _params = _FakeClient.calls[0]
        assert sent_method == method
        seen_methods.append(sent_method)
    assert set(seen_methods) == set(_NATIVE_VERB_CENSUS)


# ---- submit --------------------------------------------------------------


def test_submit_forwards_the_proposal_document(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _proposal_spec: Path
) -> None:
    """The proposal file's fields land on the wire, keyed as submitted."""
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_SUBMIT_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "submit",
            "--from-spec",
            str(_proposal_spec),
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == plan_cmd.PLAN_SUBMIT_METHOD
    assert params["proposal"]["key"] == "PRV-0001"
    assert params["actor"] == "OPERATOR"
    assert params["idempotency_key"] == "key-0001"
    assert params["repo_root"] == str(tmp_path.resolve())


def test_submit_missing_from_spec_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: an unreadable proposal file fails at the boundary."""
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_SUBMIT_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "submit",
            "--from-spec",
            str(tmp_path / "absent.json"),
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "cannot read --from-spec" in result.output
    assert _FakeClient.calls == []


def test_submit_malformed_json_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a payload file that is not JSON fails at the boundary."""
    spec = tmp_path / "broken.json"
    spec.write_text("{not json", encoding="utf-8")
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_SUBMIT_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "submit",
            "--from-spec",
            str(spec),
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "not valid JSON" in result.output
    assert _FakeClient.calls == []


def test_submit_invalid_proposal_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: a syntactically valid JSON object that is not a legal proposal."""
    spec = tmp_path / "incomplete.json"
    spec.write_bytes(orjson.dumps({"key": "PRV-0001"}))
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_SUBMIT_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "submit",
            "--from-spec",
            str(spec),
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "not a valid proposal" in result.output
    assert _FakeClient.calls == []


# ---- approve / apply ---------------------------------------------------------


@pytest.mark.parametrize("revision", ["0", "-1"])
def test_approve_non_positive_revision_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, revision: str
) -> None:
    """Boundary: the compare-and-swap token is positive or the request stops here."""
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_APPROVE_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "approve",
            "PRV-0001",
            "--expected-plan-revision",
            revision,
            "--action-ref",
            _ACTION_URN,
            "--approved-by",
            "OP-0001",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "positive revision" in result.output
    assert _FakeClient.calls == []


def test_approve_forwards_an_operator_principal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The human approving is always recorded as an operator principal."""
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_APPROVE_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "approve",
            "PRV-0001",
            "--expected-plan-revision",
            "1",
            "--action-ref",
            _ACTION_URN,
            "--approved-by",
            "OP-0001",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["approved_by"] == {"principal_kind": "operator", "principal_id": "OP-0001"}
    assert params["action_ref"] == _ACTION_URN


def test_apply_non_positive_revision_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: apply shares the same positive-revision bound as approve."""
    _install(monkeypatch, result=_plan_receipt_envelope(plan_cmd.PLAN_APPLY_METHOD, "PRV-0001"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "apply",
            "PRV-0001",
            "--expected-plan-revision",
            "0",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "positive revision" in result.output
    assert _FakeClient.calls == []


# ---- the machine envelope + refusals -----------------------------------------


def test_apply_json_output_is_the_strict_machine_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The printed JSON validates as a domain envelope and keeps its shape."""
    answer = _plan_receipt_envelope(plan_cmd.PLAN_APPLY_METHOD, "PRV-0001")
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "--json",
            "plan",
            "apply",
            "PRV-0001",
            "--expected-plan-revision",
            "1",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    payload = orjson.loads(result.stdout)
    assert DomainEnvelope.model_validate(payload) == DomainEnvelope.model_validate(answer)
    assert payload["operation"] == plan_cmd.PLAN_APPLY_METHOD
    assert payload["status"] == DomainStatus.OK.value


def test_apply_guard_refusal_surfaces_the_daemon_code_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An in-transaction refusal prints the daemon's code, guard and remediation."""
    refused = DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=plan_cmd.PLAN_APPLY_METHOD,
        revision_before=1,
        revision_after=1,
        errors=(
            DomainError(
                code=DomainErrorCode.ILLEGAL_TRANSITION,
                message="PRV-0001 is not APPROVED",
                entity_ref="PRV-0001",
                guard="plan_revision_approved",
                remediation="Approve the revision before applying it.",
            ),
        ),
    ).model_dump(mode="json")
    _install(monkeypatch, result=refused)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "apply",
            "PRV-0001",
            "--expected-plan-revision",
            "1",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    from eawf.surfaces.cli.commands.domain import DOMAIN_REFUSAL_EXIT

    assert result.exit_code == DOMAIN_REFUSAL_EXIT
    assert DomainErrorCode.ILLEGAL_TRANSITION.value in result.output
    assert "guard: plan_revision_approved" in result.output
    assert "remediation: Approve the revision before applying it." in result.output


def test_submit_fence_refusal_renders_the_daemons_code_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, _proposal_spec: Path
) -> None:
    """A pre-transaction fence refusal is rendered exactly as an in-transaction one."""
    _install(
        monkeypatch,
        error=DaemonRpcError(
            -32002,
            "validation_failed: native_authority_required: the tree left epoch 2",
        ),
    )
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "submit",
            "--from-spec",
            str(_proposal_spec),
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    from eawf.surfaces.cli.commands.domain import DOMAIN_REFUSAL_EXIT

    assert result.exit_code == DOMAIN_REFUSAL_EXIT
    assert DomainErrorCode.NATIVE_AUTHORITY_REQUIRED.value in result.output
    assert "the tree left epoch 2" in result.output


def test_apply_transport_failure_is_a_cli_error_not_an_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a dropped connection is not rendered as a domain refusal."""
    _install(monkeypatch, error=TimeoutError("no answer"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "apply",
            "PRV-0001",
            "--expected-plan-revision",
            "1",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert "daemon unavailable" in result.output


def test_apply_daemon_answer_that_is_not_an_envelope_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: an answer a client cannot branch on is not printed as one."""
    _install(monkeypatch, result={"status": "ok"})
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "plan",
            "apply",
            "PRV-0001",
            "--expected-plan-revision",
            "1",
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.INTERNAL_ERROR
    assert "not a domain envelope" in result.output
