"""CLI tests for the native per-entity lifecycle verbs.

The nine verbs of :mod:`eawf.surfaces.cli.commands.domain` are dispatch
over the daemon's ``domain.<entity>.<verb>`` RPCs, so these tests drive
the real Typer app with a stand-in daemon client and assert three things
the wave promises:

* one RPC per invocation, under the verb's own name and carrying the
  addressing flags plus the ``--from-spec`` payload;
* the answer printed verbatim as the strict machine envelope, which is
  pinned both against the daemon's own model and against a literal
  expected shape so a later wave cannot drift it silently;
* a refusal surfaced with the daemon's own code, message and guard, and
  a stable non-zero exit.

The envelopes the fake answers with are built by the daemon's own
:func:`~eawf.runtime.daemon.methods.domain_envelope.accepted_envelope` /
:func:`~eawf.runtime.daemon.methods.domain_envelope.refused_envelope`
rather than hand-written, so a fixture cannot promise a shape the daemon
does not produce.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

import orjson
import pytest
from typer.testing import CliRunner

from eawf.runtime.daemon.epoch2_transaction import (
    MutationReceipt,
    TransactionRefusalCode,
    TransactionRefusedError,
)
from eawf.runtime.daemon.methods.domain import DOMAIN_LIFECYCLE_METHODS, LifecycleParams
from eawf.runtime.daemon.methods.domain_envelope import (
    ENVELOPE_SCHEMA_VERSION,
    DomainEnvelope,
    DomainError,
    DomainErrorCode,
    DomainStatus,
    accepted_envelope,
    refused_envelope,
)
from eawf.surfaces.cli import exit_codes
from eawf.surfaces.cli._daemon_client import DaemonRpcError
from eawf.surfaces.cli.app import app
from eawf.surfaces.cli.commands import domain as domain_cmd

pytestmark = pytest.mark.integration

runner = CliRunner()

_ROOT = "eawf://WS-CANARY/PRJ-CANARY/REP-CANARY"
_MILESTONE_URN = f"{_ROOT}/milestone/MLS-0001"
_TRACK_URN = f"{_ROOT}/track/TRK-CANARY"
_BATCH_URN = f"{_ROOT}/batch/BAT-0001"
_TASK_URN = f"{_ROOT}/task/CANARY-0001"
_RUN_URN = f"{_ROOT}/run/RUN-00000001"
_APPROVAL_URN = f"{_ROOT}/pending-action/ACT-0001"

#: Every command line under test, with the RPC it must forward to and the
#: per-entity revision flag it declares. One row per registered verb.
_VERB_ROWS: tuple[tuple[list[str], str, str, str], ...] = (
    (["track", "retire"], domain_cmd.TRACK_RETIRE, "--expected-track-revision", _TRACK_URN),
    (
        ["milestone", "activate"],
        domain_cmd.MILESTONE_ACTIVATE,
        "--expected-milestone-revision",
        _MILESTONE_URN,
    ),
    (
        ["milestone", "open-review"],
        domain_cmd.MILESTONE_OPEN_REVIEW,
        "--expected-milestone-revision",
        _MILESTONE_URN,
    ),
    (
        ["milestone", "accept"],
        domain_cmd.MILESTONE_ACCEPT,
        "--expected-milestone-revision",
        _MILESTONE_URN,
    ),
    (
        ["milestone", "cancel"],
        domain_cmd.MILESTONE_CANCEL,
        "--expected-milestone-revision",
        _MILESTONE_URN,
    ),
    (["batch", "activate"], domain_cmd.BATCH_ACTIVATE, "--expected-batch-revision", _BATCH_URN),
    (["batch", "ready"], domain_cmd.BATCH_READY, "--expected-batch-revision", _BATCH_URN),
    (["task", "promote"], domain_cmd.TASK_PROMOTE, "--expected-task-revision", _TASK_URN),
    (["task", "start"], domain_cmd.TASK_START, "--expected-task-revision", _TASK_URN),
)


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


def _receipt(*, event_name: str, entity_ref: str, revision_before: int = 3) -> MutationReceipt:
    """Return a committed receipt for one move."""
    return MutationReceipt(
        event_name=event_name,
        entity_ref=entity_ref,
        revision_before=revision_before,
        revision_after=revision_before + 1,
        canonical_sequence=12,
        event_id="evt-0001",
        idempotency_key="key-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0001",
    )


def _accepted(method: str, entity_ref: str, *, revision_before: int = 3) -> dict[str, Any]:
    """Return the daemon's own ok envelope, as JSON."""
    receipt = _receipt(event_name=method, entity_ref=entity_ref, revision_before=revision_before)
    return accepted_envelope(receipt, operation=method).model_dump(mode="json")


def _refused(method: str, *, code: TransactionRefusalCode, entity_ref: str) -> dict[str, Any]:
    """Return the daemon's own refusal envelope, as JSON."""
    refusal = TransactionRefusedError(
        code=code,
        detail=f"{method} refused for the test",
        entity_ref=entity_ref,
        guard="track_active",
        remediation="Fix the guard and retry.",
        revision=3,
    )
    return refused_envelope(refusal, operation=method).model_dump(mode="json")


def _approval_refused(method: str, *, entity_ref: str) -> dict[str, Any]:
    """Return the refusal an acceptance with no sealed approval earns.

    Built from the public envelope models rather than from a transaction
    refusal: ``protected_approval_required`` is decided by the verb's own
    preflight, which is outside the transaction's refusal vocabulary.
    """
    return DomainEnvelope(
        schema_version=ENVELOPE_SCHEMA_VERSION,
        status=DomainStatus.ERROR,
        operation=method,
        revision_before=3,
        revision_after=3,
        errors=(
            DomainError(
                code=DomainErrorCode.PROTECTED_APPROVAL_REQUIRED,
                message=f"{method} needs the reference of a sealed pending-action receipt",
                entity_ref=entity_ref,
                guard="acceptance_journey_passed",
                remediation="Seal the acceptance approval and retry with its receipt reference.",
            ),
        ),
    ).model_dump(mode="json")


def _install(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> None:
    """Point the command module at a fake client built with *kwargs*."""
    monkeypatch.setattr(domain_cmd, "DaemonClient", lambda *a, **k: _FakeClient(**kwargs))


def _base_args(verb: list[str], revision_flag: str, urn: str) -> list[str]:
    """Return the addressing flags every verb takes."""
    return [
        *verb,
        urn,
        revision_flag,
        "3",
        "--idempotency-key",
        "key-0001",
        "--actor",
        "OPERATOR",
    ]


# ---- the verb table ---------------------------------------------------------


def test_cli_methods_match_the_registered_domain_rpcs() -> None:
    """Every CLI verb names a registered RPC, and none is left unexposed."""
    assert set(domain_cmd.DOMAIN_CLI_METHODS) == set(DOMAIN_LIFECYCLE_METHODS)


def test_idempotency_key_bound_matches_the_request_model() -> None:
    """The CLI-side key bound is the daemon's bound, not a second number."""
    metadata = LifecycleParams.model_fields["idempotency_key"].metadata
    bounds = [item.max_length for item in metadata if getattr(item, "max_length", None)]
    assert bounds == [domain_cmd.IDEMPOTENCY_KEY_MAX]


@pytest.mark.parametrize(("verb", "method", "revision_flag", "urn"), _VERB_ROWS)
def test_verb_forwards_exactly_one_rpc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: list[str],
    method: str,
    revision_flag: str,
    urn: str,
) -> None:
    """Each verb sends its own RPC once, with the addressing flags on it."""
    _install(monkeypatch, result=_accepted(method, urn))
    result = runner.invoke(
        app, ["--workspace", str(tmp_path), *_base_args(verb, revision_flag, urn)]
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert len(_FakeClient.calls) == 1
    sent_method, params = _FakeClient.calls[0]
    assert sent_method == method
    assert params["urn"] == urn
    assert params["expected_revision"] == 3
    assert params["idempotency_key"] == "key-0001"
    assert params["actor"] == "OPERATOR"
    assert params["repo_root"] == str(tmp_path.resolve())


# ---- --from-spec ------------------------------------------------------------


def test_from_spec_payload_reaches_the_rpc(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """The payload file's fields land on the wire beside the addressing flags."""
    spec = tmp_path / "activate.json"
    spec.write_bytes(
        orjson.dumps(
            {
                "updates": {"target_branch": "main"},
                "observations": [{"guard": "track_active", "satisfied": True}],
                "reason_code": "scope-complete",
                "binding_refs": [_RUN_URN],
                "correlation_id": "corr-0001",
            }
        )
    )
    _install(monkeypatch, result=_accepted(domain_cmd.MILESTONE_ACTIVATE, _MILESTONE_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["milestone", "activate"], "--expected-milestone-revision", _MILESTONE_URN),
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["updates"] == {"target_branch": "main"}
    assert params["observations"] == [{"guard": "track_active", "satisfied": True}]
    assert params["reason_code"] == "scope-complete"
    assert params["binding_refs"] == [_RUN_URN]
    assert params["correlation_id"] == "corr-0001"


def test_absent_from_spec_sends_an_empty_payload(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: no payload file means empty payload fields, never absent keys."""
    _install(monkeypatch, result=_accepted(domain_cmd.BATCH_READY, _BATCH_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["batch", "ready"], "--expected-batch-revision", _BATCH_URN),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["updates"] == {}
    assert params["observations"] == []
    assert params["binding_refs"] == []
    assert params["reason_code"] is None
    assert params["correlation_id"] is None
    assert "approval_receipt_ref" not in params


def test_from_spec_missing_file_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: an unreadable payload file fails at the boundary."""
    _install(monkeypatch, result=_accepted(domain_cmd.TASK_START, _TASK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["task", "start"], "--expected-task-revision", _TASK_URN),
            "--from-spec",
            str(tmp_path / "absent.json"),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "cannot read --from-spec" in result.output
    assert _FakeClient.calls == []


def test_from_spec_malformed_json_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a payload file that is not JSON fails at the boundary."""
    spec = tmp_path / "broken.json"
    spec.write_text("{not json", encoding="utf-8")
    _install(monkeypatch, result=_accepted(domain_cmd.TASK_START, _TASK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["task", "start"], "--expected-task-revision", _TASK_URN),
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "not valid JSON" in result.output
    assert _FakeClient.calls == []


def test_from_spec_unknown_field_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: the payload model forbids extras, so a typo names itself."""
    spec = tmp_path / "typo.json"
    spec.write_bytes(orjson.dumps({"reason": "scope-complete"}))
    _install(monkeypatch, result=_accepted(domain_cmd.TASK_START, _TASK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["task", "start"], "--expected-task-revision", _TASK_URN),
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "check reason" in result.output
    assert _FakeClient.calls == []


# ---- addressing bounds ------------------------------------------------------


@pytest.mark.parametrize("revision", ["0", "-1"])
def test_non_positive_revision_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, revision: str
) -> None:
    """Boundary: the compare-and-swap token is positive or the request stops here."""
    _install(monkeypatch, result=_accepted(domain_cmd.MILESTONE_CANCEL, _MILESTONE_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "cancel",
            _MILESTONE_URN,
            "--expected-milestone-revision",
            revision,
            "--idempotency-key",
            "key-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "positive revision" in result.output
    assert _FakeClient.calls == []


@pytest.mark.parametrize(
    ("length", "expected_exit"),
    [
        (0, exit_codes.USER_ERROR),
        (1, exit_codes.OK),
        (128, exit_codes.OK),
        (129, exit_codes.USER_ERROR),
    ],
)
def test_idempotency_key_length_boundaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, length: int, expected_exit: int
) -> None:
    """Boundary: empty, single, max-length and one-over retry keys."""
    _install(monkeypatch, result=_accepted(domain_cmd.MILESTONE_CANCEL, _MILESTONE_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "cancel",
            _MILESTONE_URN,
            "--expected-milestone-revision",
            "3",
            "--idempotency-key",
            "k" * length,
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == expected_exit, result.output
    assert bool(_FakeClient.calls) is (expected_exit == exit_codes.OK)


# ---- the machine envelope ---------------------------------------------------


def test_json_output_is_the_strict_machine_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The printed JSON validates as a domain envelope and keeps its shape."""
    answer = _accepted(domain_cmd.MILESTONE_ACTIVATE, _MILESTONE_URN)
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "--json",
            *_base_args(["milestone", "activate"], "--expected-milestone-revision", _MILESTONE_URN),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    payload = orjson.loads(result.stdout)
    assert DomainEnvelope.model_validate(payload) == DomainEnvelope.model_validate(answer)
    assert set(payload) == {
        "schema_version",
        "status",
        "operation",
        "revision_before",
        "revision_after",
        "result",
        "warnings",
        "errors",
        "links",
    }
    assert payload["schema_version"] == ENVELOPE_SCHEMA_VERSION
    assert payload["status"] == DomainStatus.OK.value
    assert payload["operation"] == domain_cmd.MILESTONE_ACTIVATE
    assert payload["revision_before"] == 3
    assert payload["revision_after"] == 4
    assert payload["errors"] == []


def test_text_output_names_the_operation_and_the_revisions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The text branch renders one stable headline per committed move."""
    _install(monkeypatch, result=_accepted(domain_cmd.BATCH_ACTIVATE, _BATCH_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["batch", "activate"], "--expected-batch-revision", _BATCH_URN),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert result.output.splitlines()[0] == (
        f"{domain_cmd.BATCH_ACTIVATE} ok {_BATCH_URN} revision 3 -> 4"
    )


def test_daemon_answer_that_is_not_an_envelope_is_an_internal_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: an answer a client cannot branch on is not printed as one."""
    _install(monkeypatch, result={"status": "ok"})
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["batch", "activate"], "--expected-batch-revision", _BATCH_URN),
        ],
    )
    assert result.exit_code == exit_codes.INTERNAL_ERROR
    assert "not a domain envelope" in result.output


# ---- refusals ---------------------------------------------------------------


def test_guard_refusal_surfaces_the_daemon_code_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A denied move prints the daemon's code, guard and remediation."""
    answer = _refused(
        domain_cmd.MILESTONE_ACTIVATE,
        code=TransactionRefusalCode.TRANSITION_GUARD_FAILED,
        entity_ref=_MILESTONE_URN,
    )
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["milestone", "activate"], "--expected-milestone-revision", _MILESTONE_URN),
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert DomainErrorCode.TRANSITION_GUARD_FAILED.value in result.output
    assert "guard: track_active" in result.output
    assert "remediation: Fix the guard and retry." in result.output


def test_milestone_accept_forwards_the_approval_reference(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The one verb that needs a sealed approval carries it on the wire."""
    _install(monkeypatch, result=_accepted(domain_cmd.MILESTONE_ACCEPT, _MILESTONE_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["milestone", "accept"], "--expected-milestone-revision", _MILESTONE_URN),
            "--approval-receipt-ref",
            _APPROVAL_URN,
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    _, params = _FakeClient.calls[0]
    assert params["approval_receipt_ref"] == _APPROVAL_URN


def test_milestone_accept_without_an_approval_is_the_daemon_refusal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: the CLI forwards and renders the daemon's own refusal code."""
    _install(
        monkeypatch,
        result=_approval_refused(domain_cmd.MILESTONE_ACCEPT, entity_ref=_MILESTONE_URN),
    )
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["milestone", "accept"], "--expected-milestone-revision", _MILESTONE_URN),
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    _, params = _FakeClient.calls[0]
    assert "approval_receipt_ref" not in params
    assert DomainErrorCode.PROTECTED_APPROVAL_REQUIRED.value in result.output


def test_verb_addressed_at_another_kind_keeps_its_own_method(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A foreign URN never re-routes the verb; the daemon refuses the mismatch."""
    answer = _refused(
        domain_cmd.MILESTONE_ACTIVATE,
        code=TransactionRefusalCode.IDENTITY_KIND_MISMATCH,
        entity_ref=_TASK_URN,
    )
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["milestone", "activate"], "--expected-milestone-revision", _TASK_URN),
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    sent_method, params = _FakeClient.calls[0]
    assert sent_method == domain_cmd.MILESTONE_ACTIVATE
    assert params["urn"] == _TASK_URN
    assert DomainErrorCode.IDENTITY_KIND_MISMATCH.value in result.output


def test_transport_failure_is_a_cli_error_not_an_envelope(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a non-domain RPC failure keeps the CLI error taxonomy."""
    _install(monkeypatch, error=DaemonRpcError(-32601, "method not found"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["task", "promote"], "--expected-task-revision", _TASK_URN),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "method not found" in result.output


def test_unreachable_daemon_is_a_daemon_unreachable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a dropped connection is not rendered as a domain refusal."""
    _install(monkeypatch, error=TimeoutError("no answer"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *_base_args(["task", "promote"], "--expected-task-revision", _TASK_URN),
        ],
    )
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert "daemon unavailable" in result.output


# ---- help panels ------------------------------------------------------------


def test_new_nouns_render_in_the_help_listing() -> None:
    """The three native nouns appear on the root help, under a panel."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == exit_codes.OK, result.output
    for noun in ("milestone", "batch", "task"):
        assert noun in result.output


def test_operator_verb_spelling_of_each_method() -> None:
    """Boundary: every RPC name maps back to the command an operator typed."""
    assert [domain_cmd._operator_verb(method) for method in domain_cmd.DOMAIN_CLI_METHODS] == [
        "track retire",
        "milestone activate",
        "milestone open-review",
        "milestone accept",
        "milestone cancel",
        "batch activate",
        "batch ready",
        "task promote",
        "task start",
    ]


# ---- create -------------------------------------------------------------


#: Every create command line under test, with the RPC it must forward to.
#: One row per registered create verb (Run is excluded: it is admitted by
#: its own lease flow, not by an operator create).
_CREATE_VERB_ROWS: tuple[tuple[list[str], str, str], ...] = (
    (["track", "create"], domain_cmd.TRACK_CREATE, _TRACK_URN),
    (["milestone", "create"], domain_cmd.MILESTONE_CREATE, _MILESTONE_URN),
    (["batch", "create"], domain_cmd.BATCH_CREATE, _BATCH_URN),
    (["task", "create"], domain_cmd.TASK_CREATE, _TASK_URN),
)


def _created(method: str, entity_ref: str, *, revision_after: int = 1) -> dict[str, Any]:
    """Return the daemon's own ok envelope for a create, as JSON."""
    receipt = MutationReceipt(
        event_name=method,
        entity_ref=entity_ref,
        revision_before=None,
        revision_after=revision_after,
        canonical_sequence=1,
        event_id="evt-0001",
        idempotency_key="create-0001",
        occurred_at=datetime(2026, 9, 18, tzinfo=UTC),
        wal_record_id="wal-0001",
    )
    return accepted_envelope(receipt, operation=method).model_dump(mode="json")


def test_create_cli_methods_are_a_subset_of_the_registered_create_rpcs() -> None:
    """Every create verb this module exposes names a registered RPC.

    Run is the one lifecycle kind with no create command here: it is
    admitted by its own lease flow, so the daemon's own registry names
    one more create verb than this CLI exposes.
    """
    from eawf.runtime.daemon.methods.domain_create import DOMAIN_CREATE_METHODS

    registered = set(DOMAIN_CREATE_METHODS.values())
    assert set(domain_cmd.DOMAIN_CREATE_CLI_METHODS) < registered
    assert registered - set(domain_cmd.DOMAIN_CREATE_CLI_METHODS) == {"domain.run.create"}


@pytest.mark.parametrize(("verb", "method", "urn"), _CREATE_VERB_ROWS)
def test_create_verb_forwards_exactly_one_rpc(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    verb: list[str],
    method: str,
    urn: str,
) -> None:
    """Each create verb sends its own RPC once, with the whole document."""
    document = {"key": "CANARY-0001", "title": "Canary"}
    spec = tmp_path / "create.json"
    spec.write_bytes(orjson.dumps(document))
    _install(monkeypatch, result=_created(method, urn))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            *verb,
            urn,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    assert len(_FakeClient.calls) == 1
    sent_method, params = _FakeClient.calls[0]
    assert sent_method == method
    assert params["urn"] == urn
    assert params["expected_revision"] == 0
    assert params["idempotency_key"] == "create-0001"
    assert params["actor"] == "OPERATOR"
    assert params["spec"] == document
    assert params["correlation_id"] is None
    assert params["repo_root"] == str(tmp_path.resolve())


def test_create_zero_tree_revision_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: 0 addresses a tree nothing has committed to yet, and is legal."""
    spec = tmp_path / "create.json"
    spec.write_bytes(orjson.dumps({"key": "TRK-CANARY"}))
    _install(monkeypatch, result=_created(domain_cmd.TRACK_CREATE, _TRACK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "track",
            "create",
            _TRACK_URN,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output


def test_create_negative_tree_revision_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: the cursor cannot be negative, unlike a move's revision."""
    spec = tmp_path / "create.json"
    spec.write_bytes(orjson.dumps({"key": "TRK-CANARY"}))
    _install(monkeypatch, result=_created(domain_cmd.TRACK_CREATE, _TRACK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "track",
            "create",
            _TRACK_URN,
            "--expected-tree-revision",
            "-1",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "must not be negative" in result.output
    assert _FakeClient.calls == []


def test_create_missing_from_spec_file_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: an unreadable create document fails at the boundary."""
    _install(monkeypatch, result=_created(domain_cmd.TRACK_CREATE, _TRACK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "track",
            "create",
            _TRACK_URN,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(tmp_path / "absent.json"),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "cannot read --from-spec" in result.output
    assert _FakeClient.calls == []


def test_create_from_spec_not_a_json_object_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: a JSON array or scalar could never be a create document."""
    spec = tmp_path / "create.json"
    spec.write_bytes(orjson.dumps(["not", "an", "object"]))
    _install(monkeypatch, result=_created(domain_cmd.TRACK_CREATE, _TRACK_URN))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "track",
            "create",
            _TRACK_URN,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "must be a JSON object" in result.output
    assert _FakeClient.calls == []


def test_create_refusal_surfaces_the_daemon_code_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A refused create prints the daemon's code, guard and remediation."""
    spec = tmp_path / "create.json"
    spec.write_bytes(orjson.dumps({"key": "MLS-0001"}))
    answer = _refused(
        domain_cmd.MILESTONE_CREATE,
        code=TransactionRefusalCode.SCHEMA_VALIDATION_FAILED,
        entity_ref=_MILESTONE_URN,
    )
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "create",
            _MILESTONE_URN,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert DomainErrorCode.SCHEMA_VALIDATION_FAILED.value in result.output


def test_milestone_create_then_activate_reaches_active_through_the_daemon(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CR-02: create then activate returns the URN and reaches ACTIVE.

    The activate call reads the revision the create left the record at
    (1), and its own success is the proof of the transition: the daemon
    answers ``domain.milestone.activate`` -- move a PLANNED Milestone to
    ACTIVE -- with an ``ok`` envelope only when that move actually lands.
    """
    document = {"key": "MLS-0001", "primary_track_ref": _TRACK_URN, "title": "Canary Milestone"}
    spec = tmp_path / "milestone-create.json"
    spec.write_bytes(orjson.dumps(document))

    _install(monkeypatch, result=_created(domain_cmd.MILESTONE_CREATE, _MILESTONE_URN))
    create_result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "--json",
            "milestone",
            "create",
            _MILESTONE_URN,
            "--expected-tree-revision",
            "0",
            "--idempotency-key",
            "create-0001",
            "--actor",
            "OPERATOR",
            "--from-spec",
            str(spec),
        ],
    )
    assert create_result.exit_code == exit_codes.OK, create_result.output
    create_method, create_params = _FakeClient.calls[0]
    assert create_method == domain_cmd.MILESTONE_CREATE
    assert create_params["urn"] == _MILESTONE_URN
    assert create_params["expected_revision"] == 0
    assert create_params["spec"] == document
    create_payload = orjson.loads(create_result.stdout)
    assert create_payload["result"]["entity_ref"] == _MILESTONE_URN
    assert create_payload["revision_after"] == 1

    _FakeClient.calls = []
    _install(
        monkeypatch,
        result=_accepted(domain_cmd.MILESTONE_ACTIVATE, _MILESTONE_URN, revision_before=1),
    )
    activate_result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "activate",
            _MILESTONE_URN,
            "--expected-milestone-revision",
            "1",
            "--idempotency-key",
            "activate-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert activate_result.exit_code == exit_codes.OK, activate_result.output
    activate_method, activate_params = _FakeClient.calls[0]
    assert activate_method == domain_cmd.MILESTONE_ACTIVATE
    assert activate_params["urn"] == _MILESTONE_URN
    assert activate_params["expected_revision"] == 1


# ---- seal-approval + submit (non-envelope answer shapes) ---------------------


def test_seal_approval_forwards_the_resolver_and_the_answer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The resolver defaults to --actor, and the daemon's answer is printed."""
    from eawf.runtime.daemon.methods.delivery_approval import ApprovalAnswer

    answer = ApprovalAnswer(
        action_ref=_APPROVAL_URN,
        status="sealed",
        revision=2,
        bundle_revision=1,
        bundle_digest="sha256:" + "a" * 64,
        acceptance_bundle=None,
        created=False,
        canonical_sequence=3,
        reason="every seal check holds",
    ).model_dump(mode="json")
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "seal-approval",
            _APPROVAL_URN,
            "--expected-approval-revision",
            "1",
            "--idempotency-key",
            "seal-0001",
            "--actor",
            "OPERATOR",
            "--option-id",
            "accept",
            "--receipt-ref",
            f"{_ROOT}/evidence/EVD-0001",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == domain_cmd.DELIVERY_SEAL_APPROVAL
    assert params["resolver"] == {"principal_kind": "human", "principal_id": "OPERATOR"}
    assert params["option_id"] == "accept"
    assert "sealed" in result.output
    assert "every seal check holds" in result.output


def test_seal_approval_non_positive_revision_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: the pending action's compare-and-swap token is positive."""
    _install(monkeypatch, result={})
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "seal-approval",
            _APPROVAL_URN,
            "--expected-approval-revision",
            "0",
            "--idempotency-key",
            "seal-0001",
            "--actor",
            "OPERATOR",
            "--option-id",
            "accept",
            "--receipt-ref",
            f"{_ROOT}/evidence/EVD-0001",
        ],
    )
    assert result.exit_code == exit_codes.USER_ERROR
    assert "positive revision" in result.output
    assert _FakeClient.calls == []


def test_seal_approval_refusal_renders_the_daemons_code_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A daemon refusal is printed with its own code and exits non-zero.

    ``seal-approval`` answers outside the :class:`DomainEnvelope` shape, so
    its refusal is a raised JSON-RPC error rather than an ok-shaped result
    carrying an error row -- the message is still rendered unchanged.
    """
    _install(
        monkeypatch,
        error=DaemonRpcError(
            -32002,
            "validation_failed: acceptance_action_not_waiting: "
            f"{_APPROVAL_URN} is not waiting for an answer",
        ),
    )
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "seal-approval",
            _APPROVAL_URN,
            "--expected-approval-revision",
            "1",
            "--idempotency-key",
            "seal-0001",
            "--actor",
            "OPERATOR",
            "--option-id",
            "accept",
            "--receipt-ref",
            f"{_ROOT}/evidence/EVD-0001",
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "acceptance_action_not_waiting" in result.output
    assert f"{_APPROVAL_URN} is not waiting for an answer" in result.output


def test_seal_approval_unreachable_daemon_is_a_daemon_unreachable_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Error path: a dropped connection is not rendered as a refusal."""
    _install(monkeypatch, error=TimeoutError("no answer"))
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "milestone",
            "seal-approval",
            _APPROVAL_URN,
            "--expected-approval-revision",
            "1",
            "--idempotency-key",
            "seal-0001",
            "--actor",
            "OPERATOR",
            "--option-id",
            "accept",
            "--receipt-ref",
            f"{_ROOT}/evidence/EVD-0001",
        ],
    )
    assert result.exit_code == exit_codes.DAEMON_UNREACHABLE
    assert "daemon unavailable" in result.output


def test_task_submit_forwards_the_changed_paths(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Repeated --changed-path flags land as the wire's changed_paths list."""
    from eawf.runtime.daemon.methods.candidate import CandidateSubmitAnswer

    answer = CandidateSubmitAnswer(
        candidate_ref="CND-" + "0" * 32,
        run_ref=_RUN_URN,
        replayed=False,
        report_binding="pending",
        submission={"candidate_ref": "CND-" + "0" * 32},
        reason="candidate recorded with its report binding pending",
    ).model_dump(mode="json")
    _install(monkeypatch, result=answer)
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "task",
            "submit",
            _RUN_URN,
            "--task-ref",
            _TASK_URN,
            "--submission-ref",
            f"artifact://git/commit/{'a' * 40}",
            "--changed-path",
            "src/eawf/one.py",
            "--changed-path",
            "src/eawf/two.py",
            "--resulting-tree-digest",
            "sha256:" + "b" * 64,
            "--idempotency-key",
            "submit-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == exit_codes.OK, result.output
    method, params = _FakeClient.calls[0]
    assert method == domain_cmd.CANDIDATE_SUBMIT
    assert params["changed_paths"] == ["src/eawf/one.py", "src/eawf/two.py"]
    assert params["task_ref"] == _TASK_URN
    assert "candidate recorded" in result.output


def test_task_submit_without_a_changed_path_is_refused_before_the_wire(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Boundary: Typer's own required-option gate stops a bare invocation.

    ``--changed-path`` carries no default, so Typer marks it required and
    refuses an invocation naming none before this module's own body ever
    runs -- there is no empty-list case left for this module to check.
    """
    _install(monkeypatch, result={})
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "task",
            "submit",
            _RUN_URN,
            "--task-ref",
            _TASK_URN,
            "--submission-ref",
            f"artifact://git/commit/{'a' * 40}",
            "--resulting-tree-digest",
            "sha256:" + "b" * 64,
            "--idempotency-key",
            "submit-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == 2
    assert "Missing option" in result.output
    assert "changed" in result.output and "path" in result.output
    assert _FakeClient.calls == []


def test_task_submit_refusal_renders_the_daemons_code_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A candidate refusal prints the daemon's code unchanged and exits non-zero."""
    _install(
        monkeypatch,
        error=DaemonRpcError(
            -32002,
            f"validation_failed: candidate_lease_absent: run {_RUN_URN} holds no active lease",
        ),
    )
    result = runner.invoke(
        app,
        [
            "--workspace",
            str(tmp_path),
            "task",
            "submit",
            _RUN_URN,
            "--task-ref",
            _TASK_URN,
            "--submission-ref",
            f"artifact://git/commit/{'a' * 40}",
            "--changed-path",
            "src/eawf/one.py",
            "--resulting-tree-digest",
            "sha256:" + "b" * 64,
            "--idempotency-key",
            "submit-0001",
            "--actor",
            "OPERATOR",
        ],
    )
    assert result.exit_code == domain_cmd.DOMAIN_REFUSAL_EXIT
    assert "candidate_lease_absent" in result.output
    assert f"run {_RUN_URN} holds no active lease" in result.output
